"""The ORM models, and the one place that maps ALL of them.

`Base.metadata` is a single graph: a table's ForeignKey names its target by
STRING, resolved lazily against whatever else has been imported by the time
SQLAlchemy needs it — at flush, where `Mapper._sorted_tables` walks the FKs of
the table it is about to write. So a module holding a foreign key into a table
another module defines is not independently mappable: import it alone and the
first INSERT raises `NoReferencedTableError`, not at import, which is why
`app.list_items -> app.notes` went unnoticed (`test_lists_pg.py` was green only
because pytest-xdist's `--dist loadscope` happened to seat a sibling that does
import `notes` in the same worker).

Fixing the offenders one import at a time cannot work: `notes` already imports
`analysis` for a column_property, so `analysis -> notes` would be a cycle. The
package is the seam instead. Python imports a parent package before any
submodule, so `import jbrain.models.lists` runs THIS file first — and every
model module named here is therefore mapped before any of them can be used,
whichever one the caller reached for. That is the invariant
`tests/unit/test_model_module_isolation.py` pins, per module, in a fresh
interpreter: a module whose tables somebody else's foreign keys point AT must be
listed here (or imported by its referrer), or the guard fails.

The `noqa: F401` block below is imported for that mapping side effect alone —
membership here is the requirement, re-export in `__all__` is not.
"""

from jbrain.models import (  # noqa: F401
    appointments,
    jlaunch,
    jmolt,
    jmolt_outbox,
    lists,
    note_conversation,
    notes,
    owner_prefs,
    plan,
    proposals,
    tasks,
)
from jbrain.models.agent import AgentSession, Run, RunStep
from jbrain.models.analysis import (
    Entity,
    EntityAlias,
    EntityDistinction,
    EntityMention,
    Fact,
    NoteAnalysis,
    ReviewItem,
    TemporalToken,
)
from jbrain.models.archivist import ArchivistMemory
from jbrain.models.core import Base, DeviceSession, Domain, Principal, Subject
from jbrain.models.images import GeneratedImage
from jbrain.models.intake import IntakeLink, IntakeSession, IntakeSubmission
from jbrain.models.jcode import JcodeSession
from jbrain.models.jpet import PetMemory, PetState
from jbrain.models.location import GeofenceState, LocationFix, PlaceGeofence
from jbrain.models.telemetry import HostMetric, HostMetricHourly, LlmUsage
from jbrain.models.wiki import (
    WikiArticle,
    WikiCitation,
    WikiIndexEntry,
    WikiLink,
    WikiRevision,
    WikiSection,
    WikiSourceExclusion,
)
from jbrain.models.workflow import (
    Event,
    Pipeline,
    Schedule,
    Trigger,
)

__all__ = [
    "AgentSession",
    "ArchivistMemory",
    "Base",
    "DeviceSession",
    "Domain",
    "Entity",
    "EntityAlias",
    "EntityDistinction",
    "EntityMention",
    "Event",
    "Fact",
    "GeneratedImage",
    "GeofenceState",
    "HostMetric",
    "HostMetricHourly",
    "IntakeLink",
    "IntakeSession",
    "IntakeSubmission",
    "JcodeSession",
    "LlmUsage",
    "LocationFix",
    "NoteAnalysis",
    "PetMemory",
    "PetState",
    "Pipeline",
    "PlaceGeofence",
    "Principal",
    "ReviewItem",
    "Run",
    "RunStep",
    "Schedule",
    "Subject",
    "TemporalToken",
    "Trigger",
    "WikiArticle",
    "WikiCitation",
    "WikiIndexEntry",
    "WikiLink",
    "WikiRevision",
    "WikiSection",
    "WikiSourceExclusion",
]
