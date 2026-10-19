# ZBR FF GLORY

Web dashboard for the squad engine. Replaces the old terminal `main.py` menu —
every client (escort/server) is now created, started, stopped and watched from
the browser instead of stdin.

## Layout

```
zbr_ff_glory/
  app/
    main.py              FastAPI entrypoint
    core/
      auth.py             session cookie auth (HMAC signed, no DB)
      clients.py           client (escort) CRUD -> clients.json
    api/
      auth.py              POST /api/auth/login, /logout
      clients.py            /api/clients CRUD + start/stop/restart/slots
      live.py                /ws/live WS feed, /api/stats, /api/logs
      pages.py                serves login.html / dashboard.html
    web/
      templates/            login.html, dashboard.html
      static/css/theme.css    dark/light tokens + components
      static/js/              api.js, toast.js, dashboard.js (SPA)
  protocols/
    engine.py              full protocol stack (ported from main.py) +
                            client orchestration (start_client/stop_client)
    FYT*_pb2.py, thunderFF_pb2.py, data_pb2.py, reqClan_pb2.py,
    encode_id_clan_pb2.py, StartMatch_pb2.py, message_ids.py
                            untouched protobuf/message-id modules
  accounts.json           bot account pool (uid/password pairs) — persists
  devices.json             per-account device fingerprints — persists
  clients.json              every client/escort you create — persists
  token_cache.json, clan_state.json, glory_count.json
                            engine runtime caches — persist
  requirements.txt
```

## Setup

```bash
cd zbr_ff_glory
python3 -m venv venv
source venv/bin/activate        # venv\Scripts\activate on Windows
pip install -r requirements.txt
```

Set admin credentials (defaults to `admin` / `zbrglory2026` if skipped —
change this before exposing the port to anyone):

```bash
export ZBR_ADMIN_USER=youradmin
export ZBR_ADMIN_PASS=your-strong-password
export ZBR_SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

## Run

**Always run from the project root** — `accounts.json`, `devices.json`, etc.
are read relative to the working directory, same as the original script.

```bash
python3 -m app.main
# or
uvicorn app.main:app --host 0.0.0.0 --port 20333
```

Open `http://localhost:20333` — redirects to `/login`.

## Deploy

```bash
uvicorn app.main:app --host 0.0.0.0 --port 20333 --workers 1
```

Keep `--workers 1`. The engine's live state (`bot_state`, running account
workers, team formation) lives in one process's memory — multiple workers
would each run their own disconnected copy of every client. Put this behind
nginx/caddy for TLS; don't run it workers>1 or behind a load balancer that
round-robins requests across processes.

For always-on, wrap it in systemd or run under `screen`/`tmux` — same as you'd
have done with the old terminal script, just one process instead of
interactive stdin.

## How the old terminal flow maps to the web

| Old terminal action | Web equivalent |
|---|---|
| `1) Mere saath khelo` + enter your UID | New Client modal → mode **Play with me** → enter Game UID |
| `2) Normal (4 bots)` | New Client modal → mode **Normal** |
| Clan UID prompt | New Client modal → Clan UID field (optional) |
| Bot name prefix prompt | New Client modal → Bot name prefix field |
| Accounts auto-generated if short | Same — happens automatically on client create if the account pool doesn't have enough unused accounts |
| Live printed status (`[TEAM] IN_TEAM (3/4)...`, `[+]`, `[-]`) | Live Logs page + per-client log panel, streamed over `/ws/live` |
| No way to see which of the 4 bots was doing what | Client detail page → 4 slot cards: **Filling/Joining**, **Standing By**, **In Match**, or **Connecting/Error**, live |
| `Ctrl+C` to stop everything | Per-client **Stop** button (stops only that squad) |
| Restarting meant re-running the script | Per-client **Restart** button |
| `GLORY_COUNT` printed to console | GLORY pill in the top bar, live |

## Notes / known limits

- One process = one engine instance. Don't run multiple `uvicorn` workers.
- Team-formation state (`_team_state`) is now scoped per client, so running
  several clients (escorts) at once is safe — each squad's peer list, leader
  election and match/team code are isolated and won't bleed into another
  client's squad.
- `TEAM_BOTS` / `USER_GAME_UID` / `CLAN_ID` are still read as module globals
  at the moment a client's workers spawn (see comment in
  `protocols/engine.py` → `start_client`). This is fine for normal use
  (clients started one at a time, a few seconds apart); if you script
  simultaneous creation of many clients within the same second, a late
  global write from client B could theoretically be read by a client A
  worker that hasn't finished connecting yet. Low-probability, cosmetic
  (wrong bot count / wrong captain for one squad, not data loss) — flagging
  it so you know where it lives if you ever see it.
- Account pool: `ensure_bot_accounts()` reuses unused rows from
  `accounts.json` before generating new ones, and never hands the same
  account to two running clients at once.
