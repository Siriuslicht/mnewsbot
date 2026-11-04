# marketnewsfeed-telegram-bot

A small service that polls the [MarketNewsFeed](https://marketnewsfeed.com) API and
posts every new financial news item to a Telegram channel, in the original English.

## How it works

```
APScheduler (every POLL_INTERVAL s, never overlapping, one instance via file lock)
  └─ NewsPoller.run_once()                 app/scheduler.py
       1. MarketNewsClient.fetch_latest()  app/services/marketnews.py   GET /api/feed?limit=50
       2. first run + INITIAL_SYNC_MODE    store all hids, post nothing
       3. NewsStorage.filter_new()         app/services/storage.py      skip hids already stored
       4. TelegramPublisher.publish_news() app/services/telegram.py     retries + backoff
       5. NewsStorage.mark_processed()     only after Telegram accepted the post
FastAPI                                    app/main.py                  GET /  /health  /ready
```

| Path | Responsibility |
| --- | --- |
| `app/config.py` | Typed, validated settings from env / `.env` |
| `app/database/` | Engine (SQLite or PostgreSQL), `processed_news` table |
| `app/services/marketnews.py` | HTTP + response validation only |
| `app/services/storage.py` | "Have we handled this hid yet?" |
| `app/services/telegram.py` | Permission check, safe formatting, retrying sender |
| `app/scheduler.py` | Combines the services; never lets an error stop polling |
| `app/utils/lock.py` | Guarantees a single scheduler |
| `app/utils/logger.py` | Text/JSON logs, token redaction, optional file rotation |
| `app/main.py` | Startup/shutdown order and health endpoints |

### Behaviour you can rely on

- **No flood on first start.** With `INITIAL_SYNC_MODE=true` (default), an empty database is filled with the current 50 ids and nothing is posted. After that, only news published from then on is posted.
- **No duplicates across restarts.** `hid` is a unique column on a persistent volume and is checked before every post.
- **Failures are retried, never lost.** An `hid` is saved only after Telegram accepts it. Network errors and timeouts retry with exponential backoff (1s, 2s, 4s…), and `RetryAfter` waits exactly as long as Telegram asks. If Telegram is still failing, the cycle stops and the next poll picks up where it left off.
- **One bad message can't block the queue.** If Telegram permanently rejects one item (for example a formatting error), it is logged, recorded and skipped.
- **Fails fast on bad config.** Startup checks the token, the channel, and that the bot is an admin with *Post messages*. If any check fails, the app exits with a clear log line.
- **Graceful shutdown.** `docker stop` lets the message being sent finish (up to `SHUTDOWN_TIMEOUT`), then exits without `CancelledError` tracebacks.
- **Known limit (at-least-once delivery):** if the process dies in the millisecond between Telegram accepting a post and the database write, or Telegram times out after actually delivering, that one item can appear twice. That's the safer trade-off than losing news.

## Configuration

Copy `.env.example` to `.env` and fill in the two required values. Every setting can also be passed as a real environment variable, and those take priority over `.env`.

| Variable | Default | Notes |
| --- | --- | --- |
| `BOT_TOKEN` | — | **Required.** From @BotFather |
| `CHANNEL_ID` | — | **Required.** `@channel_username` or `-100…`. `t.me/…` links are normalised |
| `POLL_INTERVAL` | `30` | Seconds, 5–3600 |
| `INITIAL_SYNC_MODE` | `true` | See above |
| `POST_DELAY` | `3` | Seconds between posts (Telegram allows ~20/min per channel) |
| `DATABASE_URL` | `sqlite:///./database.db` | Docker forces `sqlite:////app/data/database.db` |
| `ENVIRONMENT` | `development` | `production` disables `/docs` |
| `LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `LOG_FORMAT` | `text` | `json` in Docker |
| `LOG_FILE` | empty | Optional rotating file (10 MB × 5) for non-Docker runs |
| `TELEGRAM_MAX_ATTEMPTS` | `4` | Per message, for transient errors |
| `SHUTDOWN_TIMEOUT` | `15` | Seconds to finish an in-flight post on shutdown |

Settings are validated on startup. A malformed token, a bad channel id or an out-of-range interval stops the app with a clear message.

## Telegram bot setup

1. In Telegram, open **@BotFather**, send `/newbot`, and copy the token.
2. Recommended in @BotFather: `/setjoingroups` → *Disable* (so nobody can add the bot to random groups).
3. Channel → *Administrators* → *Add admin* → your bot → enable **only** *Post messages*.
4. `CHANNEL_ID`: public channel → `@channel_username` (no `t.me/`). Private channel → the numeric `-100…` id.

## Health endpoints

| Endpoint | Meaning | Used by |
| --- | --- | --- |
| `GET /` | `{"status": "running"}` | humans |
| `GET /health` | process alive **and** database reachable (503 otherwise) | Docker healthcheck |
| `GET /ready` | database + Telegram reachable + a poll succeeded recently, plus `last_cycle_at`, `last_error`, `posted_since_start` | uptime monitoring |

`/health` deliberately ignores Telegram and MarketNewsFeed: restarting the container can't fix their outages. Point an external monitor (UptimeRobot, Better Stack…) at `/ready` through a reverse proxy if you want alerts.

## Local development

Requires Python 3.12+.

```bash
python3 -m venv .venv
```

```bash
source .venv/bin/activate
```

```bash
pip install -r requirements-dev.txt
```

```bash
cp .env.example .env
```

```bash
uvicorn app.main:app --port 8000
```

Run the tests (no network, no Telegram, no real `.env` values used):

```bash
pytest
```

Check dependencies for known vulnerabilities:

```bash
pip-audit -r requirements.txt
```

Send a test message to the channel (does not touch the database):

```bash
python -m scripts.send_test_message
```

```bash
python -m scripts.send_test_message --latest
```

> Never run the local bot and the Docker bot against the same channel at the same time. They use different databases, so both would post every item.

## Production deployment

See **[DEPLOYMENT.md](DEPLOYMENT.md)** for the full VPS guide: server setup, Docker install, firewall and SSH hardening, deploys with automatic rollback, monitoring and alerts, backups and restore, cron, and CI/CD.

Quick reference:

```bash
docker compose up -d --build
```

| Script | Purpose |
| --- | --- |
| `scripts/deploy.sh` | pull, build, health-gated restart, automatic rollback |
| `scripts/backup.sh` / `scripts/restore.sh` | online SQLite backup / verified restore |
| `scripts/monitor.sh` | cron watchdog, Telegram alerts, heartbeat |
| `scripts/reliability-test.sh` | restart / crash / network-outage / reboot checks |

## Security notes

- **Secrets:** `.env` is git-ignored and docker-ignored, so it's never baked into the image. The token is a `SecretStr` and is scrubbed from every log line, including library error messages. `/ready` never returns exception text.
- **If the token leaks:** @BotFather → `/revoke` → put the new token in `.env` → `docker compose up -d`.
- **Bot permissions:** the bot only needs *Post messages*. It has no command handlers and never reads incoming messages.
- **Container:** non-root user (uid 10001), read-only filesystem except the data volume and `/tmp`, all Linux capabilities dropped, `no-new-privileges`, memory and process limits, init process for signal handling.
- **Network:** port 8000 is bound to `127.0.0.1` only, and `/docs` is disabled in production. If you expose `/ready` publicly, do it through a reverse proxy with TLS. Firewall the rest: `sudo ufw allow OpenSSH && sudo ufw enable`. Note that Docker-published ports bypass ufw, which is another reason the port is bound to localhost.
- **Dependencies:** exact versions are pinned. Run `pip-audit -r requirements.txt` before each deploy, and rebuild regularly to pick up base-image security patches: `docker compose build --pull && docker compose up -d`.

## Database schema and migrations

One table, `processed_news(id, hid UNIQUE, processed_at)`, created on startup with SQLAlchemy `create_all`. That is idempotent and never changes an existing table, so restarts are safe. There is no migration tool yet on purpose. Add Alembic before the first schema change, because `create_all` would silently skip new columns on existing databases.
