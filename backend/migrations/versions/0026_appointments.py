"""Appointments — the typed `appointments` projection (docs/ROADMAP.md Phase 4,
schema/defs/types/appointment.yaml).

An appointment is NOT a new source of truth: it is a denormalized read-model of
the appointment entities the analysis pipeline already extracts (notes are the
sole sources of truth, non-negotiable #7). Each row projects exactly one
appointment entity — `entity_id` is the key the projector upserts on, and it
cascades, so when a note is deleted and its provisional entity purged, the
projection row goes with it (the privacy promise, docs/reference/ANALYSIS.md "purge").

Owner-only and domain-firewalled like lists: a health appointment is invisible to
a finance-scoped session and to any non-owner principal (#8). The agent never
writes here directly — it stages a `manage_appointment` Proposal that re-enters
as a note; the projector materializes the resulting entity into this table.

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-13
"""

from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

# The appointment.yaml Lifecycle.status enum — a reschedule supersedes the time
# binding, a cancellation flips status; the feed maps these to ICS STATUS.
_STATUSES = "('tentative', 'confirmed', 'cancelled', 'occurred')"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE app.appointments (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            domain_code text NOT NULL REFERENCES app.domains(code),
            -- The graph entity this row projects. One row per entity (the
            -- projector's upsert key); cascade so an entity purge clears it.
            entity_id uuid NOT NULL UNIQUE REFERENCES app.entities(id) ON DELETE CASCADE,
            title text NOT NULL,
            starts_at timestamptz NOT NULL,
            ends_at timestamptz,
            all_day boolean NOT NULL DEFAULT false,
            location text,
            status text NOT NULL DEFAULT 'confirmed' CHECK (status IN {_STATUSES}),
            -- RFC 5545 RRULE for a recurring series; NULL is a single event.
            rrule text,
            -- [{{name, entity_id?}}] — denormalized for display and ICS ATTENDEE.
            attendees jsonb NOT NULL DEFAULT '[]',
            -- The note the appointment was lifted from; SET NULL if it is edited
            -- away without a full purge (a purge cascades via entity_id instead).
            source_note_id uuid REFERENCES app.notes(id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # The listing/feed read path: in-scope, upcoming-first within a domain.
    op.execute(
        "CREATE INDEX appointments_when_idx ON app.appointments (domain_code, starts_at)"
        " WHERE status <> 'cancelled'"
    )
    op.execute("ALTER TABLE app.appointments ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE app.appointments FORCE ROW LEVEL SECURITY")
    # Owner-only AND domain-narrowed: an appointment lives in exactly one in-scope
    # domain, and a non-owner principal sees/writes none (#7/#8).
    op.execute(
        """
        CREATE POLICY appointments_owner ON app.appointments
        USING (app.is_owner() AND app.has_domain_scope(domain_code))
        WITH CHECK (app.is_owner() AND app.has_domain_scope(domain_code))
        """
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON app.appointments TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.appointments")
