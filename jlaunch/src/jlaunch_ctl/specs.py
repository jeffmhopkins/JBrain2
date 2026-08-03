"""The registry of runnable job specs.

A ``JobSpec`` declares a named, reproducible computation: the repo to clone, the
ordered shell phases to run in the checkout, the artifact it produces, and how success
is verified. Specs are CODE-DEFINED (never owner-supplied URLs), so the repo is a
trusted constant — but the clone still runs with git's ext/file transports denied and
the URL shape validated, defense in depth.

Erdos-Straus 1e12 is the first spec; adding a computation for a later paper is a new
entry here, no new plumbing.
"""

from __future__ import annotations

from dataclasses import dataclass


class SpecError(ValueError):
    """A malformed job spec — raised at import so a bad spec fails the service fast."""


# Only fetch-over-the-wire transports (mirrors jcode's workspace guard): ``ext::`` runs
# an arbitrary command at clone time and ``file://`` reads host-visible trees — both are
# refused even though spec URLs are code-defined.
_ALLOWED_SCHEMES = ("https://", "git://")


@dataclass(frozen=True)
class Phase:
    """One labeled step of a job, run as ``bash -c <run>`` in the checkout root. Phases
    are independent subshells (each re-sources any venv it needs), run in order; the
    first non-zero exit aborts the job and records the failing phase."""

    name: str
    run: str


@dataclass(frozen=True)
class JobSpec:
    name: str
    title: str
    repo_url: str
    branch: str
    phases: tuple[Phase, ...]
    # The deliverable, relative to the checkout root. Its presence is a hard gate.
    artifact_path: str
    artifact_media_type: str
    # A log the run writes whose tail must contain ``verify_must_end_with`` — a second,
    # spec-independent success gate on top of the phases' exit codes.
    verify_log: str
    verify_must_end_with: str
    # Lines to capture verbatim from the console for the public results page.
    headline_markers: tuple[str, ...]
    # UI metadata (strings so "6-10 h" reads naturally).
    est_hours: str
    disk_gb: int
    all_cores: bool
    notes: str

    def public(self) -> dict[str, object]:
        return {
            "name": self.name,
            "title": self.title,
            "repo": self.repo_url,
            "phases": [p.name for p in self.phases],
            "artifact": self.artifact_path,
            "est_hours": self.est_hours,
            "disk_gb": self.disk_gb,
            "all_cores": self.all_cores,
            "notes": self.notes,
        }


def _validate(spec: JobSpec) -> JobSpec:
    if "::" in spec.repo_url or not spec.repo_url.startswith(_ALLOWED_SCHEMES):
        raise SpecError(f"{spec.name}: repo must be an https:// or git:// URL")
    if spec.branch.startswith("-") or not spec.phases:
        raise SpecError(f"{spec.name}: invalid branch or empty phases")
    return spec


# Erdos-Straus census to 10^12 (github.com/jeffmhopkins/Erd-s-Straus-attack,
# scripts/RUN_1E12.md). One-shot, not resumable, all cores, ~15 GB disk, ~6-10 h. The
# run writes run_1e12_verify.log (must end "VERIFICATION OK") and the tarball
# es_1e12_artifacts.tar.gz, and prints a headline block (hard-prime count, max minimal R
# + the R=107 record verdict, R>=87 counts). The ~10.5 GB scratch npz is kept in the
# checkout (not shipped) until the owner deletes the run.
ERDOS_STRAUS_1E12 = _validate(
    JobSpec(
        name="erdos_straus_1e12",
        title="Erdos-Straus census to 10^12",
        repo_url="https://github.com/jeffmhopkins/Erd-s-Straus-attack",
        branch="main",
        phases=(
            Phase(
                "install",
                'python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"',  # noqa: E501
            ),
            Phase("smoke", ". .venv/bin/activate && python -m pytest -q"),
            # run_1e12.sh spins a fixed pool of WORKERS processes for the whole range
            # (WORKERS defaults to nproc). Each worker holds ~3 GB of non-shared memory,
            # so one-per-core exhausted this host's RAM. Size the pool to memory: budget
            # ~1/3 of total RAM at ~3 GB/worker (mem_gb/9), capped at the core count,
            # floored at 1. Fewer workers means a longer run, not more memory.
            Phase(
                "run",
                ". .venv/bin/activate && "
                "read -r _ mem_kb _ < /proc/meminfo && "
                "cores=$(nproc) && "
                "workers=$(( mem_kb / 1048576 / 9 )) && "
                '[ "$workers" -gt "$cores" ] && workers=$cores; '
                '[ "$workers" -lt 1 ] && workers=1; '
                'echo "== workers: $workers of $cores cores '
                '(mem $(( mem_kb / 1048576 ))G, ~3G each) ==" && '
                'WORKERS="$workers" bash scripts/run_1e12.sh',
            ),
        ),
        artifact_path="es_1e12_artifacts.tar.gz",
        artifact_media_type="application/gzip",
        verify_log="run_1e12_verify.log",
        verify_must_end_with="VERIFICATION OK",
        headline_markers=("hard primes:", "max minimal R:", "R >= 87 counts:"),
        est_hours="10-20 h",
        disk_gb=15,
        all_cores=False,
        notes=(
            "One-shot scientific computation, not resumable. The worker pool is sized "
            "to ~1/3 of RAM (~3 GB each), not one-per-core, so it doesn't exhaust "
            "memory; fewer workers means a longer run. Keeps the ~10.5 GB scratch npz "
            "until you delete the run."
        ),
    )
)


SPECS: dict[str, JobSpec] = {ERDOS_STRAUS_1E12.name: ERDOS_STRAUS_1E12}


def get_spec(name: str) -> JobSpec | None:
    return SPECS.get(name)
