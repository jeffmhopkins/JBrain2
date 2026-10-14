"""An index for a map tile's crossed-ground read (plan §M8).

Every map tile now asks which chunks inside it any player crossed
(`jbrain.minecraft.visited`: world, dimension, and a chunk range, from every player). The
fog's only index is its primary key `(world, xuid, dim, cx, cz)`, whose `xuid` second
column leaves only the `world` prefix usable, so each of the dozens of tiles a map view
asks for at once would scan the whole world's fog. This one serves the read directly.

No new table, so no RLS change.

Revision ID: 0248
Revises: 0247
Create Date: 2026-10-14
"""

from alembic import op

revision = "0248"
down_revision = "0247"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX mc_player_explored_tile ON app.mc_player_explored (world, dim, cx, cz)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX app.mc_player_explored_tile")
