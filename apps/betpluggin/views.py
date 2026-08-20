"""
In-game UI for BetPluggin: a persistent HUD widget, a transient per-player result card shown at the
podium, and two interactive list views (the live betting market with quick-bet buttons, and the
all-time player leaderboard).
"""
import asyncio
import time

from collections import OrderedDict

from pyplanet.views.generics.alert import PromptView
from pyplanet.views.generics.list import ManualListView
from pyplanet.views.generics.widget import WidgetView
from pyplanet.views.template import TemplateView

from .models import Bet


class CancellablePromptView(PromptView):
	"""
	A PromptView whose second button backs out instead of submitting.

	PyPlanet's own PromptView.handle() never looks at *which* button was pressed: whatever you click, it
	validates the input box and returns it. So an amount prompt built on the stock ask_input() has
	exactly one way out -- type a valid number -- and a player who opened it by mistake, or changed
	their mind about the opponent, is stuck staking money to close a window.
	"""

	CANCEL_INDEX = 1

	async def handle(self, player, action, values, **kwargs):
		if action.endswith('button_{}'.format(self.CANCEL_INDEX)):
			await self.close(player)
			if not self.response_future.done():
				self.response_future.set_result(None)
			return
		await super().handle(player, action, values, **kwargs)


async def ask_amount(player, message, default=None, size='md'):
	"""
	Ask for a number with a working Cancel button. Returns None when the player backs out.

	PromptView's sizes are hard-coded and centred, so this box always lands over the driving view. Pass
	size='sm' when the question is asked mid-race: it is the smallest footprint on offer, and the
	player still has Cancel to get the road back in one click.
	"""
	view = CancellablePromptView(message, size, [{'name': 'OK'}, {'name': 'Cancel'}], default=default)
	await view.display(player_logins=[player.login])
	return await view.wait_for_input()


# Handed out by position on the duel board, not by threshold: a title you can only get by being ahead
# of everyone else is a title somebody has to lose for you to take it, which is the whole point.
DUEL_TITLES = {
	1: '$ff0Duel king',
	2: '$dddChallenger',
	3: '$e79Third blood',
}


def format_duel_record(entry):
	"""
	One player's duel record as a single cell: "3 of 5". Empty for anyone who has never duelled.

	Empty rather than "0 of 0", because a column full of zeroes on a board most players are on for
	their betting reads as everyone having lost, which is the opposite of what it says.
	"""
	if not entry.get('duels'):
		return ''
	wins = entry['duel_wins']
	return '{} of {}'.format(
		'$0f0{}$z'.format(wins) if wins else '$aaa0$z', entry['duels']
	)


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
		# The duel orange, the same one the challenge window and the widget's duel band already use, so
		# the record board is visibly part of the duel and not a fifth unrelated statistics window.
		'orange': dict(tint='F08A2E12', header='F08A2E1E', line='FFD2A8DD', button='F08A2EFF'),
		# The one board that measures driving rather than betting, so it gets the one colour that is not
		# a variation on the others.
		'purple': dict(tint='8A5CD812', header='8A5CD81E', line='D9C4FFDD', button='8A5CD8FF'),
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

		async def to_duels(player, values, view=None, **kwargs):
			await self._nav_to(player, BetDuelBoardView)

		async def to_pace(player, values, view=None, **kwargs):
			await self._nav_to(player, BetPaceView)

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
			('duels', 'Duel record', 22, accents['orange']['button'], to_duels),
			('pace', 'Pace', 14, accents['purple']['button'], to_pace),
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
	size_y = 21.8
	# What the widget grows to while a duel is on the table. The band is only worth its screen space for
	# the minute or two a duel lasts, so it is added and removed rather than left empty the rest of the
	# time -- and a duel that appears under the widget you are already watching does not need finding.
	DUEL_BAND_HEIGHT = 14.2

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
		self.subscribe('back_challenger', self.action_back_challenger)
		self.subscribe('back_opponent', self.action_back_opponent)

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

		# Same reasoning as the coordinates: set before super(), which copies the size into the context.
		duel = self.app.duels.duel
		self.size_y = type(self).size_y + (self.DUEL_BAND_HEIGHT if duel else 0)

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

		# Who the room is backing, and at what multiplier. The header used to spend this space repeating
		# the period ("This map"), which never changes and which nobody needed telling. The favourite
		# does change, several times a period, and it is the one number that makes you want to answer it.
		favourite = ''
		if self.app.current_bets:
			pots = {}
			for bet in self.app.current_bets:
				login = bet['target_login'].lower()
				pots[login] = pots.get(login, 0) + bet['amount']
			top_login = max(pots, key=pots.get)
			favourite = '$fff{}$z$s $aaax{}'.format(
				self.app.display_name(top_login), self.app.format_odds(self.app.get_odds(top_login))
			)

		# The countdown is the widget's second headline number, so it always has a value and always has a
		# caption explaining what it is counting -- an empty slot next to a CLOSED badge used to leave the
		# whole right half of the widget looking broken rather than merely idle.
		time_left = format_time_left(self.app.market_closes_at) if self.app.market_is_open else None
		if time_left:
			time_value, time_caption = time_left, 'LEFT TO BET'
		elif self.app.market_is_open:
			# Open with no armed deadline: the modes where betting simply stays open for the period.
			time_value, time_caption = 'OPEN', 'BET ANY TIME'
		elif status == 'RUNNING':
			time_value, time_caption = 'LIVE', 'BETS ARE RIDING'
		elif status == 'PAUSED':
			time_value, time_caption = '--', 'RESTART PENDING'
		else:
			time_value, time_caption = '--', 'BETTING CLOSED'

		# The one button people came for changes its wording with the state instead of always reading
		# "open the market": when the market is open the useful action is placing a bet, and when it is
		# shut the window is still worth opening to read the pool.
		if self.app.market_is_open:
			cta_label = 'PLACE A BET'
			cta_color = '09F4FFE0'
			cta_text_color = '00141AFF'
		else:
			cta_label = 'BETTING MARKET'
			cta_color = 'FFFFFF14'
			cta_text_color = 'BBBBCCFF'

		# The duel band. Everything a spectator needs to answer "who do I put money on" is here, so the
		# only click left is the answer itself -- a duel lasts one map and asking players to open a window
		# to find it is asking most of them not to bother.
		if duel:
			context.update(await self._duel_context(duel))

		context.update({
			'duel_shown': duel is not None,
			'status': status,
			'status_color': status_color,
			'favourite': favourite,
			'time_value': time_value,
			'time_caption': time_caption,
			'total_pot': total_pot,
			'bet_count': bet_count,
			'bet_word': 'bet' if bet_count == 1 else 'bets',
			'cta_label': cta_label,
			'cta_color': cta_color,
			'cta_text_color': cta_text_color,
		})

		return context

	DUEL_LIVE_COLOUR = 'F08A2EE0'
	DUEL_LIVE_TEXT = '140A00FF'
	DUEL_DEAD_COLOUR = 'FFFFFF12'
	DUEL_DEAD_TEXT = '8A8AA8FF'

	async def _duel_side_amount(self):
		"""
		The one-click stake on the duel buttons: the smallest quick-bet amount, floored at the minimum.

		A fixed amount rather than a prompt because this button is pressed mid-race, with a hand on the
		accelerator. Players who want to size their stake still have /duelbet, and can press the button
		more than once.
		"""
		amounts = await self.app.get_quick_bet_amounts()
		minimum = await self.app.setting_min_stake.get_value()
		return max(min(amounts) if amounts else minimum, minimum)

	async def _duel_context(self, duel):
		challenger, opponent = duel['challenger'], duel['opponent']
		pots = self.app.duels.side_pots()
		amount = await self._duel_side_amount()

		# Spectator betting needs an accepted duel *and* the setting: while the challenge is still being
		# answered there are no agreed stakes to bet against, and the whole thing may yet evaporate.
		spectators = await self.app.setting_duel_spectators.get_value()
		live = self.app.duels.is_active and spectators

		if not self.app.duels.is_active:
			caption = 'WAITING FOR AN ANSWER'
		elif not spectators:
			caption = 'SIDE BETS ARE OFF'
		else:
			side_total = sum(pots.values())
			caption = '{} PLANETS ON THE SIDE'.format(side_total) if side_total else 'NOBODY HAS PICKED A SIDE'

		def side(player, stake):
			# Pot and odds share one line rather than sitting beside the name: a Trackmania nickname is
			# arbitrarily long and carries its own colour codes, so anything placed next to it on the same
			# line gets overrun. Giving the name the full column width is the only layout that survives.
			pot = pots.get(player.login.lower(), 0)
			odds = self.app.duels.spectator_odds(player.login)
			if pot:
				market = '{} behind'.format(pot)
				if odds:
					market = 'x{} - {}'.format(self.app.format_odds(odds), market)
			else:
				market = 'nobody behind them yet'
			return {
				'name': player.nickname or player.login,
				'stake': '{} planets'.format(stake) if stake else 'answering...',
				'market': market,
			}

		left = side(challenger, duel['challenger_amount'])
		right = side(opponent, duel['opponent_amount'])

		label = 'BACK FOR {}'.format(amount) if live else ('NOT YET' if not self.app.duels.is_active else 'OFF')
		colour = self.DUEL_LIVE_COLOUR if live else self.DUEL_DEAD_COLOUR
		text_colour = self.DUEL_LIVE_TEXT if live else self.DUEL_DEAD_TEXT

		return {
			'duel_caption': caption,
			'duel_left_name': left['name'], 'duel_left_stake': left['stake'],
			'duel_left_market': left['market'],
			'duel_right_name': right['name'], 'duel_right_stake': right['stake'],
			'duel_right_market': right['market'],
			# Per-player overrides in get_player_data() replace these for anyone who can't take the bet.
			'duel_left_label': label, 'duel_right_label': label,
			'duel_left_color': colour, 'duel_right_color': colour,
			'duel_left_text_color': text_colour, 'duel_right_text_color': text_colour,
		}

	async def get_player_data(self):
		data = await super().get_player_data()

		max_stake = await self.app.setting_max_stake.get_value()

		duel = self.app.duels.duel
		duel_live = duel is not None and self.app.duels.is_active \
			and await self.app.setting_duel_spectators.get_value()

		for player in self.app.instance.player_manager.online:
			login = player.login.lower()
			active = [b for b in self.app.current_bets if b['player'].login.lower() == login]
			# Only this period's pending stakes: older ones linger in pending_stakes on purpose (a late
			# confirmation still has to be refunded) and would otherwise show up here as ghost bets.
			pending = [b for b in self.app.pending_stakes_this_period() if b['player'].login.lower() == login]

			# One line per driver, not per stake. Three stakes on the same driver is the normal way to use
			# this plugin, and spelling them out one by one filled the line with the same nickname three
			# times over -- which reads as three different bets. What a player wants back from this line is
			# who they are on and for how much in total; the number of stakes is a detail, so it is a
			# suffix on the total rather than a repetition of it.
			totals = OrderedDict()
			for bet in active + pending:
				key = bet['target_login'].lower()
				entry = totals.setdefault(key, dict(amount=0, count=0, pending=0))
				entry['amount'] += bet['amount']
				entry['count'] += 1
			for bet in pending:
				totals[bet['target_login'].lower()]['pending'] += 1

			parts = []
			for target_login, entry in totals.items():
				suffix = ' $777({} pending)'.format(entry['pending']) if entry['pending'] else ''
				parts.append('$fff{}$z$s $aaa({}{}){}'.format(
					self.app.display_name(target_login),
					entry['amount'],
					' over {} bets'.format(entry['count']) if entry['count'] > 1 else '',
					suffix,
				))

			used = len(active) + len(pending)
			staked = sum(b['amount'] for b in active + pending)
			left = self.app.MAX_BETS_PER_PERIOD - used

			# The button says what is left to the player reading it, and stops looking clickable once
			# nothing is. Both limits close the same door, so both say so here rather than letting the
			# player find out by clicking through two windows and collecting a refusal.
			player_data = {'own_bets': '$aaa + '.join(parts) if parts else '$666none yet'}
			if self.app.market_is_open and used:
				if left <= 0:
					player_data.update({
						'cta_label': 'ALL {} BETS PLACED'.format(self.app.MAX_BETS_PER_PERIOD),
						'cta_color': 'FFFFFF14', 'cta_text_color': '8A8AA8FF',
					})
				elif staked >= max_stake:
					player_data.update({
						'cta_label': 'MAX STAKE REACHED',
						'cta_color': 'FFFFFF14', 'cta_text_color': '8A8AA8FF',
					})
				else:
					player_data['cta_label'] = 'BET AGAIN ({} LEFT)'.format(left)

			# Duel side buttons, per player. Two people in the room may not take this bet at all -- the
			# duellists, whose stake is already down -- and anyone who has picked a side is held to it.
			# Both are rules back_side() already enforces; drawing them here is what stops a player
			# discovering them by clicking.
			if duel_live:
				backing = next(
					(b['target_login'].lower() for b in duel['spectator_bets']
					 if b['player'].login.lower() == login),
					None
				)
				for key, side_login in (
					('duel_left', duel['challenger'].login.lower()),
					('duel_right', duel['opponent'].login.lower()),
				):
					if self.app.duels.involves(login):
						blocked, label = True, 'YOUR OWN DUEL'
					elif backing == side_login:
						blocked, label = False, 'ADD {}'.format(await self._duel_side_amount())
					elif backing:
						blocked, label = True, 'OTHER SIDE PICKED'
					else:
						continue
					player_data.update({
						key + '_label': label,
						key + '_color': self.DUEL_DEAD_COLOUR if blocked else self.DUEL_LIVE_COLOUR,
						key + '_text_color': self.DUEL_DEAD_TEXT if blocked else self.DUEL_LIVE_TEXT,
					})

			data[player.login] = player_data

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

	async def _back_duel_side(self, player, side):
		# The duel is re-read here rather than trusted from the render: a widget is a picture of a moment
		# that may be several seconds old, and the duel it showed can have been resolved, refunded or
		# replaced since. back_side() refuses with its own sentence when it has, which is what a player
		# who clicked a button that had just gone stale needs to read.
		duel = self.app.duels.duel
		if not duel:
			await self.app._safe_chat(
				'{}$f00That duel is over.'.format(self.app.CHAT_PREFIX), player
			)
			return
		await self.app.duels.back_side(player, duel[side].login, await self._duel_side_amount())

	async def action_back_challenger(self, player, action, values, **kwargs):
		await self._back_duel_side(player, 'challenger')

	async def action_back_opponent(self, player, action, values, **kwargs):
		await self._back_duel_side(player, 'opponent')


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

	def _own_stakes(self):
		"""Every stake the player reading this window holds in the period still open, pending included."""
		if not self.requesting_login:
			return []
		login = self.requesting_login.lower()
		return [
			dict(b, pending=False) for b in self.app.current_bets
			if b['player'].login.lower() == login
		] + [
			# This period only -- see BetWidget.get_player_data.
			dict(b, pending=True) for b in self.app.pending_stakes_this_period()
			if b['player'].login.lower() == login
		]

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

		# Once the player has committed, the generic rule is behind them and the useful thing to say is
		# where they stand under it. The grey buttons below say "you can't"; this says why.
		own_bets = self._own_stakes()
		if own_bets:
			target = self.app.display_name(own_bets[0]['target_login'])
			used = len(own_bets)
			if used >= self.app.MAX_BETS_PER_PERIOD:
				rule = 'all {} bets placed on {}$z$s -- nothing left this period'.format(used, target)
			else:
				rule = 'you are on {}$z$s -- {} of {} bets left on them'.format(
					target, self.app.MAX_BETS_PER_PERIOD - used, self.app.MAX_BETS_PER_PERIOD
				)
		else:
			rule = 'pick ONE driver, up to {} bets on them'.format(self.app.MAX_BETS_PER_PERIOD)

		return 'Betting is OPEN{} -- {}'.format(countdown, rule)

	async def get_data(self):
		own_bets = self._own_stakes()

		# Historical record of each player as a bet target, keyed by lowercased login to match the rest
		# of the plugin's login comparisons. Fetched once per render rather than per row.
		history = {entry['login'].lower(): entry for entry in await self.app.get_target_stats()}

		# What this player is still allowed to do, worked out once for the whole window. The same three
		# limits place_bet() enforces -- one driver per period, MAX_BETS_PER_PERIOD stakes, a cumulative
		# max_stake -- decide which buttons are drawn live and which are drawn dead. The refusal messages
		# were always there; what was missing is that a player had to spend a bet to read one.
		max_stake = await self.app.setting_max_stake.get_value()
		min_stake = await self.app.setting_min_stake.get_value()
		committed_login = own_bets[0]['target_login'].lower() if own_bets else None
		bets_used = len(own_bets)
		staked = sum(b['amount'] for b in own_bets)
		# Room left in planets: what the biggest additional stake could be. Zero means the row is done,
		# whatever buttons it still shows.
		room = max(max_stake - staked, 0)
		if bets_used >= self.app.MAX_BETS_PER_PERIOD or room < min_stake:
			room = 0

		# Same idea as the betting lock above, for the "Duel" button: a duel is a separate offer to one
		# specific player, so none of the market's own-bet rules apply to it. What does: duels can be
		# switched off, the market has to be open to start one, only one duel runs at a time, and you
		# cannot challenge yourself.
		duel_enabled = await self.app.setting_duel_enabled.get_value()
		duel_running = self.app.duels.duel is not None
		duel_blocked = not duel_enabled or not self.app.market_is_open or duel_running

		rows = []
		for player in self.app.instance.player_manager.online:
			pot_on_player = sum(
				bet['amount'] for bet in self.app.current_bets
				if bet['target_login'].lower() == player.login.lower()
			)
			odds = self.app.get_odds(player.login)

			# All of this player's stakes on this driver, added up. One row can hold up to
			# MAX_BETS_PER_PERIOD of them and the column used to show only the first, so a player who had
			# reinforced twice read their own commitment as a third of what it was.
			mine_here = [b for b in own_bets if b['target_login'].lower() == player.login.lower()]
			if mine_here:
				staked_here = sum(b['amount'] for b in mine_here)
				notes = []
				if len(mine_here) > 1:
					notes.append('{} bets'.format(len(mine_here)))
				if any(b['pending'] for b in mine_here):
					notes.append('pending')
				your_bet = '{}{}'.format(
					staked_here, ' ({})'.format(', '.join(notes)) if notes else ''
				)
				if odds:
					payout = round(staked_here * odds)
					profit = payout - staked_here
					potential_win = '{} (+{})'.format(payout, profit)
				else:
					potential_win = '-'
			else:
				your_bet = '-'
				potential_win = '-'

			# Is this row still worth clicking? Closed market, a driver already committed to elsewhere, or
			# no allowance left -- three different sentences from place_bet(), one dead row here. The
			# window's title carries the explanation, so the row itself only has to look inert.
			is_mine = committed_login is not None and player.login.lower() == committed_login
			locked = not self.app.market_is_open or (committed_login is not None and not is_mine) or room <= 0

			past = history.get(player.login.lower())
			if past:
				form = '{}/{} ({}%)'.format(past['wins'], past['backed'], round(past['win_rate']))
				avg_odds = 'x{}'.format(self.app.format_odds(past['avg_odds']))
			else:
				# Never been backed. "new" reads better than a row of dashes and says the useful thing:
				# there is nothing to go on here.
				form = '$aaanew'
				avg_odds = '$aaa-'

			is_self = self.requesting_login is not None and player.login.lower() == self.requesting_login.lower()

			rows.append(dict(
				# Read by list.xml to draw this row's bet buttons dead instead of live. The click still
				# reaches place_bet() -- greying is a hint, not the rule -- so a player who clicks anyway
				# gets the same sentence explaining why, rather than nothing happening.
				_locked=locked,
				_room=room if not locked else 0,
				# Same, for the "Duel" button -- see the note above get_actions().
				_duel_locked=duel_blocked or is_self,
				login=player.login,
				# The driver you are committed to is the one row that still matters once every other row
				# is grey, so it is marked rather than merely left un-greyed.
				nickname='$ff0>$z$s {}'.format(player.nickname) if is_mine else player.nickname,
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
	CUSTOM_BET_WIDTH = 13
	# The duel entry point lives here rather than as a new "who to bet on" column: this row already
	# carries a login and a live bet-button strip, so the only new thing a duel button needs is its own
	# width -- taken from the quick-amount split, same as the custom button was.
	DUEL_WIDTH = 15

	# Button colours. Open: the green already used for the "Who to bet on" window, plus gold on the
	# free-amount button so it reads as the odd one out rather than a sixth preset. Closed: a flat slate
	# that is still clearly a button-shaped thing, so the row does not look broken -- just inactive.
	QUICK_BET_COLOUR = BetListStyleMixin.ACCENTS['green']['button']
	CUSTOM_BET_COLOUR = BetListStyleMixin.ACCENTS['gold']['button']
	# The same orange as the duel challenge window's accent (duel_challenge.xml), so the button that opens
	# it and the window it opens read as the same feature.
	DUEL_COLOUR = 'F08A2EE0'
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
		quick_budget = max(self.BUTTON_BUDGET - self.CUSTOM_BET_WIDTH - self.DUEL_WIDTH, 0)
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
				# Read by list.xml, per row: `amount` against the row's remaining allowance, and the
				# colour to fall back to when this particular button on this particular row could only
				# ever be refused. See the `_locked` note in get_data().
				'amount': amount,
				'locked_bgcolor': self.DISABLED_COLOUR,
			})

		# Free-amount button. The quick amounts cover the common cases, but they are fixed numbers: a
		# player who wants to put their whole balance on someone, or the exact 37 planets they have left,
		# previously had to close the window and type /bet by hand.
		min_stake = await self.app.setting_min_stake.get_value()
		max_stake = await self.app.setting_max_stake.get_value()

		async def custom_bet_action(player, values, instance, view=None, **kwargs):
			answer = await ask_amount(
				player,
				'How many planets do you want to bet on $<{}$>?\n(between {} and {})'.format(
					instance['nickname'], min_stake, max_stake
				),
				default=str(min_stake),
			)
			# None when the player pressed Cancel or dismissed the prompt.
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
			# No `amount`: this button has no fixed size, so it only goes dead when the whole row does.
			'locked_bgcolor': self.DISABLED_COLOUR,
		})

		# The duel entry point. Previously the only way into a duel was the /duel chat command, which
		# meant knowing the command and typing an opponent's login by hand. This row already has both the
		# opponent and a bet-amount picker, so re-using ask_input gets a duel challenge out of chat and
		# into the window the player already has open. Availability is its own rule, not the betting
		# one -- see the `_duel_locked` note in get_data() -- so it reads `lock_key` instead of `_locked`.
		duel_enabled = await self.app.setting_duel_enabled.get_value()
		duel_min = await self.app.setting_duel_min_stake.get_value()
		duel_max = await self.app.setting_duel_max_stake.get_value()

		async def duel_action(player, values, instance, view=None, **kwargs):
			answer = await ask_amount(
				player,
				'Challenge $<{}$> to a duel for how many planets?\n(between {} and {})'.format(
					instance['nickname'], duel_min, duel_max
				),
				default=str(duel_min),
			)
			if answer is None:
				return
			try:
				amount = int(str(answer).strip())
			except ValueError:
				await self.app.instance.chat(
					'$i$f00That is not a number. Type only digits, for example 250.', player
				)
				return
			await self.app.duels.challenge(player, instance['login'], amount)
			if view is not None:
				await view.refresh(player)

		actions.append({
			'name': 'Challenge to a duel',
			'type': 'label',
			'text': 'Duel' if duel_enabled else '{}Duel'.format(self.DISABLED_TEXT),
			'bgcolor': self.DUEL_COLOUR if duel_enabled else self.DISABLED_COLOUR,
			'width': self.DUEL_WIDTH,
			'action': duel_action if duel_enabled else closed_action,
			'safe': True,
			# No `amount`: a duel's own availability (already running, closed market, yourself) is decided
			# per row by `_duel_locked`, not by the betting allowance this column's `amount`/`_room` pair
			# reads -- hence `lock_key` instead.
			'lock_key': '_duel_locked',
			'locked_bgcolor': self.DISABLED_COLOUR,
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
		{'name': 'Player',           'index': 'nickname',  'sorting': True,  'searching': True,  'width': 28},
		{'name': 'Badge',            'index': 'title',     'sorting': False, 'searching': False, 'width': 24},
		# Duels sit next to the badge rather than at the far end: it is the one number on this board a
		# player earns by driving instead of by being picked, so it is the one they came to look at.
		{'name': 'Duels won',        'index': 'duel_form', 'sorting': False, 'searching': False, 'width': 20},
		{'name': 'Times bet on',     'index': 'backed',    'sorting': False, 'searching': False, 'width': 18},
		{'name': 'Wins',             'index': 'form',      'sorting': False, 'searching': False, 'width': 16},
		{'name': 'Planets bet',      'index': 'staked',    'sorting': False, 'searching': False, 'width': 18},
		{'name': "Backers' profit",  'index': 'net',       'sorting': False, 'searching': False, 'width': 21},
		{'name': 'Usual multiplier', 'index': 'avg_odds',  'sorting': False, 'searching': False, 'width': 21},
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
				duel_form=format_duel_record(entry),
				staked=str(entry['staked']),
				net=net_str,
				avg_odds='x{}'.format(self.app.format_odds(entry['avg_odds'])),
			))
		return rows


class BetDuelBoardView(BetListStyleMixin, BetNavMixin, ManualListView):
	"""
	The duel record board: who has actually beaten whom, head to head.

	Every other window in this plugin is about money -- what the room staked, what it got back, who
	priced whom correctly. This one is about the driving, which is why wins come before planets in the
	sort and why the top of the board cannot be bought by duelling for larger amounts. It is the board
	a player is supposed to be able to point at.
	"""

	nav_key = 'duels'
	accent = 'orange'
	title = 'BetPluggin -- duel record'
	icon_style = 'Icons128x128_1'
	icon_substyle = 'Solo'

	fields = [
		{'name': '#',            'index': 'rank',      'sorting': False, 'searching': False, 'width': 10},
		{'name': 'Player',       'index': 'nickname',  'sorting': True,  'searching': True,  'width': 40},
		{'name': 'Title',        'index': 'crown',     'sorting': False, 'searching': False, 'width': 28},
		{'name': 'Won',          'index': 'wins',      'sorting': False, 'searching': False, 'width': 12},
		{'name': 'Lost',         'index': 'losses',    'sorting': False, 'searching': False, 'width': 12},
		{'name': 'Draw',         'index': 'draws',     'sorting': False, 'searching': False, 'width': 12},
		{'name': 'Duels',        'index': 'duels',     'sorting': False, 'searching': False, 'width': 14},
		{'name': 'Win %',        'index': 'win_rate',  'sorting': False, 'searching': False, 'width': 14},
		{'name': 'Planets won',  'index': 'net',       'sorting': False, 'searching': False, 'width': 22},
	]

	def __init__(self, app):
		super().__init__()
		self.app = app
		self.manager = app.context.ui

	async def get_data(self):
		entries = await self.app.get_duel_stats()
		rows = []
		for position, entry in enumerate(entries, start=1):
			net = entry['duel_net']
			net_str = (
				'$0f0{:+d}$z'.format(net) if net > 0
				else '$f00{:+d}$z'.format(net) if net < 0
				else '{:+d}'.format(net)
			)
			rows.append(dict(
				rank=str(position),
				nickname=entry['nickname'],
				# One line of bragging rights, given out on the spot rather than stored. The top three
				# are named because a board where only the first row means anything stops being worth
				# climbing at position two.
				crown=DUEL_TITLES.get(position, ''),
				wins='$0f0{}$z'.format(entry['duel_wins']) if entry['duel_wins'] else '0',
				losses='$f00{}$z'.format(entry['duel_losses']) if entry['duel_losses'] else '0',
				draws=str(entry['duel_draws']),
				duels=str(entry['duels']),
				win_rate='{}%'.format(round(entry['duel_win_rate'])),
				net=net_str,
			))
		return rows


class BetPaceView(BetListStyleMixin, BetNavMixin, ManualListView):
	"""
	Who actually drives fast, read off every race the server has a record of.

	The duel board only knows about duels, and the market boards only know about races somebody put
	money on. This one counts every race -- live and reconstructed by //raceimport alike -- which makes
	it the only window that can answer "is this person worth backing" for a driver who has never been
	backed before.

	The column that decides the order is Rating, not Wins: see `BetplugginApp.get_pace_stats`. And a
	driver who has not raced enough for that number to mean anything is told so, in their own row, in
	place of a rank -- the alternative is a public board that reports 1.000 for someone who won a single
	duel two months ago.
	"""

	nav_key = 'pace'
	accent = 'purple'
	title = 'BetPluggin -- pace'
	icon_style = 'Icons128x128_1'
	icon_substyle = 'Rankings'

	fields = [
		{'name': '#',        'index': 'rank',      'sorting': False, 'searching': False, 'width': 10},
		{'name': 'Player',   'index': 'nickname',  'sorting': True,  'searching': True,  'width': 40},
		{'name': 'Rating',   'index': 'rating',    'sorting': False, 'searching': False, 'width': 16},
		{'name': 'Races',    'index': 'races',     'sorting': False, 'searching': False, 'width': 13},
		{'name': 'Wins',     'index': 'wins',      'sorting': False, 'searching': False, 'width': 13},
		{'name': 'Avg place','index': 'avg_place', 'sorting': False, 'searching': False, 'width': 19},
		{'name': 'Field',    'index': 'avg_field', 'sorting': False, 'searching': False, 'width': 13},
		{'name': 'Ranked',   'index': 'status',    'sorting': False, 'searching': False, 'width': 32},
	]

	def __init__(self, app):
		super().__init__()
		self.app = app
		self.manager = app.context.ui

	async def get_data(self):
		rows = []
		rank = 0
		for entry in await self.app.get_pace_stats():
			rated = entry['missing'] == 0
			if rated:
				rank += 1

			rating = '{:.3f}'.format(entry['rating'])
			rows.append(dict(
				# Provisional drivers are listed but not numbered: a rank next to a rating that is not yet
				# a measurement is the exact claim this board is trying not to make.
				rank=str(rank) if rated else '$888-$z',
				nickname=entry['nickname'],
				rating=rating if rated else '$888{}$z'.format(rating),
				races=str(entry['races']),
				wins=str(entry['wins']),
				avg_place='{:.1f}'.format(entry['avg_position']),
				avg_field='{:.1f}'.format(entry['avg_field']),
				status=(
					'$0f0ranked$z' if rated
					else '$888{} more race{}$z'.format(
						entry['missing'], '' if entry['missing'] == 1 else 's'
					)
				),
			))
		return rows


class DuelChallengeView(TemplateView):
	"""
	The "X has challenged you" window, shown to the challenged player and to nobody else.

	Deliberately not a list view: this is a question with a deadline, and every extra click between
	reading it and answering it is time off the clock. So the three amounts a player realistically
	wants -- half, the same, double -- are one click each, and refusing is one click too.

	The amounts are computed once, when the challenge is made, and never refreshed. The window's whole
	life is the accept timeout; a redraw mid-answer would move the buttons under the player's cursor.
	"""

	template_name = 'betpluggin/duel_challenge.xml'

	def __init__(self, app, duel):
		super().__init__()
		self.app = app
		self.manager = app.context.ui
		self.duel = duel

		self.subscribe('decline', self.action_decline)
		self.subscribe('custom', self.action_custom)
		for index in range(3):
			self.subscribe('accept_{}'.format(index), self.action_accept)

		# Filled by display(), because working them out needs the settings and __init__ can't await.
		self.amounts = []

	async def display(self, **kwargs):
		self.amounts = await self.app.duels.suggested_amounts(self.duel['challenger_amount'])
		return await super().display(**kwargs)

	async def get_context_data(self):
		context = await super().get_context_data()
		challenger = self.duel['challenger']

		# Three slots, always: the template draws a fixed row, and clamping to the stake limits can
		# collapse "half / same / double" down to one or two distinct amounts. The unused slots are sent
		# as empty and the template skips them, rather than the template having to loop.
		labels = ['Half', 'Match', 'Double']
		buttons = []
		for index in range(3):
			if index < len(self.amounts):
				buttons.append(dict(
					index=index, amount=self.amounts[index],
					label=labels[index] if index < len(labels) else '', shown=True,
				))
			else:
				buttons.append(dict(index=index, amount=0, label='', shown=False))

		context.update({
			'challenger': challenger.nickname or challenger.login,
			'challenge_amount': self.duel['challenger_amount'],
			'timeout': await self.app.setting_duel_accept_seconds.get_value(),
			'buttons': buttons,
		})
		return context

	async def action_decline(self, player, action, values, **kwargs):
		await self.app.duels.decline(player)

	async def action_accept(self, player, action, values, **kwargs):
		# The button index is the tail of the action name, which is the only thing the manialink sends
		# back -- ManiaLink actions carry no payload of their own.
		try:
			index = int(action.rsplit('accept_', 1)[1])
		except (IndexError, ValueError):
			return
		if index >= len(self.amounts):
			return
		await self.app.duels.accept(player, self.amounts[index])

	async def action_custom(self, player, action, values, **kwargs):
		"""Any other amount. Closes this window first: the prompt would otherwise open behind it."""
		await self.destroy()

		amount = await ask_amount(
			player, 'How many planets do you put up?',
			default=str(self.duel['challenger_amount']), size='sm',
		)
		# Cancel here backs out of the amount prompt, not out of the duel: the challenge is still
		# standing and still timing out, so say so rather than leaving them wondering.
		if amount is None:
			await self.app._safe_chat(
				'{}$ff0The duel is still waiting for you -- answer with $fff/accept <amount>$ff0 '
				'or $fff/decline$ff0.'.format(self.app.CHAT_PREFIX),
				player
			)
			return
		try:
			amount = int(str(amount).strip())
		except (TypeError, ValueError):
			await self.app._safe_chat(
				'{}$f00That is not a number -- the duel is still waiting, answer with '
				'$fff/accept <amount>$f00.'.format(self.app.CHAT_PREFIX),
				player
			)
			return

		await self.app.duels.accept(player, amount)
