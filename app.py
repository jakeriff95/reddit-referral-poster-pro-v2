#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reddit Referral Poster (Pro v2, ToS-friendly)
- OAuth web login (no password). Refresh token stored in memory for demo.
- Drip scheduling and limits.
- CSV export of logs.
- Subreddit rule check: auto-skips subs forbidding referrals/self-promo; prefers megathreads or enforces megathread-only.
- Presets for common programs.
- **Natural copy variation engine**: template spins, synonyms, disclaimers (optional), emoji usage control.
- Deployable to Render/Replit/Docker.
"""
import os
import csv
import io
import time
import random
import threading
import datetime as dt
from typing import List, Dict, Optional
from urllib.parse import urlencode
from flask import Flask, render_template, request, jsonify, redirect, url_for, session, Response, send_from_directory, abort
from flask_cors import CORS
import praw
import prawcore

# ---- Configuration via env ----
SECRET = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_REDIRECT_URI = os.environ.get("REDDIT_REDIRECT_URI", "http://localhost:5000/oauth/callback")
DEFAULT_USER_AGENT = os.environ.get("USER_AGENT", "referral-poster-pro v2 by u/yourname")

app = Flask(__name__)
app.secret_key = SECRET
CORS(app)

# ---- In-memory state (demo) ----
STATE = {
    "refresh_token": None,
    "running": False,
    "stop_requested": False,
    "logs": [],
    "summary": {},
    "last_login_user": None,
}

def _log(level: str, event: str, **kwargs):
    entry = {"level": level, "event": event, "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())}
    entry.update(kwargs)
    STATE["logs"].append(entry)
    if len(STATE["logs"]) > 2000:
        STATE["logs"] = STATE["logs"][-2000:]

# ---- Rules heuristics ----
PROHIBIT_PATTERNS = [
    "no referrals", "no referral", "no codes", "no promo codes", "no self-promotion", "no self promotion",
    "referrals not allowed", "no affiliate", "no affiliate links"
]
MEGATHREAD_REQUIRED_PATTERNS = [
    "referrals only in", "referrals allowed only in", "post referrals only in", "megathread", "weekly thread"
]

DEFAULT_ALLOWLIST = [
    "ReferralCodes", "ReferAFriend", "ReferralTrains", "SignUpBonuses",
    "ReferralLinks", "Referrals", "Referralcodes", "TheReferralHub",
]
DEFAULT_GENERIC_QUERIES = [
    "referral", "referrals", "referral code", "referral codes",
    "promo code", "promocodes", "coupon code", "megathread", "weekly megathread"
]
PRESETS = {
    "gopuff": {
        "brand": "gopuff",
        "brand_terms": ["gopuff", "alcohol delivery", "grocery delivery"],
        "allowlist": DEFAULT_ALLOWLIST,
        "generic_terms": DEFAULT_GENERIC_QUERIES,
    },
    "uber eats": {
        "brand": "uber eats",
        "brand_terms": ["uber eats", "ubereats", "food delivery"],
        "allowlist": DEFAULT_ALLOWLIST,
        "generic_terms": DEFAULT_GENERIC_QUERIES,
    },
    "grubhub": {
        "brand": "grubhub",
        "brand_terms": ["grubhub", "food delivery"],
        "allowlist": DEFAULT_ALLOWLIST,
        "generic_terms": DEFAULT_GENERIC_QUERIES,
    },
    "doordash": {
        "brand": "doordash",
        "brand_terms": ["doordash", "food delivery"],
        "allowlist": DEFAULT_ALLOWLIST,
        "generic_terms": DEFAULT_GENERIC_QUERIES,
    },
    "instacart": {
        "brand": "instacart",
        "brand_terms": ["instacart", "grocery delivery"],
        "allowlist": DEFAULT_ALLOWLIST,
        "generic_terms": DEFAULT_GENERIC_QUERIES,
    },
    "uber": {
        "brand": "uber",
        "brand_terms": ["uber", "ride share", "rideshare"],
        "allowlist": DEFAULT_ALLOWLIST,
        "generic_terms": DEFAULT_GENERIC_QUERIES,
    },
    "lyft": {
        "brand": "lyft",
        "brand_terms": ["lyft", "ride share", "rideshare"],
        "allowlist": DEFAULT_ALLOWLIST,
        "generic_terms": DEFAULT_GENERIC_QUERIES,
    },
}

# ---- Reddit client ----
def build_reddit(read_only=False) -> praw.Reddit:
    """Create a Reddit instance using refresh_token if available; else read-only.
    For posting, refresh_token is required.
    """
    if STATE["refresh_token"] and not read_only:
        reddit = praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            user_agent=DEFAULT_USER_AGENT,
            redirect_uri=REDDIT_REDIRECT_URI,
            refresh_token=STATE["refresh_token"],
            ratelimit_seconds=5,
        )
    else:
        reddit = praw.Reddit(
            client_id=REDDIT_CLIENT_ID,
            client_secret=REDDIT_CLIENT_SECRET,
            user_agent=DEFAULT_USER_AGENT,
            redirect_uri=REDDIT_REDIRECT_URI,
            ratelimit_seconds=5,
        )
    return reddit

def is_megathread_title(title: str) -> bool:
    t = (title or "").lower()
    return "megathread" in t or ("weekly" in t and "thread" in t)

def rules_disallow_referrals(subreddit) -> (bool, bool, str):
    """Return (disallowed, megathread_only, memo)."""
    try:
        text_blobs = []
        for r in subreddit.rules():
            text_blobs.append((r.short_name or "") + " " + (r.description or ""))
        about = subreddit.description or ""
        text_blobs.append(about)
        blob = " ".join(text_blobs).lower()

        disallow = any(pat in blob for pat in PROHIBIT_PATTERNS)
        mega_only = any(pat in blob for pat in MEGATHREAD_REQUIRED_PATTERNS)
        memo = "rules_checked"
        return disallow, mega_only, memo
    except Exception as e:
        return False, False, f"rules_error:{e!r}"

def find_candidate_threads(reddit, brand_terms: List[str], generic_terms: List[str], allowlist: List[str], days_back: int):
    cutoff = time.time() - days_back * 86400
    seen = set()

    # 1) allowlist first
    for s in allowlist:
        try:
            sr = reddit.subreddit(s)
            disallow, mega_only, memo = rules_disallow_referrals(sr)
            q_terms = brand_terms + generic_terms
            query = " OR ".join([f'title:"{t}"' for t in q_terms if t])
            for subm in sr.search(query or "referral", sort="new", time_filter="year", limit=50):
                if subm.created_utc < cutoff:
                    continue
                if (s, subm.id) in seen:
                    continue
                seen.add((s, subm.id))
                title = (subm.title or "").lower()
                if any(t in title for t in ["referral", "referrals", "promo", "code", "coupon", "discount", "megathread"]):
                    yield (s, subm, disallow, mega_only)
        except Exception as e:
            _log("warn", "allowlist_search_error", sub=s, error=repr(e))

    # 2) discovery
    discovery_queries = list(dict.fromkeys(brand_terms + generic_terms))
    for q in discovery_queries:
        try:
            for sr in reddit.subreddits.search(q, limit=25):
                s = sr.display_name
                if any(s.lower() == a.lower() for a in allowlist):
                    continue
                disallow, mega_only, memo = rules_disallow_referrals(sr)
                q_terms = brand_terms + generic_terms
                query = " OR ".join([f'title:"{t}"' for t in q_terms if t])
                try:
                    for subm in sr.search(query or "referral", sort="new", time_filter="year", limit=25):
                        if subm.created_utc < cutoff:
                            continue
                        if (s, subm.id) in seen:
                            continue
                        seen.add((s, subm.id))
                        title = (subm.title or "").lower()
                        if any(t in title for t in ["referral", "referrals", "promo", "code", "coupon", "discount", "megathread"]):
                            yield (s, subm, disallow, mega_only)
                except Exception as inner:
                    _log("warn", "subreddit_search_error", sub=s, error=repr(inner))
        except Exception as e:
            _log("warn", "discovery_error", query=q, error=repr(e))

# ---- Natural Copy Variation Engine ----
SYNONYMS = {
    "hey": ["Hey", "Hi", "Hello", "Quick heads-up:", "FYI:"],
    "delivers": ["delivers", "brings", "drops off"],
    "more": ["more", "other essentials", "etc."],
    "get": ["Get", "Grab", "Take"],
    "first_order": ["first order", "first purchase", "first delivery"],
    "use_code": ["use my code", "apply code", "use the code"],
    "at_checkout": ["at checkout", "at sign-up", "when you order"],
    "hope_helps": ["hope this helps", "hope this is useful", "might help someone"],
    "link_phrase": ["Here’s the link", "Direct link", "Sign-up link", "My link"],
}

CTA_TEMPLATES = [
    "{get} {discount}% off your {first_order} — {use_code} {code} {at_checkout}.",
    "Score {discount}% off your {first_order} with code {code} {at_checkout}.",
    "{get} {discount}% off: code {code} {at_checkout}.",
]

OPENERS = [
    "{hey}! {brand} {delivers} alcohol, food, drinks and {more} in ~30 minutes.",
    "{hey}! If you’re trying {brand} for the first time, this might help.",
    "{hey}! Sharing a {brand} referral that helped me recently:",
]

CLOSERS = [
    "{hope_helps}. {link_phrase}: {link}",
    "{link_phrase}: {link} — {hope_helps}.",
    "{link} ({hope_helps}).",
]

def spin_piece(key: str) -> str:
    vals = SYNONYMS.get(key, [key])
    return random.choice(vals)

def spin_template(tmpl: str, brand: str, code: str, link: str, discount: int) -> str:
    return tmpl.format(
        hey=spin_piece("hey"),
        brand=(brand or "").strip().title() or "This service",
        delivers=spin_piece("delivers"),
        more=spin_piece("more"),
        get=spin_piece("get"),
        first_order=spin_piece("first_order"),
        use_code=spin_piece("use_code"),
        at_checkout=spin_piece("at_checkout"),
        hope_helps=spin_piece("hope_helps"),
        link_phrase=spin_piece("link_phrase"),
        code=code,
        link=link,
        discount=discount,
    )

def generate_variant(base_message: str, brand: str, code: str, link: str, discount: int, tone: str, emoji_level: str, add_disclaimer: bool) -> str:
    """Build one varied message around a base message. Will include the base text occasionally to avoid synthetic feel."""
    parts = []

    # 40% chance to include base message at top
    if random.random() < 0.4 and base_message.strip():
        parts.append(base_message.strip())

    # Opener + CTA
    opener = spin_template(random.choice(OPENERS), brand, code, link, discount)
    cta = spin_template(random.choice(CTA_TEMPLATES), brand, code, link, discount)

    # Closer
    closer = spin_template(random.choice(CLOSERS), brand, code, link, discount)

    # Tone shaping
    if tone == "concise":
        # Concise keeps it short
        msg = f"{cta}\n\n{link}"
    elif tone == "friendly":
        msg = f"{opener}\n\n{cta}\n\n{closer}"
    else:  # helpful
        msg = f"{opener}\n\n{cta}\n\nIf you don’t see the code field, sign up first, then add it {spin_piece('at_checkout')}. {closer}"

    parts.append(msg)

    # Disclaimer for mods
    if add_disclaimer:
        parts.append("Mods: if this isn’t allowed here, please remove — no worries.")

    final = "\n\n".join(parts)

    # Emoji usage
    if emoji_level == "low":
        # sprinkle a single emoji sometimes
        if random.random() < 0.35:
            final += " 🙂"
    elif emoji_level == "none":
        pass
    else:  # normal
        if random.random() < 0.2:
            final += " 🚚"

    return final.strip()

# ---- Worker ----
def drip_worker(config: Dict):
    STATE["running"] = True
    STATE["stop_requested"] = False
    STATE["summary"] = {}
    posted = 0
    seen_targets = {}
    per_sub_counts = {}

    try:
        if config.get("random_seed"):
            random.seed(int(config["random_seed"]))

        reddit = build_reddit(read_only=False)
        try:
            me = str(reddit.user.me())
            STATE["last_login_user"] = me
            _log("info", "auth_ok", user=me)
        except Unauthorized:
            _log("error", "auth_failed", reason="No valid refresh token")
            STATE["running"] = False
            return

        # Inputs
        base_message = config["message"]
        brand = (config.get("brand") or "").strip()
        # Extract code/link/discount by simple heuristics, with UI overrides
        code = (config.get("ref_code") or "").strip()
        link = (config.get("ref_link") or "").strip()
        try:
            discount = int(config.get("discount", "0"))
        except Exception:
            discount = 0

        tone = config.get("tone", "friendly")  # concise|friendly|helpful
        emoji_level = config.get("emoji_level", "low")  # none|low|normal
        add_disclaimer = bool(config.get("add_disclaimer", True))

        brand_terms = [t.strip() for t in (config.get("brand_terms") or "").split(",") if t.strip()]
        if brand and brand.lower() not in [t.lower() for t in brand_terms]:
            brand_terms.append(brand)
        generic_terms = [t.strip() for t in (config.get("generic_terms") or "").split(",") if t.strip()] or DEFAULT_GENERIC_QUERIES
        allowlist = [s.strip() for s in (config.get("allowlist") or "").split(",") if s.strip()] or DEFAULT_ALLOWLIST

        days_back = int(config.get("days_back", 60))
        per_sub_limit = int(config.get("per_sub_limit", 1))
        max_total_posts = int(config.get("max_total_posts", 10))

        posts_per_hour = float(config.get("posts_per_hour", 1))
        cadence_seconds = max(10, int(3600 / posts_per_hour))
        jitter = int(config.get("jitter_seconds", 30))

        start_at_iso = config.get("start_at")
        end_at_iso = config.get("end_at")
        start_at = dt.datetime.fromisoformat(start_at_iso) if start_at_iso else dt.datetime.now()
        end_at = dt.datetime.fromisoformat(end_at_iso) if end_at_iso else None

        only_megathreads = bool(config.get("only_megathreads", True))
        dry_run = bool(config.get("dry_run", False))

        while True:
            if STATE["stop_requested"]:
                _log("warn", "stop_requested")
                break

            now = dt.datetime.now()
            if now < start_at:
                time.sleep(1)
                continue
            if end_at and now > end_at:
                _log("info", "end_time_reached")
                break

            # Build candidates
            candidates = []
            for s, submission, disallow, mega_only in find_candidate_threads(reddit, brand_terms, generic_terms, allowlist, days_back):
                # Skip by rules
                if disallow:
                    _log("info", "skip_rules_disallow", sub=s, title=submission.title)
                    continue
                if (only_megathreads or mega_only) and not is_megathread_title(submission.title):
                    _log("info", "skip_not_megathread", sub=s, title=submission.title)
                    continue
                if per_sub_counts.get(s, 0) >= per_sub_limit:
                    continue
                key = f"{s}_{submission.id}"
                if seen_targets.get(key):
                    continue
                candidates.append((s, submission))

            if not candidates:
                time.sleep(5)
                continue

            # Pick a candidate
            s, submission = random.choice(candidates)
            key = f"{s}_{submission.id}"
            seen_targets[key] = True

            try:
                title = submission.title
                msg = generate_variant(base_message, brand, code, link, discount, tone, emoji_level, add_disclaimer)
                if dry_run:
                    _log("info", "dry_run_match", sub=s, title=title, url=f"https://www.reddit.com{submission.permalink}")
                else:
                    reply = submission.reply(msg)
                    posted += 1
                    per_sub_counts[s] = per_sub_counts.get(s, 0) + 1
                    STATE["summary"][s] = per_sub_counts[s]
                    _log("success", "comment_posted", sub=s, title=title, comment_id=reply.id, url=f"https://www.reddit.com{reply.permalink}")

                if posted >= max_total_posts and not dry_run:
                    _log("info", "total_cap_reached", count=posted)
                    break
            except Exception as e:
                _log("error", "post_failed", sub=s, title=title, error=repr(e))

            # Sleep
            delay = cadence_seconds + random.randint(0, jitter)
            _log("info", "sleep", seconds=delay)
            time.sleep(delay)

    finally:
        STATE["running"] = False
        _log("info", "job_done", running=False)

# ---- Routes ----
@app.route("/")
def index():
    presets = sorted(PRESETS.keys())
    use_oauth_ready = bool(REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET and REDDIT_REDIRECT_URI)
    return render_template("index.html", presets=presets, use_oauth_ready=use_oauth_ready,
                           logged_in=bool(STATE["refresh_token"]), user=STATE.get("last_login_user"))

@app.route("/presets.json")
def presets_json():
    return jsonify(PRESETS)

@app.route("/oauth/login")
def oauth_login():
    if not (REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET and REDDIT_REDIRECT_URI):
        abort(400, "OAuth env vars missing")
    reddit = build_reddit(read_only=True)
    scopes = ["identity", "submit", "read", "modconfig"]
    state = os.urandom(16).hex()
    session["reddit_state"] = state
    auth_url = reddit.auth.url(scopes=scopes, state=state, duration="permanent")
    return redirect(auth_url)

@app.route("/oauth/callback")
def oauth_callback():
    error = request.args.get("error")
    if error:
        return f"OAuth Error: {error}", 400
    state = request.args.get("state")
    code = request.args.get("code")
    if not code or state != session.get("reddit_state"):
        return "Invalid state or missing code", 400
    reddit = build_reddit(read_only=True)
    refresh_token = reddit.auth.authorize(code)
    STATE["refresh_token"] = refresh_token
    try:
        me = str(build_reddit(read_only=False).user.me())
        STATE["last_login_user"] = me
    except Exception:
        pass
    return redirect(url_for("index"))

@app.route("/logout")
def logout():
    STATE["refresh_token"] = None
    STATE["last_login_user"] = None
    return redirect(url_for("index"))

@app.route("/start", methods=["POST"])
def start():
    if STATE["running"]:
        return jsonify({"ok": False, "error": "A job is already running."}), 400
    if not STATE["refresh_token"]:
        return jsonify({"ok": False, "error": "Not logged in via Reddit OAuth."}), 401

    data = request.json or {}
    required = ["message"]
    missing = [r for r in required if not data.get(r)]
    if missing:
        return jsonify({"ok": False, "error": f"Missing fields: {', '.join(missing)}"}), 400

    t = threading.Thread(target=drip_worker, args=(data,), daemon=True)
    t.start()
    return jsonify({"ok": True})

@app.route("/stop", methods=["POST"])
def stop():
    STATE["stop_requested"] = True
    return jsonify({"ok": True})

@app.route("/progress")
def progress():
    return jsonify({
        "running": STATE["running"],
        "logs": STATE["logs"],
        "summary": STATE["summary"],
        "user": STATE.get("last_login_user"),
        "logged_in": bool(STATE["refresh_token"]),
    })

@app.route("/export.csv")
def export_csv():
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["ts","level","event","sub","title","url","comment_id","error","seconds","user","count"]);
    writer.writeheader()
    for row in STATE["logs"]:
        writer.writerow({k: row.get(k,"") for k in writer.fieldnames})
    csv_data = output.getvalue()
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=progress_logs.csv"}
    )

@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory("static", filename)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
