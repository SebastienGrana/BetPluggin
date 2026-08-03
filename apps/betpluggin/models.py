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
