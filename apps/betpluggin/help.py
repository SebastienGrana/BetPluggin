"""
The text of BetPluggin's in-game help window, and nothing else.

Kept out of views.py on purpose. views.py is imported *from* `__init__.py`, and the help copy needs to
name settings and constants that live in `__init__.py` -- so the text carries `{placeholders}` instead
and BetHelpView fills them in at render time from the values that are actually configured. A player
reading "between 10 and 2500 planets" is reading this server's numbers, not a documentation default
that quietly went stale the first time an admin typed //settings.

Structure: TOPICS is a list of pages. Each page is a dict:

    key      the topic id, used by the sidebar buttons and by the "?" on each list window
    label    what the sidebar button says
    group    sidebar heading this page sits under
    accent   a key of BetListStyleMixin.ACCENTS, so a window's help page is the colour of that window;
             None for the pages that are not about one particular window
    blocks   the copy, as a list of tuples:
               ('h', text)  a heading
               ('p', text)  a paragraph, wrapped
               ('b', text)  a bullet, wrapped with a hanging indent
               ('gap',)     a blank line

Everything here is written to be read by someone who does not bet and may not speak much English:
short sentences, no betting vocabulary that isn't explained on the spot, and the rule stated before
the exception. It is the one place in the plugin allowed to spend words.
"""
import re


# Trackmania formatting codes -- `$fff`, `$z`, `$o`, `$i`... They take no width on screen but count as
# characters to anything that measures a string, which is why textwrap cannot be pointed at this copy
# directly: a line with four colour codes in it would be wrapped sixteen characters early.
FORMAT_CODE = re.compile(r'\$(?:[0-9a-fA-F]{3}|[a-zA-Z])')

# Roughly how many *visible* characters fit on one line of the content pane (167 units at textsize 1.2).
# Deliberately short of the true maximum: nicknames aren't involved here, so the only cost of stopping
# early is a ragged right edge, and the cost of stopping late is text disappearing off the card.
LINE_CHARS = 100


def visible_length(text):
	"""Length of `text` as it is actually drawn, with the formatting codes discounted."""
	return len(FORMAT_CODE.sub('', text))


def wrap(text, width=LINE_CHARS):
	"""
	Wrap on spaces, measuring visible width. Returns a list of lines.

	A word longer than the whole line is left alone rather than cut: every such word in this copy is a
	command (`/duelbet <login> <amount>`), and half a command is worse than a line that runs a little
	long.
	"""
	lines = []
	current = ''
	for word in text.split(' '):
		if not word:
			continue
		candidate = '{} {}'.format(current, word) if current else word
		if current and visible_length(candidate) > width:
			lines.append(current)
			current = word
		else:
			current = candidate
	if current:
		lines.append(current)
	return lines or ['']


TOPICS = [
	{
		'key': 'start',
		'label': 'How it works',
		'group': 'Start here',
		'accent': None,
		'blocks': [
			('h', 'You are betting real Planets'),
			('p', 'Planets are the game\'s own currency, the one in your ManiaPlanet account. Nothing '
				  'here is play money: what you stake leaves your account, and what you win arrives in it.'),
			('p', 'Two ways to make some. $fffBet$ccc on whoever you think will win, or $fffduel$ccc '
				  'somebody one against one.'),
			('h', 'Betting, in four steps'),
			('b', 'Every map opens a $fffmarket$ccc. {window_line}'),
			('b', 'Pick $fffone$ccc driver and put planets on them -- from the $fffMarket$ccc window, one '
				  'click per amount, or by typing $fff/bet <login> <amount>$ccc.'),
			('b', 'Your game asks you to confirm the payment. Nothing is taken until you accept it.'),
			('b', '{settle_line} Everyone who backed them shares out everything that was staked.'),
			('h', 'Duelling, in three steps'),
			('b', 'Challenge someone with $fff/duel <login> <amount>$ccc, or the $fffDuel$ccc button on '
				  'their row in the Market window.'),
			('b', 'They answer within {duel_seconds} seconds -- and may take it for more or less than you '
				  'put up.'),
			('b', 'Whoever finishes ahead on the map takes both stakes. Everyone else can back a side.'),
			('h', 'Where to look'),
			('p', 'The $fffBETS$ccc panel on your screen always shows the pot, the time you have left and '
				  'your own bet. The buttons under it open the windows listed on the left of this page.'),
			('p', 'Every one of those windows has a $fff?$ccc next to its close button, which opens this '
				  'help on that window.'),
		],
	},
	{
		'key': 'rules',
		'label': 'The rules',
		'group': 'Start here',
		'accent': None,
		'blocks': [
			('h', 'How much you win'),
			('p', 'Everything staked on the period goes into one pot. When it ends, the whole pot is shared '
				  'between the people who backed the winner, in proportion to what each of them put up.'),
			('p', 'The server keeps $fffnothing$ccc. There is no cut, no fee: every planet staked is paid '
				  'back out.'),
			('p', 'The $fffmultiplier$ccc shown next to a driver is what the pot would pay right now -- the '
				  'whole pot divided by what is already on that driver. It moves every time anyone bets, in '
				  'either direction, and the one that decides your payout is the one at the end, not the one '
				  'you saw when you clicked.'),
			('p', 'So the fewer people are on your driver, the more you make if they win. Being right on '
				  'your own pays far better than being right with everybody else.'),
			('h', 'What you are allowed to stake'),
			('b', 'Between $fff{min_stake}$ccc and $fff{max_stake}$ccc planets.'),
			('b', '$fffOne driver per period.$ccc Once you have backed someone you cannot also back their '
				  'opponent -- there is no way to bet on both sides.'),
			('b', 'Up to $fff{max_bets}$ccc separate stakes on that driver, so you can add to your bet as '
				  'the multiplier moves. All of them together still cannot pass $fff{max_stake}$ccc.'),
			('b', 'The driver has to be connected and actually driving. Someone watching from the stands '
				  'cannot be backed -- their row is greyed and marked $888spec$ccc.'),
		],
	},
	{
		# Split off from "The rules" because the two together ran off the bottom of the card, and this is
		# the half a player looks up on its own: not "how do I bet" but "what happens to my money when
		# the map is skipped".
		'key': 'refunds',
		'label': 'Closing & refunds',
		'group': 'Start here',
		'accent': None,
		'blocks': [
			('h', 'When the market shuts'),
			('p', '{window_line} You get a warning in chat $fff{warn_seconds} seconds$ccc before it does, '
				  'and the countdown is on the $fffBETS$ccc panel the whole time.'),
			('p', 'Betting shuts before the end on purpose: with the market open to the last second you '
				  'would be betting on a result you can already see, which is not a bet.'),
			('p', 'If you confirm a payment more than $fff{grace} seconds$ccc after the market shut, it is '
				  'refunded instead of joining the pot. Answer the popup while it is still on screen.'),
			('h', 'When nobody wins and nobody loses'),
			('p', 'You are refunded in full, automatically, every time the period cannot produce an honest '
				  'result:'),
			('b', 'the map is skipped, restarted, or jumped by an admin or by a chat vote,'),
			('b', 'no winner can be worked out at the end,'),
			('b', 'or nobody at all had backed the driver who won.'),
			('p', 'A refund is your stake back, exactly. You never lose money to a cancelled map.'),
		],
	},
	{
		'key': 'duels',
		'label': 'Duels',
		'group': 'Start here',
		'accent': 'orange',
		'blocks': [
			('h', 'One against one, on the same map'),
			('p', 'A duel is a private bet between two players, separate from the market. Whoever finishes '
				  'ahead on the map takes $fffboth stakes$ccc. No share, no margin -- the winner takes the lot.'),
			('h', 'Challenging'),
			('b', 'Type $fff/duel <login> <amount>$ccc, or click $fffDuel$ccc on their row in the Market '
				  'window. Challenging costs you nothing.'),
			('b', 'They have $fff{duel_seconds} seconds$ccc to answer, with $fff/accept$ccc, '
				  '$fff/decline$ccc, or the window that pops up in front of them.'),
			('b', 'They may accept for $fffmore or less$ccc than you offered. That is deliberate: it is how '
				  'the two of you agree a handicap when one is clearly faster. Each side is between '
				  '$fff{duel_min}$ccc and $fff{duel_max}$ccc planets.'),
			('b', 'Both of you are charged only once the duel is on. If one payment fails, the other is '
				  'refunded straight away.'),
			('h', 'How it ends'),
			('p', 'At the podium, the better placing wins. Both sides are refunded in full if neither of you '
				  'finished, if one of you has no result at all, or if you finished dead level.'),
			('h', 'Backing somebody else\'s duel'),
			('p', 'While a duel is running, everyone except the two duellists can put planets on a side, '
				  'with $fff/duelbet <login> <amount>$ccc or the two one-click buttons that appear on the '
				  '$fffBETS$ccc panel.'),
			('p', 'That is a $fffseparate pot$ccc. It never touches what the two players put up, and they '
				  'never see a planet of it. It is shared out between the backers of the winner, and '
				  'refunded in full if nobody had backed them.'),
			('p', 'A duel stays open to backers for the rest of the map, so you can still answer one that '
				  'started before you looked up.'),
		],
	},
	{
		'key': 'commands',
		'label': 'All commands',
		'group': 'Start here',
		'accent': None,
		'blocks': [
			('p', 'All of this can be done by clicking instead. The commands are for people who type faster '
				  'than they aim.'),
			('h', 'Betting'),
			('b', '$fff/bet <login> <amount>$ccc  --  back a driver for the current period.'),
			('b', '$fff/bet market$ccc  ($fff/market$ccc, $fff/odds$ccc)  --  the betting window, with '
				  'one-click bets.'),
			('b', '$fff/bet mine$ccc  ($fff/mybet$ccc, $fff/bets$ccc)  --  what you have riding on this '
				  'period.'),
			('b', '$fff/bet wallet$ccc  ($fff/wallet$ccc, $fff/stats$ccc)  --  your own stat sheet.'),
			('b', '$fff/bet top$ccc  ($fff/bettop$ccc)  --  the top bettors board.'),
			('b', '$fff/bet targets$ccc  ($fff/targets$ccc, $fff/cotes$ccc)  --  the best bets board.'),
			('h', 'Duels'),
			('b', '$fff/duel <login> <amount>$ccc  --  challenge a player.'),
			('b', '$fff/accept <amount>$ccc  --  take a challenge. Leave the amount out to match it exactly.'),
			('b', '$fff/decline$ccc  ($fff/refuse$ccc)  --  turn it down. Nothing is charged.'),
			('b', '$fff/duelbet <login> <amount>$ccc  ($fff/back$ccc)  --  back one side of the running duel.'),
			('b', '$fff/duels$ccc  ($fff/duel list$ccc, $fff/dueltop$ccc)  --  the top duellists board.'),
			('h', 'Driving'),
			('b', '$fff/pace$ccc  ($fff/form$ccc, $fff/racetop$ccc)  --  the top drivers board.'),
			('h', 'This window'),
			('b', '$fff/help bet$ccc  ($fff/bet help$ccc)  --  opens the page you are reading. Add a page '
				  'name to land on it, as in $fff/help bet duels$ccc.'),
			('p', 'A login is the plain name, not the nickname with the colours in it -- the buttons never '
				  'get it wrong.'),
		],
	},

	# ------------------------------------------------------------------
	# One page per window. Each opens from the "?" in that window's title bar, so the reader is looking
	# at the columns while they read about them -- which is why these pages name the columns rather than
	# describing the window in the abstract.
	# ------------------------------------------------------------------
	{
		'key': 'market',
		'label': 'Market',
		'group': 'The windows',
		'accent': 'blue',
		'blocks': [
			('h', 'The window you bet from'),
			('p', 'One row per player who could win the period that is open now. The buttons on the right '
				  'stake that many planets on that row in a single click -- {quick} are set up here, and '
				  '$fffOther$ccc asks you for any amount between $fff{min_stake}$ccc and '
				  '$fff{max_stake}$ccc.'),
			('p', 'The title bar tells you whether betting is open, how long is left, and where you stand '
				  'under the one-driver rule. A row whose buttons are grey cannot take your money: the '
				  'market is shut, you are already on somebody else, you have used your allowance, or that '
				  'player is spectating. Clicking anyway tells you which.'),
			('h', 'The columns'),
			('b', '$fffPlayer$ccc  --  who you would be backing. $ff0>$ccc marks the driver you have already '
				  'committed to; $888spec$ccc marks somebody watching rather than driving.'),
			('b', '$fffPlanets on them$ccc  --  everything the room has staked on that player so far this '
				  'period.'),
			('b', '$fffMultiplier now$ccc  --  what one planet on them would pay if they won, at this '
				  'instant. It falls as more people join and rises as people back other drivers.'),
			('b', '$fffPast wins$ccc  --  how many periods they have won out of the ones they were backed '
				  'for, and the percentage. $aaanew$ccc means nobody has ever bet on them.'),
			('b', '$fffUsually$ccc  --  the multiplier they normally go off at. Compared with the column '
				  'three places to its left, it says whether they are underrated tonight or overrated.'),
			('b', '$fffYour bet$ccc  --  what you have on them, all your stakes added up. '
				  '$fffpending$ccc means a payment popup you have not answered yet.'),
			('b', '$fffIf they win$ccc  --  what you would be paid at the multiplier showing now, and your '
				  'profit in brackets. It is an estimate, not a promise: it moves with the multiplier.'),
			('b', '$fffDuel$ccc  --  challenge that player instead of betting on them. See the Duels page.'),
		],
	},
	{
		'key': 'targets',
		'label': 'Best bets',
		'group': 'The windows',
		'accent': 'green',
		'blocks': [
			('h', 'Who is worth backing'),
			('p', 'The market\'s memory of everyone who has ever been bet $ffon$ccc, over every bet this '
				  'server has settled. Open it before you bet, not after: the Market window tells you what '
				  'the room thinks tonight, this one tells you whether the room is usually right.'),
			('p', 'Sorted by how often each player has been backed, so the names the market actually has an '
				  'opinion about come first.'),
			('h', 'The columns'),
			('b', '$fffPlayer$ccc  --  the driver people bet on.'),
			('b', '$fffBadge$ccc  --  a title handed out here for standing out on one of these numbers.'),
			('b', '$fffDuels won$ccc  --  their head-to-head record, as "won of played". The one number on '
				  'this board they earned by driving rather than by being picked. Blank means never duelled.'),
			('b', '$fffTimes bet on$ccc  --  how many settled periods somebody had money on them.'),
			('b', '$fffWins$ccc  --  how many of those they won, and the percentage.'),
			('b', '$fffPlanets bet$ccc  --  the total the room has ever staked on them.'),
			('b', '$fffBettors\' profit$ccc  --  what backing them has paid, added up across everyone who '
				  'ever did. Green means people made money on them; red means people lost money on them.'),
			('b', '$fffUsually$ccc  --  the multiplier they normally go off at. A high number here next to a '
				  'good win rate is the interesting combination: often right, rarely backed.'),
			('h', 'Reading it'),
			('p', 'A high win rate is not automatically a good bet. If everybody already knows, the '
				  'multiplier is low and there is little to win. The money is in the rows where '
				  '$fffWins$ccc is better than $fffUsually$ccc suggests it should be.'),
		],
	},
	{
		'key': 'leaderboard',
		'label': 'Top bettors',
		'group': 'The windows',
		'accent': 'gold',
		'blocks': [
			('h', 'Who bets best'),
			('p', 'Ranked by profit -- planets won minus planets staked -- over every bet ever settled on '
				  'this server. Not by how much they bet, and not by how often they are right: you can be '
				  'right most of the time and still be losing.'),
			('h', 'The columns'),
			('b', '$fffPlayer$ccc  --  the bettor.'),
			('b', '$fffBets$ccc  --  how many settled bets they have placed.'),
			('b', '$fffWin %$ccc  --  the share of those that came in.'),
			('b', '$fffTotal bet$ccc  --  everything they have ever staked.'),
			('b', '$fffProfit$ccc  --  what they are up or down overall. Green is ahead, red is behind. This '
				  'is the column the board is sorted on.'),
			('b', '$fffBest win$ccc  --  the biggest single payout they have collected.'),
			('b', '$fffBest multiplier$ccc  --  the longest shot they ever got right. Shown green past x2, '
				  'which is where they were betting against the room rather than with it.'),
			('b', '$fffIn a row$ccc  --  their current run of wins or losses. Green counts wins, red counts '
				  'losses.'),
			('h', 'Getting on it'),
			('p', 'Place one bet and you are on this board. Where you land on it is on your own page, '
				  '$fffMy stats$ccc, without having to hunt for your name here.'),
		],
	},
	{
		'key': 'duelboard',
		'label': 'Top duellists',
		'group': 'The windows',
		'accent': 'orange',
		'blocks': [
			('h', 'Head to head'),
			('p', 'Ranked by duels $ffwon$ccc, then by win rate, then by planets. Deliberately not by '
				  'money: the top of this board cannot be bought by duelling for larger amounts, only by '
				  'beating more people.'),
			('p', 'Only the two duellists\' own stakes count here. Planets that spectators put on a duel '
				  'settle in their own pot and never touch this board.'),
			('h', 'The columns'),
			('b', '$fff#$ccc  --  position on the board.'),
			('b', '$fffPlayer$ccc  --  the duellist.'),
			('b', '$fffTitle$ccc  --  handed out by position, to the top three only. Somebody has to lose '
				  'theirs for you to take it, which is the point of it.'),
			('b', '$fffWon$ccc / $fffLost$ccc / $fffDraw$ccc  --  their record. A draw is a duel where both '
				  'finished level; both sides got their stake back.'),
			('b', '$fffDuels$ccc  --  how many they have played in total.'),
			('b', '$fffWin %$ccc  --  the share of those they won.'),
			('b', '$fffPlanets won$ccc  --  what duelling has actually paid them, their own stakes '
				  'included. Green is ahead, red is behind.'),
		],
	},
	{
		'key': 'pace',
		'label': 'Top drivers',
		'group': 'The windows',
		'accent': 'purple',
		'blocks': [
			('h', 'Who actually drives fast'),
			('p', 'The only board here that is not about money. It counts $ffevery$ccc race the server has a '
				  'record of, whether or not anybody bet on it -- which makes it the one place you can size '
				  'up a driver nobody has ever backed.'),
			('h', 'The columns'),
			('b', '$fff#$ccc  --  position, given only to drivers with enough races behind them.'),
			('b', '$fffPlayer$ccc  --  the driver.'),
			('b', '$fffRating$ccc  --  where they finish, on a scale from 0.000 to 1.000: $fff1.000$ccc is '
				  'first every single time, $fff0.000$ccc is last every single time, $fff0.500$ccc is '
				  'mid-field. This is what the board is sorted on.'),
			('b', '$fffRaces$ccc  --  how many races they are rated on.'),
			('b', '$fffWins$ccc  --  how many of those they won outright.'),
			('b', '$fffAverage place$ccc  --  where they usually finish, as a number: 3.4 means about '
				  'third or fourth.'),
			('b', '$fffDrivers$ccc  --  how big the field usually was. Finishing third out of four is not '
				  'the same as third out of twenty, and this is the column that tells the two apart.'),
			('b', '$fffStatus$ccc  --  $0f0ranked$ccc, or how many more races they need. Below '
				  '$fff{pace_min}$ccc races the rating is shown grey and the driver is listed underneath '
				  'everyone who has crossed that bar.'),
			('h', 'Why the unranked ones are marked'),
			('p', 'One race gives a rating of 1.000 or 0.000. That looks like a verdict and is nothing of '
				  'the kind, so the board says so instead of quietly printing it as fact.'),
		],
	},
	{
		'key': 'wallet',
		'label': 'My stats',
		'group': 'The windows',
		'accent': 'teal',
		'blocks': [
			('h', 'Your own numbers'),
			('p', 'One row per number, taken from all three boards at once, grouped by where each comes '
				  'from: your betting, your duels, your driving.'),
			('h', 'The columns'),
			('b', '$fffStat$ccc  --  what is being counted.'),
			('b', '$fffYou$ccc  --  your figure. $888-$ccc means you have nothing on record for that row '
				  'yet, not that you scored zero.'),
			('b', '$fffServer best$ccc  --  the best anyone here has managed.'),
			('b', '$fffHeld by$ccc  --  who holds it. $0f0you$ccc, when it is you.'),
			('h', 'What it is for'),
			('p', '"42 bets" on its own says nothing. "42 bets, and the most anyone has placed is 210" is a '
				  'position -- and the same glance tells a new player what this plugin even keeps track of.'),
			('p', 'Rate records (best win rate, and so on) need at least $fff5$ccc duels behind them before '
				  'they count. A perfect record from one duel is a wall, not a target.'),
			('h', 'Your Planets balance'),
			('p', 'It is not here, because the plugin is not allowed to read it. Your balance lives in your '
				  'ManiaPlanet account and your game client shows it -- the plugin only ever sees the '
				  'payments you confirm.'),
		],
	},
]


# nav_key (BetNavMixin) -> topic key. The "?" in a list window's title bar opens the help on the page
# about that window rather than on the front page; a help button that always lands somewhere general is
# a help button people stop pressing.
NAV_TOPICS = {
	'market': 'market',
	'targets': 'targets',
	'leaderboard': 'leaderboard',
	'duels': 'duelboard',
	'pace': 'pace',
	'wallet': 'wallet',
}

TOPIC_KEYS = [topic['key'] for topic in TOPICS]


def topic_index(key):
	"""Position of `key` in TOPICS, or 0 (the front page) for anything unknown."""
	try:
		return TOPIC_KEYS.index(key)
	except ValueError:
		return 0
