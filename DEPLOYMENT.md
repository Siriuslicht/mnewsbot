# VPS deployment and operations

Everything needed to run the bot on one Ubuntu server with Docker Compose.
No reverse proxy, no Kubernetes, no extra databases: one container, one volume, cron.

## 1. Server

| | Recommendation |
| --- | --- |
| OS | **Ubuntu 24.04 LTS** (22.04 works too) |
| CPU / RAM | **1 vCPU, 1 GB RAM**. The bot uses ~70 MB, but `docker build` needs headroom |
| Disk | 15–20 GB SSD (Docker images ~1 GB, logs capped at 50 MB, backups tiny) |
| Location | Any; Europe is closest to Telegram's main data centres |
| Provider | Any VPS (Hetzner CX22, DigitalOcean Basic, Vultr…) |

### Network

| Direction | Port | Why |
| --- | --- | --- |
| Inbound | **22/tcp** (SSH) | administration and CI deploys. **The only open port** |
| Outbound | 443/tcp | Telegram API, marketnewsfeed.com, GitHub, Docker Hub, PyPI, heartbeat |
| Outbound | 53, 123 | DNS, time sync |

The health endpoint (port 8000) is bound to `127.0.0.1`, so it's reachable only on the server.
External monitoring uses an outbound **heartbeat** (section 7) instead of an open port.

## 2. First-time server setup

Run as root (or with `sudo`) on a fresh server.

### Update Ubuntu and enable automatic security updates

```bash
sudo apt-get update && sudo apt-get -y upgrade
```

```bash
sudo apt-get install -y unattended-upgrades git curl ufw fail2ban
```

```bash
sudo dpkg-reconfigure -plow unattended-upgrades
```

### Create a non-root user

```bash
sudo adduser --disabled-password --gecos "" deploy
```

```bash
sudo mkdir -p /home/deploy/.ssh && sudo cp ~/.ssh/authorized_keys /home/deploy/.ssh/ && sudo chown -R deploy:deploy /home/deploy/.ssh && sudo chmod 700 /home/deploy/.ssh && sudo chmod 600 /home/deploy/.ssh/authorized_keys
```

Check that `ssh deploy@<server-ip>` works **before** hardening SSH below.

### Harden SSH (key-only, no root login)

```bash
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\nPermitRootLogin no\nMaxAuthTries 3\n' | sudo tee /etc/ssh/sshd_config.d/99-hardening.conf
```

```bash
sudo sshd -t && sudo systemctl restart ssh
```

`fail2ban` (installed above) blocks repeated SSH login failures with its default config.

### Firewall: allow only SSH

```bash
sudo ufw default deny incoming && sudo ufw default allow outgoing && sudo ufw allow OpenSSH && sudo ufw --force enable
```

Docker-published ports bypass ufw. That's why `docker-compose.yml` publishes 8000 on `127.0.0.1` only. Keep it that way.

### Install Docker Engine and the Compose plugin (official repository)

```bash
sudo install -m 0755 -d /etc/apt/keyrings && sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc && sudo chmod a+r /etc/apt/keyrings/docker.asc
```

```bash
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
```

```bash
sudo apt-get update && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
```

Start Docker now **and at every boot** (this is what brings the bot back after a reboot):

```bash
sudo systemctl enable --now docker containerd
```

Let the `deploy` user run Docker. Note that membership in the `docker` group is root-equivalent, so keep this user's key safe:

```bash
sudo usermod -aG docker deploy
```

Verify (log in again as `deploy` first):

```bash
docker --version && docker compose version
```

## 3. Install the bot

As the `deploy` user:

### Clone

Public repository:

```bash
git clone https://github.com/<you>/mnewsbot.git ~/mnewsbot
```

Private repository: create a read-only deploy key on the server, add it under GitHub → repo → *Settings → Deploy keys*, then clone over SSH:

```bash
ssh-keygen -t ed25519 -N "" -f ~/.ssh/github_deploy -C "mnewsbot-vps" && cat ~/.ssh/github_deploy.pub
```

```bash
printf 'Host github.com\n  IdentityFile ~/.ssh/github_deploy\n  IdentitiesOnly yes\n' >> ~/.ssh/config
```

```bash
git clone git@github.com:<you>/mnewsbot.git ~/mnewsbot
```

### Configure `.env`

```bash
cd ~/mnewsbot && cp .env.example .env && chmod 600 .env && nano .env
```

Required: `BOT_TOKEN`, `CHANNEL_ID`. Recommended: `ALERT_CHAT_ID`, `HEARTBEAT_URL` (section 7).
Docker forces `ENVIRONMENT=production`, `LOG_FORMAT=json` and the database path, so you don't set those.

> **Only one bot may post to the channel.** Stop any copy on your laptop before starting the server. They use different databases, so both would post every item.

### Start

```bash
docker compose up -d --build
```

```bash
docker compose ps
```

Within about 30 s the status shows `(healthy)`. The first start on an empty volume runs **initial sync**: it stores the current 50 news ids and posts nothing.

```bash
docker compose logs --tail 20 bot
```

You should see `Telegram bot @… ready`, `Scheduler started`, `Initial sync: stored 50 existing news ids without posting`.

## 4. Day-to-day operations

| Task | Command |
| --- | --- |
| Status | `docker compose ps` |
| Health detail | `curl -s localhost:8000/ready` |
| Live logs | `docker compose logs -f --tail 100 bot` |
| Errors only | `docker compose logs --no-log-prefix bot \| grep '"level": "ERROR"'` |
| Restart | `docker compose restart bot` |
| Stop (stays down after reboot) | `docker compose stop` |
| Start again | `docker compose up -d` |
| Update to latest code | `./scripts/deploy.sh` |
| Resource usage | `docker stats --no-stream` |

### Updating

```bash
cd ~/mnewsbot && ./scripts/deploy.sh
```

`deploy.sh` does `git pull --ff-only`, keeps the running image as `:previous`, runs `docker compose up -d --build`, and waits up to 2 minutes for `healthy`. If the new version is not healthy, it **rolls back automatically** to `:previous` and sends an alert. Running `docker compose up -d --build` by hand also works; it just has no rollback.

Manual rollback to the previous image:

```bash
docker tag marketnewsfeed-telegram-bot:previous marketnewsfeed-telegram-bot:latest && docker compose up -d --no-build
```

### Logs

- JSON, one line per event, with the token scrubbed.
- Rotated by Docker: at most 5 × 10 MB, kept across restarts, removed with the container.
- Uvicorn access logs are muted, so health checks don't flood the log.

## 5. Reliability: how each failure is handled

| Failure | What happens |
| --- | --- |
| App crashes | Docker restarts it (`restart: unless-stopped`), usually within seconds |
| App hangs (health check fails 3×) | Docker marks it `unhealthy`; `monitor.sh` restarts it and alerts you |
| VPS reboot | `systemctl enable docker` starts Docker, which restarts the container. The volume keeps the history |
| MarketNewsFeed or network down | Each poll logs an error and the next one retries. No restart needed |
| Telegram down / rate limit | Retries with backoff. Unsent items stay unsaved and go out when Telegram is back |
| `docker compose stop` / `down` | Stays down, including after reboot, until you `docker compose up -d` |
| Bad config (token, channel rights) | Container exits with a clear log line. Docker retries with growing delays; fix `.env` and run `docker compose up -d` |

### Verifying it (run on the server after the first deploy)

```bash
./scripts/reliability-test.sh
```

This takes about 3 minutes and checks three scenarios:

1. **Graceful restart:** the shutdown is logged, there's no `CancelledError` noise, and the container is healthy again.
2. **Hard crash:** `SIGKILL` on the app process, then Docker restarts it automatically.
3. **Network outage:** the container is disconnected for 70 s. Errors are logged, and polling recovers on its own after reconnecting.

Each scenario also confirms that the database row count is unchanged and that initial sync did not run again.

Reboot test:

```bash
sudo reboot
```

Then SSH back in after about a minute:

```bash
cd ~/mnewsbot && ./scripts/reliability-test.sh --after-reboot
```

## 6. Backups

The only state is the list of posted `hid`s. Losing it is not a disaster: initial sync rebuilds it without flooding the channel, at worst missing or repeating a few items around that moment. Backups make it a non-event.

| | |
| --- | --- |
| What | `processed_news` table in the SQLite volume |
| How | `scripts/backup.sh`: SQLite online backup (safe while running), integrity check, gzip |
| Where | `~/mnewsbot/backups/` (mode 700), configurable with `BACKUP_DIR` |
| When | daily at 03:15 UTC (cron, below) |
| Retention | 14 days (`BACKUP_KEEP_DAYS`) |

Manual backup:

```bash
./scripts/backup.sh
```

Restore. It takes a safety backup first, stops the bot, verifies the file, swaps it in, restarts the bot and waits for health:

```bash
ls -lt backups/ | head
```

```bash
./scripts/restore.sh backups/database-YYYYMMDD-HHMMSS.db.gz
```

Off-server copy (recommended, optional): pull backups to your own machine now and then:

```bash
rsync -av deploy@<server-ip>:mnewsbot/backups/ ./mnewsbot-backups/
```

Or use your VPS provider's snapshot feature.

PostgreSQL setup instead of SQLite (`docker-compose.postgres.yml`): back up with this instead:

```bash
docker compose -f docker-compose.yml -f docker-compose.postgres.yml exec -T db pg_dump -U bot marketnews | gzip > backups/pg-$(date +%F).sql.gz
```

## 7. Monitoring

Three lightweight layers, all free:

| Layer | Catches | Tool |
| --- | --- | --- |
| Docker healthcheck + restart policy | crashes, hangs | built in |
| `scripts/monitor.sh` every 2 min | unhealthy container (auto-restart), container stopped, `/ready` failing (Telegram unreachable, no successful poll for 5 intervals, DB down), ≥5 ERROR lines in 10 min | cron + Telegram alert to you |
| Heartbeat | **the whole server or cron dying**, which nothing on the server can report | [healthchecks.io](https://healthchecks.io) (free, built-in Telegram integration) or Better Stack heartbeats |

### Set up alerts

1. Find your personal Telegram user id with **@userinfobot**, and put it in `.env` as `ALERT_CHAT_ID=`.
2. Open your bot in Telegram and press **Start** once. Bots can't message you otherwise. Optionally use a separate bot for alerts via `ALERT_BOT_TOKEN`.
3. healthchecks.io → *New check* → period **5 min**, grace **5 min** → copy the ping URL into `.env` as `HEARTBEAT_URL=`. Add the Telegram integration there too.

Test an alert. This stops the bot, runs the monitor, then starts it again:

```bash
docker compose stop bot && ./scripts/monitor.sh; docker compose up -d bot
```

You should get "PROBLEM: container is not running", and two minutes later (from cron) "RECOVERED".
Alerts repeat at most hourly while a problem lasts.

## 8. Cron schedule

```bash
mkdir -p ~/mnewsbot/logs && crontab -e
```

Add these lines (they assume the clone lives in `~/mnewsbot`):

```cron
*/2 * * * * cd $HOME/mnewsbot && ./scripts/monitor.sh >> logs/monitor.log 2>&1
15 3 * * * cd $HOME/mnewsbot && ./scripts/backup.sh >> logs/backup.log 2>&1
0 4 * * 0 truncate -s 0 $HOME/mnewsbot/logs/monitor.log
```

The third line empties the monitor log weekly so it never grows unbounded.

## 9. CI/CD (GitHub Actions)

`.github/workflows/ci.yml`:

- **Every push and pull request:** `pytest`, `pip-audit`, Docker image build, and an import check inside the image.
- **Push to `main`:** all of the above, then SSH to the server and run `./scripts/deploy.sh`, which gives you health-gated deploys with automatic rollback.

The deploy job is skipped (CI stays green) until the secrets exist.

### One-time setup

On your laptop, create a key used **only** by GitHub Actions:

```bash
ssh-keygen -t ed25519 -N "" -f ./mnewsbot_ci -C "github-actions-deploy"
```

On the server, authorise it, restricted so it can **only** run the deploy script (no shell, no port forwarding). Paste the contents of `mnewsbot_ci.pub` in place of `<PUBLIC KEY>`:

```bash
echo 'command="cd ~/mnewsbot && ./scripts/deploy.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty <PUBLIC KEY>' >> ~/.ssh/authorized_keys
```

Get the server's host key, which pins its identity so CI can't be tricked into deploying elsewhere:

```bash
ssh-keyscan -t ed25519 <server-ip>
```

GitHub → repo → *Settings → Secrets and variables → Actions* → add:

| Secret | Value |
| --- | --- |
| `VPS_HOST` | server IP |
| `VPS_USER` | `deploy` |
| `VPS_SSH_KEY` | contents of `mnewsbot_ci` (private key) |
| `VPS_KNOWN_HOSTS` | output of `ssh-keyscan` |

Then delete the local key files (`rm mnewsbot_ci mnewsbot_ci.pub`). Optional: *Settings → Environments → production* → add yourself as a required reviewer to approve each deploy.

## 10. Security checklist

- [ ] SSH: key-only, no root login, fail2ban running
- [ ] ufw: only 22 open (`sudo ufw status`)
- [ ] `.env` is `chmod 600` and not in git (`git check-ignore .env` prints `.env`)
- [ ] Bot is a channel admin with **only** *Post messages*; @BotFather `/setjoingroups` → Disable
- [ ] Container: non-root, read-only filesystem, no capabilities, port bound to 127.0.0.1 (all in `docker-compose.yml`)
- [ ] Unattended security upgrades enabled
- [ ] Monthly: `docker compose build --pull && ./scripts/deploy.sh --no-pull` to pick up base-image patches. CI runs `pip-audit` on every push
- [ ] Token leaked? @BotFather → `/revoke`, update `.env`, then `docker compose up -d`
