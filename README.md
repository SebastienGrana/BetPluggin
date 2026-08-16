# BetPluggin

A [PyPlanet](http://pypla.net/) plugin for **Trackmania² Stadium (ManiaPlanet)** dedicated servers:
players wager real **ManiaPlanet Planets** on who will win the current map (or round), staked and paid
out through the server's own in-game payment system (no separate fictional currency).

> Planets are ManiaPlanet's in-game currency, earned by playing (daily login, medals, ladder points).
> As of this writing there's no active real-money purchase path for them (the ManiaPlanet store is
> gone), so this isn't wagering money — it's wagering an in-game resource, the same way `//pay` or
> `/donate` already move Planets around on plenty of servers.

## How betting works

- `/bet <login> <amount>` — wager `<amount>` planets that `<login>` will win the current market period.
  This sends you a **payment confirmation popup in your game client** (the same one you'd see for
  `/donate`) — the bet only counts once you accept it there.
- `/betmarket` (or `/market`, `/odds`) — opens an in-game window listing online players, the pot and
  live odds on each of them, with quick-bet buttons.
- A player can hold several bets at once, as long as each is on a *different* target for the current
  period (no doubling down on the same target).
- **Market period depends on the game mode:**
  - **TimeAttack, Cup, and anything not listed below** — one market per map. It opens at map start,
    auto-closes partway through (see the betting window below), and settles on whoever finished first.
  - **Rounds** (and `Teams`/`Knockout`, see `ROUND_SCOPED_MODES` in `apps/betpluggin/__init__.py`) — a
    map is played as several rounds and only the final standing matters, so there is still just **one
    market per map**: it opens for the **whole warmup**, closes the moment the warmup ends, then rides
    every round and settles on whoever holds the most map points. The rounds themselves neither open,
    close nor resolve anything — they're the race being bet on. Betting during the warmup is the point:
    everyone is watching everyone practise, and that practice is the information a bettor wants.
  - Any other mode you run defaults to map-scoped betting until you add it to `ROUND_SCOPED_MODES` —
    the list is a single line to edit once you know how that mode should behave.
  - **The betting window closes before the period does**, so nobody bets on a result they can already
    see. Set it either as a plain duration (`betting_window_seconds`, e.g. 60 = betting open for the
    first minute) or, if that's left at 0, as a share of the map time (`betting_window_percent`).
    Players get a chat warning `market_closing_warning_seconds` before it shuts. In round-based modes
    the warmup's end is the close, so neither setting applies. An admin can also close early with
    `//bet close` (e.g. once a race is clearly decided); `//bet open` reopens it.
  - If PyPlanet (re)starts mid-map, betting stays closed for the rest of that map (so nobody bets with
    information others didn't have) and the HUD widget shows **PAUSED** instead of CLOSED to make that
    reason clear. It resumes normally on the next real map/round start.
- **Odds** are pari-mutuel: `total pot / pot on that target`. They move live as people bet and are shown
  in `/betmarket`, in the HUD widget, and locked in on your bet at the moment you place it.
- **Results land during the podium.** Resolution is triggered by the server's final `scores` payload,
  which arrives just as the podium (`S_ChatTime`) begins — so the payout card is on screen while
  players are still looking at the end-of-map screen, rather than after it has been dismissed.
- At resolution, the server (`scores` signal) tells us who actually won. Winners are paid from the
  server's own Planets balance, split proportionally to their stake; if nobody bet on the winner, their
  stakes are simply not returned (normal betting outcome). If BetPluggin genuinely can't tell who won,
  everyone is automatically refunded instead of the server keeping the money.
- If a bet's payment confirms *after* betting has already closed (you took too long on the popup, or
  the map/round changed), it's automatically refunded.

## Duels

Alongside the pari-mutuel market, any two players can bet against each other directly on the map being
played: whoever finishes ahead of the other takes both stakes. The pot is not shared with anyone and the
server takes no margin — this one is strictly between the two of them.

- `/duel <login> <amount>` challenges someone. Nothing is charged yet: the challenged player gets a
  panel in the corner of their screen with one-click answers (the same amount, half it, double it),
  plus *Another amount* and *Refuse*. Half and double are there so two drivers of different speed can
  agree a handicap as easily as they agree a stake. The challenge expires on its own after
  `duel_accept_seconds`.
- Both stakes are only taken once the duel is accepted; if either payment fails, whoever did pay is
  refunded and the duel never starts.
- **Spectators can back a side** (`/duelbet <login> <amount>`, or the two buttons that appear on the HUD
  widget while a duel runs). Those side bets are pari-mutuel between themselves, so backing the
  underdog pays more. Turn this off with the `duel_spectators` setting.
- A duel that can't be settled honestly is refunded in full rather than decided: if either player has no
  result on the map, or the two of them finish dead level, everybody gets their planets back.
- `/duels` is the duel record board — who has won the most duels on this server. Duel wins also show as
  a column on the *Who to bet on* board.

## PyPlanet commands the plugin reacts to

BetPluggin watches PyPlanet's own commands, because they change the shape of the thing people are
betting on. This works from the chat line, from the admin toolbar buttons, and from any other app that
runs them.

| Command | What betting does |
|---|---|
| `//skip`, `//next`, `//previous`, `//restart` (and `//res`, `//rs`, `//prev`) | **Betting is called off and everyone is refunded** — the market pot and any running duel, spectators included. A map that was cut short or is about to be driven again from scratch has no honest winner, and the times on the board belong to a race nobody finished. Nothing is written to anyone's record: a skipped map doesn't count as a win or a loss and doesn't dent a streak. |
| `//extend` | The betting window is re-measured against the map's new length, so "open for the first 30% of the map" stays true after the map gets longer. Only applies to the percentage window — `betting_window_seconds` is an absolute duration and an extension is no reason to hand out more of it. |
| `//pause` / `//unpause` | The betting countdown freezes and resumes with the match. Without this the window would quietly expire while nobody is driving. |
| `//endwu` | Already closes betting in round-based modes, as the end of the warmup always does. |
| `/skip`, `/restart`, `/previous`, `/extend` (players) | Same reactions — but **only once the chat vote actually passes**. These come from PyPlanet's `voting` app and don't do anything by themselves, so betting is hooked to the vote's outcome, not to the command: a vote the server refuses must leave the market exactly as it found it. `//pass` lands on the same handlers and is covered too. |

`//replay`, `//mode`, `//shuffle`, `//endround` and the maplist commands don't affect a market in
progress, so betting ignores them. There is no `/retry` command in PyPlanet — retrying is the player's
own in-game restart and has no bearing on a bet.

**Commands**

| Command | Description |
|---|---|
| `/bet <login> <amount>` | Place a bet (pops a payment confirmation in your client) |
| `/betmarket`, `/market`, `/odds` | Open the live betting window (odds + quick-bet buttons) |
| `/mybet`, `/bets` | List your active/pending bet(s) for the current period |
| `/wallet`, `/stats`, `/betstats` | Show your BetPluggin wagering history (your *live* Planets balance is shown in your game client, not here) |
| `/bettop`, `/betladder` | Open the all-time leaderboard (bets, win %, wagered, net profit) |
| `/bettargets`, `/targets`, `/cotes` | Open the "who's worth betting on" board (past wins, usual multiplier, badges per player) |
| `/duel <login> <amount>` | Challenge a player head to head — whoever finishes ahead takes both stakes |
| `/accept <amount>` | Accept the duel you were challenged to (or use the popup panel) |
| `/decline`, `/refuse` | Turn down a challenge. Nothing is charged |
| `/duelbet <login> <amount>`, `/back` | Back one of the two players in the running duel |
| `/duels`, `/duel list`, `/dueltop` | Open the duel record board |
| `//bet close` (admin) | Close betting for the current period early |
| `//bet open` (admin) | Force-open betting for the current period |
| `//bet help` (admin) | List the admin commands, including the native ones betting reacts to |

A persistent HUD widget also shows the current market status (open/closed, pot, your own bets) with a
button that opens `/betmarket`, and grows a duel band while a duel is running.

## Project layout

```
manage.py             entrypoint (python manage.py start)
requirements.txt      pins pyplanet==0.11.12
settings/
  base.py             non-secret defaults (db connection, storage, ...) - safe to commit
  apps.py             which apps/plugins get loaded, including apps.betpluggin
  local.py            reads DEDICATED/OWNERS from environment variables - safe to commit, no secrets in it
apps/betpluggin/
  __init__.py          the plugin: mode detection, market lifecycle, SendBill/Pay plumbing, odds, commands,
                       and the hooks that make it react to PyPlanet's own admin commands
  duel.py              head-to-head duels: challenge/accept flow, spectator side bets, settlement
  models.py            Bet table (a wager, scoped to a map or a round, tracks its own payment state)
  views.py              in-game UI: BetWidget (HUD), BetMarketView, BetLeaderboardView, BetTargetsView, BetResultView, BetDuelBoardView
  templates/            manialink markup: widget.xml (HUD), duel_challenge.xml (the challenge panel),
                       list.xml (leaderboard/targets tables), result.xml (payout popup)
.env.example           copy to .env and fill in your real server details (gitignored)
```

## Deploying to a live server

The simplest way to run this plugin on an already-running PyPlanet install: add the
`apps/betpluggin/` folder from this repo into that install's own `apps/` directory (via
`git clone`/`git pull`, or by copying the folder manually), then add `'apps.betpluggin'` to the `APPS`
list in that install's `settings/apps.py` (see this repo's `settings/apps.py` for the exact line) and
restart PyPlanet.

> If your host runs a fully managed PyPlanet (their own control panel, curated plugin list, no exposed
> RPC credentials or FTP access), ask their support to either add this plugin's folder to their
> PyPlanet's `apps/` directory themselves, or give you FTP/SFTP access to it so you can drop it in
> yourself.

## Notes

- `//settings` in-game lets an admin tweak `quick_bet_amounts`, `bet_minimum_stake`,
  `bet_maximum_stake`, `betting_window_seconds`, `betting_window_percent`,
  `market_closing_warning_seconds` and the duel settings (`duel_enabled`, `duel_minimum_stake`,
  `duel_maximum_stake`, `duel_accept_seconds`, `duel_spectators`) without touching code.
  `quick_bet_amounts` takes as many amounts as you like — the market window sizes its buttons to fit
  however many you configure. Everything that affects how the plugin *feels* is a setting rather than a
  constant, so it can be tuned between games without a redeploy.
- The reactions to `//skip`, `//restart`, `//extend` and `//pause` are installed by wrapping the target
  of PyPlanet's own registered `Command` objects (`_install_command_hooks`), and undone on app stop.
  That is deliberately the one place all three call paths meet: there is no signal for "an admin skipped
  the map", `map_start`'s `restarted` flag arrives *after* the pot would already have been paid out, and
  listening for the chat line would miss the admin toolbar entirely — its buttons call the command
  dispatcher directly. The player-facing `/skip` and friends are hooked separately
  (`_install_vote_hooks`), on the `voting` app's `vote_*_passed` handlers rather than on its commands,
  since the commands only open a vote. If PyPlanet ever renames any of them, the plugin logs a warning
  at startup naming what it could not hook rather than failing silently.
- Still unhooked: ManiaPlanet's own built-in callvotes (the server-side vote UI, `mp_signals.other.vote_updated`),
  which are a separate path to NextMap/RestartMap from PyPlanet's chat votes. Most servers disable them
  in favour of the `voting` app.
- BetPluggin never tracks a player's Planets balance itself (there's no API to query it) — only its own
  betting history (`Bet` table), used for `/wallet` and the leaderboard. Live balance is always whatever
  your game client shows you.
- Payouts come from the **server's own Planets balance** (`GetServerPlanets`), the same pool the
  `transactions`/`donate` app or `//pay` draws from. If the server runs low, payouts fail gracefully
  (the player is told to contact an admin) rather than silently dropping the payout.
- Leaderboard/stats are computed on demand from the `Bet` table (no separately maintained running totals
  to drift out of sync) — fine for a single server's bet volume.
- If you add another custom app later, drop it under `apps/<name>/` and list it in `settings/apps.py`
  as `'apps.<name>'`.
