"""
Duels: one driver publicly bets another that they will finish ahead of them.

Why this is a separate feature and not another row in the betting market: a duel is *relative*. "X
finishes ahead of Y" can be true while both of them come 12th and 13th, so it cannot be expressed as
a finishing position, and the market's pari-mutuel pot cannot price it. See
docs/conception-paris-position.md.

The flow, and the reason for each step:

  1. X runs `/duel Y 500`. Nothing is charged yet -- Y has not agreed to anything.
  2. Y gets a window: decline, or accept with an amount of their own. They may stake less, the same,
     or more than X: an even-money duel between unequal drivers isn't a bet, it's a formality, and
     letting the two of them settle the handicap themselves is the whole point.
  3. Only once Y accepts are both sides billed, in parallel. If either payment fails or is declined,
     the other is refunded and the duel never existed.
  4. The duel is announced, and stays open to spectators for the rest of the map.
  5. At the podium, whoever finished ahead takes the other's stake.

Two separate pots, deliberately. The duel pot (X + Y) settles between the two of them exactly as they
agreed. The spectator pot settles pari-mutuel among the spectators. Merging them was the obvious
design and it is wrong: a popular challenger attracts spectator money onto his own side, which dilutes
his share of the merged pot -- so the crowd would silently rewrite the deal the two players struck,
and the more popular you are the worse a duel you get.
"""
import asyncio
import logging

from .models import Bet

logger = logging.getLogger(__name__)

# Terminal bill states, same rule as the market's: a single bill reports its progress several times
# and only these settle it. Imported by value rather than from __init__ to keep the import one-way.
BILL_STATE_VALIDATED = 4
BILL_STATE_REFUSED = 5
BILL_STATE_ERROR = 6
BILL_STATES_TERMINAL = (BILL_STATE_VALIDATED, BILL_STATE_REFUSED, BILL_STATE_ERROR)

STATE_OFFERED = 'offered'
"""Challenge sent, waiting on the opponent. Nobody has paid anything."""
STATE_BILLING = 'billing'
"""Both sides agreed; both payment popups are out. The duel exists only if both come back paid."""
STATE_ACTIVE = 'active'
"""Both stakes confirmed. Announced, open to spectators, riding until the podium."""


def rank_key(entry, by_map_points):
	"""
	Sort key for one `scores` entry, best first -- or None when that driver has no usable result.

	None is not "last": a driver who never finished has no standing to compare, and a duel decided by
	a disconnect is not a duel. Callers refund rather than crown anyone on the strength of it.
	"""
	if not entry:
		return None

	# The driven time, or None when there isn't one. This is the only trustworthy evidence that a driver
	# has a result at all: the mode hands a `rank` to every connected player whether they drove or not,
	# so two drivers who both fail to finish still come back ranked 1 and 2. Reading that rank as a
	# result is what handed a duel to someone on a map the server itself called a draw.
	time = entry.get('best_race_time')
	if time is None or time <= 0:
		time = None

	if by_map_points:
		points = entry.get('map_points')
		if points is None:
			return None
		# Cumulative points stand on their own: in Rounds you can lead the scoreboard without finishing
		# this particular map. But no points *and* no time is someone who has done nothing to compare.
		if points <= 0 and time is None:
			return None
		return (-points, time if time is not None else 9999999)

	# Time attack: the time is the result. Ordering on the time rather than on `rank` also makes a real
	# dead heat -- the same time to the millisecond -- come out as two equal keys, so it refunds instead
	# of awarding the duel to whichever of them the server happened to list first.
	if time is None:
		return None
	return (time,)


def find_entry(last_scores, login):
	if not last_scores:
		return None
	for entry in last_scores.get('players', []):
		player = entry.get('player')
		if player and player.login.lower() == login.lower():
			return entry
	return None


class DuelManager:
	"""
	Owns the one duel a map may have, and everything staked on it.

	One at a time on purpose. Two simultaneous duels are two chat announcements, two spectator pots and
	two podium results competing for the same ten seconds, and nobody can follow either of them.
	"""

	def __init__(self, app):
		self.app = app

		# The duel currently offered or running, or None. Shape:
		#   state, map_id, challenger/opponent (Player), challenger_amount/opponent_amount (int),
		#   challenger_bet/opponent_bet (Bet), challenger_paid/opponent_paid (bool),
		#   spectator_bets (list of dicts like the market's current_bets), timeout_task, view
		self.duel = None

		# Stakes sent for a duel, awaiting bill_updated. Keyed by bill_id, kept apart from the market's
		# pending_stakes so neither settlement path can consume the other's bills.
		#   dict(side='challenger'|'opponent'|'spectator', bet=Bet, player=Player, amount=int,
		#        target_login=str)
		self.pending = {}

	# ------------------------------------------------------------------
	# State helpers
	# ------------------------------------------------------------------

	@property
	def is_active(self):
		return self.duel is not None and self.duel['state'] == STATE_ACTIVE

	@property
	def is_pending(self):
		return self.duel is not None and self.duel['state'] in (STATE_OFFERED, STATE_BILLING)

	def involves(self, login):
		"""True if this login is one of the two duellists -- who may not bet on their own duel."""
		if not self.duel:
			return False
		login = login.lower()
		return login in (self.duel['challenger'].login.lower(), self.duel['opponent'].login.lower())

	def side_pots(self):
		"""Spectator planets on each side, keyed by lowercased login. Empty dict when there is no duel."""
		pots = {}
		if not self.duel:
			return pots
		for bet in self.duel['spectator_bets']:
			login = bet['target_login'].lower()
			pots[login] = pots.get(login, 0) + bet['amount']
		return pots

	def spectator_odds(self, target_login, extra=0):
		"""
		Pari-mutuel odds a spectator stake would face on `target_login`, counting `extra` as already in.

		Same reasoning as the market's get_projected_odds: quoting the odds *before* your own stake
		joins quotes a number that stops being true the instant you accept it.
		"""
		pots = self.side_pots()
		total = sum(pots.values()) + extra
		target = pots.get(target_login.lower(), 0) + extra
		if target <= 0:
			return None
		return round(total / target, 2)

	# ------------------------------------------------------------------
	# Settings
	# ------------------------------------------------------------------

	async def _limits(self):
		minimum = await self.app.setting_duel_min_stake.get_value()
		maximum = await self.app.setting_duel_max_stake.get_value()
		return max(1, minimum or 1), max(1, maximum or 1)

	async def suggested_amounts(self, challenge_amount):
		"""
		The amounts offered to the challenged player as one-click buttons: half, level, double.

		Level is what they will mostly take. Half and double are there because the handicap is the
		interesting part of a duel between two drivers who are not the same speed.
		"""
		minimum, maximum = await self._limits()
		raw = [challenge_amount // 2, challenge_amount, challenge_amount * 2]
		amounts = []
		for amount in raw:
			amount = max(minimum, min(maximum, amount))
			if amount not in amounts:
				amounts.append(amount)
		return amounts

	# ------------------------------------------------------------------
	# Declaring
	# ------------------------------------------------------------------

	async def challenge(self, challenger, opponent_login, amount):
		"""Handle `/duel <login> <amount>`. Sends no bill: nothing is charged before the answer."""
		from .views import DuelChallengeView

		if not await self.app.setting_duel_enabled.get_value():
			await self.app._safe_chat(
				'{}$f00Duels are switched off on this server.'.format(self.app.CHAT_PREFIX), challenger
			)
			return

		# Only while betting is open. A duel declared once the window has shut would be declared by
		# someone who has already seen how the map is going, which is exactly what the window exists to
		# prevent -- and it is the same rule, and the same settings, as every other bet here.
		if not self.app.market_is_open:
			await self.app._safe_chat(
				'{}$f00Too late to start a duel -- betting is closed for this map.'.format(self.app.CHAT_PREFIX),
				challenger
			)
			return

		if self.duel is not None:
			state = 'already running' if self.is_active else 'waiting for an answer'
			await self.app._safe_chat(
				'{}$f00There is already a duel {} on this map. One at a time!'.format(
					self.app.CHAT_PREFIX, state
				),
				challenger
			)
			return

		opponent = self.app.get_online_player(opponent_login)
		if not opponent:
			await self.app._safe_chat(
				'{}$f00No online player called $fff{}$f00.'.format(self.app.CHAT_PREFIX, opponent_login),
				challenger
			)
			return

		if opponent.login.lower() == challenger.login.lower():
			await self.app._safe_chat(
				'{}$f00You cannot duel yourself.'.format(self.app.CHAT_PREFIX), challenger
			)
			return

		minimum, maximum = await self._limits()
		if amount < minimum or amount > maximum:
			await self.app._safe_chat(
				'{}$f00Duel stakes go from $fff{}$f00 to $fff{}$f00 planets.'.format(
					self.app.CHAT_PREFIX, minimum, maximum
				),
				challenger
			)
			return

		timeout = await self.app.setting_duel_accept_seconds.get_value()

		self.duel = dict(
			state=STATE_OFFERED,
			# Both the row and the id: the rows are written with the Map instance, like every other bet
			# here, and the id is what the "is this still the same map?" guards compare.
			map=self.app.instance.map_manager.current_map,
			map_id=self.app.instance.map_manager.current_map.get_id(),
			challenger=challenger, opponent=opponent,
			challenger_amount=amount, opponent_amount=None,
			challenger_bet=None, opponent_bet=None,
			challenger_paid=False, opponent_paid=False,
			spectator_bets=[],
			timeout_task=None,
			view=None,
		)

		await self.app.instance.chat(
			'{}$fff{}$z$s$ff0 challenges $fff{}$z$s$ff0: "I will finish ahead of you" -- $fff{}$ff0 '
			'planets on it. $aaa({}s to answer)'.format(
				self.app.CHAT_PREFIX, challenger.nickname or challenger.login,
				opponent.nickname or opponent.login, amount, timeout
			)
		)

		view = DuelChallengeView(self.app, self.duel)
		self.duel['view'] = view
		await view.display(player=opponent)

		self.duel['timeout_task'] = asyncio.ensure_future(self._expire_after(timeout))
		await self.app._refresh_ui()

	async def _expire_after(self, seconds):
		try:
			await asyncio.sleep(seconds)
		except asyncio.CancelledError:
			return

		if self.duel is None or self.duel['state'] != STATE_OFFERED:
			return

		opponent = self.duel['opponent']
		await self._teardown_view()
		await self.app.instance.chat(
			'{}$ff0Duel called off: $fff{}$z$s$ff0 did not answer in time.'.format(
				self.app.CHAT_PREFIX, opponent.nickname or opponent.login
			)
		)
		self.duel = None
		await self.app._refresh_ui()

	# ------------------------------------------------------------------
	# Answering
	# ------------------------------------------------------------------

	async def decline(self, player):
		if not self.duel or self.duel['state'] != STATE_OFFERED:
			return
		if player.login.lower() != self.duel['opponent'].login.lower():
			return

		challenger = self.duel['challenger']
		self._cancel_timeout()
		await self._teardown_view()

		await self.app.instance.chat(
			'{}$ff0$fff{}$z$s$ff0 turned down the duel.'.format(
				self.app.CHAT_PREFIX, player.nickname or player.login
			)
		)
		await self.app._safe_chat(
			'{}$ff0Nothing was charged -- the duel never started.'.format(self.app.CHAT_PREFIX), challenger
		)

		self.duel = None
		await self.app._refresh_ui()

	async def accept(self, player, amount):
		"""
		The opponent takes the duel for `amount` of their own. Both sides are billed here and only here.
		"""
		if not self.duel or self.duel['state'] != STATE_OFFERED:
			await self.app._safe_chat(
				'{}$f00There is no duel waiting for your answer.'.format(self.app.CHAT_PREFIX), player
			)
			return
		if player.login.lower() != self.duel['opponent'].login.lower():
			await self.app._safe_chat(
				'{}$f00That duel is not yours to accept.'.format(self.app.CHAT_PREFIX), player
			)
			return

		minimum, maximum = await self._limits()
		if amount < minimum or amount > maximum:
			await self.app._safe_chat(
				'{}$f00Duel stakes go from $fff{}$f00 to $fff{}$f00 planets.'.format(
					self.app.CHAT_PREFIX, minimum, maximum
				),
				player
			)
			return

		self._cancel_timeout()
		await self._teardown_view()

		duel = self.duel
		duel['state'] = STATE_BILLING
		duel['opponent_amount'] = amount

		await self.app.instance.chat(
			'{}$ff0$fff{}$z$s$ff0 accepts for $fff{}$ff0 planets. Both are confirming their payment...'.format(
				self.app.CHAT_PREFIX, player.nickname or player.login, amount
			)
		)

		# Both bills go out together. Each side is only ever charged if the other one pays too --
		# _settle_billing refunds whoever paid alone.
		ok_challenger = await self._bill_side(
			'challenger', duel['challenger'], duel['opponent'], duel['challenger_amount']
		)
		ok_opponent = await self._bill_side(
			'opponent', duel['opponent'], duel['challenger'], amount
		)

		if not ok_challenger or not ok_opponent:
			await self._abort_billing('the payment could not be sent')
			return

		await self.app._refresh_ui()

	async def _bill_side(self, side, backer, against, amount):
		"""
		Create the Bet row and send the payment request for one side. False if the bill never left.
		"""
		duel = self.duel
		bet = await Bet.create(
			map=duel['map'],
			bettor=backer,
			target_login=backer.login,
			opponent_login=against.login,
			bet_type=Bet.TYPE_DUEL,
			amount=amount,
			scope=self.app.scope,
			round_number=self.app.current_round_number,
			state=Bet.STATE_PENDING,
		)

		try:
			bill_id = await self.app.instance.gbx(
				'SendBill', backer.login, amount,
				'BetPluggin duel: {} planets against {}'.format(amount, against.login), ''
			)
		except Exception:
			logger.exception('BetPluggin: could not send the duel bill for %s', backer.login)
			await bet.destroy()
			return False

		# Registered before the save, for the same reason place_bet does it: `await bet.save()` yields,
		# and a player who confirms instantly would have their bill_updated arrive while the entry does
		# not exist yet -- planets gone, duel never started.
		self.pending[bill_id] = dict(
			side=side, bet=bet, player=backer, amount=amount, target_login=backer.login,
		)

		bet.stake_bill_id = bill_id
		await bet.save()
		duel['{}_bet'.format(side)] = bet
		return True

	# ------------------------------------------------------------------
	# Spectators
	# ------------------------------------------------------------------

	async def back_side(self, player, target_login, amount):
		"""Handle `/duelbet <login> <amount>`: a spectator puts money on one of the two duellists."""
		if not self.is_active:
			await self.app._safe_chat(
				'{}$f00There is no duel running right now.'.format(self.app.CHAT_PREFIX), player
			)
			return

		if not await self.app.setting_duel_spectators.get_value():
			await self.app._safe_chat(
				'{}$f00Betting on other people\'s duels is switched off.'.format(self.app.CHAT_PREFIX), player
			)
			return

		if self.involves(player.login):
			# Their stake is already on the table, and it is the only stake their side is supposed to
			# have: letting a duellist top up would let them turn an agreed 500 v 500 into 1500 v 500.
			await self.app._safe_chat(
				'{}$f00You are *in* this duel -- your stake is already down.'.format(self.app.CHAT_PREFIX),
				player
			)
			return

		duel = self.duel
		sides = {
			duel['challenger'].login.lower(): duel['challenger'],
			duel['opponent'].login.lower(): duel['opponent'],
		}
		backed = sides.get((target_login or '').lower())
		if not backed:
			await self.app._safe_chat(
				'{}$f00Back one of the two duellists: $fff{}$f00 or $fff{}$f00.'.format(
					self.app.CHAT_PREFIX, duel['challenger'].login, duel['opponent'].login
				),
				player
			)
			return

		minimum = await self.app.setting_min_stake.get_value()
		maximum = await self.app.setting_max_stake.get_value()
		if amount < minimum or amount > maximum:
			await self.app._safe_chat(
				'{}$f00Stakes go from $fff{}$f00 to $fff{}$f00 planets.'.format(
					self.app.CHAT_PREFIX, minimum, maximum
				),
				player
			)
			return

		# One side only. Backing both is just paying the server a fee to hold your own money.
		existing = [
			b for b in duel['spectator_bets'] if b['player'].login.lower() == player.login.lower()
		]
		if existing and existing[0]['target_login'].lower() != backed.login.lower():
			await self.app._safe_chat(
				'{}$f00You are already backing $fff{}$f00 in this duel.'.format(
					self.app.CHAT_PREFIX, self.app.display_name(existing[0]['target_login'])
				),
				player
			)
			return

		other = duel['opponent'] if backed is duel['challenger'] else duel['challenger']
		bet = await Bet.create(
			map=duel['map'],
			bettor=player,
			target_login=backed.login,
			opponent_login=other.login,
			bet_type=Bet.TYPE_DUEL_SIDE,
			amount=amount,
			odds=self.spectator_odds(backed.login, extra=amount),
			scope=self.app.scope,
			round_number=self.app.current_round_number,
			state=Bet.STATE_PENDING,
		)

		try:
			bill_id = await self.app.instance.gbx(
				'SendBill', player.login, amount,
				'BetPluggin duel: {} planets on {}'.format(amount, backed.login), ''
			)
		except Exception:
			logger.exception('BetPluggin: could not send the duel side bill for %s', player.login)
			await bet.destroy()
			await self.app._safe_chat(
				'{}$f00Could not send your payment request. Try again.'.format(self.app.CHAT_PREFIX), player
			)
			return

		self.pending[bill_id] = dict(
			side='spectator', bet=bet, player=player, amount=amount, target_login=backed.login,
		)
		bet.stake_bill_id = bill_id
		await bet.save()

	# ------------------------------------------------------------------
	# Payment settlement
	# ------------------------------------------------------------------

	def handles_bill(self, bill_id):
		return bill_id in self.pending

	async def on_bill(self, bill_id, state):
		if state not in BILL_STATES_TERMINAL:
			return

		pending = self.pending.pop(bill_id, None)
		if not pending:
			return

		paid = state == BILL_STATE_VALIDATED
		if pending['side'] == 'spectator':
			await self._settle_spectator_bill(pending, paid, state)
		else:
			await self._settle_duellist_bill(pending, paid)

	async def _settle_duellist_bill(self, pending, paid):
		duel = self.duel
		if duel is None or duel['state'] != STATE_BILLING:
			# The duel fell through while this popup was open (the other side declined, the map ended).
			# The planets are real, so they go straight back.
			if paid:
				await self.app._refund(pending['player'], pending['amount'], 'The duel did not happen.')
			pending['bet'].state = Bet.STATE_DECLINED
			await pending['bet'].save()
			return

		if not paid:
			pending['bet'].state = Bet.STATE_DECLINED
			await pending['bet'].save()
			await self._abort_billing(
				'$fff{}$ff0 did not confirm the payment'.format(
					pending['player'].nickname or pending['player'].login
				)
			)
			return

		pending['bet'].state = Bet.STATE_ACTIVE
		await pending['bet'].save()
		duel['{}_paid'.format(pending['side'])] = True

		if duel['challenger_paid'] and duel['opponent_paid']:
			await self._activate()

	async def _settle_spectator_bill(self, pending, paid, state):
		if not self.is_active or self.duel['map_id'] != self.app.instance.map_manager.current_map.get_id():
			pending['bet'].state = Bet.STATE_DECLINED
			await pending['bet'].save()
			if paid:
				await self.app._refund(
					pending['player'], pending['amount'], 'The duel was over before your payment confirmed.'
				)
			return

		if not paid:
			pending['bet'].state = Bet.STATE_DECLINED
			await pending['bet'].save()
			reason = 'declined' if state == BILL_STATE_REFUSED else 'payment error'
			await self.app._safe_chat(
				'{}$f00Your duel bet was not confirmed ({}).'.format(self.app.CHAT_PREFIX, reason), pending['player']
			)
			return

		pending['bet'].state = Bet.STATE_ACTIVE
		await pending['bet'].save()
		self.duel['spectator_bets'].append(pending)

		pots = self.side_pots()
		await self.app.instance.chat(
			'{}$fff{}$z$s$ff0 backs $fff{}$z$s$ff0 for $fff{}$ff0 in the duel $aaa({} vs {} from the crowd)'.format(
				self.app.CHAT_PREFIX,
				pending['player'].nickname or pending['player'].login,
				self.app.display_name(pending['target_login']),
				pending['amount'],
				pots.get(self.duel['challenger'].login.lower(), 0),
				pots.get(self.duel['opponent'].login.lower(), 0),
			)
		)
		await self.app._refresh_ui()

	async def _abort_billing(self, reason, duel=None):
		"""
		One side didn't go through. Refund whoever did pay, and drop the duel.

		`duel` is passed explicitly by callers that have already detached it from `self` -- resolve()
		does, because it has to clear the slot before anything can await. Without it this would find
		`self.duel` empty and refund nobody, which is the one outcome that must never happen here.
		"""
		duel = duel or self.duel
		if duel is None:
			return

		if duel is self.duel:
			self.duel = None

		for side in ('challenger', 'opponent'):
			if duel['{}_paid'.format(side)]:
				await self.app._refund(
					duel[side], duel['{}_amount'.format(side)], 'The duel did not happen.'
				)
			bet = duel['{}_bet'.format(side)]
			if bet is not None and bet.state == Bet.STATE_PENDING:
				bet.state = Bet.STATE_DECLINED
				await bet.save()

		await self.app.instance.chat(
			'{}$ff0Duel called off: {}. Everything has been given back.'.format(self.app.CHAT_PREFIX, reason)
		)
		await self.app._refresh_ui()

	async def _activate(self):
		duel = self.duel
		duel['state'] = STATE_ACTIVE

		spectators = await self.app.setting_duel_spectators.get_value()
		await self.app.instance.chat(
			'{}$f80DUEL ON: $fff{}$z$s$f80 ({}) vs $fff{}$z$s$f80 ({}). Whoever finishes ahead takes '
			'the lot.{}'.format(
				self.app.CHAT_PREFIX,
				duel['challenger'].nickname or duel['challenger'].login, duel['challenger_amount'],
				duel['opponent'].nickname or duel['opponent'].login, duel['opponent_amount'],
				' $ff0Everyone else: $fff/duelbet <login> <amount>$ff0, open until the podium.' if spectators else ''
			)
		)
		await self.app._refresh_ui()

	# ------------------------------------------------------------------
	# Resolution
	# ------------------------------------------------------------------

	async def void(self, reason):
		"""
		Call the duel off without a result: nobody wins, nobody loses, everyone gets their planets back.

		Used when the map the duel was riding on stops being a map anyone could have won -- an admin
		skipping or restarting it. Deliberately separate from resolve(): resolve reads a standing and
		decides, and there is no standing here worth reading. A duel settled on a race that was cut in
		half is the one thing two players betting real planets on each other will never accept.
		"""
		duel = self.duel
		if duel is None:
			return

		self._cancel_timeout()
		await self._teardown_view()
		self.duel = None

		if duel['state'] != STATE_ACTIVE:
			# Never fully paid for, so there is nothing to announce -- just give back whatever did land.
			await self._abort_billing(reason, duel=duel)
			return

		await self._refund_everything(duel, reason)

	async def resolve(self, last_scores, by_map_points):
		"""
		Settle the duel on the standing that just came in. Called from the market's own resolution so
		both read exactly the same `scores` payload -- two separate reads could disagree.
		"""
		duel = self.duel
		if duel is None:
			return

		self._cancel_timeout()
		await self._teardown_view()
		self.duel = None

		if duel['state'] != STATE_ACTIVE:
			# Offered or half-paid when the map ended. Refund anyone who did pay and say nothing more:
			# a duel that never started has no result to announce.
			await self._abort_billing('the map ended before it started', duel=duel)
			return

		challenger, opponent = duel['challenger'], duel['opponent']
		key_challenger = rank_key(find_entry(last_scores, challenger.login), by_map_points)
		key_opponent = rank_key(find_entry(last_scores, opponent.login), by_map_points)

		# Nobody wins: both stakes and every spectator bet go back. Which of the three cases it was gets
		# named, because "refunded" without a reason reads like the plugin lost track of the duel -- and
		# the player who did finish deserves to know their opponent's disconnect is what cost them the win.
		if key_challenger is None and key_opponent is None:
			await self._refund_everything(duel, 'neither of them was ranked on this map')
			return
		if key_challenger is None:
			await self._refund_everything(
				duel, '$fff{}$ff0 has no result on this map'.format(challenger.nickname or challenger.login)
			)
			return
		if key_opponent is None:
			await self._refund_everything(
				duel, '$fff{}$ff0 has no result on this map'.format(opponent.nickname or opponent.login)
			)
			return
		if key_challenger == key_opponent:
			await self._refund_everything(duel, 'they finished dead level')
			return

		if key_challenger < key_opponent:
			winner, loser = challenger, opponent
		else:
			winner, loser = opponent, challenger

		await self._pay_duel(duel, winner, loser)
		await self._pay_spectators(duel, winner)
		await self.app._refresh_ui()

	async def _pay_duel(self, duel, winner, loser):
		"""Winner takes both stakes. No margin: this one is between the two of them."""
		pot = duel['challenger_amount'] + duel['opponent_amount']
		winner_side = 'challenger' if winner is duel['challenger'] else 'opponent'
		loser_side = 'opponent' if winner_side == 'challenger' else 'challenger'
		profit = duel['{}_amount'.format(loser_side)]

		server_planets = await self.app._get_server_planets()
		payout_bill_id, server_planets = await self.app._pay_out(winner, pot, server_planets)

		winner_bet = duel['{}_bet'.format(winner_side)]
		loser_bet = duel['{}_bet'.format(loser_side)]

		if winner_bet is not None:
			winner_bet.state = Bet.STATE_RESOLVED
			winner_bet.won = True
			winner_bet.payout = pot if payout_bill_id is not None else 0
			winner_bet.payout_bill_id = payout_bill_id
			await winner_bet.save()
		if loser_bet is not None:
			loser_bet.state = Bet.STATE_RESOLVED
			loser_bet.won = False
			loser_bet.payout = 0
			await loser_bet.save()

		await self.app.instance.chat(
			'{}$f80DUEL: $fff{}$z$s$f80 beat $fff{}$z$s$f80 and takes $fff{}$f80 planets '
			'$aaa(+{}).'.format(
				self.app.CHAT_PREFIX,
				winner.nickname or winner.login, loser.nickname or loser.login, pot, profit
			)
		)
		await self._announce_record(winner)

	async def _announce_record(self, winner):
		"""
		Say out loud what the win did to the winner's record, right after the duel is announced.

		The board is only worth climbing if climbing it is visible, and nobody opens a window in the
		three seconds after a duel settles. Both bets are already saved by the time this runs, so the
		count includes the duel that has just been announced.
		"""
		try:
			board = await self.app.get_duel_stats()
		except Exception:
			# The record is a flourish on top of a payout that has already happened. It is not worth
			# turning a failed count into a failed resolution.
			return

		key = winner.login.lower()
		for position, entry in enumerate(board, start=1):
			if entry['login'].lower() != key:
				continue
			line = '{}$f80{} now has $fff{}$f80 duel{} won out of $fff{}$f80'.format(
				self.app.CHAT_PREFIX, winner.nickname or winner.login,
				entry['duel_wins'], '' if entry['duel_wins'] == 1 else 's', entry['duels']
			)
			# Naming the position only at the top: "12th on the duel board" is not a boast, and this
			# line is meant to be one.
			if position == 1:
				line += ' $ff0-- top of the duel board$f80'
			elif position <= 3:
				line += ' $ff0-- number {} on the duel board$f80'.format(position)
			await self.app.instance.chat(line + '. $aaa(/duels)')
			return

	async def _pay_spectators(self, duel, winner):
		bets = duel['spectator_bets']
		if not bets:
			return

		total = sum(b['amount'] for b in bets)
		winning = [b for b in bets if b['target_login'].lower() == winner.login.lower()]
		winning_pot = sum(b['amount'] for b in winning)

		if not winning:
			# Everyone backed the loser. There is nobody to share the pot with, so keeping it would just
			# be the server pocketing the lot -- same rule as the main market.
			for bet in bets:
				bet['bet'].state = Bet.STATE_RESOLVED
				bet['bet'].won = None
				bet['bet'].payout = 0
				await bet['bet'].save()
				await self.app._refund(
					bet['player'], bet['amount'], 'Nobody backed the winner of the duel.'
				)
			return

		server_planets = await self.app._get_server_planets()
		for bet in bets:
			won = bet in winning
			payout = round(bet['amount'] * total / winning_pot) if won else 0
			payout_bill_id = None
			if payout > 0:
				payout_bill_id, server_planets = await self.app._pay_out(bet['player'], payout, server_planets)
				if payout_bill_id is None:
					payout = 0

			bet['bet'].state = Bet.STATE_RESOLVED
			bet['bet'].won = won
			bet['bet'].payout = payout
			bet['bet'].payout_bill_id = payout_bill_id
			await bet['bet'].save()

			if won:
				await self.app._safe_chat(
					'{}$0f0Your duel bet came in: $fff{}$0f0 planets back $aaa(+{}).'.format(
						self.app.CHAT_PREFIX, payout, payout - bet['amount']
					),
					bet['player']
				)
			else:
				await self.app._safe_chat(
					'{}$f00Your duel bet on $fff{}$f00 lost. $fff-{}$f00 planets.'.format(
						self.app.CHAT_PREFIX, self.app.display_name(bet['target_login']), bet['amount']
					),
					bet['player']
				)

	async def _refund_everything(self, duel, reason):
		for side in ('challenger', 'opponent'):
			bet = duel['{}_bet'.format(side)]
			if bet is not None:
				bet.state = Bet.STATE_RESOLVED
				bet.won = None
				bet.payout = 0
				await bet.save()
			await self.app._refund(duel[side], duel['{}_amount'.format(side)], 'Duel refunded.')

		for bet in duel['spectator_bets']:
			bet['bet'].state = Bet.STATE_RESOLVED
			bet['bet'].won = None
			bet['bet'].payout = 0
			await bet['bet'].save()
			await self.app._refund(bet['player'], bet['amount'], 'Duel refunded.')

		await self.app.instance.chat(
			'{}$ff0The duel is off: {}. Everyone has been refunded.'.format(self.app.CHAT_PREFIX, reason)
		)
		await self.app._refresh_ui()

	# ------------------------------------------------------------------
	# Housekeeping
	# ------------------------------------------------------------------

	def _cancel_timeout(self):
		if self.duel and self.duel.get('timeout_task'):
			self.duel['timeout_task'].cancel()
			self.duel['timeout_task'] = None

	async def _teardown_view(self):
		if not self.duel or not self.duel.get('view'):
			return
		try:
			await self.duel['view'].destroy()
		except Exception:
			pass
		self.duel['view'] = None
