# Capitol Alpha — Raspberry Pi Setup Guide

Step-by-step instructions to deploy Capitol Alpha on a Raspberry Pi
as an always-on service.

## Prerequisites

- Raspberry Pi 4 or 5 (2GB+ RAM)
- Raspberry Pi OS (64-bit recommended)
- Internet connection
- Python 3.11+

## Step 1: Clone the Repository

```bash
cd ~
git clone https://github.com/nitin-bhaskaran/capitol-alpha.git
cd capitol-alpha
```

## Step 2: Set Up Python Environment

```bash
# Install Python venv if not already present
sudo apt update
sudo apt install python3-venv python3-pip -y

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## Step 3: Configure API Keys

```bash
# Copy the template
cp config/config_template.yaml config/config.yaml

# Edit with your keys
nano config/config.yaml
```

You need to fill in:

### Telegram Bot Token
1. Open Telegram, search for `@BotFather`
2. Send `/newbot`
3. Follow the prompts — name it something like "Capitol Alpha Bot"
4. Copy the token into `bot_token`
5. Search for `@userinfobot`, send `/start` — it will reply with your chat ID
6. Copy your chat ID into `chat_id` and `admin_user_ids`

### Trading212 API Keys
1. Open the Trading212 app
2. Go to Settings > API (beta)
3. Click "Generate API Key"
4. Name: `capitol-alpha`
5. Permissions: enable "Account data", "Orders", "Portfolio"
6. IP restriction: either unrestricted or your Pi's public IP
7. Copy the API Key and API Secret into config
8. **IMPORTANT**: Set `environment: "demo"` until you trust the system!

## Step 4: Test Run

```bash
# Activate venv
source .venv/bin/activate

# Run once to test
python -m src.main
```

You should see:
- Log messages about fetching House/Senate data
- New trades being ingested
- Signals being scored
- Telegram alerts being sent (if configured)
- Dashboard available at http://<your-pi-ip>:5055

Press Ctrl+C to stop.

## Step 5: Set Up as a systemd Service

This ensures Capitol Alpha starts automatically on boot and restarts on crash.

```bash
# Create the service file
sudo nano /etc/systemd/system/capitol-alpha.service
```

Paste this content:

```ini
[Unit]
Description=Capitol Alpha - Congress Trades Alpha Generator
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/capitol-alpha
Environment=PATH=/home/pi/capitol-alpha/.venv/bin:/usr/bin:/bin
ExecStart=/home/pi/capitol-alpha/.venv/bin/python -m src.main
Restart=always
RestartSec=30

# Logging
StandardOutput=journal
StandardError=journal

# Security hardening
NoNewPrivileges=true
ProtectSystem=strict
ReadWritePaths=/home/pi/capitol-alpha/data /home/pi/capitol-alpha/logs

[Install]
WantedBy=multi-user.target
```

**Note:** If your Pi username is not `pi`, replace `pi` with your username throughout.

```bash
# Enable and start the service
sudo systemctl daemon-reload
sudo systemctl enable capitol-alpha
sudo systemctl start capitol-alpha

# Check status
sudo systemctl status capitol-alpha

# View logs
journalctl -u capitol-alpha -f
```

## Step 6: Accessing the Dashboard

From any device on your local network:
```
http://<your-pi-ip>:5055
```

To find your Pi's IP:
```bash
hostname -I
```

## Maintenance

### View logs
```bash
# Application logs
tail -f ~/capitol-alpha/logs/capitol_alpha.log

# systemd journal
journalctl -u capitol-alpha -f --no-pager
```

### Update the code
```bash
cd ~/capitol-alpha
git pull
sudo systemctl restart capitol-alpha
```

### Switch from Demo to Live Trading
1. Edit `config/config.yaml`
2. Change `environment: "demo"` to `environment: "live"`
3. `sudo systemctl restart capitol-alpha`

**Only do this after you've verified the system works correctly in demo mode!**

### Database backup
```bash
# The SQLite database is a single file
cp ~/capitol-alpha/data/cta.db ~/capitol-alpha/data/cta.db.backup
```

## Troubleshooting

### "No module named 'src'"
Make sure you're running from the project root directory:
```bash
cd ~/capitol-alpha
python -m src.main
```

### Telegram bot not sending messages
- Check `bot_token` is correct
- Check `chat_id` matches your Telegram user ID
- Make sure the bot has been started (send `/start` to your bot in Telegram)

### Trading212 errors
- Verify API key/secret are correct
- Check you're using the right environment (demo vs live)
- T212 API has rate limits — the client handles these automatically

### High CPU on Pi
- Increase `poll_interval_minutes` in config (default: 30)
- The House/Senate S3 downloads are the heaviest operation
