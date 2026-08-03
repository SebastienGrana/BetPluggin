"""
BetPluggin: players wager real ManiaPlanet Planets on who will win the current market period.

A "market period" is either the whole map (TimeAttack, Cup, and any mode we haven't special-cased yet)
or a single round (Rounds mode, and any other round-based mode listed in ROUND_SCOPED_MODES below).
The scope for the current map is picked once at map_begin from the active mode script.

Staking flow: `/bet` (or a quick-bet button) calls `SendBill` (player -> server) for the stake. This
pops a payment confirmation in the player's own game client -- the stake only joins the pool once the
`bill_updated` signal confirms it (state 4). Declined or failed payments never join the pool. If a
stake confirms after betting has already closed (or the map/round changed), it's refunded automatically.

Payout flow: resolution uses the server-authoritative `scores` signal (fired around each podium) to know
who actually won; the pot from all confirmed bets in that period is split pari-mutuel style among the
winners, proportional to their stake, and paid from the server's own Planets balance via `Pay`
(server -> player). If we can't determine a winner at all, everyone is refunded rather than the server
keeping their stakes.
"""
import asyncio
import math
import time

from pyplanet.apps.config import AppConfig
from pyplanet.apps.core.maniaplanet import callbacks as mp_signals
from pyplanet.apps.core.maniaplanet.models import Player
from pyplanet.apps.core.trackmania.callbacks import scores as tm_scores_signal
from pyplanet.contrib.command import Command
from pyplanet.contrib.setting import Setting

from .models import Bet
from .views import BetWidget, BetMarketView, BetLeaderboardView

# Modes (matched case-insensitively against the mode script name, e.g. "Trackmania/TM_Rounds_Online")
# whose bets should resolve at the end of each round instead of at the end of the map.
# Everything not listed here (TimeAttack, Cup, Laps, Stunts, and anything new/unknown) defaults to
# map-scoped betting. Add to this list as you learn which of your server's modes behave like Rounds.
ROUND_SCOPED_MODES = ('rounds', 'teams', 'knockout')

# ManiaPlanet.BillUpdated state codes.
BILL_STATE_VALIDATED = 4
BILL_STATE_REFUSED = 5
BILL_STATE_ERROR = 6


class BetplugginApp(AppConfig):
	game_dependencies = ['trackmania', 'trackmania_next']
	app_dependencies = ['core.maniaplanet', 'core.trackmania']

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.lock = asyncio.Lock()

		self.scope = Bet.SCOPE_MAP
		self.market_open = False
		self.market_open_until = None
		self.market_manually_closed = False
		self.current_round_number = None
		self._market_generation = 0
		self._close_timer_task = None

		# Confirmed bets in the market period currently open, not yet resolved.
		# Each entry: dict(bet=Bet instance, player=Player instance, target_login=str, amount=int)
		self.current_bets = []

		# Stakes sent via SendBill, awaiting the bill_updated confirmation. Keyed by bill_id.
		# Each entry: dict(bet=Bet, player=Player, target_login=str, amount=int, odds=float|None,
		#                   round_number=int|None) -- scope/round_number are the values that applied
		#                   when the stake was sent, to detect a period change before it confirms.
		self.pending_stakes = {}

		# Payouts/refunds sent via Pay, awaiting bill_updated just so we can report success/failure.
		# Keyed by bill_id. Each entry: dict(player=Player, amount=int)
		self.pending_payouts = {}

		# Cached payload from the most recent `scores` signal this period (players list + winner login).
		self.last_scores = None

		self.widget = None

		self.setting_betting_window = Setting(
			'betting_window_seconds', 'Betting window (seconds)', Setting.CAT_BEHAVIOUR, type=int,
			description='Seconds after a market opens (map start, or round start in round-based modes) '
						'during which bets can be placed. 0 = open for the whole period.',
			default=30
		)

		self.setting_quick_bet_amounts = Setting(
			'quick_bet_amounts', 'Quick-bet amounts', Setting.CAT_BEHAVIOUR, type=str,
			description='Comma separated planet amounts shown as quick-bet buttons in the betting window.',
			default='10,50,100'
		)

		self.setting_min_stake = Setting(
			'bet_minimum_stake', 'Minimum stake (planets)', Setting.CAT_BEHAVIOUR, type=int,
			description='Minimum amount of planets a player can wager on a single bet.',
			default=10
		)

		self.setting_max_stake = Setting(
			'bet_maximum_stake', 'Maximum stake (planets)', Setting.CAT_BEHAVIOUR, type=int,
			description='Maximum amount of planets a player can wager on a single bet.',
			default=2500
		)

	async def on_start(self):
		await self.instance.command_manager.register(
			Command(
				command='bet', target=self.chat_bet,
				description='Bet planets that a player will win the current period. Usage: /bet <login> <amount>'
			).add_param(name='login', type=str, required=True, help='Login of the player you think will win.')
			 .add_param(name='amount', type=int, required=True, help='Amount of planets to bet.'),

			Command(
				command='betmarket', aliases=['market', 'odds'], target=self.chat_open_market,
				description='Open the betting market: online players, live odds and quick-bet buttons.'
			),

			Command(
				command='mybet', aliases=['bets'], target=self.chat_my_bets,
				description='Show your active bet(s) for the current period.'
			),

			Command(
				command='wallet', aliases=['stats', 'betstats'], target=self.chat_my_stats,
				description='Show your BetPluggin wagering history. Your live Planets balance is shown '
							'in your game client.'
			),

			Command(
				command='bettop', aliases=['betladder'], target=self.chat_leaderboard,
				description='Open the all-time betting leaderboard.'
			),

			Command(
				command='open', namespace='bet', target=self.chat_admin_open_betting, admin=True,
				perms='betpluggin:manage_betting', description='Force-open betting for the current period.'
			),

			Command(
				command='close', namespace='bet', target=self.chat_admin_close_betting, admin=True,
				perms='betpluggin:manage_betting', description='Close betting for the current period early.'
			),

			Command(
				command='help', namespace='bet', target=self.chat_help_admin, admin=True,
				description='Show BetPluggin admin commands help.'
			),

			Command(
				command='help', target=self.chat_help_public,
				description='Show BetPluggin public commands help.'
			).add_param(name='topic', type=str, required=False, help='Topic (e.g., "bet") or leave empty.'),
		)

		await self.instance.permission_manager.register(
			'manage_betting', 'Open or close betting early', app=self, min_level=1
		)

		await self.context.setting.register(
			self.setting_betting_window, self.setting_quick_bet_amounts,
			self.setting_min_stake, self.setting_max_stake,
		)

		self.context.signals.listen(mp_signals.map.map_begin, self.map_begin)
		self.context.signals.listen(mp_signals.map.map_end, self.map_end)
		self.context.signals.listen(mp_signals.flow.round_start, self.round_start)
		self.context.signals.listen(mp_signals.flow.round_end, self.round_end)
		self.context.signals.listen(mp_signals.player.player_connect, self.player_connect)
		self.context.signals.listen(mp_signals.other.bill_updated, self.on_bill_updated)
		self.context.signals.listen(tm_scores_signal, self.on_scores)

		self.widget = BetWidget(self)

		# Handle the map that might already be running when the app (re)starts. We can't know how much
		# of it has already played out, so don't reopen a fresh betting window for it -- that would let
		# players who've already seen part of the run bet with information they shouldn't have. Betting
		# resumes normally on the next real map/round start.
		await self.map_begin(self.instance.map_manager.current_map, resuming=True)

	# ------------------------------------------------------------------
	# Market lifecycle
	# ------------------------------------------------------------------

	@property
	def market_is_open(self):
		if self.market_manually_closed:
			return False
		if not self.market_open:
			return False
		if self.market_open_until is not None and time.time() > self.market_open_until:
			return False
		return True

	async def detect_scope(self):
		try:
			script = await self.instance.mode_manager.get_current_script()
		except Exception:
			return Bet.SCOPE_MAP

		script_lower = (script or '').lower()
		if any(keyword in script_lower for keyword in ROUND_SCOPED_MODES):
			return Bet.SCOPE_ROUND
		return Bet.SCOPE_MAP

	async def map_begin(self, map, resuming=False, **kwargs):
		async with self.lock:
			self.current_bets = []
			self.last_scores = None
			self.current_round_number = None
			self.scope = await self.detect_scope()

			if self.scope == Bet.SCOPE_MAP and not resuming:
				await self._open_market()
			else:
				# Round-scoped (wait for the first `round_start` to open the market), or we're resuming
				# mid-map after a (re)start -- either way, betting stays closed until the next real start.
				self.market_open = False
				self.market_open_until = None

		if self.widget:
			await self.widget.display()

	async def round_start(self, count, **kwargs):
		if self.scope != Bet.SCOPE_ROUND:
			return

		async with self.lock:
			self.current_bets = []
			self.last_scores = None
			self.current_round_number = count
			await self._open_market()

		await self.widget.display()

	async def _open_market(self):
		"""Must be called while holding self.lock."""
		window = await self.setting_betting_window.get_value()
		self.market_manually_closed = False
		self.market_open = True
		self.market_open_until = (time.time() + window) if window and window > 0 else None

		# The widget only re-renders on specific events (map/round start, a confirmed bet, ...) -- without
		# this, once the betting window naturally elapses mid-map, the HUD keeps showing OPEN until the
		# next such event. Schedule a one-shot refresh right when the window is due to close instead.
		self._market_generation += 1
		generation = self._market_generation
		if self._close_timer_task:
			self._close_timer_task.cancel()
			self._close_timer_task = None
		if window and window > 0:
			self._close_timer_task = asyncio.ensure_future(self._auto_close_refresh(window, generation))

	async def _auto_close_refresh(self, delay, generation):
		try:
			await asyncio.sleep(delay)
		except asyncio.CancelledError:
			return
		if generation != self._market_generation:
			return
		if self.widget:
			await self.widget.display()

	async def on_scores(self, players, winner_player, **kwargs):
		# Fired around every podium (each round in round-based modes, and around map end otherwise).
		# We only care about the latest payload by the time the period actually resolves.
		self.last_scores = dict(players=players, winner_player=winner_player)

	async def round_end(self, count, **kwargs):
		if self.scope != Bet.SCOPE_ROUND:
			return
		await self._resolve_market('round {}'.format(count))

	async def map_end(self, map, **kwargs):
		if self.scope != Bet.SCOPE_MAP:
			# Round-scoped bets already got resolved at each round_end. Just close the market display.
			self.market_open = False
			if self.widget:
				await self.widget.display()
			return
		await self._resolve_market('the map')

	async def _resolve_market(self, period_label):
		async with self.lock:
			bets = self.current_bets
			self.current_bets = []
			self.market_open = False
			self.market_open_until = None
			last_scores = self.last_scores
			self.last_scores = None

		if not bets:
			if self.widget:
				await self.widget.display()
			return

		winner_login = self.determine_winner_login(last_scores)

		if not winner_login:
			# Couldn't tell who won -- refund real planets rather than let the server keep them.
			for bet in bets:
				bet['bet'].won = None
				bet['bet'].payout = 0
				bet['bet'].state = Bet.STATE_RESOLVED
				await bet['bet'].save()
				await self._refund(
					bet['player'], bet['amount'],
					'Could not determine a winner for {}.'.format(period_label)
				)
			await self.instance.chat(
				'$i$f00Could not determine a winner for {} -- all bets refunded.'.format(period_label)
			)
			if self.widget:
				await self.widget.display()
			return

		total_pot = sum(bet['amount'] for bet in bets)
		winning_bets = [bet for bet in bets if bet['target_login'].lower() == winner_login.lower()]
		winning_pot = sum(bet['amount'] for bet in winning_bets)

		server_planets = await self._get_server_planets()

		for bet in bets:
			won = bet in winning_bets
			payout = 0
			if won:
				payout = round(bet['amount'] * total_pot / winning_pot) if winning_pot > 0 else bet['amount']

			bet['bet'].state = Bet.STATE_RESOLVED
			bet['bet'].won = won
			bet['bet'].payout = payout
			await bet['bet'].save()

			if won and payout > 0:
				server_planets = await self._pay_out(bet['player'], payout, server_planets)
			elif not won:
				await self._safe_chat(
					'$i$f00Your bet on $fff{}$f00 did not win {}. You lost $fff{}$f00 planets.'.format(
						bet['target_login'], period_label, bet['amount']
					),
					bet['player']
				)

		await self.instance.chat(
			'$ff0Betting results for {}: $fff{}$ff0 won! $fff{}$ff0 planets wagered by $fff{}$ff0 player(s).'.format(
				period_label, winner_login, total_pot, len(bets)
			)
		)

		if self.widget:
			await self.widget.display()

	@staticmethod
	def determine_winner_login(last_scores):
		if not last_scores:
			return None

		if last_scores.get('winner_player'):
			return last_scores['winner_player']

		ranked = sorted(
			(p for p in last_scores.get('players', []) if p.get('rank')),
			key=lambda p: p['rank']
		)
		if ranked:
			return ranked[0]['player'].login

		return None

	async def player_connect(self, player, is_spectator, source, signal, **kwargs):
		if self.widget:
			await self.widget.display(player=player)

	# ------------------------------------------------------------------
	# Real-Planets payment plumbing (SendBill for stakes, Pay for payouts/refunds)
	# ------------------------------------------------------------------

	async def _get_server_planets(self):
		try:
			return await self.instance.gbx('GetServerPlanets')
		except Exception:
			return 0

	async def _pay_out(self, player, amount, server_planets):
		"""
		Send `amount` planets from the server's own balance to `player`. Returns the (locally tracked)
		remaining server balance so callers resolving several payouts in a row don't have to re-fetch it
		every time.
		"""
		# Pay deducts a small fee on top of the nominal amount -- mirrors the reserve check used by
		# other PyPlanet apps that pay out planets (e.g. the official `transactions` app).
		reserve = 2 + math.floor(amount * 0.05)
		if server_planets < (amount + reserve):
			await self._safe_chat(
				'$i$f00The server doesn\'t have enough planets to pay your {} planet payout right now! '
				'Contact an admin.'.format(amount),
				player
			)
			return server_planets

		try:
			bill_id = await self.instance.gbx('Pay', player.login, amount, 'BetPluggin payout!')
			self.pending_payouts[bill_id] = dict(player=player, amount=amount)
			return server_planets - (amount + reserve)
		except Exception:
			await self._safe_chat(
				'$i$f00Your payout of {} planets failed to send! Contact an admin.'.format(amount),
				player
			)
			return server_planets

	async def _refund(self, player, amount, reason):
		if amount <= 0:
			return
		try:
			bill_id = await self.instance.gbx('Pay', player.login, amount, 'BetPluggin refund: {}'.format(reason))
			self.pending_payouts[bill_id] = dict(player=player, amount=amount)
		except Exception:
			await self._safe_chat(
				'$i$f00Failed to refund your {} planets automatically! Contact an admin.'.format(amount),
				player
			)
			return
		await self._safe_chat('$ff0{} Refunding your {} planets.'.format(reason, amount), player)

	async def on_bill_updated(self, bill_id, state, state_name, transaction_id, **kwargs):
		if bill_id in self.pending_stakes:
			await self._handle_stake_bill(bill_id, state)
		elif bill_id in self.pending_payouts:
			await self._handle_payout_bill(bill_id, state)

	async def _handle_stake_bill(self, bill_id, state):
		async with self.lock:
			pending = self.pending_stakes.pop(bill_id, None)

		if not pending:
			return

		if state == BILL_STATE_VALIDATED:
			current_map_id = self.instance.map_manager.current_map.get_id()
			still_valid = (
				self.market_is_open
				and pending['bet'].map_id == current_map_id
				and (self.scope != Bet.SCOPE_ROUND or pending['round_number'] == self.current_round_number)
			)

			if still_valid:
				async with self.lock:
					self.current_bets.append(pending)
				pending['bet'].state = Bet.STATE_ACTIVE
				await pending['bet'].save()
				await self._safe_chat(
					'$ff0Bet confirmed: $fff{}$ff0 planets on $fff{}$ff0 (odds x{}).'.format(
						pending['amount'], pending['target_login'], self.format_odds(pending['odds'])
					),
					pending['player']
				)
				if self.widget:
					await self.widget.display()
			else:
				pending['bet'].state = Bet.STATE_DECLINED
				await pending['bet'].save()
				await self._refund(
					pending['player'], pending['amount'],
					'Betting was no longer open by the time your payment confirmed.'
				)
		elif state in (BILL_STATE_REFUSED, BILL_STATE_ERROR):
			pending['bet'].state = Bet.STATE_DECLINED
			await pending['bet'].save()
			reason = 'declined' if state == BILL_STATE_REFUSED else 'payment error'
			await self._safe_chat(
				'$i$f00Your bet on $fff{}$f00 was not confirmed ({}).'.format(pending['target_login'], reason),
				pending['player']
			)
		# Other states (e.g. created/waiting) are just transitional -- nothing to do yet.

	async def _handle_payout_bill(self, bill_id, state):
		async with self.lock:
			pending = self.pending_payouts.pop(bill_id, None)

		if not pending:
			return

		if state == BILL_STATE_VALIDATED:
			await self._safe_chat(
				'$ff0You received $fff{}$ff0 planets from BetPluggin!'.format(pending['amount']),
				pending['player']
			)
		elif state in (BILL_STATE_REFUSED, BILL_STATE_ERROR):
			await self._safe_chat(
				'$i$f00Your payout/refund of {} planets from BetPluggin failed to arrive! Contact an admin.'.format(
					pending['amount']
				),
				pending['player']
			)

	async def _safe_chat(self, message, player):
		try:
			await self.instance.chat(message, player)
		except Exception:
			pass

	# ------------------------------------------------------------------
	# Odds
	# ------------------------------------------------------------------

	def get_odds(self, target_login):
		"""Pari-mutuel odds for target_login: total pot / pot on that target. None if nobody bet on them yet."""
		total_pot = sum(bet['amount'] for bet in self.current_bets)
		target_pot = sum(
			bet['amount'] for bet in self.current_bets if bet['target_login'].lower() == target_login.lower()
		)
		if total_pot <= 0 or target_pot <= 0:
			return None
		return round(total_pot / target_pot, 2)

	@staticmethod
	def format_odds(odds):
		return '{:.2f}'.format(odds) if odds else '-'

	async def get_quick_bet_amounts(self):
		raw = await self.setting_quick_bet_amounts.get_value()
		amounts = []
		for part in (raw or '').split(','):
			part = part.strip()
			if not part:
				continue
			try:
				amounts.append(int(part))
			except ValueError:
				continue
		return amounts or [10, 50, 100]

	# ------------------------------------------------------------------
	# Betting
	# ------------------------------------------------------------------

	def get_online_player(self, login):
		return next(
			(p for p in self.instance.player_manager.online if p.login.lower() == login.lower()),
			None
		)

	async def place_bet(self, bettor, target_login, amount):
		"""
		Start placing a bet for `bettor` on `target_login`. This only *starts* a real-money-planets
		payment (SendBill) -- the bet isn't actually active until the player confirms it in their
		client and `on_bill_updated` picks up the confirmation. Used by both the /bet chat command and
		the quick-bet buttons in the market UI.

		:return: (ok, message) tuple.
		"""
		min_stake = await self.setting_min_stake.get_value()
		max_stake = await self.setting_max_stake.get_value()

		if amount < min_stake:
			return False, 'Minimum bet is {} planets.'.format(min_stake)
		if amount > max_stake:
			return False, 'Maximum bet is {} planets.'.format(max_stake)

		if not self.market_is_open:
			return False, 'Betting is closed right now.'

		target = self.get_online_player(target_login)
		if not target:
			return False, 'Player {} is not connected to the server.'.format(target_login)

		# Self-betting temporarily allowed for solo testing -- re-enable this check before going live.
		# if target.login.lower() == bettor.login.lower():
		# 	return False, 'You cannot bet on yourself.'

		async with self.lock:
			duplicate = any(
				b['player'].login == bettor.login and b['target_login'].lower() == target.login.lower()
				for b in (self.current_bets + list(self.pending_stakes.values()))
			)
			if duplicate:
				return False, 'You already have a bet (or a pending one) on {} for this period.'.format(
					target.nickname or target.login
				)

			odds = self.get_odds(target.login)

			bet = await Bet.create(
				map=self.instance.map_manager.current_map,
				bettor=bettor,
				target_login=target.login,
				amount=amount,
				odds=odds,
				scope=self.scope,
				round_number=self.current_round_number,
				state=Bet.STATE_PENDING,
			)

			try:
				bill_id = await self.instance.gbx(
					'SendBill', bettor.login, amount,
					'BetPluggin: betting {} planets on {} to win!'.format(amount, target.login), ''
				)
			except Exception as e:
				await bet.destroy()
				return False, 'Could not start the payment ({}). Try again.'.format(e)

			bet.stake_bill_id = bill_id
			await bet.save()

			self.pending_stakes[bill_id] = dict(
				bet=bet, player=bettor, target_login=target.login, amount=amount, odds=odds,
				round_number=self.current_round_number,
			)

		return True, 'Confirm the payment popup in your game client to lock in {} planets on {} (odds x{}).'.format(
			amount, target.nickname or target.login, self.format_odds(odds)
		)

	# ------------------------------------------------------------------
	# Stats / leaderboard
	# ------------------------------------------------------------------

	async def get_leaderboard(self, limit=100):
		# Chronological per bet id, so the win/loss streak can be accumulated in a single pass.
		rows = await Bet.execute(
			Bet.select(Bet, Player).join(Player).where(Bet.state == Bet.STATE_RESOLVED).order_by(Bet.id)
		)

		stats = {}
		for bet in rows:
			if bet.won is None:
				# Refunded (e.g. no winner could be determined) -- not a real win or loss, skip.
				continue

			login = bet.bettor.login
			entry = stats.setdefault(login, dict(
				login=login, nickname=bet.bettor.nickname, bets=0, wins=0, wagered=0, won=0,
				best_win=0, streak=0,
			))
			entry['bets'] += 1
			entry['wagered'] += bet.amount

			if bet.won:
				payout = bet.payout or 0
				entry['wins'] += 1
				entry['won'] += payout
				entry['best_win'] = max(entry['best_win'], payout)
				entry['streak'] = entry['streak'] + 1 if entry['streak'] > 0 else 1
			else:
				entry['streak'] = entry['streak'] - 1 if entry['streak'] < 0 else -1

		leaderboard = list(stats.values())
		for entry in leaderboard:
			entry['net'] = entry['won'] - entry['wagered']
			entry['win_rate'] = round((entry['wins'] / entry['bets']) * 100, 1) if entry['bets'] else 0

		leaderboard.sort(key=lambda e: e['net'], reverse=True)
		return leaderboard[:limit] if limit else leaderboard

	@staticmethod
	def format_streak(streak):
		if streak > 0:
			return 'W{}'.format(streak)
		if streak < 0:
			return 'L{}'.format(abs(streak))
		return '-'

	async def get_player_stats(self, login):
		for entry in await self.get_leaderboard(limit=None):
			if entry['login'].lower() == login.lower():
				return entry
		return None

	# ------------------------------------------------------------------
	# Chat commands
	# ------------------------------------------------------------------

	async def chat_bet(self, player, data, **kwargs):
		ok, message = await self.place_bet(player, data.login, data.amount)
		color = '$ff0' if ok else '$f00'
		await self.instance.chat('{}{}'.format(color, message), player)

	async def chat_open_market(self, player, **kwargs):
		if not self.market_is_open:
			status = 'closed — no map/round running yet.' if not self.market_open else 'closed (betting window has ended).'
			await self.instance.chat(
				'$f00Betting is {}$f00 Opening the market anyway so you can see live odds.'.format(status),
				player
			)
		view = BetMarketView(self)
		await view.display(player=player)

	async def chat_my_bets(self, player, **kwargs):
		active = [b for b in self.current_bets if b['player'].login == player.login]
		pending = [b for b in self.pending_stakes.values() if b['player'].login == player.login]

		if not active and not pending:
			if self.market_is_open:
				await self.instance.chat(
					'$f00No active bet this period. Use $fff/bet <login> <amount>$f00 or $fff/betmarket$f00 to place one.',
					player
				)
			else:
				await self.instance.chat(
					'$f00No active bet and betting is currently closed. Wait for the next map/round.',
					player
				)
			return

		parts = [
			'$fff{}$ff0 planets on $fff{}$ff0'.format(b['amount'], b['target_login']) for b in active
		]
		parts += [
			'$fff{}$ff0 planets on $fff{}$ff0 $aaa(awaiting payment confirmation)$ff0'.format(
				b['amount'], b['target_login']
			)
			for b in pending
		]
		await self.instance.chat('$ff0Your current bet(s): {}.'.format(', '.join(parts)), player)

	async def chat_my_stats(self, player, **kwargs):
		stats = await self.get_player_stats(player.login)
		if not stats or stats['bets'] == 0:
			await self.instance.chat(
				'$aaaNo resolved bets yet — place your first bet with $fff/bet <login> <amount>$aaa or $fff/betmarket$aaa!',
				player
			)
			return

		await self.instance.chat(
			'$ff0Your stats: $fff{}$ff0 bets · $fff{}%$ff0 wins · $fff{}$ff0 wagered · '
			'net $fff{:+d}$ff0 · best win $fff{}$ff0 · streak $fff{}$ff0.'.format(
				stats['bets'], stats['win_rate'], stats['wagered'], stats['net'],
				stats['best_win'], self.format_streak(stats['streak'])
			),
			player
		)

	async def chat_leaderboard(self, player, **kwargs):
		leaderboard = await self.get_leaderboard(limit=1)
		if not leaderboard:
			await self.instance.chat(
				'$aaaNo betting history yet — the leaderboard will appear once bets start resolving.',
				player
			)
			return
		view = BetLeaderboardView(self)
		await view.display(player=player)

	async def chat_admin_open_betting(self, player, **kwargs):
		# Force-open really means force: this is also the way to resume betting after a PyPlanet
		# (re)start, where the market intentionally stays closed for the map already in progress
		# (see on_start).
		async with self.lock:
			await self._open_market()

		await self.instance.chat(
			'$ff0Betting has been opened by $fff{}$ff0.'.format(player.nickname or player.login)
		)
		await self.widget.display()

	async def chat_admin_close_betting(self, player, **kwargs):
		async with self.lock:
			self.market_manually_closed = True

		await self.instance.chat(
			'$ff0Betting has been closed by $fff{}$ff0.'.format(player.nickname or player.login)
		)
		await self.widget.display()

	async def chat_help_public(self, player, data=None, **kwargs):
		topic = data.topic if data and hasattr(data, 'topic') and data.topic else None
		if topic and topic.lower() != 'bet':
			await self.instance.chat('$f00Unknown help topic. Use /help bet for betting commands.', player)
			return

		await self.instance.chat('$ff0--- BetPluggin Public Commands ---', player)
		await self.instance.chat('$fff/bet <login> <amount>$ff0 - Bet planets on a player winning the current period.', player)
		await self.instance.chat('$fff/betmarket$ff0 (or /market, /odds) - Open betting market with live odds and quick-bet buttons.', player)
		await self.instance.chat('$fff/mybet$ff0 (or /bets) - Show your active bet(s) for the current period.', player)
		await self.instance.chat('$fff/wallet$ff0 (or /stats, /betstats) - Show your betting history and stats.', player)
		await self.instance.chat('$fff/bettop$ff0 (or /betladder) - Open the all-time betting leaderboard.', player)

	async def chat_help_admin(self, player, **kwargs):
		await self.instance.chat('$ff0--- BetPluggin Admin Commands ---', player)
		await self.instance.chat('$fff//bet open$ff0 - Force-open betting for the current period.', player)
		await self.instance.chat('$fff//bet close$ff0 - Close betting for the current period early.', player)
		await self.instance.chat('$fff//bet help$ff0 - Show this admin command list.', player)
