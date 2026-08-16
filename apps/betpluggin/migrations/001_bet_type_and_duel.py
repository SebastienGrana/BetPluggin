"""
Widens `bet` from "who wins" to "what kind of bet is this", so duels (and later, position ranges) can
share the one table instead of growing a second one.

Every column added here is nullable or defaulted -- the rows already in the table need a value.

Deliberately adds the position columns too, even though nothing writes them yet. This app cannot ship
a new model and a column migration in the same release (see README.md: the migrator flags the whole
app as "initial setup" when any one of its models is missing a table, and fakes every pending
migration), so the columns have to land in the deploy *before* the one that adds the RaceResult model.
"""
from peewee import *
from playhouse.migrate import migrate, SchemaMigrator

from ..models import Bet


def upgrade(migrator: SchemaMigrator):
	bet_type = CharField(max_length=12, default=Bet.TYPE_WINNER, index=True)
	opponent_login = CharField(max_length=100, null=True, index=True)
	pos_min = IntegerField(null=True)
	pos_max = IntegerField(null=True)
	payout_multiplier = FloatField(null=True)

	migrate(
		migrator.add_column(Bet._meta.db_table, 'bet_type', bet_type),
		migrator.add_column(Bet._meta.db_table, 'opponent_login', opponent_login),
		migrator.add_column(Bet._meta.db_table, 'pos_min', pos_min),
		migrator.add_column(Bet._meta.db_table, 'pos_max', pos_max),
		migrator.add_column(Bet._meta.db_table, 'payout_multiplier', payout_multiplier),
	)


def downgrade(migrator: SchemaMigrator):
	migrate(
		migrator.drop_column(Bet._meta.db_table, 'bet_type'),
		migrator.drop_column(Bet._meta.db_table, 'opponent_login'),
		migrator.drop_column(Bet._meta.db_table, 'pos_min'),
		migrator.drop_column(Bet._meta.db_table, 'pos_max'),
		migrator.drop_column(Bet._meta.db_table, 'payout_multiplier'),
	)
