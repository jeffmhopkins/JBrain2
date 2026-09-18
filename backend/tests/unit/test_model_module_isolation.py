"""Every model module must map on its own, in a fresh interpreter.

`Base.metadata` is one graph and a ForeignKey names its target by STRING,
resolved lazily — at flush, when `Mapper._sorted_tables` walks the FKs of the
table it is about to write. A module holding an FK into a table only another
module defines therefore imports cleanly, maps cleanly, and raises
`NoReferencedTableError` on its first INSERT. `app.list_items -> app.notes` was
exactly that: `test_lists_pg.py` failed whenever it was the only file in the run
and passed in CI, because `--dist loadscope` happened to seat a sibling that does
import `notes` in the same xdist worker.

A bug that only appears when a file runs ALONE is invisible to a normal suite
run, so the guard cannot live in the suite's own import graph. It runs the check
in a SUBPROCESS instead — one interpreter, purging `jbrain.*` from `sys.modules`
between modules so each import starts from an empty registry. In-process is not
an option: that purge would hand every later test a second, different `Base`.

Two tests, because a probe that silently stops probing is worse than none. The
first pins the modules; the second pins the probe, on metadata built here whose
dangling FK is known. The module list comes from `pkgutil`, so a model module
added tomorrow is covered without anyone remembering to add it.
"""

import json
import subprocess
import sys
from pathlib import Path

_MODELS_PACKAGE = "jbrain.models"


def unresolved_foreign_keys(metadata) -> list[str]:  # noqa: ANN001 - shared with __main__
    """Every ForeignKey in `metadata` whose target table is absent, as
    `"<column> -> <target>"`. Resolving `fk.column` is what SQLAlchemy itself does
    when it sorts tables for a flush, so this asks the question the failing INSERT
    asks — not a re-implementation of it that could drift."""
    dangling = []
    for table in metadata.tables.values():
        for fk in table.foreign_keys:
            try:
                _ = fk.column  # resolving the property IS the check; it raises here
            except Exception:  # noqa: BLE001 - any resolution failure is the finding
                dangling.append(f"{fk.parent} -> {fk.target_fullname}")
    return sorted(dangling)


def _probe_each_module_in_isolation() -> dict[str, list[str]]:
    """Import each `jbrain.models.*` submodule from a clean registry and report its
    dangling FKs. Only ever called in the subprocess `__main__` below."""
    import importlib
    import pkgutil

    package = importlib.import_module(_MODELS_PACKAGE)
    names = sorted(m.name for m in pkgutil.iter_modules(package.__path__))
    findings = {}
    for name in names:
        for stale in [k for k in sys.modules if k.split(".")[0] == "jbrain"]:
            del sys.modules[stale]
        importlib.import_module(f"{_MODELS_PACKAGE}.{name}")
        base = importlib.import_module(f"{_MODELS_PACKAGE}.core").Base
        findings[name] = unresolved_foreign_keys(base.metadata)
    return findings


def test_every_model_module_maps_alone() -> None:
    """The regression itself: importing any one model module must leave a metadata
    in which every foreign key resolves. A module missing from `jbrain/models/
    __init__.py`'s fan-in shows up here as the dangling keys its own tables hold."""
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve())],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    findings = json.loads(completed.stdout)
    assert findings, "the probe found no model modules at all"
    assert {name: keys for name, keys in findings.items() if keys} == {}


def test_the_probe_reports_a_dangling_foreign_key() -> None:
    """The probe pins the modules; this pins the probe. A detector that quietly
    stopped detecting would leave the first test green forever."""
    from sqlalchemy import Column, ForeignKey, Integer, MetaData, Table

    metadata = MetaData()
    Table("here", metadata, Column("id", Integer, primary_key=True))
    Table(
        "dangling",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("ok", Integer, ForeignKey("here.id")),
        Column("bad", Integer, ForeignKey("nowhere.id")),
    )

    assert unresolved_foreign_keys(metadata) == ["dangling.bad -> nowhere.id"]


if __name__ == "__main__":
    print(json.dumps(_probe_each_module_in_isolation()))
