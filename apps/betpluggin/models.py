"""
BetPluggin models. Bets are staked and paid out in real ManiaPlanet Planets (via the dedicated
server's SendBill/Pay API) -- there is no separate fictional currency here. See apps/betpluggin/__init__.py
for how stakes are collected (player -> server, requires in-client confirmation) and payouts sent
(server -> player, from the server's own Planets balance).
"""
from peewee import *
from pyplanet.core.db import TimedModel
from pyplanet.apps.core.maniaplanet.models import Map, Player


class Bet(TimedModel):
	SCOPE_MAP = 'map'
	SCOPE_ROUND = 'round'

	TYPE_WINNER = 'winner'
	"""The original bet: `target_login` wins the period. Every row written before duels existed."""
	TYPE_RANGE = 'range'
	"""`target_login` finishes between pos_min and pos_max. Not implemented yet -- see docs/conception-paris-position.md."""
	TYPE_DUEL = 'duel'
	"""One of the two sides of a declared duel: `target_login` (the staker) finishes ahead of `opponent_login`."""
	TYPE_DUEL_SIDE = 'duel_side'
	"""A spectator backing one side of a duel. Same shape as TYPE_DUEL, different pot."""

	STATE_PENDING = 'pending'
	"""SendBill sent, waiting for the player to confirm (or decline) the payment in their client."""
	STATE_ACTIVE = 'active'
	"""Payment confirmed, stake is part of the current pool."""
	STATE_DECLINED = 'declined'
	"""Player declined/failed the payment -- never became part of the pool."""
	STATE_RESOLVED = 'resolved'
	"""Market period ended, won/payout are final."""

	map = ForeignKeyField(Map, index=True)
	"""
	Map this bet was placed on.
	"""

	bettor = ForeignKeyField(Player, index=True)
	"""
	The player who placed the bet.
	"""

	target_login = CharField(max_length=100)
	"""
	Login of the player the bettor thinks will win the market period (map or round).
	"""

	amount = IntegerField()
	"""
	Planets wagered (the stake sent to the server via SendBill once confirmed).
	"""

	odds = FloatField(null=True)
	"""
	Pari-mutuel odds at the time the bet was placed, kept for history/display (e.g. "you locked in x2.4").
	"""

	scope = CharField(max_length=10, default=SCOPE_MAP, index=True)
	"""
	What period this bet resolves at: SCOPE_MAP (map end - used for TimeAttack, Cup, ...) or
	SCOPE_ROUND (round end - used for Rounds mode and other round-based modes).
	"""

	round_number = IntegerField(null=True, index=True)
	"""
	Round number (from the `maniaplanet:round_start`/`round_end` signal `count`) this bet is scoped to.
	Only set when scope == SCOPE_ROUND.
	"""

	state = CharField(max_length=10, default=STATE_PENDING, index=True)

	stake_bill_id = IntegerField(null=True, index=True)
	"""Bill id returned by SendBill for the stake, used to match the bill_updated signal back to this bet."""

	payout_bill_id = IntegerField(null=True, index=True)
	"""Bill id returned by Pay for the payout, if this bet won."""

	won = BooleanField(null=True)
	payout = IntegerField(null=True)

	# ------------------------------------------------------------------
	# Added by migration 001. Every one of them is nullable or defaulted, because the rows already in
	# the table have no value for them -- see migrations/README.md.
	# ------------------------------------------------------------------

	bet_type = CharField(max_length=12, default=TYPE_WINNER, index=True)
	"""
	Which kind of bet this row is. Defaults to TYPE_WINNER so the whole existing history reads back as
	what it always was, and so every query that predates duels keeps working unchanged.
	"""

	opponent_login = CharField(max_length=100, null=True, index=True)
	"""
	The other driver in a duel. `target_login` is the one being backed, `opponent_login` the one they
	have to finish ahead of. Null for every other bet type.
	"""

	pos_min = IntegerField(null=True)
	pos_max = IntegerField(null=True)
	"""
	Inclusive finishing-position range for TYPE_RANGE. Reserved by this migration so the position bets
	designed in docs/conception-paris-position.md can ship without touching the schema again -- adding
	a column at the same time as a new model is the one thing this app's migrations cannot do.
	"""

	payout_multiplier = FloatField(null=True)
	"""
	A *promised* multiplier, unlike `odds` which is only the indicative pari-mutuel quote at the time
	of the bet. Set on bets the server prices itself; null on every pari-mutuel bet, including duels.
	"""


class RaceResult(TimedModel):
	"""
	One driver's finishing position on one map.

	Written at the podium from the same `scores` payload the market settles on, for every race --
	whether or not anybody bet on it. Nothing reads this table yet: it exists so that by the time the
	position bets of docs/conception-paris-position.md ship, there is already enough history to price
	them with. See that document's section 9 -- the model needs roughly thirty races before a driver's
	estimated strength means anything, and that history cannot be back-filled.

	Deliberately not tied to the Bet table in any way. It is a record of what happened on track, and
	stays true whether or not there was a market open at the time.
	"""

	SOURCE_LIVE = 'live'
	"""Observed at the podium, from the callback the market itself settles on."""
	SOURCE_IMPORT = 'import'
	"""Reconstructed after the fact from statistics PyPlanet had already been keeping. See //raceimport."""

	map = ForeignKeyField(Map, index=True)
	player = ForeignKeyField(Player, index=True)

	source = CharField(max_length=10, default=SOURCE_LIVE, index=True)
	"""
	Where this row came from.

	Reconstructed races are a guess about which finishes belonged to the same session, and a guess can
	turn out wrong -- if the session threshold is too generous, two evenings of play merge into one
	race that never happened. Keeping the two apart means a bad import can be deleted and redone
	without touching a single race that was actually watched.
	"""

	position = IntegerField()
	"""
	Finishing position, 1 = won. Computed by the plugin the same way the winner is (points then time in
	round-based modes, driven time otherwise) rather than taken from the callback's own `rank`, which
	ranks every connected player whether they drove or not.

	Not indexed on purpose. It holds a few dozen distinct values across millions of rows, so no query
	would use an index on it anyway -- and every row written would pay to maintain one. The reads this
	table is being filled for start from a driver or a map, which the two foreign keys already cover.
	"""

	field_size = IntegerField()
	"""
	How many drivers were ranked in this race.

	Stored rather than counted back from the rows because a position is meaningless without it: 3rd of
	4 is a bad race, 3rd of 20 is a good one, and the normalised score the strength model uses divides
	by exactly this number. Counting rows would give the same answer today but would quietly change
	meaning the day a race is recorded in more than one go.
	"""

	points = IntegerField(null=True)
	"""Map points at the end of the map. Null in modes that award none (TimeAttack)."""

	time = IntegerField(null=True)
	"""Best race time in milliseconds, or null if the driver never set one."""
