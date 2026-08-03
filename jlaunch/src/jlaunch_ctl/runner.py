"""Assemble a job spec into ONE bash script fed to the PTY.

The whole job — clone, every phase, the verify + artifact gates, and the headline
capture — runs as a single script inside the persistent PTY, so its output streams live
to the terminal AND is teed to a durable ``console.log`` (the PTY scrollback ring is
bounded and lost on EOF, so it is never the source of truth). A trailing status file
records the exact terminal outcome (return code + failing phase) for the job manager to
read on shell exit — outcome is decided from files, not parsed from terminal bytes.
"""

from __future__ import annotations

import shlex

from jlaunch_ctl.specs import JobSpec

# Return codes the assembled script uses for its own (non-phase) gates, so the manager
# can tell a verify/artifact failure from a phase's own exit code.
RC_VERIFY_FAILED = 91
RC_ARTIFACT_MISSING = 92


def _clone_command(spec: JobSpec) -> str:
    # Clone into the (empty) checkout dir as phase 0 so it streams live like every other
    # phase. ext/file transports denied at the git layer; ``--`` so the URL can't be
    # read as an option; ``--depth 1`` — we never push, only read the tree.
    return (
        "git -c protocol.ext.allow=never -c protocol.file.allow=never clone "
        f"--depth 1 --branch {shlex.quote(spec.branch)} -- {shlex.quote(spec.repo_url)} ."  # noqa: E501
    )


def build_script(spec: JobSpec, state_dir: str) -> str:
    """Return the bash script that runs ``spec`` in the current directory and writes its
    control files under ``state_dir``."""
    q = shlex.quote
    state = q(state_dir)
    headline_grep = " ".join(f"-e {q(m)}" for m in spec.headline_markers)

    lines: list[str] = [
        "#!/usr/bin/env bash",
        # No `-e`: run_phase inspects each phase's rc itself so it records which phase
        # failed. `-u`/pipefail still catch the usual foot-guns.
        "set -uo pipefail",
        f"STATE={state}",
        'mkdir -p "$STATE"',
        ': > "$STATE/phase_times.jsonl"',
        # Tee stdout+stderr to a durable log while still writing to the PTY (live view).
        'exec > >(tee -a "$STATE/console.log") 2>&1',
        "PHASE=start",
        # The single source of truth for the outcome, read by the manager on shell exit.
        'record() { printf \'{"rc":%s,"phase":"%s"}\\n\' "$1" "$PHASE" > "$STATE/status.json"; }',  # noqa: E501
        "run_phase() {",
        '  PHASE="$1"',
        '  echo "== jlaunch phase: $PHASE =="',
        "  local t0 t1 rc",
        "  t0=$(date +%s)",
        '  bash -c "$2"; rc=$?',
        "  t1=$(date +%s)",
        '  printf \'{"phase":"%s","seconds":%s,"rc":%s}\\n\' "$PHASE" "$((t1 - t0))" "$rc" >> "$STATE/phase_times.jsonl"',  # noqa: E501
        '  if [ "$rc" -ne 0 ]; then record "$rc"; echo "JLAUNCH phase-failed $PHASE rc=$rc"; exit "$rc"; fi',  # noqa: E501
        "}",
    ]

    if spec.repo_url:
        lines.append(f"run_phase clone {q(_clone_command(spec))}")
    for phase in spec.phases:
        lines.append(f"run_phase {q(phase.name)} {q(phase.run)}")

    # Spec-independent success gates: the verify log's tail must carry the sentinel, and
    # the artifact must exist. Belt-and-suspenders over the phases' own exit codes.
    lines += [
        "PHASE=verify",
        (
            f"if [ -f {q(spec.verify_log)} ]; then "
            f"tail -n 20 {q(spec.verify_log)} | grep -qF -- {q(spec.verify_must_end_with)} "  # noqa: E501
            f'|| {{ record {RC_VERIFY_FAILED}; echo "JLAUNCH verify-failed"; exit {RC_VERIFY_FAILED}; }}; fi'  # noqa: E501
        ),
        "PHASE=artifact",
        (
            f"test -f {q(spec.artifact_path)} "
            f'|| {{ record {RC_ARTIFACT_MISSING}; echo "JLAUNCH artifact-missing"; exit {RC_ARTIFACT_MISSING}; }}'  # noqa: E501
        ),
        "PHASE=headline",
        f'grep -F {headline_grep} "$STATE/console.log" > "$STATE/headline.txt" || true',
        "record 0",
        'echo "JLAUNCH_DONE ok"',
    ]
    return "\n".join(lines) + "\n"
