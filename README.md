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
  - **TimeAttack, Cup, and anything not listed below** — one market per map, open from map start until
    `betting_window_seconds` elapses (default 30s; 0 = open for the whole map), resolved when the map ends.
  - **Rounds** (and `Teams`/`Knockout`, see `ROUND_SCOPED_MODES` in `apps/betpluggin/__init__.py`) — a
    fresh market opens at the start of *each round* and resolves at the end of that round.
  - Any other mode you run defaults to map-scoped betting until you add it to `ROUND_SCOPED_MODES` —
    the list is a single line to edit once you know how that mode should behave.
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
| `//betstop` (admin) | Close betting for the current period early |

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
  views.py              in-game UI: BetWidget (HUD), BetMarketView, BetLeaderboardView
  templates/widget.xml  manialink markup for the HUD widget
Dockerfile, docker-compose.yml   Linux/Python 3.8 runtime for PyPlanet + MariaDB (see "Why Docker")
docker/tm-dedicated-config.txt   config for the local dev dedicated server (no secrets, safe to commit)
.env.example           copy to .env and fill in your real server details (gitignored)
```

## Why Docker, and why Python 3.8 specifically

PyPlanet 0.11.12 pins some very old dependencies. `peewee==2.10.2` imports `collections.Callable`,
removed in Python 3.10 (installs cleanly on &lt;=3.9). More strictly, the pinned `peewee_async` calls
`asyncio.Task.current_task()`, removed in Python 3.9 -- so Python 3.8 is actually the newest version
that works end-to-end (verified: 3.8 runs cleanly, 3.9 builds but crashes at startup, 3.11 fails to
install). PyPlanet also forks subprocesses per pool at startup, which needs a Unix-like OS. The
Dockerfile builds a `python:3.8-slim` Linux image so none of that depends on what's installed on your
host.

Relatedly: `peewee_async` (also pinned) only implements async support for MySQL/PostgreSQL, not SQLite
-- `docker-compose.yml` runs a `db` (MariaDB, MySQL-protocol-compatible) service for this, matching
PyPlanet's own official project template.

## Setup: local dev (recommended first)

No need to touch your real rented server while building this. `docker-compose.yml` includes an
optional `tm-dedicated` service (the official
[pyplanet/maniaplanet-dedicated](https://github.com/PyPlanet/maniaplanet-docker) image, maintained by
the PyPlanet team) that runs a real Trackmania² Stadium dedicated server in a container, purely for
development. PyPlanet controls it over XML-RPC and the game client can join it directly — no native
install needed.

1. Register a free dedicated server login at
   [maniaplanet.com/account/dedicated-servers](https://maniaplanet.com/account/dedicated-servers) —
   make up any name/password, it's separate from your normal player login.
2. Copy the env template and fill it in:

   ```bash
   cp .env.example .env
   ```

   Set `PYPLANET_OWNER_LOGIN` to your own ManiaPlanet login, `TM_DEDICATED_LOGIN`/`_PASSWORD` to the
   login from step 1, and `TM_DEDICATED_FORCE_IP` to your machine's LAN IP (`ipconfig`, the
   Ethernet/Wi-Fi adapter's IPv4 — needed so the master server registers a join address your own game
   client can actually reach). Leave everything else as-is.
3. Start everything:

   ```bash
   docker compose --profile dev up --build
   ```

   First boot downloads the TMStadium title pack, so give it a minute.
4. Join in-game via the manialink address bar (`#join=<your-dedicated-login>@TMStadium@nadeo`), then
   `/claim <code>` in chat (the code is printed in PyPlanet's logs) to give yourself admin rights.
5. Edit `apps/betpluggin/*.py` freely — the folder is bind-mounted into the container, so
   `docker compose restart betpluggin` picks up changes (no rebuild needed unless you touch
   `requirements.txt`). Note that `restart` does *not* reload `.env`; use
   `docker compose up -d --force-recreate betpluggin` after changing environment variables.

### ⚠️ Real payments don't work against the local dev dedicated server

The local Docker dedicated server (`tm-dedicated`) can't actually process `SendBill`/`Pay` -- clicking
a quick-bet button (or `/bet`) creates the `Bet` row and the confirmation-popup chat message, but the
bill immediately fails with:

```
ManiaPlanet.BillUpdated: (2, 6, 'Error: Transaction cannot be created : creator is not validated.', 0)
```

"Creator is not validated" means Nadeo's billing backend doesn't consider this free dev-server login
authorized to create real Planets transactions -- it's a platform-side restriction on the server
account, not a bug in this plugin. Everything up to the `SendBill` call (widget, market view, odds,
odds display, DB writes) can be fully tested locally; the payment confirmation popup itself and
anything downstream of it (`bill_updated`, payouts) can only be verified against a real, validated
dedicated server (e.g. the target ManiaServ deployment).

### ⚠️ Port conflict with the game client (this one costs hours)

**The ManiaPlanet game client itself listens on 2350 and 127.0.0.1:5000 while it is running.** Those are
also the dedicated server's defaults. A server on the default ports collides with it, and joins
*silently resolve to your own client instead of the server* — a baffling **"Pas un serveur" / "Not a
server"** error, with PyPlanet reporting *"Dedicated seems to be a gameclient!"*. It looks exactly like
a Docker networking problem (it isn't — TCP and UDP forwarding both work fine end-to-end) and costs
hours if you don't know to look for it. This is why `docker-compose.yml` and
`docker/tm-dedicated-config.txt` use **2360 / 3460 / 5010** instead of the defaults.

If you ever add a service on the default ports, check what's holding them first:

```bash
powershell -Command "Get-NetTCPConnection -LocalPort 2350,5000 -State Listen | ForEach-Object { (Get-Process -Id $_.OwningProcess).Name }"
```

## Setup: pointing at a real rented server (e.g. ManiaServ)

1. Get the dedicated server's XML-RPC host/port and a SuperAdmin/Admin login+password from your host.
   > If your host runs a fully managed PyPlanet for you (own control panel, curated plugin list, no
   > exposed RPC credentials or FTP access) you likely can't just point this project at it — you'll need
   > to ask their support to either add this plugin's folder to their PyPlanet's `apps/` directory, give
   > you FTP/SFTP access to it, or expose XML-RPC connection details so you can run this project's own
   > PyPlanet instead of theirs. Only worth asking once BetPluggin is actually ready.
2. In `.env`, replace `PYPLANET_RPC_HOST`/`_PORT`/`_USER`/`_PASSWORD` with those real values (the
   `TM_DEDICATED_*` variables aren't needed for this, they're dev-server-only).
3. Start without the `dev` profile so the local dedicated server never runs:

   ```bash
   docker compose up --build
   ```

## Notes

- The MySQL data lives in the `db-data` docker volume, not on your host filesystem.
- `//settings` in-game lets an admin tweak `betting_window_seconds`, `quick_bet_amounts`,
  `bet_minimum_stake` and `bet_maximum_stake` without touching code.
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
