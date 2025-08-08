# Reddit Referral Poster Pro v2 (ToS-friendly)

A Flask web app that logs in with Reddit OAuth, finds referral-friendly threads, and drip-posts your message with natural copy variation. It checks subreddit rules, prefers megathreads, and includes scheduling + CSV export.

---

## OPTION A: Deploy on Render (no terminal on your machine)

### 0) Prep your Reddit "web" app
1) Go to https://www.reddit.com/prefs/apps → Create "web app".  
2) Set a temporary redirect URI: `https://example.com/oauth/callback` (we’ll change it after your Render URL exists).  
3) Copy **client ID** and **client secret**.

### 1) Put this folder on GitHub
- Create a new GitHub repo and push all files in this directory.

### 2) Create the service on Render
1) In Render, click **New + → Blueprint** and select your GitHub repo (this uses `render.yaml`).  
2) In **Environment Variables** on Render, set:
   - `REDDIT_CLIENT_ID` = from step 0
   - `REDDIT_CLIENT_SECRET` = from step 0
   - `USER_AGENT` = `referral-poster-pro v2 by u/YOURNAME`
   - `FLASK_SECRET` is auto-generated
   - Leave `REDDIT_REDIRECT_URI` as the placeholder for now — we’ll update it after first deploy.

3) Click **Deploy**. When it finishes, open the app. You’ll get a Render URL like `https://your-app-xxxxx.onrender.com`.

### 3) Wire up OAuth correctly (two-pass step)
1) Take your actual Render URL and set `REDDIT_REDIRECT_URI` to:  
   `https://your-app-xxxxx.onrender.com/oauth/callback` in your Render **Environment** for this service.  
2) Go back to your Reddit "web app" settings and **add that exact redirect URI** (replace the temporary one).  
3) Back in Render, click **Manual Redeploy** (or restart) so the env takes effect.

Now visit your app and click **Login with Reddit**. Approve scopes: identity, submit, read, modconfig.

### 4) Use the app
- Choose a preset (e.g., GoPuff) → Fields auto-fill.
- Paste your base message (optional). Fill **Referral code**, **Discount %**, **Referral link**.
- Keep **Only megathreads** on.
- Start with **Dry run**. When happy, uncheck and press **Start**.
- Use low **posts per hour** (e.g., 0.5–1), **per-sub cap** = 1, total = 5–10.
- Export CSV for a log of actions.

---

## Natural copy variation (what v2 adds)
- Multiple openers, CTAs, closers, and synonym pools (see `SYNONYMS`/`OPENERS`/`CTA_TEMPLATES`/`CLOSERS` in `app.py`).
- Tones: **concise**, **friendly**, **helpful** (adds a small tip line).
- Emoji usage: none, low (default), normal.
- Optional mod-friendly disclaimer line.
- Deterministic testing: set **Random seed** in the UI to reproduce a run.

> Intent: variety for readability and to avoid looking like a bot, **not** to bypass moderation. Posting where it isn’t allowed or blasting frequency will still get removed/banned.

---

## Local run (optional)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export FLASK_SECRET="change-me"
export REDDIT_CLIENT_ID="your_id"
export REDDIT_CLIENT_SECRET="your_secret"
export REDDIT_REDIRECT_URI="http://localhost:5000/oauth/callback"
export USER_AGENT="referral-poster-pro v2 by u/YOURNAME"
python app.py
```

## Replit (browser-only dev)
- New Repl → Python (Flask), upload files, add Secrets (env vars), click Run.
- Use your Replit URL + `/oauth/callback` in both the env var and Reddit app settings.

---

## Tips
- Always read subreddit rules; keep "Only megathreads" on.
- Keep volume modest; contribute to communities beyond referrals.
- For production: store refresh tokens in a DB and add user auth.
