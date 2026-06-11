"""Migration graph invariants.

Concurrent branches each add a migration and CI tests them in isolation
against a fresh DB, so neither branch sees the other's revision — exactly how
two 0009 revisions reached main and broke `alembic upgrade head` with
"Multiple head revisions are present". This guard fails at PR time instead:
the revision graph must stay a single linear chain with one head.
"""

from collections import Counter
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _scripts() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg)


def test_single_migration_head() -> None:
    heads = _scripts().get_heads()
    assert len(heads) == 1, (
        f"migrations have multiple heads {heads}; chain the newer one after the "
        "other (bump its revision/down_revision) so `alembic upgrade head` is "
        "unambiguous"
    )


def test_revision_ids_are_unique() -> None:
    revisions = [s.revision for s in _scripts().walk_revisions()]
    dupes = [rev for rev, n in Counter(revisions).items() if n > 1]
    assert not dupes, f"duplicate migration revision ids: {dupes}"
