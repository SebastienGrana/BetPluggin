"""
In-game UI for BetPluggin: a persistent HUD widget, a transient per-player result card shown at the
podium, and two interactive list views (the live betting market with quick-bet buttons, and the
all-time player leaderboard).
"""
import asyncio
import time

from pyplanet.views.generics.alert import ask_input
from pyplanet.views.generics.list import ManualListView
from pyplanet.views.generics.widget import WidgetView
from pyplanet.views.template import TemplateView

from .models import Bet


def format_time_left(closes_at):
	"""
	"1m20s" / "45s" for a `time.time()` deadline, or None when there is nothing to count down to.

	Shared by the widget and the market window so the two can never disagree about how long is left.
	"""
	if not closes_at:
		return None
	left = int(closes_at - time.time())
	if left <= 0:
		return None
	return '{}m{:02d}s'.format(left // 60, left % 60) if left >= 60 else '{}s'.format(left)


class BetListStyleMixin:
	"""
	Gives each list window a colour of its own: a pale wash over the whole card, a slightly stronger one
	on the title bar and column headers, and a solid rule under the title in the same hue.

	Three windows that are identically grey are three windows a player has to *read* to know which one is
	in front of them. A colour tells them before they read anything, and it stays legible for the players
	this was written for -- the ones who don't speak much English.

	Kept deliberately pale: this floats over a race, and a saturated panel would fight the game behind it.

	Subclasses set `accent` to one of ACCENTS. The rendering lives in templates/list.xml, BetPluggin's
	copy of PyPlanet's generic list template (see the header comment there for why a copy was needed).
	"""

	# tint   = body wash, header = title bar / column headers, line = the solid rule and the title text,
	# button = the navigation button that leads *to* this window, seen from the other two.
	#
	# The two washes are alpha-blended over the stock grey card and are kept barely-there on purpose (a
	# first pass at roughly double these alphas tinted the whole track behind the window). The button
	# colour is the opposite: fully opaque, because a button whose colour you have to look for is not
	# doing its job.
	ACCENTS = {
		'blue': dict(tint='2F6FE012', header='2F6FE01E', line='9CC8FFDD', button='2F6FE0FF'),
		'green': dict(tint='2FC06A12', header='2FC06A1E', line='A8F0BEDD', button='2E9E58FF'),
		'gold': dict(tint='E0A93012', header='E0A9301E', line='FFDF9CDD', button='C9891AFF'),
	}

	template_name = 'betpluggin/list.xml'
	accent = 'blue'

	# Header shown above the action buttons, in the same strip as the column names. PyPlanet's list only
	# labels `fields`, so a row of buttons arrives with nothing saying what they do -- which pushes you
	# into repeating the verb on every single button ("Bet 50", "Bet 100", ...). Naming the group once
	# here lets each button carry just its amount. None (the default) leaves the strip empty, which is
	# what the windows without buttons want.
	actions_header = None

	async def get_context_data(self):
		context = await super().get_context_data()
		colours = self.ACCENTS[self.accent]
		context.update({
			'accent_tint': colours['tint'],
			'accent_header': colours['header'],
			'accent_line': colours['line'],
			'actions_header': self.actions_header,
		})
		return context


class BetNavMixin:
	"""
	The bar of links to BetPluggin's other windows, shown top-right in every list view.

	Without it each window is a dead end: the only way from the market to the leaderboard was to close
	everything and remember a command. Every entry is a plain button in the list toolbar, so no template
	is duplicated -- ManualListView already renders `get_buttons()` there.

	Subclasses set `nav_key` to their own entry so the bar never offers a link to the window you are
	already looking at.
	"""

	nav_key = None

	async def _nav_to(self, player, factory):
		# Close first: PyPlanet keeps both manialinks alive otherwise, and the one underneath stays
		# clickable through the new window.
		await self.close(player)
		await factory(self.app).display(player=player)

	async def get_buttons(self):
		async def to_market(player, values, view=None, **kwargs):
			await self._nav_to(player, BetMarketView)

		async def to_targets(player, values, view=None, **kwargs):
			await self._nav_to(player, BetTargetsView)

		async def to_leaderboard(player, values, view=None, **kwargs):
			await self._nav_to(player, BetLeaderboardView)

		async def to_wallet(player, values, view=None, **kwargs):
			# Answers in chat, so the window is closed rather than swapped -- otherwise the reply lands
			# behind the list the player is still staring at.
			await self.close(player)
			await self.app.chat_my_stats(player)

		# The colour of each button is the colour of the window it opens (BetListStyleMixin.ACCENTS), so
		# the panel that appears is visibly the one that was clicked. "My stats" answers in chat and has
		# no window of its own, hence the neutral grey.
		accents = BetListStyleMixin.ACCENTS
		entries = [
			('market', 'Market', 20, accents['blue']['button'], to_market),
			('targets', 'Who to bet on', 26, accents['green']['button'], to_targets),
			('leaderboard', 'Leaderboard', 24, accents['gold']['button'], to_leaderboard),
			('wallet', 'My stats', 20, '55555FFF', to_wallet),
		]
		return [
			{'title': title, 'width': width, 'colour': colour, 'action': action}
			for key, title, width, colour, action in entries if key != self.nav_key
		]


class BetWidget(WidgetView):
	# Docked right below the "Best CPs" widget (x=-124.5, y=90, 35x6), in the same column, so it sits
	# flush beside the left-hand PyPlanet column (server info / ladder range / version, then Dedimania).
	widget_x = -124.5
	widget_y = 83.5

	# Where it goes in round-based modes instead: the empty band at the top of the screen, just right of
	# the rounds rankings column and just under the game's own round-scores strip. Picked by hand from a
	# screenshot, so the numbers are exact rather than round:
	#   x=-86.5 is the right edge of PyPlanet's rounds rankings widget (x=-124.5, 38 wide), which in this
	#     mode takes over our normal slot -- so we start exactly where it stops, flush, no overlap.
	#   y=86.5 clears the in-game round-scores strip running along the very top (y=90 down to ~87).
	# The rest of the left side is taken too: live rankings drops to x=-160 / y=12.5 once Dedimania is on.
	widget_x_rounds = -86.5
	widget_y_rounds = 86.5

	z_index = 130
	size_x = 40
	size_y = 15.5

	template_name = 'betpluggin/widget.xml'

	def __init__(self, app):
		super().__init__()
		self.app = app
		self.manager = app.context.ui
		self.id = 'betpluggin__widget'

		self.subscribe('open_market', self.action_open_market)
		self.subscribe('open_targets', self.action_open_targets)
		self.subscribe('open_leaderboard', self.action_open_leaderboard)
		self.subscribe('open_wallet', self.action_open_wallet)

	async def get_context_data(self):
		# Positioned per mode, not once at class level: the scope is only known from map_begin onwards
		# and a server can switch between a Rounds night and a TimeAttack one without a restart. Set
		# before super(), which is what copies the coordinates into the template context.
		if self.app.scope == Bet.SCOPE_ROUND:
			self.widget_x = self.widget_x_rounds
			self.widget_y = self.widget_y_rounds
		else:
			self.widget_x = type(self).widget_x
			self.widget_y = type(self).widget_y

		context = await super().get_context_data()

		total_pot = sum(bet['amount'] for bet in self.app.current_bets)
		bet_count = len(self.app.current_bets)

		if self.app.market_is_open:
			status = 'OPEN'
			status_color = '73FF51FF'
		elif self.app.closed_for_reboot:
			status = 'PAUSED'
			status_color = 'FFB347FF'
		elif self.app.scope == Bet.SCOPE_ROUND and bet_count:
			# Round-based modes take bets during the warmup only, then run the whole map with the pool
			# riding on it. "CLOSED" would read as "nothing is happening" during the part that actually
			# decides the bets, so the widget says so instead.
			status = 'RUNNING'
			status_color = 'FFCC00FF'
		else:
			status = 'CLOSED'
			status_color = 'FF7B7BFF'

		if self.app.scope == Bet.SCOPE_ROUND:
			# The period is the whole map here too -- it is simply opened during the warmup and settled
			# on points once the last round has been played.
			period_label = 'Map (points)'
		else:
			period_label = 'This map'

		# Only while betting is actually open: a countdown next to a CLOSED badge would be asking players
		# to watch a clock that buys them nothing. This is the widget most players will read instead of
		# opening the market window, so it is the countdown that matters most.
		time_left = format_time_left(self.app.market_closes_at) if self.app.market_is_open else None

		context.update({
			'status': status,
			'status_color': status_color,
			'period_label': period_label,
			'time_left': time_left or '',
			'total_pot': total_pot,
			'bet_count': bet_count,
			'bet_word': 'bet' if bet_count == 1 else 'bets',
		})

		return context

	async def get_player_data(self):
		data = await super().get_player_data()

		for player in self.app.instance.player_manager.online:
			login = player.login.lower()
			active = [b for b in self.app.current_bets if b['player'].login.lower() == login]
			# Only this period's pending stakes: older ones linger in pending_stakes on purpose (a late
			# confirmation still has to be refunded) and would otherwise show up here as ghost bets.
			pending = [b for b in self.app.pending_stakes_this_period() if b['player'].login.lower() == login]

			parts = ['{} on {}'.format(b['amount'], b['target_login']) for b in active]
			parts += ['{} on {} (pending)'.format(b['amount'], b['target_login']) for b in pending]

			data[player.login] = {'own_bets': ', '.join(parts) if parts else 'no active bet'}

		return data

	async def action_open_market(self, player, action, values, **kwargs):
		view = BetMarketView(self.app)
		await view.display(player=player)

	async def action_open_targets(self, player, action, values, **kwargs):
		await self.app.chat_targets(player)

	async def action_open_leaderboard(self, player, action, values, **kwargs):
		await self.app.chat_leaderboard(player)

	async def action_open_wallet(self, player, action, values, **kwargs):
		# The only one of the four that answers in chat rather than a window: it is three lines about
		# you, and a whole paged window for three lines would be worse, not better.
		await self.app.chat_my_stats(player)


class BetResultView(TemplateView):
	"""
	The "what did my bet do?" card, shown at the podium to the players who had a bet on the period that
	just resolved -- and only to them.

	It exists because the chat is the worst possible place to deliver this: the podium is exactly when
	the chat fills with records, callvotes and end-of-map spam, and the payout confirmation arrives
	asynchronously seconds later, often once the next map has already loaded. A card that states the
	outcome in one glance, then gets out of the way on its own, is the readable version of the same
	information.

	One instance per resolution, thrown away afterwards -- so it carries a unique (uuid) manialink id
	and never collides with the persistent widget.
	"""

	template_name = 'betpluggin/result.xml'

	def __init__(self, app, results):
		"""
		:param results: dict keyed by player login, each value the context for that player's card
			(headline / winner_line / stake_line / result_line / footer_line / accent).
		"""
		super().__init__()
		self.app = app
		self.manager = app.context.ui
		# Kept separately from self.player_data: TemplateView.display() rebuilds player_data from
		# scratch on every call, so anything assigned here directly would be wiped before rendering.
		self.results = dict(results)

		self.subscribe('close', self.action_close)

	async def get_all_player_data(self, logins):
		return {login: data for login, data in self.results.items() if login in set(logins)}

	async def action_close(self, player, action, values, **kwargs):
		await self.hide(player_logins=[player.login])

	# How often the podium showing re-sends itself. See show_for.
	PODIUM_REFRESH_SECONDS = 2

	async def show_for(self, timeout):
		"""
		Display to every player this card was built for during the podium, then hide it again.

		Re-sent every PODIUM_REFRESH_SECONDS rather than displayed once: the card is built the moment
		the period resolves, which is *before* the podium sequence actually starts, and that sequence
		wipes the custom UI on its way in -- so a single send was reliably swallowed and the card only
		ever became visible on the next map (via replay_for). Re-sending costs one small manialink per
		couple of seconds and survives the wipe whenever it happens.

		Hides rather than destroys: the same card is shown a second time at the start of the next map
		(see replay_for), and destroying it here would drop the manialink from the UI manager for good.
		"""
		logins = list(self.results.keys())
		if not logins:
			return

		remaining = timeout
		while remaining > 0:
			await self.display(player_logins=logins)
			slice_ = min(self.PODIUM_REFRESH_SECONDS, remaining)
			await asyncio.sleep(slice_)
			remaining -= slice_

		await self.hide(player_logins=logins)

	async def replay_for(self, timeout):
		"""
		Show the card once more for `timeout`s at the start of the following map, then throw it away.

		A podium is a bad moment to expect anyone to read carefully: the scoreboard is up, the chat is
		full, and a player who was still loading sees none of it. Repeating the card on the next map
		gives them a quiet second look at what their bet actually did.

		This is a fresh display, not a continuation -- manialinks do not survive a map change.
		"""
		logins = list(self.results.keys())
		if logins:
			await self.display(player_logins=logins)
			await asyncio.sleep(timeout)
		# destroy() hides for everyone *and* drops the manialink from the UI manager's registry. Without
		# it every resolution would leak one more dead manialink for the life of the process.
		await self.destroy()


class BetMarketView(BetListStyleMixin, BetNavMixin, ManualListView):
	nav_key = 'market'
	accent = 'blue'
	title = 'BetPluggin -- current market'
	icon_style = 'Icons128x128_1'
	icon_substyle = 'Statistics'

	# The search box only filters the Player column, and this window lists nothing but the players
	# currently connected -- a list you can already read at a glance. It earns its place on the
	# leaderboard and targets windows, which grow with every player who has ever been bet on; here it
	# just takes the top bar away from the navigation buttons.
	provide_search = False

	fields = [
		{
			'name': 'Player',
			'index': 'nickname',
			'sorting': True,
			'searching': True,
			'width': 28,
		},
		# Column names avoid betting jargon on purpose: most players here do not speak English natively,
		# and "pot", "odds", "stake" or "payout" are words you only know if you already bet. Every header
		# below is meant to be guessable from the words alone.
		{
			'name': 'Planets on them',
			'index': 'pot',
			'sorting': False,
			'searching': False,
			'width': 22,
		},
		{
			'name': 'Multiplier',
			'index': 'odds',
			'sorting': False,
			'searching': False,
			'width': 18,
		},
		# The two history columns below are the whole point of keeping target stats: without them the
		# only thing distinguishing two players in this list is how much other people happened to bet on
		# them a minute ago, which tells you nothing about who actually wins maps.
		{
			'name': 'Past wins',
			'index': 'form',
			'sorting': False,
			'searching': False,
			'width': 18,
		},
		{
			'name': 'Usual multiplier',
			'index': 'avg_odds',
			'sorting': False,
			'searching': False,
			'width': 24,
		},
		{
			'name': 'Your bet',
			'index': 'your_bet',
			'sorting': False,
			'searching': False,
			'width': 18,
		},
		{
			'name': 'If they win',
			'index': 'potential_win',
			'sorting': False,
			'searching': False,
			'width': 20,
		},
	]
	# Column widths above total 148, leaving 70 of the list template's 218-unit row for the bet buttons
	# in get_actions(). They were each two units wider before the "Bet ..." button was added; the room
	# had to come from somewhere, and a column of numbers loses less to a couple of units than a button
	# does to being unreadable.

	def __init__(self, app):
		super().__init__()
		self.app = app
		self.manager = app.context.ui
		self.requesting_login = None

	async def display(self, player=None, **kwargs):
		if player is not None:
			self.requesting_login = player.login
			# Hand the app a handle on this window. Every /betmarket builds a fresh view instance, so
			# without this registry the app can't refresh the window a player is actually reading when
			# their bet confirms asynchronously (they click quick-bet, then confirm the payment popup
			# seconds later) -- the numbers would sit there stale until they reopened it.
			self.app.market_views[player.login] = self
		return await super().display(player=player, **kwargs)

	async def close(self, player, *args, **kwargs):
		# Drop the handle as soon as the window is gone, so _refresh_ui doesn't keep pushing updates
		# into a closed view.
		if self.app.market_views.get(player.login) is self:
			self.app.market_views.pop(player.login, None)
		await super().close(player, *args, **kwargs)

	async def get_title(self):
		# The rule lives in the title because it's the one line of this window nobody can miss, and it
		# is the rule players get wrong: they expect to be able to spread bets over several drivers.
		if not self.app.market_is_open:
			return 'Betting is currently closed'

		# How long is left to bet, when an auto-close is armed. A manialink list is drawn once and never
		# redraws itself, so this is a snapshot at render time -- what keeps it honest is the app's ticker
		# (BetPluggin._tick_market_countdown), which re-renders every open window on a schedule that
		# tightens as the close approaches. Worst case it trails reality by one tick interval.
		left = format_time_left(self.app.market_closes_at)
		countdown = ' -- {} left'.format(left) if left else ''

		return 'Betting is OPEN{} -- pick ONE driver, up to {} bets on them'.format(
			countdown, self.app.MAX_BETS_PER_PERIOD
		)

	async def get_data(self):
		own_bets = []
		if self.requesting_login:
			login = self.requesting_login.lower()
			own_bets = [
				dict(b, pending=False) for b in self.app.current_bets
				if b['player'].login.lower() == login
			] + [
				# This period only -- see BetWidget.get_player_data.
				dict(b, pending=True) for b in self.app.pending_stakes_this_period()
				if b['player'].login.lower() == login
			]

		# Historical record of each player as a bet target, keyed by lowercased login to match the rest
		# of the plugin's login comparisons. Fetched once per render rather than per row.
		history = {entry['login'].lower(): entry for entry in await self.app.get_target_stats()}

		rows = []
		for player in self.app.instance.player_manager.online:
			pot_on_player = sum(
				bet['amount'] for bet in self.app.current_bets
				if bet['target_login'].lower() == player.login.lower()
			)
			odds = self.app.get_odds(player.login)

			own_bet = next(
				(b for b in own_bets if b['target_login'].lower() == player.login.lower()), None
			)
			if own_bet:
				your_bet = '{}{}'.format(own_bet['amount'], ' (pending)' if own_bet['pending'] else '')
				if odds:
					payout = round(own_bet['amount'] * odds)
					profit = payout - own_bet['amount']
					potential_win = '{} (+{})'.format(payout, profit)
				else:
					potential_win = '-'
			else:
				your_bet = '-'
				potential_win = '-'

			past = history.get(player.login.lower())
			if past:
				form = '{}/{} ({}%)'.format(past['wins'], past['backed'], round(past['win_rate']))
				avg_odds = 'x{}'.format(self.app.format_odds(past['avg_odds']))
			else:
				# Never been backed. "new" reads better than a row of dashes and says the useful thing:
				# there is nothing to go on here.
				form = '$aaanew'
				avg_odds = '$aaa-'

			rows.append(dict(
				login=player.login,
				nickname=player.nickname,
				pot=str(pot_on_player),
				odds='x{}'.format(self.app.format_odds(odds)),
				form=form,
				avg_odds=avg_odds,
				your_bet=your_bet,
				potential_win=potential_win,
			))
		return rows

	# Buttons per row: every configured quick amount, then "Bet ...". The setting is free-form -- rather
	# than hardcode a max count and silently drop the rest, the row's button budget (what's left of the
	# 218 units after `fields`, see the note above) is split evenly across however many amounts are
	# configured, so adding a fourth or fifth quick amount just makes each button a bit narrower instead
	# of disappearing.
	BUTTON_BUDGET = 218 - sum(f['width'] for f in fields)
	QUICK_BET_MIN_WIDTH = 11
	CUSTOM_BET_WIDTH = 15

	# Button colours. Open: the green already used for the "Who to bet on" window, plus gold on the
	# free-amount button so it reads as the odd one out rather than a sixth preset. Closed: a flat slate
	# that is still clearly a button-shaped thing, so the row does not look broken -- just inactive.
	QUICK_BET_COLOUR = BetListStyleMixin.ACCENTS['green']['button']
	CUSTOM_BET_COLOUR = BetListStyleMixin.ACCENTS['gold']['button']
	DISABLED_COLOUR = '444444AA'
	DISABLED_TEXT = '$999'

	# Names the button strip in the header row, so the buttons themselves only carry their amount.
	actions_header = 'Bet'

	async def _submit_bet(self, player, target_login, amount, view):
		"""Place a bet and report the outcome in chat. Shared by the quick and custom buttons."""
		try:
			ok, message = await self.app.place_bet(player, target_login, amount)
		except Exception as e:
			await self.app.instance.chat('$i$f00BetPluggin error: {}'.format(e), player)
			raise
		color = '$ff0' if ok else '$i$f00'
		await self.app.instance.chat('{}{}'.format(color, message), player)
		if view is not None:
			await view.refresh(player)

	async def get_actions(self):
		# Buttons stay on the card at all times -- closed or before opening -- rather than vanishing, so
		# the window doesn't reflow and players always know where to click once betting opens. When the
		# market isn't open they're just greyed out and wired to a no-op instead of the real bet action.
		market_open = self.app.market_is_open

		async def closed_action(player, values, instance, view=None, **kwargs):
			return

		amounts = await self.app.get_quick_bet_amounts()
		quick_budget = max(self.BUTTON_BUDGET - self.CUSTOM_BET_WIDTH, 0)
		quick_width = max(quick_budget // len(amounts), self.QUICK_BET_MIN_WIDTH) if amounts else 0

		actions = []
		for amount in amounts:
			async def bet_action(player, values, instance, view=None, amount=amount, **kwargs):
				await self._submit_bet(player, instance['login'], amount, view)

			# Just the number: the "Bet" header above the strip already says what clicking one does, and
			# repeating the verb on every button cost the width that was making them run into each other.
			actions.append({
				'name': 'Bet {}'.format(amount),
				'type': 'label',
				'text': str(amount) if market_open else '{}{}'.format(self.DISABLED_TEXT, amount),
				'bgcolor': self.QUICK_BET_COLOUR if market_open else self.DISABLED_COLOUR,
				'width': quick_width,
				'action': bet_action if market_open else closed_action,
				'safe': True,
			})

		# Free-amount button. The quick amounts cover the common cases, but they are fixed numbers: a
		# player who wants to put their whole balance on someone, or the exact 37 planets they have left,
		# previously had to close the window and type /bet by hand.
		min_stake = await self.app.setting_min_stake.get_value()
		max_stake = await self.app.setting_max_stake.get_value()

		async def custom_bet_action(player, values, instance, view=None, **kwargs):
			answer = await ask_input(
				player,
				'How many planets do you want to bet on $<{}$>?\n(between {} and {})'.format(
					instance['nickname'], min_stake, max_stake
				),
				default=str(min_stake),
			)
			# ask_input returns None when the prompt is dismissed rather than answered.
			if answer is None:
				return
			try:
				amount = int(str(answer).strip())
			except ValueError:
				await self.app.instance.chat(
					'$i$f00That is not a number. Type only digits, for example 250.', player
				)
				return
			await self._submit_bet(player, instance['login'], amount, view)

		actions.append({
			'name': 'Bet a chosen amount',
			'type': 'label',
			'text': 'Other' if market_open else '{}Other'.format(self.DISABLED_TEXT),
			'bgcolor': self.CUSTOM_BET_COLOUR if market_open else self.DISABLED_COLOUR,
			'width': self.CUSTOM_BET_WIDTH,
			'action': custom_bet_action if market_open else closed_action,
			'safe': True,
		})

		return actions


class BetLeaderboardView(BetListStyleMixin, BetNavMixin, ManualListView):
	nav_key = 'leaderboard'
	accent = 'gold'
	title = 'BetPluggin -- leaderboard'
	icon_style = 'Icons128x128_1'
	icon_substyle = 'Statistics'

	# Same rule as the market window: plain words, no betting vocabulary. Widths are tight because the
	# list template only has 218 units to share between fields and the navigation buttons.
	fields = [
		{'name': 'Player',          'index': 'nickname',  'sorting': True,  'searching': True,  'width': 32},
		{'name': 'Bets',            'index': 'bets',      'sorting': False, 'searching': False, 'width': 11},
		{'name': 'Win %',           'index': 'win_rate',  'sorting': False, 'searching': False, 'width': 13},
		{'name': 'Total bet',       'index': 'wagered',   'sorting': False, 'searching': False, 'width': 18},
		{'name': 'Profit',          'index': 'net',       'sorting': False, 'searching': False, 'width': 15},
		{'name': 'Best win',        'index': 'best_win',  'sorting': False, 'searching': False, 'width': 17},
		{'name': 'Best multiplier', 'index': 'best_odds', 'sorting': False, 'searching': False, 'width': 24},
		{'name': 'In a row',        'index': 'streak',    'sorting': False, 'searching': False, 'width': 14},
	]

	def __init__(self, app):
		super().__init__()
		self.app = app
		self.manager = app.context.ui

	async def get_data(self):
		leaderboard = await self.app.get_leaderboard()
		rows = []
		for entry in leaderboard:
			net = entry['net']
			net_str = (
				'$0f0{:+d}$z'.format(net) if net > 0
				else '$f00{:+d}$z'.format(net) if net < 0
				else '{:+d}'.format(net)
			)
			streak = self.app.format_streak(entry['streak'])
			s = entry['streak']
			streak_str = (
				'$0f0{}$z'.format(streak) if s > 0
				else '$f00{}$z'.format(streak) if s < 0
				else streak
			)
			rows.append(dict(
				nickname=entry['nickname'],
				bets=str(entry['bets']),
				win_rate='{}%'.format(entry['win_rate']),
				wagered=str(entry['wagered']),
				net=net_str,
				best_win=str(entry['best_win']),
				# The best multiplier they ever cashed in. Highlighted past x2 because that is the point
				# where they were betting against the room rather than with it.
				best_odds=(
					'$0f0x{}$z'.format(self.app.format_odds(entry['best_odds']))
					if entry['best_odds'] >= 2 else 'x{}'.format(self.app.format_odds(entry['best_odds']))
				),
				streak=streak_str,
			))
		return rows


class BetTargetsView(BetListStyleMixin, BetNavMixin, ManualListView):
	"""
	The market's memory of every player who has ever been bet *on*.

	The leaderboard answers "who bets well"; this answers "who is worth betting on" -- which is the
	question a player actually has in front of them when the market opens. Badges are handed out here
	rather than announced in chat: they are a reason to open the window, not another line scrolling past
	during the podium.
	"""

	nav_key = 'targets'
	accent = 'green'
	title = 'BetPluggin -- who to bet on'
	icon_style = 'Icons128x128_1'
	icon_substyle = 'Statistics'

	fields = [
		{'name': 'Player',           'index': 'nickname', 'sorting': True,  'searching': True,  'width': 28},
		{'name': 'Badge',            'index': 'title',    'sorting': False, 'searching': False, 'width': 26},
		{'name': 'Times bet on',     'index': 'backed',   'sorting': False, 'searching': False, 'width': 20},
		{'name': 'Wins',             'index': 'form',     'sorting': False, 'searching': False, 'width': 16},
		{'name': 'Planets bet',      'index': 'staked',   'sorting': False, 'searching': False, 'width': 18},
		{'name': "Backers' profit",  'index': 'net',      'sorting': False, 'searching': False, 'width': 22},
		{'name': 'Usual multiplier', 'index': 'avg_odds', 'sorting': False, 'searching': False, 'width': 22},
	]

	def __init__(self, app):
		super().__init__()
		self.app = app
		self.manager = app.context.ui

	async def get_data(self):
		rows = []
		for entry in await self.app.get_target_stats():
			net = entry['net']
			net_str = (
				'$0f0{:+d}$z'.format(net) if net > 0
				else '$f00{:+d}$z'.format(net) if net < 0
				else '{:+d}'.format(net)
			)
			rows.append(dict(
				nickname=entry['nickname'],
				title=entry['title'] or '',
				backed=str(entry['backed']),
				form='{}/{} ({}%)'.format(entry['wins'], entry['backed'], round(entry['win_rate'])),
				staked=str(entry['staked']),
				net=net_str,
				avg_odds='x{}'.format(self.app.format_odds(entry['avg_odds'])),
			))
		return rows
