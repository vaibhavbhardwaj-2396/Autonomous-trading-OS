# Setup Checklist

Three things need to happen on your side before the agent can go live. None of them can be
done by Claude directly — they all require you logging into your own accounts.

## 1. Create a Zerodha Kite Connect API app

1. Go to https://developers.kite.trade and log in with your Zerodha credentials.
2. Click "Create new app". Fill in:
   - App name: anything, e.g. `trading-agent`
   - App type: "Connect"
   - Redirect URL: can be a placeholder for now, e.g. `https://127.0.0.1` — it matters for
     the login flow but not for a first setup pass.
3. Once created, you'll see an **API key** and can generate an **API secret**. Copy both —
   the secret is only shown once.
4. Order execution is currently free for personal use; live market data via the API costs
   ₹500/month if you want it (there's a free/delayed alternative we can use for research
   instead — to be decided when we build the research module).
5. Send me the API key and secret **only when we're ready to wire them into the VPS as
   environment variables** — never paste them into a git repo or a chat message that gets
   committed anywhere. We'll put them directly into a `.env` file on the VPS.

## 2. Create a Telegram bot

1. In Telegram, message **@BotFather**.
2. Send `/newbot`, give it a name and a username (must end in `bot`, e.g. `vb_trading_agent_bot`).
3. BotFather replies with a **bot token** — copy it.
4. Start a chat with your new bot (search its username, hit Start) so it's allowed to message
   you.
5. Get your **chat ID**: message @userinfobot (or @getidsbot) and it will reply with your
   numeric Telegram user ID — that's your `TELEGRAM_CHAT_ID`.

## 3. Provision a VPS with a static IP

A VPS is a small rented computer that's on 24/7 with its own fixed IP address — that fixed
IP is what Zerodha needs to whitelist, and it's what keeps the agent running when your
laptop is off. You control it entirely from a terminal (no screen needed).

Recommended: **DigitalOcean** (cleanest UI for a first VPS). Steps:

1. Go to digitalocean.com, sign up, add a payment method.
2. Click "Create" → "Droplet."
3. Image: Ubuntu 24.04. Plan: the cheapest one (~$4-6/month is plenty). Region: Bangalore
   (BLR1) if offered, for lower latency to NSE — not critical for this strategy, but free.
4. Set a root password (simplest to start with).
5. Click Create. Within about a minute you'll see its IP address, e.g. `164.90.XXX.XXX` —
   that's your static IP.
6. From your Mac's Terminal app: `ssh root@164.90.XXX.XXX` (your real IP), enter the
   password. Landing at a command prompt means it's working.

(Hetzner Cloud or AWS Lightsail work the same way if you prefer either — same steps, just a
different signup page.)

## 4. Point a subdomain at the VPS (you already have bhardwajvaibhav.com — use it) — DONE

Kite Connect requires the login redirect URL to be `https://`, and having a real domain
makes this simple. Status: an A record for `tradingagent.bhardwajvaibhav.com` → the VPS's IP
(`65.20.81.119`) has already been created in Netlify DNS. Nothing left to do here.

`docs/VPS_DEPLOY.md` uses this hostname to get free auto-renewing HTTPS via Caddy, and it's
what gets registered as the Kite redirect URL.

Once this DNS record resolves (`ping tradingagent.bhardwajvaibhav.com` should show your VPS's IP),
you're ready for `docs/VPS_DEPLOY.md`.

Once you have all three (Kite key+secret, Telegram token+chat ID, VPS IP + subdomain), let
me know and we'll wire everything together and do a small live test.
