# Beginner's Guide: Setting Up Your Paper Challenge Agent
### Step-by-step, no programming skills required

---

## Step 1: Create a Telegram Bot

1. Open Telegram on your phone or computer.
2. Search for **@BotFather** (official Telegram bot for creating bots).
3. Send the message: `/newbot`
4. BotFather will ask for a **name** — type something like: `My Challenge Agent`
5. BotFather will ask for a **username** — type something like: `my_challenge_agent_bot` (must end with `bot`)
6. BotFather will reply with a **token** that looks like:
   ```
   7123456789:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
7. **Copy this token** — you'll need it in Step 4. Do not share it with anyone.

---

## Step 2: Register on Render (Free Cloud Hosting)

1. Go to **https://render.com** in your browser.
2. Click **"Get Started for Free"**.
3. Sign up with GitHub, Google, or email.
4. Confirm your email if prompted.

---

## Step 3: Upload the Project Files

### Option A: Using GitHub (Recommended)

1. Go to **https://github.com** and sign in (or create a free account).
2. Click **"New repository"** (the `+` button in the top right).
3. Name it something like `challenge-agent`.
4. Keep it **Private**, click **"Create repository"**.
5. Upload all project files by dragging and dropping the entire project folder into the repository page, then click **"Commit changes"**.

### Option B: Directly in Render

1. On Render, go to **Dashboard** → **New** → **Web Service**.
2. Choose **"Build and deploy from a Git repository"**.
3. Connect your GitHub account if not already connected.
4. Select the repository you created in Option A.

---

## Step 4: Set Your Telegram Token (Environment Variable)

**Important**: Never paste your token directly into the code.

1. In Render, go to your service → **Environment**.
2. Click **"Add Environment Variable"**.
3. Add these variables:

| Key | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Your token from Step 1 (e.g., `7123456789:AAH...`) |
| `AGENT_MODE` | `PAPER_CHALLENGE` |
| `BEGINNER_EXPLANATIONS` | `true` |

4. Click **"Save Changes"**.

---

## Step 5: Deploy

1. In your Render service, go to **Settings**.
2. Set:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `python main.py`
3. Click **"Save Changes"**.
4. Go to **"Manual Deploy"** → **"Deploy latest commit"**.
5. Wait 1–2 minutes. The log should show:
   ```
   Telegram bot created
   Scheduler started (every 15 minutes)
   Agent mode: PAPER_CHALLENGE
   ```

---

## Step 6: Verify It Works

1. Open Telegram and find your bot by its username (e.g., `@my_challenge_agent_bot`).
2. Send `/start` — the bot should greet you.
3. Send `/help` — you should see a list of commands.
4. Send `/status` — shows current balance and regime info.
5. Send `/portfolio` — shows your $1000 virtual balance.

If the bot doesn't respond, check the Render logs for errors.

---

## Step 7: Getting Your Chat ID (for Proactive Notifications)

The bot can send you signals proactively (not just when you ask). To enable this:

1. Send any message to your bot in Telegram.
2. Open this URL in your browser (replace `YOUR_TOKEN` with your actual token):
   ```
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```
3. Find `"chat":{"id":123456789}` in the response — that number is your Chat ID.
4. In Render → Environment, add:
   - **Key**: `TELEGRAM_CHAT_ID`
   - **Value**: your chat ID number
5. Redeploy.

---

## Step 8: Using the Bot

### Daily Usage
The bot will automatically:
- Check markets every 15 minutes
- Send you a morning report at 08:00 (Jerusalem time)
- Send an evening report at 22:30
- Alert you only when action is needed

### When You Get a Signal
1. Read the signal carefully — it shows what to do and why.
2. If you agree, send `/confirm` to record it in your virtual portfolio.
3. If you disagree, send `/reject` to skip it.
4. Then manually execute the trade on Kraken (the bot does NOT place real trades).

### Useful Commands
| Command | What it does |
|---|---|
| `/status` | See current market regime and balance |
| `/portfolio` | Full portfolio details |
| `/signal` | View latest signals |
| `/history` | See past trades |
| `/pause` | Stop receiving signals |
| `/resume` | Start receiving signals again |
| `/settings` | View/change settings |
| `/settings beginner false` | Turn off beginner explanations |

---

## Step 9: Pausing and Stopping

- **Pause signals**: Send `/pause` in Telegram. The bot keeps watching markets but won't send signals.
- **Resume**: Send `/resume`.
- **Stop the service entirely**: In Render → your service → **"Suspend"**.

---

## Step 10: Updating the Code

If you need to update the bot:

1. Push new code to your GitHub repository.
2. Render will automatically detect changes and redeploy.
   - Or go to Render → **"Manual Deploy"** → **"Deploy latest commit"**.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Bot doesn't respond | Check Render logs for errors. Make sure `TELEGRAM_BOT_TOKEN` is set correctly. |
| No signals after hours | This is normal — the bot only sends signals during active hours (08:00–23:00 Jerusalem time). |
| "No data" errors | Market data APIs may be temporarily unavailable. The bot will retry automatically. |
| Balance seems wrong | Remember: only `/confirm`-ed trades affect the virtual balance. |

---

## Important Reminders

- This is a **Paper Challenge** — the bot never places real trades.
- You execute trades manually on Kraken based on the bot's recommendations.
- The bot is conservative by design — "no signal" is often the right decision.
- Your token is a secret — never share it or commit it to code.
