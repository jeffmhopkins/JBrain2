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

# esptool's own default for the native USB. Slower than a bridge could go, and not
# worth tuning: the whole image is ~900 KB and the flash takes seconds either way.
BAUD = 460800

# The chips a panel can be, and the flash geometry each is written with. An allowlist
# rather than a pass-through because the api names the chip and this process runs as
# root with /dev mounted: an unknown value is refused before esptool ever sees it.
#
# The S3 values are firmware/partitions.csv's 16 MB table, asserted by the tests. The C6
# is "keep" on all three — esptool then leaves the bootloader header exactly as the
# firmware-c6 build wrote it. Nothing on this side knows that board's flash part, and
# guessing a size here would be overwriting the build's own correct answer with one.
CHIPS: dict[str, tuple[str, str, str]] = {
    "esp32s3": ("16MB", "dio", "80m"),
    "esp32c6": ("keep", "keep", "keep"),
}
DEFAULT_CHIP = "esp32s3"


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


def esptool_argv(
    port: str, images: list[tuple[str, str]], chip: str = DEFAULT_CHIP
) -> list[str]:
    """The exact command that writes the images.

    `write_flash` takes offset/path pairs in order. `--before default_reset` and
    `--after hard_reset` drive the board's onboard auto-download circuit, so the owner
    never has to hold BOOT on a board they cannot see. An erase, when one is asked for,
    is its own esptool invocation in `flash()` rather than a flag here — they are two
    commands, and pretending otherwise inside an argv list is how you ship a literal
    "&&" to execve.

    `chip` is NOT left to esptool's autodetect: a C6 and an S3 enumerate as the same
    303a:1001, and naming it makes esptool refuse a board that is not what the image
    set was built for, before a byte of the wrong bootloader is written.
    """
    if chip not in CHIPS:
        raise FlashError(f"unknown chip {chip!r}; known: {sorted(CHIPS)}")
    size, mode, freq = CHIPS[chip]
    argv = [
        "esptool",
        "--chip",
        chip,
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
        size,
        "--flash_mode",
        mode,
        "--flash_freq",
        freq,
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
    chip: str = DEFAULT_CHIP,
) -> Iterator[str]:
    """The whole operation, as a stream of log lines.

    The temporary directory holds the owner's Wi-Fi password and the device token, so it
    is removed in a `finally` — a flash that fails partway must not leave credentials on
    disk in a container the next flash will reuse.
    """
    # Before anything is staged: an unknown chip must not leave credentials on disk even
    # briefly, and it must not reach the erase, which would wipe the board for nothing.
    if chip not in CHIPS:
        raise FlashError(f"unknown chip {chip!r}; known: {sorted(CHIPS)}")
    workdir = tempfile.mkdtemp(prefix="flash-")
    try:
        yield f"port {port} chip {chip}"
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
            yield from run(["esptool", "--chip", chip, "--port", port, "erase_flash"], workdir)

        argv = esptool_argv(port, paths, chip)
        yield " ".join(argv)
        yield from run(argv, workdir)
        yield "OK"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
