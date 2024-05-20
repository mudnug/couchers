"""Add admin_note to users

Revision ID: d45f557f87e8
Revises: 12de0481c47d
Create Date: 2024-05-18 17:35:27.362898

"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "d45f557f87e8"
down_revision = "12de0481c47d"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("users", sa.Column("admin_note", sa.String(), server_default=sa.text("''"), nullable=False))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("users", "admin_note")
    # ### end Alembic commands ###