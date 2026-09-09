# VPS Deployment

Covers: getting HTTPS working for the Kite login callback (required — Kite rejects plain
`http://` redirect URLs), running the callback server persistently, and scheduling the daily
login reminder. Trading-cycle scripts (research/execute/journal) will get their own cron
entries here once they're built (see `docs/SETUP.md` task list).

## 0. Prerequisites
- A VPS with a static public IP (this project's box: Vultr, Mumbai, `trading-agent-v1`).
- You're logged in as **root** (no separate SSH-key/limited-user setup was used, so every
  path below is under `/root`, not `/home/someuser`).
- This repo cloned onto the VPS, e.g. `git clone <your-repo-url> /root/trading-agent`.
- Your Kite API key + secret and Telegram bot token + chat ID (from `docs/SETUP.md`).

## 1. Install dependencies

```bash
sudo apt update && sudo apt install -y python3-pip python3-venv unzip
```

Caddy isn't in Ubuntu's default repos — add its official one:

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
```

Then the Python environment:

```bash
cd /root/trading-agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. Set environment variables

```bash
cp .env.example .env
nano .env   # fill in KITE_API_KEY, KITE_API_SECRET, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
chmod 600 .env
```

Scripts load these from the environment — export them in the systemd unit (below) rather
than committing `.env` anywhere.

## 3. Get HTTPS via your domain (tradingagent.bhardwajvaibhav.com)

Kite Connect requires an `https://` redirect URL. Since you already added a DNS A record
pointing `tradingagent.bhardwajvaibhav.com` at this VPS's IP (see `docs/SETUP.md` step 4), Caddy can
get a real, free, auto-renewing Let's Encrypt certificate for it automatically — no manual
cert management needed.

Create `/etc/caddy/Caddyfile`:

```
tradingagent.bhardwajvaibhav.com {
    reverse_proxy 127.0.0.1:5000
}
```

Then:

```bash
sudo systemctl reload caddy
```

Caddy handles the certificate the first time it gets a request. Test it once the callback
server (next step) is running:
`curl https://tradingagent.bhardwajvaibhav.com/healthz` should return `ok`.

(If your domain's DNS hasn't propagated yet, this will fail with a connection or cert error
— wait a bit and retry; `ping tradingagent.bhardwajvaibhav.com` should already show the VPS's IP
before this will work.)

## 4. Register the redirect URL with Kite

Go to developers.kite.trade → your app → set the redirect URL to:
`https://tradingagent.bhardwajvaibhav.com/callback`

## 5. Run the callback server persistently (systemd)

Create `/etc/systemd/system/kite-callback.service`:

```ini
[Unit]
Description=Kite login callback server
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/trading-agent/scripts
EnvironmentFile=/root/trading-agent/.env
ExecStart=/root/trading-agent/venv/bin/python kite_callback_server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now kite-callback
sudo systemctl status kite-callback   # should show "active (running)"
```

## 6. Schedule the daily login reminder

```bash
crontab -e
```

Add (adjust the venv path, and times are IST — set the VPS timezone to Asia/Kolkata first
with `sudo timedatectl set-timezone Asia/Kolkata`):

```
# 8:30am IST, Mon-Fri: send today's Kite login link via Telegram if not already logged in
30 8 * * 1-5 cd /root/trading-agent/scripts && /root/trading-agent/venv/bin/python login_reminder.py >> /root/trading-agent/logs/login_reminder.log 2>&1
```

```bash
mkdir -p /root/trading-agent/logs
```

## 7. Test end to end

```bash
cd /root/trading-agent

# 1. HTTPS + callback server reachable
curl https://tradingagent.bhardwajvaibhav.com/healthz     # expect: ok

# 2. Credentials loaded, session state
venv/bin/python scripts/status.py

# 3. Telegram working
venv/bin/python scripts/telegram_notify.py

# 4. Send today's login link
venv/bin/python scripts/login_reminder.py
```

Tap the link on your phone, log into Zerodha, confirm the browser shows "Logged in — the
agent has a session for today." Then re-run the status check:

```bash
venv/bin/python scripts/status.py
```

You want **READY — credentials, session and broker all good**, with your name and equity
balance shown. That's the whole daily-auth loop working end to end.

`status.py` is the go-to health check from here on — run it any time to see whether the
agent is in a position to operate.
