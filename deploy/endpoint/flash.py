"""Turning a set of images plus a per-unit config into a flashed panel.

The pure half — what the NVS CSV says, what esptool is asked to do — is separated from
the half that spawns processes, so the argument list that will be run against a real
board is assertable without one.

TWO THINGS THIS DELIBERATELY DOES NOT DO.

It does not fetch anything. The api hands over the images and the values; this container
has no route off the box (compose puts it on an `internal: true` network) and no idea
where firmware comes from. That is what keeps a GitHub credential off the box entirely.

It does not decide what goes in NVS. The api owns the Wi-Fi credentials, the device
token and the box's own CA root, and passes them through. They cross an internal-only
network into a container that cannot reach the internet and keeps nothing: the CSV and
the values are written under a temporary directory that is removed when the flash
finishes, successfully or not.
"""

from __future__ import annotations

import csv
import io
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator

# The NVS namespace the firmware opens (firmware/main/cfg.c).
NAMESPACE = "jbrain"

# esptool's own default for the S3's native USB. Slower than a bridge could go, and not
# worth tuning: the whole image is ~900 KB and the flash takes seconds either way.
BAUD = 460800


class FlashError(RuntimeError):
    pass


def nvs_csv(keys: list[str]) -> str:
    """The generator's input, with EVERY value passed as a file reference.

    `file` encoding rather than inline values is not an optimisation — it is what makes
    the CA certificate possible at all. A PEM is multi-line and contains commas, and this
    is a real CSV, so inlining it would mean escaping a certificate correctly on every
    path through. A file reference has no such surface, and it costs one write per key.
    """
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["key", "type", "encoding", "value"])
    w.writerow([NAMESPACE, "namespace", "", ""])
    for key in keys:
        w.writerow([key, "file", "string", f"./{key}.val"])
    return out.getvalue()


def esptool_argv(port: str, images: list[tuple[str, str]]) -> list[str]:
    """The exact command that writes the images.

    `write_flash` takes offset/path pairs in order. `--before default_reset` and
    `--after hard_reset` drive the board's onboard auto-download circuit, so the owner
    never has to hold BOOT on a board they cannot see. An erase, when one is asked for,
    is its own esptool invocation in `flash()` rather than a flag here — they are two
    commands, and pretending otherwise inside an argv list is how you ship a literal
    "&&" to execve.
    """
    argv = [
        "esptool",
        "--chip",
        "esp32s3",
        "--port",
        port,
        "--baud",
        str(BAUD),
        "--before",
        "default_reset",
        "--after",
        "hard_reset",
        "write_flash",
        "--flash_size",
        "16MB",
        "--flash_mode",
        "dio",
        "--flash_freq",
        "80m",
    ]
    for offset, path in images:
        argv += [offset, path]
    return argv


def build_nvs(values: dict[str, str], size: str, workdir: str) -> str:
    """Generate the NVS partition image, returning its path.

    `size` is the partition's size as the table declares it (0x6000), not a guess: a
    generated image that disagrees with the table would be written over the top of
    otadata, which is the one partition whose corruption costs a cable.
    """
    for key, value in values.items():
        with open(os.path.join(workdir, f"{key}.val"), "w", encoding="utf-8") as fh:
            fh.write(value)
    csv_path = os.path.join(workdir, "nvs.csv")
    with open(csv_path, "w", encoding="utf-8") as fh:
        fh.write(nvs_csv(sorted(values)))
    out_path = os.path.join(workdir, "nvs.bin")
    proc = subprocess.run(
        ["python3", "-m", "esp_idf_nvs_partition_gen", "generate", "nvs.csv", "nvs.bin", size],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise FlashError(f"NVS generation failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return out_path


def run(argv: list[str], workdir: str) -> Iterator[str]:
    """Run one command, yielding its output as it arrives.

    Streamed rather than captured because a flash takes tens of seconds and the owner is
    watching a PWA with no other way to know it is alive. stderr is folded into stdout:
    esptool writes its progress there, so separating them would show a silent pane and
    then a wall of text at the end.
    """
    proc = subprocess.Popen(
        argv,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line.rstrip("\n")
    code = proc.wait()
    if code != 0:
        raise FlashError(f"{argv[0]} exited {code}")


def flash(
    port: str,
    images: list[tuple[str, bytes]],
    nvs_values: dict[str, str],
    nvs_offset: str,
    nvs_size: str,
    *,
    erase: bool,
) -> Iterator[str]:
    """The whole operation, as a stream of log lines.

    The temporary directory holds the owner's Wi-Fi password and the device token, so it
    is removed in a `finally` — a flash that fails partway must not leave credentials on
    disk in a container the next flash will reuse.
    """
    workdir = tempfile.mkdtemp(prefix="flash-")
    try:
        yield f"port {port}"
        paths: list[tuple[str, str]] = []
        for offset, blob in images:
            name = f"img-{offset}.bin"
            with open(os.path.join(workdir, name), "wb") as fh:
                fh.write(blob)
            paths.append((offset, name))
            yield f"staged {offset} ({len(blob)} bytes)"

        if nvs_values:
            build_nvs(nvs_values, nvs_size, workdir)
            paths.append((nvs_offset, "nvs.bin"))
            yield f"staged {nvs_offset} nvs ({len(nvs_values)} keys)"

        paths.sort(key=lambda p: int(p[0], 16))
        if erase:
            yield "erasing flash"
            yield from run(["esptool", "--chip", "esp32s3", "--port", port, "erase_flash"], workdir)

        argv = esptool_argv(port, paths)
        yield " ".join(argv)
        yield from run(argv, workdir)
        yield "OK"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
