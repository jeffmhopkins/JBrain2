"""The No-Spawn Beacons and wormhole gates in each world, as the map and Dave show them
(plan §M26, the binding mock docs/mocks/minecraft-map-markers/, variant A).

Both live only inside the world, as the pack's dynamic properties, which nothing outside
the game can read. The pack now sends a structured line when one changes and every one of
them after each load; the session drain keeps these two tables in step: an event patches a
world's rows, a snapshot replaces them, and a reset or import of the world clears them. The
rows outlive the session, so the map can draw them, "as of" `seen_at`, for a world that
isn't loaded.

- **Beacons** are keyed by their block (one per block in a dimension). `placed_by` is the
  placer's gamertag as the pack heard it ('' for one placed before placers were kept);
  `placed_at` is when the drain first saw it placed (NULL when only a snapshot ever named it).
- **Gates** are keyed by the pack's own number, unique per world. `x, y, z` is the ring's
  lowest corner and `axis` its direction; `state` is `open` with a target, `dormant`
  without one. `planted_at` / `aimed_at` are as heard, NULL when unknown.

Owner-only: the PWA map and the owner's Dave read them; the in-game tool narrows to the
asker's own in code, under the owner's session like every door turn.

Revision ID: 0240
Revises: 0239
Create Date: 2026-10-12
"""

from alembic import op

revision = "0240"
down_revision = "0239"
branch_labels = None
depends_on = None

_DIMS = "('overworld', 'nether', 'the_end')"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE app.mc_world_beacons (
            world text NOT NULL,
            dim text NOT NULL CHECK (dim IN {_DIMS}),
            x integer NOT NULL,
            y integer NOT NULL,
            z integer NOT NULL,
            placed_by text NOT NULL DEFAULT '',
            placed_at timestamptz,
            seen_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (world, dim, x, y, z)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE app.mc_world_gates (
            world text NOT NULL,
            gate_id integer NOT NULL,
            dim text NOT NULL CHECK (dim IN {_DIMS}),
            x integer NOT NULL,
            y integer NOT NULL,
            z integer NOT NULL,
            axis text NOT NULL CHECK (axis IN ('x', 'z')),
            planter text NOT NULL DEFAULT '',
            state text NOT NULL CHECK (state IN ('open', 'dormant')),
            target_place text,
            target_dim text CHECK (target_dim IN {_DIMS}),
            target_x integer,
            target_y integer,
            target_z integer,
            planted_at timestamptz,
            aimed_at timestamptz,
            seen_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (world, gate_id),
            CHECK ((state = 'open') = (target_place IS NOT NULL))
        )
        """
    )
    for table in ("mc_world_beacons", "mc_world_gates"):
        op.execute(f"ALTER TABLE app.{table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE app.{table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_owner ON app.{table}"
            " USING (app.is_owner()) WITH CHECK (app.is_owner())"
        )
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON app.{table} TO jbrain_app")


def downgrade() -> None:
    op.execute("DROP TABLE app.mc_world_gates")
    op.execute("DROP TABLE app.mc_world_beacons")
