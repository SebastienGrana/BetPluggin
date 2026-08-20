"""
BetPluggin: players wager real ManiaPlanet Planets on who will win the current market period.

A "market period" is always the whole map, but it is opened and settled differently depending on the
mode (picked once at map_begin from the active mode script, see ROUND_SCOPED_MODES below):

- Map-scoped modes (TimeAttack, Cup, anything we haven't special-cased): betting opens at map_begin,
  auto-closes partway through the map, and settles on whoever finished first.
- Round-based modes (Rounds, Teams, Knockout): a map is played as several rounds and what matters is
  the standing at the end of them, so betting opens for the whole warmup, closes the moment the warmup
  ends, and settles at map_end on whoever holds the most map points. The rounds themselves neither
  open, close nor resolve anything -- they are the race being bet on.

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
import datetime
import logging
import math
import time

from collections import OrderedDict

from pyplanet.apps.config import AppConfig
from pyplanet.apps.core.maniaplanet import callbacks as mp_signals
from pyplanet.apps.core.maniaplanet.models import Player
from pyplanet.apps.core.trackmania.callbacks import (
	scores as tm_scores_signal,
	warmup_end as tm_warmup_end_signal,
	warmup_start as tm_warmup_start_signal,
)
from pyplanet.contrib.command import Command
from pyplanet.contrib.setting import Setting

from .duel import DuelManager
from .models import Bet, RaceResult
from .views import (
	BetWidget, BetMarketView, BetLeaderboardView, BetResultView, BetTargetsView, BetDuelBoardView
)

# Modes (matched case-insensitively against the mode script name, e.g. "Trackmania/TM_Rounds_Online")
# that play a map as a series of rounds. Betting in these opens for the warmup only and settles on map
# points at map_end -- see the module docstring. Everything not listed here (TimeAttack, Cup, Laps,
# Stunts, and anything new/unknown) opens at map_begin and settles on who finished first. Add to this
# list as you learn which of your server's modes behave like Rounds.
ROUND_SCOPED_MODES = ('rounds', 'teams', 'knockout')

# ManiaPlanet.BillUpdated state codes.
BILL_STATE_VALIDATED = 4
BILL_STATE_REFUSED = 5
BILL_STATE_ERROR = 6

# The states that actually settle a bill. Everything below 4 (1 CreatingTransaction, 2 Issued,
# 3 ValidatingPayement) is progress reporting: the *same* bill fires bill_updated once per state, so
# anything that consumes a pending entry must only do so once one of these lands. Consuming it on a
# transitional state throws the entry away before "Payed" ever arrives -- the stake is taken from the
# player and the bet is never activated, refunded or declined.
BILL_STATES_TERMINAL = (BILL_STATE_VALIDATED, BILL_STATE_REFUSED, BILL_STATE_ERROR)

# How long _resolve_market waits for a `scores` payload before giving up and refunding everyone.
# `scores` is a *script* callback fired around the podium, while map_end is the *native*
# ManiaPlanet.EndMap -- so the scores we need in order to pick a winner routinely arrive a few seconds
# after we've been asked to resolve. Without this wait, map-scoped bets get refunded wholesale.
SCORES_GRACE_SECONDS = 10

# How long the per-player result window stays up after a period resolves, before hiding itself. Long
# enough to read three lines during the podium. Players can also dismiss it early with its close button.
RESULT_POPUP_SECONDS = 12

# The same card is shown again for this long at the start of the next map. The podium alone isn't a
# reliable place to deliver a result: it's the noisiest moment of the map, and a player still on the
# loading screen misses it entirely. Kept short -- it's a reminder, not a second announcement.
RESULT_REPLAY_SECONDS = 6

# `scores` fires at the end of every round as well as at the end of the map, and only the latter is a
# final standing. Matched case-insensitively as a substring of the callback's `section`. An empty or
# missing section is also treated as final -- that's what map-scoped modes report at the podium.
FINAL_SCORE_SECTIONS = ('endmap', 'endmatch')

# How long after the betting window shuts a stake whose payment popup was already open may still
# confirm and join the pool. See _within_stake_grace.
STAKE_CONFIRM_GRACE_SECONDS = 20

# Prefix on every BetPluggin chat line. `$z$s` resets whatever formatting the previous message left
# behind (nicknames routinely leave colours open), so our messages are recognisable at a glance in a
# chat that scrolls fast during the podium.
CHAT_PREFIX = '$z$s$ff0[$fffBet$ff0]$z$s '

logger = logging.getLogger(__name__)


def plural(count, word):
	"""
	"1 player" / "3 players". Every count this plugin announces is read as a headcount by whoever reads
	it, so getting the noun right matters more than the usual "(s)" shrug: "3 bet(s)" was routinely read
	as three people when it was one player reinforcing three times.
	"""
	return '{} {}{}'.format(count, word, '' if count == 1 else 's')


class BetplugginApp(AppConfig):
	game_dependencies = ['trackmania', 'trackmania_next']
	app_dependencies = ['core.maniaplanet', 'core.trackmania']

	# How many separate stakes one player may put on their chosen driver in a single betting period.
	# More than one so a pick can be reinforced as the odds move; the sum is still capped by the
	# max stake setting, so this never becomes a way to bet above the maximum. See place_bet.
	MAX_BETS_PER_PERIOD = 3

	# The chat prefix, exposed on the app as well as at module level. duel.py and views.py both need it
	# and cannot import it back out of this module: both are imported *from* here, on a line above the
	# one that defines it, so the import would run before the constant exists. The right-hand side below
	# is that module constant -- the class namespace is still empty at this point, so the name resolves
	# outward to the module.
	CHAT_PREFIX = CHAT_PREFIX

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.lock = asyncio.Lock()

		self.scope = Bet.SCOPE_MAP
		self.market_open = False
		self.market_manually_closed = False
		# True only right after a (re)start, for the map that was already running when PyPlanet came back
		# up -- see map_begin(). Cleared as soon as a market genuinely opens again (_open_market).
		self.closed_for_reboot = False
		self.current_round_number = None

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

		# Per-period holder for the `scores` payload, rebuilt by _open_market. A dict rather than a plain
		# attribute so _resolve_market can capture *this* period's slot and keep reading it even if the
		# next map/round starts while it's still waiting on the podium.
		# Shape: {'scores': dict|None, 'event': asyncio.Event}
		self.scores_slot = None

		# id of the last map whose finishing order was written to RaceResult. `scores` fires more than
		# once at the end of a map (EndMap, then EndMatch), and both carry a final standing -- without
		# this the same race would be recorded twice and count double towards a driver's strength.
		self.results_recorded_for = None

		self.widget = None

		# The result card from the period that just resolved, kept alive past its podium showing so
		# map_begin can replay it on the next map (see RESULT_REPLAY_SECONDS). The task is the podium
		# showing itself, cancelled at replay time so its trailing hide() doesn't close the replay.
		self.result_view = None
		self.result_view_task = None

		# Market windows currently open, keyed by login. Each /betmarket builds a fresh view instance, so
		# without this the app has no handle on the window a player is actually looking at and can't
		# refresh it when their bet confirms asynchronously.
		self.market_views = {}

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

		self.setting_betting_window_seconds = Setting(
			'betting_window_seconds', 'Betting window (seconds)', Setting.CAT_BEHAVIOUR, type=int,
			description='How many seconds betting stays open from the start of the map/round. This is the '
						'straightforward way to set the window -- you get exactly the number you type, '
						'whatever the map time limit is. 0 falls back to the percentage setting below.',
			default=0
		)

		self.setting_betting_window = Setting(
			'betting_window_percent', 'Betting window (% of the period)', Setting.CAT_BEHAVIOUR, type=int,
			description='Fallback for when the seconds setting above is 0: close betting once this share '
						'of the map/round time has passed. Betting open until the very end lets players '
						'bet on a result they can already see; closing early makes it a prediction again. '
						'0 or 100 keeps the market open all period.',
			default=50
		)

		self.setting_closing_warning = Setting(
			'market_closing_warning_seconds', 'Closing warning (seconds)', Setting.CAT_BEHAVIOUR, type=int,
			description='How long before the betting window shuts that players are warned in chat. Long '
						"enough to still act on it, short enough that the warning isn't forgotten before "
						'it matters.',
			default=30
		)

		# ------------------------------------------------------------------
		# Duels. Every number here is a setting rather than a constant on purpose: whether a duel is fun
		# depends entirely on how long you get to answer one and how big the stakes are allowed to be,
		# and that can only be found out by playing. Changing a constant means a new deploy; changing a
		# setting means typing //settings between two maps.
		# ------------------------------------------------------------------

		self.setting_duel_enabled = Setting(
			'duel_enabled', 'Duels enabled', Setting.CAT_BEHAVIOUR, type=bool,
			description='Lets players challenge each other with /duel. A duel is settled between the two '
						'of them (whoever finishes ahead takes the other\'s stake) and is separate from '
						'the betting market.',
			default=True
		)

		self.setting_duel_min_stake = Setting(
			'duel_minimum_stake', 'Duel: minimum stake (planets)', Setting.CAT_BEHAVIOUR, type=int,
			description='Smallest amount either side of a duel can put up. Worth setting higher than the '
						'market minimum: a duel you barely notice losing is not a duel.',
			default=50
		)

		self.setting_duel_max_stake = Setting(
			'duel_maximum_stake', 'Duel: maximum stake (planets)', Setting.CAT_BEHAVIOUR, type=int,
			description='Largest amount either side of a duel can put up. Both sides are capped by this '
						'independently, so the answer to a challenge can still be bigger than the challenge.',
			default=5000
		)

		self.setting_duel_accept_seconds = Setting(
			'duel_accept_seconds', 'Duel: seconds to answer', Setting.CAT_BEHAVIOUR, type=int,
			description='How long the challenged player has to accept or refuse before the duel is called '
						'off. Long enough to answer while driving, short enough that the map is not half '
						'over by the time the duel starts.',
			default=45
		)

		self.setting_duel_spectators = Setting(
			'duel_spectators', 'Duel: let others bet on it', Setting.CAT_BEHAVIOUR, type=bool,
			description='Lets everyone except the two duellists back a side with /duelbet, in a pot of '
						'their own that never touches what the two players agreed between themselves.',
			default=True
		)

		self.setting_record_results = Setting(
			'record_race_results', 'Record race history', Setting.CAT_BEHAVIOUR, type=bool,
			description='Writes the finishing order of every race, which the position bets will later '
						'need to estimate how strong each player is. One row per driver per map -- a full '
						'server running non-stop adds a few million rows a year, on a table nothing reads '
						'yet. Turn it off to stop collecting; the history already gathered is kept.',
			default=True
		)

		# Owns the one duel a map may have. Given the app rather than the other way round so every chat
		# line, refund and payout still goes through the app's single implementation of each.
		self.duels = DuelManager(self)

		# Pending auto-close of the betting window for the period currently open. See _schedule_market_close.
		self.market_close_task = None

		# When that auto-close is due to fire (time.time()), or None when nothing is armed. Kept so the
		# widget and the market window can tell players how long they have left to bet.
		self.market_closes_at = None

		# Redraws that countdown on a schedule while the window is open. A manialink is a static drawing:
		# nothing on the client counts anything down, so a "live" timer has to be the server re-rendering.
		# See _tick_market_countdown for why this is not simply a 1s loop.
		self.market_tick_task = None

		# When the betting window last shut on a pool that is still alive (time.time()), or None. Only
		# used to grant late payment confirmations a short grace -- see _within_stake_grace.
		self.market_closed_at = None

		# True between the warmup_start and warmup_end callbacks. Round-based modes fire round_start /
		# round_end for warmup laps exactly as they do for real ones, and those must not open, close or
		# resolve anything. See warmup_start.
		self.warmup_active = False

		# When the market currently open was opened (time.time()), or None. The auto-close is computed
		# from this anchor rather than from "now" so that recomputing it mid-period -- which //extend
		# forces us to do -- still lands the window where it would have landed had the period always
		# had its new length. See _schedule_market_close.
		self.market_opened_at = None

		# S_TimeLimit as it was when the auto-close was last armed, so //extend can tell whether the
		# period it is lengthening is the one our window was measured against.
		self.period_time_limit = None

		# When //pause froze the game (time.time()), or None. The betting countdown is an asyncio timer
		# and knows nothing about the game being paused, so it has to be frozen by hand -- otherwise the
		# window expires while nobody is driving. See _pause_market / _resume_market.
		self.market_paused_at = None

		# The native PyPlanet commands whose behaviour we react to, as
		# [(Command instance, original target)] so on_stop can put them back exactly as they were.
		# See _install_command_hooks.
		self.hooked_commands = []

		# The same, for the chat-vote outcomes in PyPlanet's `voting` app, as
		# [(voting app, method name, original method)]. See _install_vote_hooks.
		self.hooked_votes = []

	async def on_start(self):
		await self.instance.command_manager.register(
			Command(
				command='bet', target=self.chat_bet,
				description='Bet planets that a player will win the current period. Usage: /bet <login> <amount>'
			).add_param(name='login', type=str, required=False, help='Login of the player you think will win.')
			 .add_param(name='amount', type=int, required=False, help='Amount of planets to bet.'),

			Command(
				command='betmarket', aliases=['market', 'odds'], target=self.chat_open_market,
				description='Open the betting window: online players, live multipliers and one-click bets.'
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
				command='bettargets', aliases=['targets', 'cotes'], target=self.chat_targets,
				description='Who is worth betting on: how often each player is backed, how often they '
							'deliver, and the badges they have earned.'
			),

			Command(
				command='duels', aliases=['dueltop', 'duelboard'], target=self.chat_duel_board,
				description='Open the duel record board: who has won the most duels on this server.'
			),

			Command(
				command='duel', target=self.chat_duel,
				description='Challenge another player: whoever finishes ahead takes the other\'s stake. '
							'Usage: /duel <login> <amount> -- or /duel list for the record board.'
			).add_param(name='login', type=str, required=False, help='Login of the player you want to duel, or "list".')
			 .add_param(name='amount', type=int, required=False, help='Planets you are putting up.'),

			# Chat answers as well as the popup window, because the popup is one manialink among many: it
			# can be missed, dismissed, or lost to a client hiccup, and a duel that can only be answered
			# through a window nobody can find any more is a duel that dies of a timeout.
			Command(
				command='accept', target=self.chat_duel_accept,
				description='Accept the duel you have been challenged to. Usage: /accept <amount>'
			).add_param(name='amount', type=int, required=False, help='Planets you put up (defaults to matching).'),

			Command(
				command='decline', aliases=['refuse'], target=self.chat_duel_decline,
				description='Turn down the duel you have been challenged to. Nothing is charged.'
			),

			Command(
				command='duelbet', aliases=['back'], target=self.chat_duel_bet,
				description='Back one of the two players in the running duel. Usage: /duelbet <login> <amount>'
			).add_param(name='login', type=str, required=False, help='Which duellist you are backing.')
			 .add_param(name='amount', type=int, required=False, help='Amount of planets to bet.'),

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
			self.setting_quick_bet_amounts,
			self.setting_min_stake, self.setting_max_stake,
			self.setting_betting_window_seconds, self.setting_betting_window,
			self.setting_closing_warning,
			self.setting_duel_enabled,
			self.setting_duel_min_stake, self.setting_duel_max_stake,
			self.setting_duel_accept_seconds, self.setting_duel_spectators,
			self.setting_record_results,
		)

		self.context.signals.listen(mp_signals.map.map_begin, self.map_begin)
		self.context.signals.listen(mp_signals.map.map_end, self.map_end)
		self.context.signals.listen(mp_signals.flow.round_start, self.round_start)
		self.context.signals.listen(mp_signals.flow.round_end, self.round_end)
		self.context.signals.listen(mp_signals.player.player_connect, self.player_connect)
		self.context.signals.listen(mp_signals.other.bill_updated, self.on_bill_updated)
		self.context.signals.listen(tm_scores_signal, self.on_scores)
		self.context.signals.listen(tm_warmup_start_signal, self.warmup_start)
		self.context.signals.listen(tm_warmup_end_signal, self.warmup_end)

		self.widget = BetWidget(self)

		# The commands that cut a map short, lengthen it, or freeze it -- the admin ones an admin types,
		# and the player ones that get there by chat vote. Not signals: there aren't any for these.
		await self._install_command_hooks()
		await self._install_vote_hooks()

		# Handle the map that might already be running when the app (re)starts. We can't know how much
		# of it has already played out, so don't reopen a fresh betting window for it -- that would let
		# players who've already seen part of the run bet with information they shouldn't have. Betting
		# resumes normally on the next real map/round start.
		current_map = self.instance.map_manager.current_map
		if current_map:
			await self.map_begin(current_map, resuming=True)
			await self._recover_bets_after_restart(current_map)
		if self.widget:
			await self.widget.display()

	async def on_stop(self):
		self._restore_command_hooks()

	# ------------------------------------------------------------------
	# Native PyPlanet commands
	# ------------------------------------------------------------------

	# The admin commands that change the shape of the period players are betting on, and what we owe
	# them when one is used.
	#
	# Each entry is (command name, when to react, handler). `command` is the name PyPlanet registered
	# in its own admin app; aliases come along for free because we hook the Command *object*, and
	# //res, //rs, //prev, //endpause and //resume all share one object with their main name.
	#
	# `when` is 'before' for anything that cuts the map short -- the reaction has to be finished and
	# the pool emptied before the map actually ends, or map_end resolves it on the way past. It is
	# 'after' for anything whose effect we need to read back off the server (the new time limit, the
	# fact that the game really is paused now).
	NATIVE_COMMAND_HOOKS = (
		# NextMap: the map stops here, whether or not it was going to have a winner.
		('next', 'before', '_native_map_cut'),
		('skip', 'before', '_native_map_cut'),
		('previous', 'before', '_native_map_cut'),
		# RestartMap: the map is played again from zero, so every time set so far is thrown away.
		('restart', 'before', '_native_map_cut'),
		# The map gets longer. Nothing is voided; the betting window is re-measured against the new length.
		('extend', 'after', '_native_extend'),
		# The game freezes but our asyncio countdown doesn't, so the window would expire during the pause.
		('pause', 'after', '_native_pause'),
		('unpause', 'after', '_native_unpause'),
	)

	# The same map-flow actions, reached the other way: PyPlanet's `voting` app gives *players* /skip,
	# /restart, /previous and /extend, which start a chat vote rather than doing anything themselves.
	#
	# So these hook the vote's outcome, not its command. A vote that nobody backs must leave the market
	# exactly as it found it, and hooking /skip would call betting off the moment somebody asked for a
	# skip the server then refused. `//pass` (an admin forcing a vote through) lands on the same
	# handlers, so it is covered by these too.
	#
	# Each entry is (method on the voting app, when to react, handler, name to react under).
	NATIVE_VOTE_HOOKS = (
		('vote_skip_passed', 'before', '_native_map_cut', 'vote skip'),
		('vote_previous_passed', 'before', '_native_map_cut', 'vote previous'),
		('vote_restart_passed', 'before', '_native_map_cut', 'vote restart'),
		('vote_extend_passed', 'after', '_native_extend', 'vote extend'),
	)

	# Why the period was called off, per hooked action. Named rather than generic because "betting is
	# off" with no reason reads as the plugin having lost track of the map -- and a player who has just
	# had a stake handed back is owed the difference between "an admin skipped it" and "you all voted to".
	CUT_REASONS = {
		'skip': 'the map was skipped by an admin',
		'next': 'the map was skipped by an admin',
		'previous': 'the map was skipped by an admin',
		'restart': 'the map was restarted by an admin',
		'vote skip': 'the players voted to skip the map',
		'vote previous': 'the players voted to go back to the previous map',
		'vote restart': 'the players voted to restart the map',
	}

	async def _install_command_hooks(self):
		"""
		Make the plugin react to PyPlanet's own admin commands.

		There is no signal for "an admin skipped the map". `mp_signals.map.map_start` carries a
		`restarted` flag but arrives *after* map_end, i.e. after the pot has already been paid out, and
		listening for the chat line misses the way admins actually skip: the admin toolbar calls
		`command_manager.execute(player, '//skip')`, which invokes the command dispatcher directly and
		never raises player_chat.

		So we hook the one place every path passes through. `Command.handle()` checks the permission,
		parses and validates the arguments, and only then calls `self.target(...)`. Replacing that
		target with a wrapper catches the chat line, the toolbar button and any programmatic call in
		one go, and fires only for an admin who is actually allowed to run the command with arguments
		that actually parsed -- which a chat listener could never tell.

		Registering our own //skip would not work: the dispatcher hands a line to the *first* matching
		command, so ours would either shadow the real one or never be reached.

		The wrapper must be `async def`: Command.handle only awaits its target if
		`iscoroutinefunction` says it can.
		"""
		wanted = {name: (when, handler) for name, when, handler in self.NATIVE_COMMAND_HOOKS}
		found = []

		for command in list(self.instance.command_manager._commands):
			if not getattr(command, 'admin', False) or getattr(command, 'namespace', None):
				continue
			if command.command not in wanted:
				continue

			when, handler_name = wanted[command.command]
			original = command.target
			command.target = self._make_command_hook(command.command, when, handler_name, original)
			self.hooked_commands.append((command, original))
			found.append(command.command)

		missing = sorted(set(wanted) - set(found))
		if missing:
			# Not fatal -- the plugin works, it just won't notice those commands. Worth a line in the log
			# because the symptom ("the pot was paid out on a skipped map") gives no hint of the cause.
			logger.warning(
				'BetPluggin: could not hook the native admin command(s) %s -- betting will not react to '
				'them. Is pyplanet.apps.contrib.admin enabled, and listed before apps.betpluggin?',
				', '.join('//{}'.format(name) for name in missing)
			)

	def _make_command_hook(self, name, when, handler_name, original):
		async def hook(*args, **kwargs):
			handler = getattr(self, handler_name)
			# Logged unconditionally: "betting was called off and I don't know why" is otherwise
			# indistinguishable from a bug, and this line is the difference.
			logger.info('BetPluggin: reacting to //%s (%s it runs).', name, when)

			if when == 'before':
				# Never let our reaction stop the admin's command. An admin who types //skip has to get
				# a skip even if the refunds blew up; the alternative is a server nobody can steer.
				try:
					await handler(name, kwargs.get('player'), kwargs.get('data'))
				except Exception as e:
					logger.exception('BetPluggin: reacting to //%s failed: %s', name, e)
				return await original(*args, **kwargs)

			result = await original(*args, **kwargs)
			try:
				await handler(name, kwargs.get('player'), kwargs.get('data'))
			except Exception as e:
				logger.exception('BetPluggin: reacting to //%s failed: %s', name, e)
			return result

		return hook

	async def _install_vote_hooks(self):
		"""
		Same reactions, for the chat votes that reach the same actions. See NATIVE_VOTE_HOOKS.

		Wrapping methods on the voting app instance rather than its commands, because the commands only
		open a vote. There is no signal for "a vote passed" either -- PyPlanet calls these handlers
		directly from its own vote bookkeeping.
		"""
		voting = self.instance.apps.apps.get('voting')
		if voting is None:
			# Perfectly normal: the voting app is optional and plenty of servers run without it.
			logger.info('BetPluggin: the `voting` app is not loaded -- no chat votes to react to.')
			return

		for attr, when, handler_name, name in self.NATIVE_VOTE_HOOKS:
			original = getattr(voting, attr, None)
			if original is None:
				logger.warning(
					'BetPluggin: the voting app has no %s() -- betting will not react to that vote.', attr
				)
				continue
			setattr(voting, attr, self._make_vote_hook(name, when, handler_name, original))
			self.hooked_votes.append((voting, attr, original))

	def _make_vote_hook(self, name, when, handler_name, original):
		async def hook(vote, forced, *args, **kwargs):
			handler = getattr(self, handler_name)
			logger.info('BetPluggin: reacting to the %s vote passing (%s it runs).', name, when)

			if when == 'before':
				try:
					await handler(name, getattr(vote, 'requester', None), None)
				except Exception as e:
					logger.exception('BetPluggin: reacting to the %s vote failed: %s', name, e)
				return await original(vote, forced, *args, **kwargs)

			result = await original(vote, forced, *args, **kwargs)
			try:
				await handler(name, getattr(vote, 'requester', None), None)
			except Exception as e:
				logger.exception('BetPluggin: reacting to the %s vote failed: %s', name, e)
			return result

		return hook

	def _restore_command_hooks(self):
		"""Put every wrapped target back. Leaving a closure over a dead app behind would keep firing."""
		for command, original in self.hooked_commands:
			command.target = original
		self.hooked_commands = []

		for owner, attr, original in self.hooked_votes:
			setattr(owner, attr, original)
		self.hooked_votes = []

	async def _native_map_cut(self, name, player, data=None):
		"""
		//skip, //next, //previous, //restart: the map stops without a result, or is about to be driven
		again from scratch. Either way nothing that was bet on it can be settled honestly -- the times
		on the board belong to a race that was never finished.

		Refunding here rather than at map_end is what makes this ordering-independent: by the time
		ManiaPlanet.EndMap comes back from the server the pool is already empty and the duel already
		void, so _resolve_market walks straight past.
		"""
		if name == 'previous':
			# //previous refuses to do anything in two cases, and voiding a map that then keeps playing
			# would be worse than not reacting at all. Same two checks PyPlanet makes. The *vote*
			# version makes no such check and always calls NextMap, so it is deliberately not included.
			previous = self.instance.map_manager.previous_map
			if not previous or previous == self.instance.map_manager.current_map:
				return

		await self._void_period(self.CUT_REASONS.get(name, 'the map was cut short'))

	async def _native_extend(self, name, player, data=None):
		"""
		//extend lengthens the current map (TimeAttack only). It rewrites S_TimeLimit -- the very number
		_schedule_market_close measured the betting window against -- so without this the window silently
		becomes a much smaller share of the map than the setting says it is.

		Only the percentage window moves. "Betting is open for the first 30% of the map" is a statement
		about the map's length and has to follow it; "betting is open for 60 seconds" is not, and an
		extension is no reason to hand out more time than the admin asked for.
		"""
		async with self.lock:
			if not self.market_is_open or self.market_closes_at is None:
				return
			if self.market_paused_at is not None:
				# Frozen mid-pause; _resume_market re-arms it and will pick up the new limit then.
				return

			seconds = await self.setting_betting_window_seconds.get_value()
			if seconds and seconds > 0:
				return

			previous_limit = self.period_time_limit
			# The extension is applied with SetModeScriptSettings and read back with
			# GetModeScriptSettings; give the script a moment to have taken it before asking.
			await asyncio.sleep(1)
			await self._schedule_market_close(anchor=self.market_opened_at)
			new_limit = self.period_time_limit

		if not previous_limit or not new_limit or new_limit <= previous_limit:
			return

		left = int(max((self.market_closes_at or 0) - time.time(), 0))
		await self.instance.chat(
			'{}$ff0The map was extended -- betting stays open for $fff{}$ff0 more seconds.'.format(
				CHAT_PREFIX, left
			)
		)
		await self._refresh_ui()

	async def _native_pause(self, name, player, data=None):
		await self._pause_market()

	async def _native_unpause(self, name, player, data=None):
		await self._resume_market()

	async def _pause_market(self):
		"""
		Freeze the betting countdown for the duration of a //pause. The close is an asyncio timer and
		has no idea the game stopped, so a long enough pause closes the market before anyone has driven
		a metre of the map they were betting on.

		market_closes_at is deliberately left where it is: the widget draws a static manialink, so with
		the ticker cancelled the number simply stops moving on everyone's screen, which is exactly what
		a frozen clock should look like. _resume_market pushes it forward by however long the pause was.
		"""
		async with self.lock:
			if self.market_paused_at is not None:
				return
			if not self.market_is_open or self.market_closes_at is None:
				return
			self.market_paused_at = time.time()
			self._cancel_market_close(keep_deadline=True)

		await self.instance.chat(
			'{}$ff0Match paused -- the betting countdown is on hold.'.format(CHAT_PREFIX)
		)
		await self._refresh_ui()

	async def _resume_market(self):
		async with self.lock:
			paused_at, self.market_paused_at = self.market_paused_at, None
			if paused_at is None or self.market_closes_at is None:
				return
			if not self.market_is_open:
				return

			self.market_closes_at += time.time() - paused_at
			remaining = self.market_closes_at - time.time()
			self.market_close_task = asyncio.ensure_future(self._close_market_after(remaining))
			self.market_tick_task = asyncio.ensure_future(self._tick_market_countdown())

		await self.instance.chat(
			'{}$ff0Match resumed -- $fff{}$ff0 seconds left to bet.'.format(CHAT_PREFIX, int(max(remaining, 0)))
		)
		await self._refresh_ui()

	async def _void_period(self, reason):
		"""
		Call off the whole period: every confirmed stake goes back to the player who made it, and any
		duel -- offered, half-paid or running -- is called off too.

		This is not a resolution. Nobody wins, nobody loses, and nothing is written to anyone's record:
		each bet is closed with won=None, the same marker _resolve_market uses for a refund, which
		get_leaderboard skips outright so a skipped map never dents a streak.
		"""
		async with self.lock:
			self._cancel_market_close()
			bets = self.current_bets
			self.current_bets = []
			self.market_open = False
			self.market_manually_closed = False
			# Same as _resolve_market: this pool is gone, so a stake confirming a second from now has
			# nothing left to join and is refunded by _handle_stake_bill instead.
			self.market_closed_at = None

		await self.duels.void(reason)

		if bets:
			total = sum(bet['amount'] for bet in bets)
			for bet in bets:
				bet['bet'].won = None
				bet['bet'].payout = 0
				bet['bet'].state = Bet.STATE_RESOLVED
				await bet['bet'].save()
				await self._refund(bet['player'], bet['amount'], 'Betting was called off: {}.'.format(reason))

			await self.instance.chat(
				'{}$ff0Betting is off: {}. All {} planets from {} have been refunded.'.format(
					CHAT_PREFIX, reason, total, plural(len(bets), 'bet')
				)
			)

		await self._refresh_ui()

	# ------------------------------------------------------------------
	# Market lifecycle
	# ------------------------------------------------------------------

	@property
	def market_is_open(self):
		if self.market_manually_closed:
			return False
		if not self.market_open:
			return False
		return True

	async def _recover_bets_after_restart(self, current_map):
		"""
		Called once at startup. A bet left in PENDING or ACTIVE state means the previous process
		crashed or was restarted before that bet's map/round ever ended -- without this, it would sit
		in the database forever, never paid out, lost, or refunded.

		- ACTIVE bets for the map that's still running: reload them into memory so they resolve normally
		  at the next map_end/round_end, exactly as if the process had never restarted.
		- ACTIVE bets for any other (older) map: that period is definitely over and can never be
		  resolved properly anymore, so refund them right away instead of leaving them stuck.
		- PENDING bets: closed out as declined, never re-armed and never auto-refunded. See below.
		"""
		orphaned = await Bet.execute(
			Bet.select(Bet, Player).join(Player).where(Bet.state.in_([Bet.STATE_PENDING, Bet.STATE_ACTIVE]))
		)
		if not orphaned:
			return

		current_map_id = current_map.get_id()
		unsettled = []

		for bet in orphaned:
			if bet.state == Bet.STATE_PENDING:
				# Never re-arm pending_stakes[bet.stake_bill_id]. Bill ids are handed out by the
				# *dedicated server* and restart at 1 along with it, so a recovered id 1 would be
				# confirmed by the next brand-new, completely unrelated bill -- and two orphans sharing
				# an id would silently overwrite each other in the dict.
				#
				# Deliberately not auto-refunded either: unlike an ACTIVE bet, we never saw a "Payed"
				# confirmation, so we don't know the server ever received these planets. Refunding on
				# sight would turn every restart into a money printer (send a bill, decline it, wait for
				# a reboot, collect). Genuinely paid-but-orphaned stakes need an admin to settle by hand
				# -- which is why they're logged loudly below.
				bet.state = Bet.STATE_DECLINED
				await bet.save()
				unsettled.append(bet)
				continue

			if bet.map_id == current_map_id:
				entry = dict(
					bet=bet, player=bet.bettor, target_login=bet.target_login, amount=bet.amount,
					odds=bet.odds, round_number=bet.round_number,
				)
				async with self.lock:
					self.current_bets.append(entry)
			else:
				bet.won = None
				bet.payout = 0
				bet.state = Bet.STATE_RESOLVED
				await bet.save()
				await self._refund(
					bet.bettor, bet.amount,
					"PyPlanet restarted after this bet's map already ended."
				)

		if unsettled:
			logger.warning(
				'BetPluggin: %d stake(s) were still awaiting payment confirmation when PyPlanet '
				'restarted and can no longer be matched to a bill (bet ids: %s). Marked declined, NOT '
				'refunded -- check whether those planets actually reached the server and settle them '
				'by hand.',
				len(unsettled), ', '.join(str(b.id) for b in unsettled)
			)

	async def detect_scope(self):
		try:
			script = await self.instance.mode_manager.get_current_script()
		except Exception:
			return Bet.SCOPE_MAP

		script_lower = (script or '').lower()
		if any(keyword in script_lower for keyword in ROUND_SCOPED_MODES):
			return Bet.SCOPE_ROUND
		return Bet.SCOPE_MAP

	async def _discard_result_view(self):
		"""Stop and destroy the pending result card, if any. Safe to call when there isn't one."""
		view, task = self.result_view, self.result_view_task
		self.result_view = self.result_view_task = None
		if task and not task.done():
			task.cancel()
		if view:
			await view.destroy()

	async def _replay_result_view(self):
		"""Hand the card from the period that just resolved its second showing, on the map now starting."""
		view, task = self.result_view, self.result_view_task
		self.result_view = self.result_view_task = None
		if not view:
			return
		# The podium showing is normally still sleeping at this point; left running, its hide() would
		# close the replay a couple of seconds in.
		if task and not task.done():
			task.cancel()
		asyncio.ensure_future(view.replay_for(RESULT_REPLAY_SECONDS))

	async def map_begin(self, map, resuming=False, **kwargs):
		await self._replay_result_view()

		# Defensive: a missed warmup_end (mode change, script reload) would otherwise leave every round
		# of every following map treated as a warmup lap, and betting would never resolve again.
		self.warmup_active = False

		# Cleared per map, not just set once: map.id alone can't tell a fresh play of the same map apart
		# from the one that was just recorded, and the same map coming back around (map list looping,
		# an admin replaying it) is still a race worth counting.
		self.results_recorded_for = None

		async with self.lock:
			self.current_bets = []
			self.current_round_number = None
			self.scope = await self.detect_scope()

			if self.scope == Bet.SCOPE_MAP and not resuming:
				await self._open_market()
			else:
				# Round-scoped (wait for the first `round_start` to open the market), or we're resuming
				# mid-map after a (re)start -- either way, betting stays closed until the next real start.
				self._cancel_market_close()
				self.market_open = False
				self.closed_for_reboot = resuming

		if self.widget:
			await self.widget.display()

	async def warmup_start(self, **kwargs):
		"""
		Open betting for the whole warmup in round-based modes. This is the *only* window they get.

		The warmup is the one moment where betting on a Rounds map makes sense: nothing is at stake on
		track, everyone is watching everyone practise, and that practice is exactly the information a
		bettor wants. So no auto-close here -- the window lasts as long as the warmup does. It shuts the
		instant the warmup ends (see warmup_end) and the pool then rides the whole map, settling at
		map_end on whoever holds the most points.
		"""
		self.warmup_active = True
		if self.scope != Bet.SCOPE_ROUND:
			return

		async with self.lock:
			self.current_bets = []
			# Left at None for the entire map: the betting period *is* the map, so every period-matching
			# check (pending_stakes_this_period, _handle_stake_bill) compares None to None and keeps
			# recognising a warmup stake right through the rounds that follow.
			self.current_round_number = None
			await self._open_market(auto_close=False)

		await self.instance.chat(
			'{}$ff0Warmup: betting is $fffOPEN$ff0 for the whole warmup -- and only for the warmup. '
			'Watch them practise, then back the driver you think will finish the map with the most '
			'points.'.format(CHAT_PREFIX)
		)
		await self._refresh_ui()

	async def warmup_end(self, **kwargs):
		"""Shut the betting window. The pool stays alive until map_end -- see _resolve_market."""
		self.warmup_active = False
		if self.scope != Bet.SCOPE_ROUND:
			return

		async with self.lock:
			if not self.market_is_open:
				# Never opened (no warmup bets possible), or an admin already closed it by hand. Either
				# way there is nothing to close and nothing to announce.
				return
			self._cancel_market_close()
			self.market_open = False
			self.market_closed_at = time.time()
			pot = sum(bet['amount'] for bet in self.current_bets)
			count = len(self.current_bets)

		if count:
			await self.instance.chat(
				'{}$ff0Warmup over -- betting is now $fffCLOSED$ff0 for this map. $fff{}$ff0 planets '
				'over $fff{}$ff0 bet(s) are riding on the final standings. Results at the end of the '
				'last round!'.format(CHAT_PREFIX, pot, count)
			)
		else:
			await self.instance.chat(
				'{}$ff0Warmup over -- betting is now $fffCLOSED$ff0 for this map. Nobody bet this time; '
				'the window reopens at the next warmup.'.format(CHAT_PREFIX)
			)
		await self._refresh_ui()

	async def round_start(self, count, **kwargs):
		"""
		Deliberately does nothing in round-based modes.

		The betting period is the whole map: it was opened during the warmup and shut when the warmup
		ended, and it settles at map_end. A round is the race being bet on, not a market of its own --
		opening one here would let players bet with a scoreboard they can already read.
		"""
		return

	async def _open_market(self, auto_close=True):
		"""
		Must be called while holding self.lock.

		:param auto_close: arm the mid-period auto-close. False during the warmup, whose length is not a
			share of a round and which is meant to stay open from end to end.
		"""
		self.market_manually_closed = False
		self.market_open = True
		self.closed_for_reboot = False
		self.market_closed_at = None
		self.market_opened_at = time.time()
		# A pause that was never ended before the period turned over must not freeze the new one.
		self.market_paused_at = None
		# Always a fresh slot: whatever `scores` the warmup produced says nothing about who wins the
		# map now starting, and resolving against it would hand the pot to the wrong driver.
		self.scores_slot = dict(scores=None, event=asyncio.Event())
		if auto_close:
			await self._schedule_market_close()
		else:
			self._cancel_market_close()

	def _cancel_market_close(self, keep_deadline=False):
		"""
		Drop the pending auto-close, if any. Safe to call when there isn't one.

		:param keep_deadline: leave market_closes_at standing. Only _pause_market wants this: the
			deadline is still the truth about this window, it just isn't counting for the moment, and
			clearing it would blank the timer on every player's widget mid-pause.
		"""
		task, self.market_close_task = self.market_close_task, None
		if not keep_deadline:
			self.market_closes_at = None
		if task and not task.done():
			task.cancel()

		tick, self.market_tick_task = self.market_tick_task, None
		if tick and not tick.done():
			tick.cancel()

	async def _period_time_limit(self):
		"""
		How many seconds the period now starting is expected to last, or None if that can't be known.

		Read from the mode script rather than assumed: S_TimeLimit is what actually governs the clock,
		and it changes with the mode and with whatever the admins set. Modes that don't impose a limit
		report 0 or -1, in which case there is no share of the period to compute and betting simply
		stays open -- better than guessing a duration and cutting the market off at an arbitrary moment.
		"""
		try:
			settings = await self.instance.mode_manager.get_settings()
		except Exception as e:
			logger.warning('BetPluggin: could not read the mode settings (%s) -- betting stays open.', e)
			return None

		try:
			limit = int(settings.get('S_TimeLimit') or 0)
		except (TypeError, ValueError):
			limit = 0

		if limit <= 0:
			# Logged rather than swallowed: "betting never closes early" and "this mode has no clock"
			# look identical from in-game, and the difference is one setting name.
			logger.info(
				'BetPluggin: no usable S_TimeLimit in the mode settings (got %r) -- betting stays open '
				'for the whole period.', settings.get('S_TimeLimit')
			)
			return None
		return limit

	async def _schedule_market_close(self, anchor=None):
		"""
		Arm the auto-close for the period being opened. Must be called while holding self.lock.

		:param anchor: the moment the window is measured from, defaulting to now. //extend passes the
			time the market actually opened, so re-measuring the window against the map's new length
			lands it where it would have been had the map always been that long -- rather than
			restarting the whole window from the moment the admin typed the command.
		"""
		self._cancel_market_close()

		limit = await self._period_time_limit()
		# Remembered so //extend can tell how much longer the map just got.
		self.period_time_limit = limit

		# An explicit duration in seconds wins over the percentage. It's the setting an admin can reason
		# about without first working out what S_TimeLimit currently is ("betting is open for the first
		# minute" rather than "for the first 33% of however long the map lasts"), and it's the number
		# the window actually gets. The percentage stays as the fallback so setups configured that way
		# keep behaving exactly as before.
		seconds = await self.setting_betting_window_seconds.get_value()
		if seconds and seconds > 0:
			# A window longer than the period itself would simply never fire, so it's capped -- but only
			# when there *is* a time limit. Without one (untimed modes) the seconds setting is the only
			# thing that can close the market, which is precisely when it's most useful.
			delay = min(seconds, limit) if limit else seconds
		else:
			percent = await self.setting_betting_window.get_value()
			if not percent or percent <= 0 or percent >= 100:
				return
			if not limit:
				return
			delay = limit * percent / 100

		# Remembered so the widget and the market window can show how long is left.
		self.market_closes_at = (anchor if anchor is not None else time.time()) + delay
		# What is left to wait is not the same as the window's length once the anchor is in the past
		# (//extend). Clamped at zero: a window that has already run out closes on the next tick of the
		# loop rather than being scheduled for a moment that has been and gone.
		remaining = max(self.market_closes_at - time.time(), 0)
		# Fire and forget: this sleeps for most of the map, and _open_market is on the map_begin path.
		self.market_close_task = asyncio.ensure_future(self._close_market_after(remaining))
		self.market_tick_task = asyncio.ensure_future(self._tick_market_countdown())

	# How often the countdown is redrawn, as (seconds still left, seconds between redraws). Read in order,
	# first match wins. A flat 1s tick would be a full re-render of the widget for every player plus a
	# round of database queries per open market window, every second, for the whole betting window -- to
	# animate a number nobody is watching while there are still two minutes to go. The rate tightens as
	# the close approaches, which is where a second of staleness actually costs a player a bet.
	COUNTDOWN_TICKS = ((15, 1), (60, 5), (None, 15))

	async def _tick_market_countdown(self):
		"""
		Redraw the countdown while betting is open. Cancelled by _cancel_market_close.

		Nothing on the client is counting: a manialink is drawn once and then sits there. So "live" here
		means the server pushing a fresh render, and the displayed time trails reality by up to one tick.
		"""
		try:
			while True:
				closes_at = self.market_closes_at
				if not closes_at or not self.market_is_open:
					return
				left = closes_at - time.time()
				if left <= 0:
					# _close_market_after owns the close itself, and refreshes the UI when it happens.
					return

				interval = next(step for threshold, step in self.COUNTDOWN_TICKS if threshold is None or left <= threshold)
				# Never sleep past the deadline: overshooting would leave the last redraw showing a stale
				# "10s left" while the market is already shut.
				await asyncio.sleep(min(interval, left))
				await self._refresh_ui()
		except asyncio.CancelledError:
			raise
		except Exception as e:
			# A ticker is decoration. If it dies, betting must carry on without it rather than take the
			# period down -- but silently swallowing it would make a stuck countdown impossible to explain.
			logger.warning('BetPluggin countdown ticker stopped: {}'.format(e))

	async def _close_market_after(self, delay):
		"""Warn, then shut the betting window mid-period. Cancelled by _cancel_market_close."""
		# The announced countdown always matches the configured warning ("closes in N seconds" means
		# exactly N seconds from now) so it stays truthful to what the admin set. It's only ever reduced
		# below that if the window itself is too short to fit it -- at which point the warning fires
		# immediately on open, announcing however much time is actually left.
		warning = min(await self.setting_closing_warning.get_value(), delay)

		await asyncio.sleep(max(delay - warning, 0))
		# "closes in 0 seconds" is not a warning, it's noise -- and it's what a window with nothing left
		# in it would announce (a re-armed close whose deadline has already passed, see
		# _schedule_market_close). Skip straight to the close in that case.
		if warning >= 1 and self.market_is_open:
			await self.instance.chat(
				'{}$ff0Betting closes in $fff{}$ff0 seconds -- last chance to place your bet!'.format(
					CHAT_PREFIX, int(warning)
				)
			)

		await asyncio.sleep(warning)

		async with self.lock:
			# An admin may have closed (or the period ended) while we slept; don't announce a close that
			# already happened, and never re-close a market someone deliberately re-opened.
			if not self.market_open:
				return
			self.market_open = False
			self.market_closed_at = time.time()
		self.market_close_task = None
		self.market_closes_at = None

		await self.instance.chat(
			'{}$ff0Betting is now CLOSED for this period. Good luck!'.format(CHAT_PREFIX)
		)
		await self._refresh_ui()

	async def on_scores(self, players, winner_player, section=None, **kwargs):
		# Fired at the end of every round *and* around the end of the map. Always keep the latest
		# payload -- map_points are cumulative, so even a mid-map one is a usable fallback -- but only
		# wake a waiting _resolve_market on a final section. Setting the event on an EndRound payload
		# would let a map resolve against the standings of the second-to-last round.
		label = (section or '').lower()
		payload = dict(players=players, winner_player=winner_player, section=section)

		# Deliberately before the slot check below, which returns whenever no market was ever opened:
		# the history the odds model needs has to be built from *every* race, including the ones nobody
		# bet on. Only on EndMap -- EndMatch carries a final standing too, but it lands after the server
		# has already moved on to the next map, so map_manager would attribute it to the wrong one.
		if not label or 'endmap' in label:
			try:
				await self._record_race_results(payload)
			except Exception as e:
				# Bookkeeping must never cost anyone a payout: the pot is settled a few lines down.
				logger.exception('BetPluggin: recording the finishing order failed: %s', e)

		slot = self.scores_slot
		if slot is None:
			return
		slot['scores'] = payload

		if not label or any(final in label for final in FINAL_SCORE_SECTIONS):
			slot['event'].set()
			# Resolve right now instead of waiting for map_end: the final `scores` payload lands
			# right as the podium (chat_time) starts, while map_end only fires once the podium has
			# already played out and been dismissed -- by then nobody's still looking at it. map_end
			# still calls _resolve_market as a fallback for the rare case scores never arrive; that
			# call finds an already-emptied pool and is a no-op.
			await self._resolve_market('the map', by_map_points=self.scope == Bet.SCOPE_ROUND)

	async def round_end(self, count, **kwargs):
		"""
		Deliberately does nothing. See round_start: rounds play out the result, they don't settle it.

		The pool opened during the warmup rides every round of the map and is paid out once, at map_end,
		on the final points standing.
		"""
		return

	async def map_end(self, map, **kwargs):
		# Both scopes settle here. In round-based modes the map has just been played out over its N
		# rounds and the standing that decides the bet -- most points -- is only final now.
		await self._resolve_market('the map', by_map_points=self.scope == Bet.SCOPE_ROUND)

	async def _resolve_market(self, period_label, by_map_points=False):
		"""
		:param by_map_points: settle on whoever ends with the most map points rather than on whoever
			finished first. What decides a map played as several rounds.
		"""
		async with self.lock:
			self._cancel_market_close()
			bets = self.current_bets
			self.current_bets = []
			self.market_open = False
			# No late-confirmation grace here, unlike the other close paths: this pool is being paid out
			# right now, so a stake joining it a second later would join a market that no longer exists.
			self.market_closed_at = None
			slot = self.scores_slot

		# A duel is settled here too, so an empty market is not on its own a reason to stop: there may be
		# no market bets at all and still two players with real planets riding on each other.
		if not bets and self.duels.duel is None:
			await self._refresh_ui()
			return

		# We're most likely here before the podium has reported anything: map_end is the native
		# ManiaPlanet.EndMap while `scores` is a script callback fired around the podium a few seconds
		# later. Give it that grace period rather than declaring "no winner" and refunding everyone.
		# `slot` is captured above, so this keeps reading *this* period's scores even if the next
		# map/round starts while we wait.
		if slot is not None and slot['scores'] is None:
			try:
				await asyncio.wait_for(slot['event'].wait(), timeout=SCORES_GRACE_SECONDS)
			except asyncio.TimeoutError:
				logger.warning(
					'BetPluggin: no `scores` payload arrived within %ss of resolving %s -- refunding '
					'%d bet(s).', SCORES_GRACE_SECONDS, period_label, len(bets)
				)

		last_scores = slot['scores'] if slot else None

		# The duel reads the *same* payload as the market, and reads it here rather than from its own
		# signal handler: two separate reads can land either side of the next map's scores arriving, and
		# a duel decided on a different standing than the market it was played alongside would be
		# impossible to explain to the two players it just cost planets.
		await self.duels.resolve(last_scores, by_map_points)

		if not bets:
			await self._refresh_ui()
			return

		if by_map_points:
			winner_login = self.determine_map_winner_login(last_scores)
			# Phrasing matters here: on a Rounds map the driver who crosses the line first in the final
			# round is very often *not* the one who wins the map, and "finished first" would read as a
			# mistake to everyone who just watched it.
			win_reason = 'finished the map with the most points'
		else:
			winner_login = self.determine_winner_login(last_scores)
			win_reason = 'finished first'

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
				'{}$f00Could not determine a winner for {} -- all bets refunded.'.format(
					CHAT_PREFIX, period_label
				)
			)
			await self._refresh_ui()
			return

		total_pot = sum(bet['amount'] for bet in bets)
		winning_bets = [bet for bet in bets if bet['target_login'].lower() == winner_login.lower()]
		winning_pot = sum(bet['amount'] for bet in winning_bets)

		server_planets = await self._get_server_planets()
		winner_label = self.display_name(winner_login)

		if not winning_bets:
			# Nobody backed the driver who won. There is no pool to share the pot with, so the server
			# would silently keep every stake -- which reads as being robbed by a result nobody could
			# have profited from. Real pari-mutuel pools refund in exactly this case, and so do we.
			refund_cards = {}
			for bet in bets:
				# won=None is the ledger's "refunded" marker: get_leaderboard skips those rows entirely,
				# so a refund counts as neither a win nor a loss and doesn't dent anyone's streak.
				bet['bet'].won = None
				bet['bet'].payout = 0
				bet['bet'].state = Bet.STATE_RESOLVED
				await bet['bet'].save()
				await self._refund(
					bet['player'], bet['amount'],
					'Nobody bet on {}, the {} winner.'.format(winner_label, period_label)
				)
				refund_cards[bet['player'].login] = dict(
					_won=False,
					accent='FFD98CFF',
					headline='REFUNDED',
					winner_line='$fff{}$z winner: {}'.format(period_label.capitalize(), winner_label),
					stake_line='Your bet: {} on {}'.format(
						bet['amount'], self.display_name(bet['target_login'])
					),
					result_line='{} planets back'.format(bet['amount']),
					footer_line='Nobody backed the winner, so every bet was returned.',
				)

			await self._discard_result_view()
			view = BetResultView(self, refund_cards)
			self.result_view = view
			self.result_view_task = asyncio.ensure_future(view.show_for(RESULT_POPUP_SECONDS))

			await self.instance.chat(
				'{}$ff0{} won {}, and nobody had bet on them -- all $fff{}$ff0 planets refunded.'.format(
					CHAT_PREFIX, winner_label, period_label, total_pot
				)
			)
			await self._refresh_ui()
			return

		# Bettors, not stakes. One player may hold up to MAX_BETS_PER_PERIOD stakes on their driver, and
		# every line written below used to be per stake: three "You WON" messages for a single win, and a
		# public "3 bet(s)" that reads as three people when it was one player reinforcing three times.
		# Everything a player reads is grouped from here on; the payments stay per stake, because that is
		# what the ledger rows are and what the odds were quoted against.
		#
		# Grouped by (player, driver) rather than by player alone: one driver per period is a rule of
		# place_bet(), not an invariant this method can rely on -- an admin bet or a future mode could
		# break it, and a merged line would then claim a win and a loss at once.
		groups = OrderedDict()
		for bet in bets:
			won = bet in winning_bets
			payout = 0
			if won:
				payout = round(bet['amount'] * total_pot / winning_pot) if winning_pot > 0 else bet['amount']

			# Pari-mutuel: the odds quoted when the bet was placed are only indicative, since later bets
			# move the pot. Report what the stake *actually* returned, not the stale quote.
			profit = payout - bet['amount']
			key = (bet['player'].login, bet['target_login'].lower())
			groups.setdefault(key, []).append(
				dict(bet=bet, won=won, payout=payout, profit=profit)
			)

		# Heads, and stakes. Everything public counts heads; the stake count only ever appears next to it
		# as a parenthesis, so the two can no longer be mistaken for one another.
		stake_count = len(bets)
		bettor_count = len({bet['player'].login for bet in bets})
		winning_bettors = len({bet['player'].login for bet in winning_bets})

		# Announce before paying: the payment confirmation arrives asynchronously seconds later and, on
		# its own, is indistinguishable from a refund. Both passes run over the same `groups`, so nothing
		# said here can disagree with what is paid below.
		for (_, target_login), group in groups.items():
			player = group[0]['bet']['player']
			staked = sum(entry['bet']['amount'] for entry in group)
			# "over 3 bets" only when there were several. Saying it every time turns the normal case into
			# a line about bookkeeping.
			spread = ' over {} bets'.format(len(group)) if len(group) > 1 else ''

			if group[0]['won']:
				payout = sum(entry['payout'] for entry in group)
				await self._safe_chat(
					'{}$0f0You WON {}! $fff{}$0f0 {}. Your $fff{}$0f0 planets{} x $fff{}$0f0 '
					'give back $fff{}$0f0 planets $0f0(you gain $fff+{}$0f0). Payment on its way.'.format(
						CHAT_PREFIX, period_label, winner_label, win_reason, staked, spread,
						self.format_odds(payout / staked if staked else 0), payout, payout - staked
					),
					player
				)
			else:
				await self._safe_chat(
					'{}$f00You lost {}. You bet $fff{}$f00 planets{} on $fff{}$f00, but $fff{}$f00 {}.'.format(
						CHAT_PREFIX, period_label, staked, spread, self.display_name(target_login),
						winner_label, win_reason
					),
					player
				)

		# Per-login context for the podium result card, built as we go so the card shows exactly the
		# numbers the player was told in chat.
		cards = {}
		settled = []

		for (_, target_login), group in groups.items():
			for entry in group:
				bet, won, payout = entry['bet'], entry['won'], entry['payout']

				payout_bill_id = None
				if won and payout > 0:
					# Pay *before* recording the result. Writing won/payout first and only then discovering
					# the server can't afford it left the database -- and therefore the leaderboard --
					# claiming a payout that never actually arrived.
					payout_bill_id, server_planets = await self._pay_out(bet['player'], payout, server_planets)
					if payout_bill_id is None:
						# They still won; we just owe them. payout=0 keeps the ledger honest so an admin can
						# find the debt (won=True with payout=0) instead of it vanishing into the stats.
						payout = 0
						entry['payout'] = 0
						entry['profit'] = -bet['amount']

				bet['bet'].state = Bet.STATE_RESOLVED
				bet['bet'].won = won
				bet['bet'].payout = payout
				bet['bet'].payout_bill_id = payout_bill_id
				await bet['bet'].save()

			# One card per player, and the card adds the group up rather than showing whichever stake was
			# written last: "Your bet: 100" under a +900 result, when the player had staked 300 over three
			# bets, is the same confusion as the public line counting stakes as people.
			player = group[0]['bet']['player']
			won = group[0]['won']
			staked = sum(entry['bet']['amount'] for entry in group)
			paid = sum(entry['payout'] for entry in group)
			profit = paid - staked
			spread = ' over {} bets'.format(len(group)) if len(group) > 1 else ''

			settled.append(dict(player=player, won=won, staked=staked, payout=paid, profit=profit))

			existing = cards.get(player.login)
			if existing is None or (won and not existing['_won']):
				cards[player.login] = dict(
					_won=won,
					accent='73FF51FF' if won else 'FF7B7BFF',
					headline='YOU WON' if won else 'YOU LOST',
					winner_line='$fff{}$z winner: {}'.format(period_label.capitalize(), winner_label),
					stake_line='Your bet: {}{} on {}'.format(
						staked, spread, self.display_name(target_login)
					),
					result_line=(
						'+{} planets  (x{})'.format(
							profit, self.format_odds(paid / staked if staked else 0)
						) if won else '-{} planets'.format(staked)
					),
					footer_line=(
						'From {} planets played, shared by {}.'.format(
							total_pot, plural(winning_bettors, 'winner')
						)
						if won else '{} planets played by {}.'.format(
							total_pot, plural(bettor_count, 'player')
						)
					),
				)

		# Public line: nickname (not login), and who actually cashed in -- the old version announced the
		# race winner and left everyone guessing whether anyone had backed them. One entry per player, so
		# a player who reinforced three times appears once, with the three stakes added up.
		top_winners = sorted(
			(s for s in settled if s['won']), key=lambda s: s['profit'], reverse=True
		)[:3]
		if top_winners:
			payout_summary = 'Winners: {}.'.format(', '.join(
				'$fff{}$ff0 $0f0+{}$ff0'.format(
					s['player'].nickname or s['player'].login, s['profit']
				) for s in top_winners
			))
		else:
			payout_summary = '$f00Nobody bet on them -- no winner this time.'

		# Players first, stakes only as a parenthesis and only when the two differ. "3 bets" alone was
		# read as three people every time one player had reinforced.
		await self.instance.chat(
			'{}$ff0{} won {}! $fff{}$ff0 planets from $fff{}$ff0{}. {}'.format(
				CHAT_PREFIX, winner_label, period_label, total_pot, plural(bettor_count, 'player'),
				' $aaa({} bets)$ff0'.format(stake_count) if stake_count != bettor_count else '',
				payout_summary
			)
		)

		if cards:
			# Round-scoped modes can resolve several periods between two maps; only the newest card is
			# worth replaying, so retire whatever is still hanging around from the previous one.
			await self._discard_result_view()

			view = BetResultView(self, cards)
			self.result_view = view
			# Fire and forget: show_for() sleeps for the lifetime of the card, and resolution must not
			# block on it -- the next map is already starting.
			self.result_view_task = asyncio.ensure_future(view.show_for(RESULT_POPUP_SECONDS))

		await self._refresh_ui()

	async def _record_race_results(self, last_scores):
		"""
		Write the finishing order of the map that just ended to RaceResult, one row per driver.

		Nothing in the plugin reads this table yet -- see the model's docstring. It is being filled now
		because the history cannot be back-filled: a race that went unrecorded is gone, and the odds of
		docs/conception-paris-position.md need roughly thirty of them before they mean anything.
		"""
		if not await self.setting_record_results.get_value():
			return

		map = self.instance.map_manager.current_map
		if map is None:
			return
		if self.results_recorded_for == map.id:
			return

		ranked = self.rank_players(last_scores, by_map_points=self.scope == Bet.SCOPE_ROUND)
		if len(ranked) < 2:
			# A driver alone on the server finishes first every time, which says nothing about how good
			# they are -- and the normalised score the strength model uses divides by field_size - 1.
			# Not a race, so not a result.
			return

		# Set before the insert, not after: `await` yields, and the EndMatch payload arriving in the
		# meantime would find the guard still clear and record the same race a second time.
		self.results_recorded_for = map.id

		now = datetime.datetime.now()
		rows = [
			dict(
				map=map.id,
				player=entry['player'].id,
				position=position,
				field_size=len(ranked),
				points=entry.get('map_points'),
				time=entry['best_race_time'] if (entry.get('best_race_time') or 0) > 0 else None,
				# insert_many bypasses TimedModel.create(), which is what normally stamps these.
				created_at=now,
				updated_at=now,
			)
			for position, entry in enumerate(ranked, start=1)
		]
		await RaceResult.execute(RaceResult.insert_many(rows))
		logger.info(
			'BetPluggin: recorded the finishing order of %d driver(s) on %s.', len(rows), map.name
		)

	@classmethod
	def rank_players(cls, last_scores, by_map_points=False):
		"""
		The whole finishing order, best first -- the same question determine_winner_login and
		determine_map_winner_login answer for the top spot only, decided on the same two rules.

		Drivers with nothing to show for the map are left out rather than ranked last. A spectator and
		someone who disconnected on the first lap did not come last, they did not race, and counting
		them would inflate every real driver's position and the field size along with it.
		"""
		if not last_scores:
			return []

		def driven_time(entry):
			value = entry.get('best_race_time') or 0
			return value if value > 0 else None

		players = [p for p in last_scores.get('players', []) if p.get('player')]

		if by_map_points:
			ranked = [p for p in players if (p.get('map_points') or 0) > 0 or driven_time(p)]
			# Points first, driven time as the tie-break: determine_map_winner_login's ordering exactly,
			# so the driver this puts first is always the one the market just paid out on.
			ranked.sort(key=lambda p: (-(p.get('map_points') or 0), driven_time(p) or 9999999))
		else:
			ranked = [p for p in players if driven_time(p)]
			ranked.sort(key=driven_time)

		return ranked

	@classmethod
	def determine_map_winner_login(cls, last_scores):
		"""
		Who won a map that was played as several rounds: whoever holds the most points at the end.

		Not the same question as determine_winner_login. The `winner_player` the callback reports is the
		driver who crossed the line first in the round that just finished, which on a Rounds map is
		routinely someone other than the player who actually won it.
		"""
		if not last_scores:
			return None

		ranked = sorted(
			(p for p in last_scores.get('players', []) if p.get('player')),
			# Points first, then the driven time as the tie-break. Not `rank`: the mode ranks every
			# connected player whether they drove or not, so it cannot separate two drivers on equal
			# points -- it would just prefer whichever of them the server listed first.
			key=lambda p: (
				-(p.get('map_points') or 0),
				p['best_race_time'] if (p.get('best_race_time') or 0) > 0 else 9999999,
			)
		)
		if not ranked:
			return None

		if (ranked[0].get('map_points') or 0) <= 0:
			# Nobody scored a single point: the mode awards none, or the map was abandoned before any
			# round completed. Crowning the top of an all-zero table would be arbitrary, so fall back to
			# the generic "who finished first" rule and let it refund if even that can't be answered.
			logger.info(
				'BetPluggin: no map points on the board at map end (section %r) -- falling back to the '
				'finish-order winner.', last_scores.get('section')
			)
			return cls.determine_winner_login(last_scores)

		return ranked[0]['player'].login

	@staticmethod
	def determine_winner_login(last_scores):
		if not last_scores:
			return None

		# Only drivers with a time on the board. `rank` is not the test it looks like: the mode ranks
		# every connected player, so on a map nobody finishes they all come back ranked 1, 2, 3 and the
		# first of them was being paid out as the winner of a race that had no winner. A driven time is
		# the only thing that proves a result, so the ordering is done on the time itself.
		finished = sorted(
			(
				p for p in last_scores.get('players', [])
				if p.get('player') and p.get('best_race_time') is not None and p['best_race_time'] > 0
			),
			key=lambda p: p['best_race_time']
		)
		if not finished:
			# Nobody drove a time. Callers refund on None, which is the right answer here.
			return None

		# The mode's own winner, but only once we know somebody actually finished -- it is reported even
		# on a map that ended in a draw.
		if last_scores.get('winner_player'):
			return last_scores['winner_player']

		return finished[0]['player'].login

	async def player_connect(self, player, is_spectator, source, signal, **kwargs):
		if self.widget:
			await self.widget.display(player=player)

	# ------------------------------------------------------------------
	# UI refresh
	# ------------------------------------------------------------------

	async def _refresh_ui(self):
		"""
		Push the current market state to everything that displays it: the always-on widget, plus any
		market window still open. Without the second half, a bet that confirms asynchronously (the
		player clicks the quick-bet button, then confirms the payment popup seconds later) updates the
		widget but leaves the market window they're actually reading frozen on stale numbers.
		"""
		if self.widget:
			await self.widget.display()

		for login, view in list(self.market_views.items()):
			player = self.get_online_player(login)
			if not player:
				self.market_views.pop(login, None)
				continue
			try:
				await view.refresh(player)
			except Exception:
				# A window that can no longer be refreshed (player left mid-refresh, view torn down) must
				# not take the rest of the resolution down with it.
				self.market_views.pop(login, None)

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
		Send `amount` planets from the server's own balance to `player`.

		:return: (bill_id, remaining_server_planets). bill_id is None when nothing was sent. The balance
			is tracked locally so callers resolving several payouts in a row don't have to re-fetch it
			every time.
		"""
		# Pay deducts a small fee on top of the nominal amount -- mirrors the reserve check used by
		# other PyPlanet apps that pay out planets (e.g. the official `transactions` app).
		reserve = 2 + math.floor(amount * 0.05)
		if server_planets < (amount + reserve):
			await self._safe_chat(
				'{}$f00The server doesn\'t have enough planets to pay your {} planet payout right now! '
				'Contact an admin.'.format(CHAT_PREFIX, amount),
				player
			)
			return None, server_planets

		try:
			bill_id = await self.instance.gbx('Pay', player.login, amount, 'BetPluggin payout!')
			self.pending_payouts[bill_id] = dict(player=player, amount=amount, kind='payout')
			return bill_id, server_planets - (amount + reserve)
		except Exception:
			await self._safe_chat(
				'{}$f00Your payout of {} planets failed to send! Contact an admin.'.format(CHAT_PREFIX, amount),
				player
			)
			return None, server_planets

	async def _refund(self, player, amount, reason):
		if amount <= 0:
			return
		try:
			bill_id = await self.instance.gbx('Pay', player.login, amount, 'BetPluggin refund: {}'.format(reason))
			self.pending_payouts[bill_id] = dict(player=player, amount=amount, kind='refund')
		except Exception:
			await self._safe_chat(
				'{}$f00Failed to refund your {} planets automatically! Contact an admin.'.format(
					CHAT_PREFIX, amount
				),
				player
			)
			return
		await self._safe_chat(
			'{}$ff0{} Refunding your $fff{}$ff0 planets.'.format(CHAT_PREFIX, reason, amount), player
		)

	async def on_bill_updated(self, bill_id, state, state_name, transaction_id, **kwargs):
		if bill_id in self.pending_stakes:
			await self._handle_stake_bill(bill_id, state)
		elif self.duels.handles_bill(bill_id):
			# Duel stakes are tracked in their own dict rather than pending_stakes: the market's settlement
			# checks the market's own period and would throw away a duel stake for reasons that have
			# nothing to do with the duel.
			await self.duels.on_bill(bill_id, state)
		elif bill_id in self.pending_payouts:
			await self._handle_payout_bill(bill_id, state)

	async def _handle_stake_bill(self, bill_id, state):
		# Only settle on a terminal state. A single bill reports 1 CreatingTransaction, 2 Issued,
		# 3 ValidatingPayement and finally 4 Payed -- popping on the first of those threw the stake away
		# before "Payed" ever arrived, so the player's planets left their account while the bet stayed
		# stuck in PENDING forever: never activated, never refunded, invisible in every UI.
		if state not in BILL_STATES_TERMINAL:
			return

		async with self.lock:
			pending = self.pending_stakes.pop(bill_id, None)

		if not pending:
			return

		if state == BILL_STATE_VALIDATED:
			current_map_id = self.instance.map_manager.current_map.get_id()
			still_valid = (
				(self.market_is_open or self._within_stake_grace())
				and pending['bet'].map_id == current_map_id
				and (self.scope != Bet.SCOPE_ROUND or pending['round_number'] == self.current_round_number)
			)

			if still_valid:
				async with self.lock:
					self.current_bets.append(pending)
				pending['bet'].state = Bet.STATE_ACTIVE
				await pending['bet'].save()
				# Recomputed, never pending['odds']: that figure was quoted before the payment popup was
				# even confirmed, and every bet placed in the meantime has moved the pool since.
				await self._safe_chat(
					'{}$0f0Bet confirmed: $fff{}$0f0 planets on $fff{}$0f0. Multiplier now $fffx{}$0f0 '
					'$aaa(it changes every time someone bets).'.format(
						CHAT_PREFIX, pending['amount'], self.display_name(pending['target_login']),
						self.format_odds(self.get_odds(pending['target_login']))
					),
					pending['player']
				)

				# ...and tell the whole server. A betting market only comes alive if people can see it
				# moving: knowing that someone just put 500 planets on a driver is what makes anyone
				# want to answer it. It also makes the multiplier shifts explainable instead of random.
				# `$z$s` after each nickname closes whatever formatting it left open.
				pot = sum(b['amount'] for b in self.current_bets)
				await self.instance.chat(
					'{}$fff{}$z$s$ff0 bets $fff{}$ff0 planets on $fff{}$z$s$ff0 $aaa(pot: {} planets '
					'over {} bet(s))'.format(
						CHAT_PREFIX,
						pending['player'].nickname or pending['player'].login,
						pending['amount'],
						self.display_name(pending['target_login']),
						pot, len(self.current_bets)
					)
				)
				await self._refresh_ui()
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
				'{}$f00Your bet on $fff{}$f00 was not confirmed ({}).'.format(
					CHAT_PREFIX, self.display_name(pending['target_login']), reason
				),
				pending['player']
			)
			await self._refresh_ui()

	def _within_stake_grace(self):
		"""
		True for a short moment after the betting window shuts on a pool that is still alive.

		The payment popup is confirmed by hand, so a player who clicks "Bet" a second before the window
		closes confirms it a second after. Refusing those refunds a stake that was placed in good time,
		and in-game that is indistinguishable from the bet being silently rejected -- which is exactly
		how it looked at every warmup end. Deliberately short: it is there to cover the popup, not to
		let anyone confirm once they have seen how the race is going.

		Cleared by _resolve_market, so this can never let a stake into a pool that has been paid out.
		"""
		if self.market_closed_at is None:
			return False
		return (time.time() - self.market_closed_at) <= STAKE_CONFIRM_GRACE_SECONDS

	async def _handle_payout_bill(self, bill_id, state):
		# Same rule as _handle_stake_bill: transitional states must not consume the entry, or the
		# "you received X planets" / "your payout failed" message never fires.
		if state not in BILL_STATES_TERMINAL:
			return

		async with self.lock:
			pending = self.pending_payouts.pop(bill_id, None)

		if not pending:
			return

		# A payout and a refund travel down the exact same Pay/bill_updated path, so without this the
		# player got one identical "you received X planets" line for two opposite events -- winning, and
		# having their bet cancelled. Old entries recovered without a kind default to the neutral wording.
		is_refund = pending.get('kind') == 'refund'
		label = 'refund' if is_refund else 'winnings'

		if state == BILL_STATE_VALIDATED:
			await self._safe_chat(
				'{}$0f0{} of $fff{}$0f0 planets received.'.format(
					CHAT_PREFIX, 'Refund' if is_refund else 'Winnings', pending['amount']
				),
				pending['player']
			)
		elif state in (BILL_STATE_REFUSED, BILL_STATE_ERROR):
			await self._safe_chat(
				'{}$f00Your {} of $fff{}$f00 planets failed to arrive! Contact an admin.'.format(
					CHAT_PREFIX, label, pending['amount']
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

	def get_projected_odds(self, target_login, amount):
		"""
		The odds a stake of `amount` on `target_login` would face *once it has joined the pool*.

		get_odds() answers "what are the odds right now", which is the wrong question when quoting a bet
		to the player placing it: their own stake is about to move those odds, and on the first bet of a
		period the pool is still empty, so get_odds() returns None and the player was told "odds x-".
		Counting the incoming stake always yields a real number -- and on that first bet it honestly
		reads x1.00, i.e. "alone in the pool you can only get your own stake back".
		"""
		total_pot = sum(bet['amount'] for bet in self.current_bets) + amount
		target_pot = sum(
			bet['amount'] for bet in self.current_bets if bet['target_login'].lower() == target_login.lower()
		) + amount
		if target_pot <= 0:
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

	def display_name(self, login):
		"""
		The nickname to show for a login, falling back to the login itself once they disconnect.

		Results are announced at the podium, by which point the player everyone bet on may already have
		left -- and a raw login in the middle of a results line is unreadable next to styled nicknames.
		"""
		player = self.get_online_player(login)
		return (player.nickname if player else None) or login

	def pending_stakes_this_period(self):
		"""
		Stakes still awaiting payment confirmation that belong to the period currently open.

		Entries are deliberately left in pending_stakes once their period ends, so a payment that
		confirms late is still recognised and refunded instead of vanishing. But those stale entries
		must not keep counting as "you already have a bet on them": a player who once ignored a payment
		popup would otherwise be blocked from ever betting on that target again for the life of the
		process, and would keep seeing a ghost bet in the widget.
		"""
		current_map = self.instance.map_manager.current_map
		current_map_id = current_map.get_id() if current_map else None
		return [
			entry for entry in self.pending_stakes.values()
			if entry['bet'].map_id == current_map_id
			and (self.scope != Bet.SCOPE_ROUND or entry['round_number'] == self.current_round_number)
		]

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
			# One driver per period, up to MAX_BETS_PER_PERIOD stakes on them, capped at max_stake in
			# total. Betting on everyone used to be legal, and in a pari-mutuel pool that's a near
			# guaranteed break-even: no risk, no choice, no interest. Committing to a single driver is
			# what makes the bet a bet. The repeats exist so a player can reinforce their pick as the
			# odds move, not so they can stake three times the maximum -- hence the cumulative cap.
			#
			# Only stakes from the period still open count -- see pending_stakes_this_period. Logins are
			# compared case-insensitively on both sides: the bettor login can reach us from the UI with
			# different casing than the Player record.
			mine = [
				b for b in (self.current_bets + self.pending_stakes_this_period())
				if b['player'].login.lower() == bettor.login.lower()
			]

			other = next(
				(b for b in mine if b['target_login'].lower() != target.login.lower()), None
			)
			if other:
				other_name = self.get_online_player(other['target_login'])
				return False, 'You are already backing {} this period -- one driver at a time.'.format(
					(other_name.nickname if other_name else None) or other['target_login']
				)

			if len(mine) >= self.MAX_BETS_PER_PERIOD:
				return False, 'You already placed your {} bets on {} for this period.'.format(
					self.MAX_BETS_PER_PERIOD, target.nickname or target.login
				)

			staked = sum(b['amount'] for b in mine)
			if staked + amount > max_stake:
				return False, 'You already have {} planets on them; {} more would pass the {} maximum.'.format(
					staked, amount, max_stake
				)

			# Projected, not current: the stake being placed is part of the pool it will be paid out of.
			odds = self.get_projected_odds(target.login, amount)

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

			# Register the pending stake *before* the save: `await bet.save()` yields to the event loop,
			# and the bill's updates are already on their way back from the server. If "Payed" is handled
			# while the dict entry doesn't exist yet, the confirmation is dropped and the bet stays
			# PENDING forever with the planets already gone.
			self.pending_stakes[bill_id] = dict(
				bet=bet, player=bettor, target_login=target.login, amount=amount, odds=odds,
				round_number=self.current_round_number,
			)

			bet.stake_bill_id = bill_id
			await bet.save()

		return True, (
			'Confirm the payment popup in your game client to lock in {} planets on {} '
			'(multiplier x{} for now -- it changes when others bet).'.format(
				amount, target.nickname or target.login, self.format_odds(odds)
			)
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
				best_win=0, best_odds=0, streak=0,
			))
			entry['bets'] += 1
			entry['wagered'] += bet.amount

			if bet.won:
				payout = bet.payout or 0
				entry['wins'] += 1
				entry['won'] += payout
				entry['best_win'] = max(entry['best_win'], payout)
				# The *effective* multiplier this stake returned, not the odds quoted when it was placed.
				# It's the number that makes an upset an upset: backing the player nobody else believed in.
				entry['best_odds'] = max(entry['best_odds'], (payout / bet.amount) if bet.amount else 0)
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
	# Market memory: what the server has learnt about each player as a *target*
	# ------------------------------------------------------------------

	async def get_target_stats(self):
		"""
		Per-player statistics from the other side of the bet: not "how well do you bet", but "how well do
		people who bet on *you* do".

		Everything here is derived from the resolved bets already in the database -- no extra table, no
		schema change. `target_login` is the grouping key rather than a Player row, because it is what a
		bet actually stores and it keeps working for people who have since left the server.

		Sorted by how often the player has been backed: a target with two bets on record is noise, and the
		list should open on the players the market actually has an opinion about.
		"""
		rows = await Bet.execute(
			Bet.select(Bet, Player).join(Player).where(Bet.state == Bet.STATE_RESOLVED).order_by(Bet.id)
		)

		def new_entry(login):
			return dict(
				login=login, backed=0, wins=0, staked=0, returned=0,
				odds_sum=0.0, odds_count=0, title='',
				duels=0, duel_wins=0, duel_losses=0, duel_draws=0, duel_net=0,
			)

		stats = {}
		for bet in rows:
			key = bet.target_login.lower()

			# Duel record. A duellist's own stake (TYPE_DUEL) is the one row per duel that names its
			# outcome, so it is the only thing counted here -- spectator bets on the same duel would
			# otherwise multiply one result by however many people had an opinion about it. Draws are
			# counted too, unlike the backing stats below, because "never lost a duel" is only worth
			# anything if the maps that ended level are on the same board.
			if bet.bet_type == Bet.TYPE_DUEL:
				entry = stats.setdefault(key, new_entry(bet.target_login))
				entry['duels'] += 1
				if bet.won:
					entry['duel_wins'] += 1
				elif bet.won is None:
					entry['duel_draws'] += 1
				else:
					entry['duel_losses'] += 1
				entry['duel_net'] += (bet.payout or 0) - bet.amount

			if bet.won is None:
				# Refunded period -- the target neither won nor lost anyone their money.
				continue

			entry = stats.setdefault(key, new_entry(bet.target_login))
			entry['backed'] += 1
			entry['staked'] += bet.amount
			entry['returned'] += (bet.payout or 0)
			if bet.odds:
				entry['odds_sum'] += bet.odds
				entry['odds_count'] += 1
			if bet.won:
				entry['wins'] += 1

		targets = list(stats.values())

		# display_name() only knows about players currently online, and this board spans the whole
		# server history -- most targets have long since disconnected, which was showing their raw
		# login instead of their (coloured) nickname. Batch-fetch the persisted nickname for whoever
		# isn't online right now so the table still reads like a nickname list.
		offline_logins = [e['login'] for e in targets if not self.get_online_player(e['login'])]
		last_known = {}
		if offline_logins:
			known_players = await Player.execute(
				Player.select(Player.login, Player.nickname).where(Player.login.in_(offline_logins))
			)
			last_known = {p.login.lower(): p.nickname for p in known_players}

		for entry in targets:
			entry['nickname'] = (
				self.display_name(entry['login'])
				if self.get_online_player(entry['login'])
				else last_known.get(entry['login'].lower(), entry['login'])
			)
			entry['win_rate'] = round((entry['wins'] / entry['backed']) * 100, 1) if entry['backed'] else 0
			entry['avg_odds'] = round(entry['odds_sum'] / entry['odds_count'], 2) if entry['odds_count'] else None
			# Draws are in the denominator on purpose: a duel you did not win is a duel you did not win,
			# and letting people quietly drop them would make a 1-win 9-draw record read as 100%.
			entry['duel_win_rate'] = (
				round((entry['duel_wins'] / entry['duels']) * 100, 1) if entry['duels'] else 0
			)
			# What backing this player has been worth overall. Negative means the crowd lost money on them.
			entry['net'] = entry['returned'] - entry['staked']

		targets.sort(key=lambda e: (e['backed'], e['staked']), reverse=True)
		self._assign_titles(targets)
		return targets

	async def get_duel_stats(self):
		"""
		The duel record board: everyone who has ever finished a duel, best record first.

		Derived from get_target_stats() rather than from its own query, because the counting rule for
		"is this row a duel result" is subtle enough (one row per duel, the duellist's own stake, draws
		included) that having it written twice is how the two boards start disagreeing.

		Sorted on wins first and win rate second, not on planets. This is the board a player points at,
		and a record should be beaten by winning more duels -- not by winning the same number for higher
		stakes, which would just mean the richest player is permanently on top.
		"""
		duellists = [e for e in await self.get_target_stats() if e['duels'] > 0]
		duellists.sort(key=lambda e: (e['duel_wins'], e['duel_win_rate'], e['duel_net']), reverse=True)
		return duellists

	# A title is only awarded once a player has been backed this many times. Below that the numbers say
	# more about who happened to be online than about the player, and handing out "Underdog" on a single
	# lucky bet makes the whole board meaningless.
	TITLE_MIN_BACKED = 3

	@classmethod
	def _assign_titles(cls, targets):
		"""
		Hand out at most one badge per player, from a fixed priority order so nobody collects two.

		Mutates `targets` in place (each entry's 'title'). Pure presentation -- nothing here is persisted.
		"""
		eligible = [e for e in targets if e['backed'] >= cls.TITLE_MIN_BACKED]
		if not eligible:
			return

		taken = set()

		def award(entry, title):
			if entry and entry['login'] not in taken:
				entry['title'] = title
				taken.add(entry['login'])

		# Most backed of all. The name the crowd reaches for by reflex.
		award(max(eligible, key=lambda e: e['backed']), '$ff0Crowd favourite')
		# Wins despite the market pricing them long: the payout everyone regrets not taking.
		longshots = [e for e in eligible if e['avg_odds'] and e['avg_odds'] >= 2.0 and e['wins'] > 0]
		if longshots:
			award(max(longshots, key=lambda e: e['avg_odds']), '$0f0Surprise winner')
		# Heavily backed and still loses -- the single most expensive habit on this server.
		traps = [e for e in eligible if e['win_rate'] < 25]
		if traps:
			award(max(traps, key=lambda e: e['staked']), '$f00Costly pick')
		# Whoever has returned the most planets to the people who believed in them.
		printers = [e for e in eligible if e['net'] > 0]
		if printers:
			award(max(printers, key=lambda e: e['net']), '$0ffPays well')
		# Never lost a bet for anyone. Rarer than it sounds once TITLE_MIN_BACKED is met.
		flawless = [e for e in eligible if e['win_rate'] == 100]
		if flawless:
			award(max(flawless, key=lambda e: e['backed']), '$fffNever loses')

	async def get_hall_of_fame(self):
		"""
		The three single best results the server has ever seen, for the /wallet footer.

		Returns a dict of optional entries -- each None until a bet of that kind has actually resolved,
		so a fresh database simply shows nothing rather than a row of zeroes.
		"""
		leaderboard = await self.get_leaderboard(limit=None)
		if not leaderboard:
			return dict(upset=None, biggest_win=None, best_streak=None)

		upset = max(leaderboard, key=lambda e: e['best_odds'])
		biggest = max(leaderboard, key=lambda e: e['best_win'])
		streak = max(leaderboard, key=lambda e: e['streak'])

		return dict(
			upset=upset if upset['best_odds'] > 0 else None,
			biggest_win=biggest if biggest['best_win'] > 0 else None,
			best_streak=streak if streak['streak'] > 0 else None,
		)

	# ------------------------------------------------------------------
	# Chat commands
	# ------------------------------------------------------------------

	async def chat_bet(self, player, data, **kwargs):
		if not data.login or not data.amount:
			others = [p.login for p in self.instance.player_manager.online if p.login != player.login]
			example = others[0] if others else 'PlayerName'
			await self.instance.chat(
				'$i$f00Usage: $fff/bet <login> <amount>$f00 -- e.g. $fff/bet {} 50$f00 to wager 50 planets. '
				'Or open $fff/betmarket$f00 for a list of players and quick-bet buttons.'.format(example),
				player
			)
			return

		ok, message = await self.place_bet(player, data.login, data.amount)
		color = '$ff0' if ok else '$f00'
		await self.instance.chat('{}{}'.format(color, message), player)

	async def chat_duel(self, player, data, **kwargs):
		# "/duel list" as well as "/duels", because the board is the kind of thing you go looking for
		# from the command you already know rather than from one you have to be told about.
		if data.login and data.login.lower() in ('list', 'top', 'board', 'classement'):
			await self.chat_duel_board(player)
			return

		if not data.login or not data.amount:
			others = [p.login for p in self.instance.player_manager.online if p.login != player.login]
			example = others[0] if others else 'PlayerName'
			await self.instance.chat(
				'$i$f00Usage: $fff/duel <login> <amount>$f00 -- e.g. $fff/duel {} 500$f00. They can refuse, '
				'or take it for more or less than you put up. Whoever finishes ahead takes the lot.'.format(example),
				player
			)
			return

		await self.duels.challenge(player, data.login, data.amount)

	async def chat_duel_accept(self, player, data, **kwargs):
		duel = self.duels.duel
		if not duel:
			await self.instance.chat('$f00Nobody has challenged you.', player)
			return

		# No amount given means "same as yours", which is the answer most people mean and the one they
		# will type fastest -- and speed matters, there is a countdown running.
		amount = data.amount if data.amount else duel['challenger_amount']
		await self.duels.accept(player, amount)

	async def chat_duel_decline(self, player, **kwargs):
		if not self.duels.duel:
			await self.instance.chat('$f00Nobody has challenged you.', player)
			return
		await self.duels.decline(player)

	async def chat_duel_bet(self, player, data, **kwargs):
		if not self.duels.is_active:
			await self.instance.chat('$f00There is no duel running right now. Start one with $fff/duel$f00.', player)
			return

		duel = self.duels.duel
		if not data.login or not data.amount:
			await self.instance.chat(
				'$i$f00Usage: $fff/duelbet <login> <amount>$f00 -- back $fff{}$f00 or $fff{}$f00.'.format(
					duel['challenger'].login, duel['opponent'].login
				),
				player
			)
			return

		await self.duels.back_side(player, data.login, data.amount)

	async def chat_open_market(self, player, **kwargs):
		if not self.market_is_open:
			if self.closed_for_reboot:
				status = 'closed until the next map (PyPlanet just restarted).'
			elif not self.market_open:
				status = 'closed — no map/round running yet.'
			else:
				status = 'closed by an admin.'
			await self.instance.chat(
				'$f00Betting is {}$f00 Opening the window anyway so you can look around.'.format(status),
				player
			)
		view = BetMarketView(self)
		await view.display(player=player)

	async def chat_my_bets(self, player, **kwargs):
		login = player.login.lower()
		active = [b for b in self.current_bets if b['player'].login.lower() == login]
		# Stakes from earlier periods stay in pending_stakes so a late confirmation is still refunded,
		# but they're not this period's bets and must not be reported as such.
		pending = [b for b in self.pending_stakes_this_period() if b['player'].login.lower() == login]

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
			'{}$ff0Your stats: $fff{}$ff0 bets · $fff{}%$ff0 wins · $fff{}$ff0 planets bet · '
			'profit $fff{:+d}$ff0 · best win $fff{}$ff0 · best multiplier $fffx{}$ff0 · in a row $fff{}$ff0.'.format(
				CHAT_PREFIX, stats['bets'], stats['win_rate'], stats['wagered'], stats['net'],
				stats['best_win'], self.format_odds(stats['best_odds']),
				self.format_streak(stats['streak'])
			),
			player
		)

		# Server records, folded into the command the player already runs. Deliberately not broadcast:
		# these are fun to look up, not worth pushing into everyone's chat on every resolution.
		records = await self.get_hall_of_fame()
		lines = []
		if records['upset']:
			lines.append('best multiplier $fffx{}$ff0 by $fff{}$ff0'.format(
				self.format_odds(records['upset']['best_odds']),
				records['upset']['nickname'] or records['upset']['login']
			))
		if records['biggest_win']:
			lines.append('biggest win $fff{}$ff0 by $fff{}$ff0'.format(
				records['biggest_win']['best_win'],
				records['biggest_win']['nickname'] or records['biggest_win']['login']
			))
		if records['best_streak']:
			lines.append('most wins in a row $fff{}$ff0 by $fff{}$ff0'.format(
				self.format_streak(records['best_streak']['streak']),
				records['best_streak']['nickname'] or records['best_streak']['login']
			))
		if lines:
			await self.instance.chat(
				'{}$ff0Server records: {}.'.format(CHAT_PREFIX, ' · '.join(lines)), player
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

	async def chat_duel_board(self, player, **kwargs):
		if not await self.get_duel_stats():
			await self.instance.chat(
				'{}$aaaNo duel has been settled yet -- challenge someone with $fff/duel <login> <amount>$aaa '
				'and be the first name on the board.'.format(CHAT_PREFIX),
				player
			)
			return
		view = BetDuelBoardView(self)
		await view.display(player=player)

	async def chat_targets(self, player, **kwargs):
		if not await self.get_target_stats():
			await self.instance.chat(
				'{}$aaaNothing to show yet -- this board fills up as bets resolve.'.format(CHAT_PREFIX),
				player
			)
			return
		view = BetTargetsView(self)
		await view.display(player=player)

	async def chat_admin_open_betting(self, player, **kwargs):
		# Force-open really means force: this is also the way to resume betting after a PyPlanet
		# (re)start, where the market intentionally stays closed for the map already in progress
		# (see on_start).
		async with self.lock:
			await self._open_market()

		await self.instance.chat(
			'{}$ff0Betting has been opened by $fff{}$ff0.'.format(
				CHAT_PREFIX, player.nickname or player.login
			)
		)
		await self._refresh_ui()

	async def chat_admin_close_betting(self, player, **kwargs):
		async with self.lock:
			# The auto-close has nothing left to close, and its "closes in 15 seconds" warning would be
			# a lie by the time it fired.
			self._cancel_market_close()
			self.market_manually_closed = True
			# Same reason as every other close path: a payment popup opened a second ago still deserves
			# to land. See _within_stake_grace.
			self.market_closed_at = time.time()

		await self.instance.chat(
			'{}$ff0Betting has been closed by $fff{}$ff0.'.format(
				CHAT_PREFIX, player.nickname or player.login
			)
		)
		await self._refresh_ui()

	async def chat_help_public(self, player, data=None, **kwargs):
		topic = data.topic if data and hasattr(data, 'topic') and data.topic else None
		if topic and topic.lower() != 'bet':
			await self.instance.chat('$f00Unknown help topic. Use /help bet for betting commands.', player)
			return

		await self.instance.chat('$ff0--- BetPluggin Public Commands ---', player)
		await self.instance.chat('$fff/bet <login> <amount>$ff0 - Bet planets on a player winning the current period.', player)
		await self.instance.chat('$fff/betmarket$ff0 (or /market, /odds) - Open the betting window: live multipliers and one-click bets.', player)
		await self.instance.chat('$fff/mybet$ff0 (or /bets) - Show your active bet(s) for the current period.', player)
		await self.instance.chat('$fff/wallet$ff0 (or /stats, /betstats) - Show your betting history and stats.', player)
		await self.instance.chat('$fff/bettop$ff0 (or /betladder) - Open the all-time betting leaderboard.', player)
		await self.instance.chat('$fff/bettargets$ff0 (or /targets, /cotes) - Who is worth betting on: past wins, usual multiplier and badges.', player)
		await self.instance.chat('$fff/duel <login> <amount>$ff0 - Challenge a player: whoever finishes ahead takes the stake. Also from the $fffDuel$ff0 button in /betmarket.', player)
		await self.instance.chat('$fff/accept <amount>$ff0 - Accept the duel you were challenged to (or use the popup window).', player)
		await self.instance.chat('$fff/decline$ff0 (or /refuse) - Turn down the duel you were challenged to. Nothing is charged.', player)
		await self.instance.chat('$fff/duelbet <login> <amount>$ff0 (or /back) - Back one of the two players in the running duel. One-click buttons appear on the widget while a duel runs.', player)
		await self.instance.chat('$fff/duels$ff0 (or /duel list, /dueltop) - The duel record board: who has won the most duels here.', player)

	async def chat_help_admin(self, player, **kwargs):
		await self.instance.chat('$ff0--- BetPluggin Admin Commands ---', player)
		await self.instance.chat('$fff//bet open$ff0 - Force-open betting for the current period.', player)
		await self.instance.chat('$fff//bet close$ff0 - Close betting for the current period early.', player)
		await self.instance.chat('$fff//bet help$ff0 - Show this admin command list.', player)
		await self.instance.chat('$ff0--- PyPlanet commands betting reacts to on its own ---', player)
		await self.instance.chat('$fff//skip$ff0, $fff//next$ff0, $fff//previous$ff0, $fff//restart$ff0 - Betting is called off and everyone is refunded: the map has no honest winner.', player)
		await self.instance.chat('$fff//extend$ff0 - The betting window is re-measured against the map\'s new length (percentage windows only).', player)
		await self.instance.chat('$fff//pause$ff0 / $fff//unpause$ff0 - The betting countdown is frozen and resumed with it.', player)
		await self.instance.chat('$fff//endwu$ff0 - Already closes betting in round-based modes, as the end of the warmup always does.', player)
		await self.instance.chat('$ff0Player chat votes ($fff/skip$ff0, $fff/restart$ff0, $fff/previous$ff0, $fff/extend$ff0) do the same - but only once the vote actually passes.', player)
