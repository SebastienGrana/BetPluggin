"""
In-game UI for BetPluggin: a persistent HUD widget plus two interactive list views (the live betting
market with quick-bet buttons, and the all-time player leaderboard).
"""
from pyplanet.views.generics.list import ManualListView
from pyplanet.views.generics.widget import WidgetView

from .models import Bet


class BetWidget(WidgetView):
	# Docked right below the "Best CPs" widget (x=-124.5, y=90, 35x6), in the same column, so it sits
	# flush beside the left-hand PyPlanet column (server info / ladder range / version, then Dedimania).
	widget_x = -124.5
	widget_y = 83.5
	z_index = 130
	size_x = 40
	size_y = 11.5

	template_name = 'betpluggin/widget.xml'

	def __init__(self, app):
		super().__init__()
		self.app = app
		self.manager = app.context.ui
		self.id = 'betpluggin__widget'

		self.subscribe('open_market', self.action_open_market)

	async def get_context_data(self):
		context = await super().get_context_data()

		if self.app.market_is_open:
			status = 'OPEN'
			status_color = '73FF51FF'
		elif self.app.closed_for_reboot:
			status = 'PAUSED'
			status_color = 'FFB347FF'
		elif self.app.scope == Bet.SCOPE_ROUND and self.app.current_round_number is None:
			status = 'WAITING'
			status_color = 'FFCC00FF'
		else:
			status = 'CLOSED'
			status_color = 'FF7B7BFF'

		if self.app.scope == Bet.SCOPE_ROUND:
			period_label = 'Round {}'.format(self.app.current_round_number) if self.app.current_round_number else 'Round'
		else:
			period_label = 'This map'

		total_pot = sum(bet['amount'] for bet in self.app.current_bets)
		bet_count = len(self.app.current_bets)

		context.update({
			'status': status,
			'status_color': status_color,
			'period_label': period_label,
			'total_pot': total_pot,
			'bet_count': bet_count,
			'bet_word': 'bet' if bet_count == 1 else 'bets',
		})

		return context

	async def get_player_data(self):
		data = await super().get_player_data()

		for player in self.app.instance.player_manager.online:
			active = [b for b in self.app.current_bets if b['player'].login == player.login]
			pending = [b for b in self.app.pending_stakes.values() if b['player'].login == player.login]

			parts = ['{} on {}'.format(b['amount'], b['target_login']) for b in active]
			parts += ['{} on {} (pending)'.format(b['amount'], b['target_login']) for b in pending]

			data[player.login] = {'own_bets': ', '.join(parts) if parts else 'no active bet'}

		return data

	async def action_open_market(self, player, action, values, **kwargs):
		view = BetMarketView(self.app)
		await view.display(player=player)


class BetMarketView(ManualListView):
	title = 'BetPluggin -- current market'
	icon_style = 'Icons128x128_1'
	icon_substyle = 'Statistics'

	fields = [
		{
			'name': 'Player',
			'index': 'nickname',
			'sorting': True,
			'searching': True,
			'width': 45,
		},
		{
			'name': 'Pot on them',
			'index': 'pot',
			'sorting': False,
			'searching': False,
			'width': 25,
		},
		{
			'name': 'Odds',
			'index': 'odds',
			'sorting': False,
			'searching': False,
			'width': 18,
		},
		{
			'name': 'Your bet',
			'index': 'your_bet',
			'sorting': False,
			'searching': False,
			'width': 22,
		},
		{
			'name': 'Payout',
			'index': 'potential_win',
			'sorting': False,
			'searching': False,
			'width': 22,
		},
	]

	def __init__(self, app):
		super().__init__()
		self.app = app
		self.manager = app.context.ui
		self.requesting_login = None

	async def display(self, player=None, **kwargs):
		if player is not None:
			self.requesting_login = player.login
		return await super().display(player=player, **kwargs)

	async def get_title(self):
		if self.app.market_is_open:
			return 'Betting is OPEN -- who will win?'
		return 'Betting is currently closed'

	async def get_data(self):
		own_bets = []
		if self.requesting_login:
			own_bets = [
				dict(b, pending=False) for b in self.app.current_bets
				if b['player'].login == self.requesting_login
			] + [
				dict(b, pending=True) for b in self.app.pending_stakes.values()
				if b['player'].login == self.requesting_login
			]

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

			rows.append(dict(
				login=player.login,
				nickname=player.nickname,
				pot=str(pot_on_player),
				odds='x{}'.format(self.app.format_odds(odds)),
				your_bet=your_bet,
				potential_win=potential_win,
			))
		return rows

	async def get_actions(self):
		if not self.app.market_is_open:
			return []

		actions = []
		for amount in await self.app.get_quick_bet_amounts():
			async def bet_action(player, values, instance, view=None, amount=amount, **kwargs):
				try:
					ok, message = await self.app.place_bet(player, instance['login'], amount)
				except Exception as e:
					await self.app.instance.chat('$i$f00BetPluggin error: {}'.format(e), player)
					raise
				color = '$ff0' if ok else '$i$f00'
				await self.app.instance.chat('{}{}'.format(color, message), player)
				if view is not None:
					await view.refresh(player)

			actions.append({
				'name': 'Bet {}'.format(amount),
				'type': 'label',
				'text': 'Bet {}'.format(amount),
				'width': 18,
				'action': bet_action,
				'safe': True,
			})

		return actions


class BetLeaderboardView(ManualListView):
	title = 'BetPluggin -- leaderboard'
	icon_style = 'Icons128x128_1'
	icon_substyle = 'Statistics'

	fields = [
		{'name': 'Player',  'index': 'nickname', 'sorting': True,  'searching': True,  'width': 38},
		{'name': 'Bets',    'index': 'bets',     'sorting': False, 'searching': False, 'width': 13},
		{'name': 'Win %',   'index': 'win_rate', 'sorting': False, 'searching': False, 'width': 15},
		{'name': 'Wagered', 'index': 'wagered',  'sorting': False, 'searching': False, 'width': 18},
		{'name': 'Net',     'index': 'net',      'sorting': False, 'searching': False, 'width': 18},
		{'name': 'Best',    'index': 'best_win', 'sorting': False, 'searching': False, 'width': 18},
		{'name': 'Streak',  'index': 'streak',   'sorting': False, 'searching': False, 'width': 13},
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
				streak=streak_str,
			))
		return rows
