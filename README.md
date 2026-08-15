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
  - **TimeAttack, Cup, and anything not listed below** — one market per map, open from map start and
    resolved when the map ends.
  - **Rounds** (and `Teams`/`Knockout`, see `ROUND_SCOPED_MODES` in `apps/betpluggin/__init__.py`) — a
    fresh market opens at the start of *each round* and resolves at the end of that round.
  - Any other mode you run defaults to map-scoped betting until you add it to `ROUND_SCOPED_MODES` —
    the list is a single line to edit once you know how that mode should behave.
  - A market stays open for bets the whole period — it doesn't auto-close early. An admin can close it
    early with `//bet close` (e.g. to stop bets once a race is clearly decided); `//bet open` reopens it.
  - If PyPlanet (re)starts mid-map, betting stays closed for the rest of that map (so nobody bets with
    information others didn't have) and the HUD widget shows **PAUSED** instead of CLOSED to make that
    reason clear. It resumes normally on the next real map/round start.
- **Odds** are pari-mutuel: `total pot / pot on that target`. They move live as people bet and are shown
  in `/betmarket`, in the HUD widget, and locked in on your bet at the moment you place it.
- At resolution, the server (`scores` signal) tells us who actually won. Winners are paid from the
  server's own Planets balance, split proportionally to their stake; if nobody bet on the winner, their
  stakes are simply not returned (normal betting outcome). If BetPluggin genuinely can't tell who won,
  everyone is automatically refunded instead of the server keeping the money.
- If a bet's payment confirms *after* betting has already closed (you took too long on the popup, or
  the map/round changed), it's automatically refunded.

**Commands**

| Command | Description |
|---|---|
| `/bet <login> <amount>` | Place a bet (pops a payment confirmation in your client) |
| `/betmarket`, `/market`, `/odds` | Open the live betting window (odds + quick-bet buttons) |
| `/mybet`, `/bets` | List your active/pending bet(s) for the current period |
| `/wallet`, `/stats`, `/betstats` | Show your BetPluggin wagering history (your *live* Planets balance is shown in your game client, not here) |
| `/bettop`, `/betladder` | Open the all-time leaderboard (bets, win %, wagered, net profit) |
| `/bettargets`, `/targets`, `/cotes` | Open the "who's worth betting on" board (past wins, usual multiplier, badges per player) |
| `//bet close` (admin) | Close betting for the current period early |
| `//bet open` (admin) | Force-open betting for the current period |

A persistent HUD widget also shows the current market status (open/closed, pot, your own bets) with a
button that opens `/betmarket`.

## Project layout

```
manage.py             entrypoint (python manage.py start)
requirements.txt      pins pyplanet==0.11.12
settings/
  base.py             non-secret defaults (db connection, storage, ...) - safe to commit
  apps.py             which apps/plugins get loaded, including apps.betpluggin
  local.py            reads DEDICATED/OWNERS from environment variables - safe to commit, no secrets in it
apps/betpluggin/
  __init__.py          the plugin: mode detection, market lifecycle, SendBill/Pay plumbing, odds, commands
  models.py            Bet table (a wager, scoped to a map or a round, tracks its own payment state)
  views.py              in-game UI: BetWidget (HUD), BetMarketView, BetLeaderboardView, BetTargetsView, BetResultView
  templates/            manialink markup: widget.xml (HUD), list.xml (leaderboard/targets tables), result.xml (payout popup)
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

- `//settings` in-game lets an admin tweak `quick_bet_amounts`, `bet_minimum_stake` and
  `bet_maximum_stake` without touching code.
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
