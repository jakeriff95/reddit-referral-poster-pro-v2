#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reddit Referral Poster — Pro v2.1

- OAuth web login (Reddit "web app" credentials).
- Drip scheduler: start immediately; stop after `duration_minutes`.
- Rule checks: prefers megathreads; skips subs forbidding referrals.
- Natural copy variation; auto-generates message when base text is empty.
- CSV export; progress API includes "stopping" state.

Environment variables (Render):
  FLASK_SECRET
  REDDIT_CLIENT_ID
  REDDIT_CLIENT_SECRET
  REDDIT_REDIRECT_URI   (e.g., https://reddit-ref-poster.onrender.com/oauth/callback)
  USER_AGENT            (e.g., reddit-ref-poster v2 by u/YourUser)
"""

import os
import io
import csv
import time
import random
import threading
import datetime as dt
from typing import Dict, List

from flask import (
    Flask, render_template, request, jsonify, redirect,
    url_for, session, Response, send_from_directory, abort
)
from flask_cors import CORS

import praw
import prawcore  # modern prawcore (no Unauthorized symbol)

# -------------------- Config --------------------

SECRET = os.environ.get("FLASK_SECRET", "dev-secret-change-me")
REDDIT_CLIENT_ID = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_CLIENT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
REDDIT_REDIRECT_URI = os.environ.get("REDDIT_REDIRECT_URI", "http://localhost:5000/oauth/callback")
DEFAULT_USER_AGENT = os.environ.get("USER_AGENT", "reddit-ref-poster v2 by u/example")

app = Flask(__name__)
app.secret_key = SECRET
CORS(app)

# -------------------- State --------------------

STATE: Dict = {
    "refresh_token": None,
    "running": False,
    "stop_requested": False,
    "logs": [],
    "summary": {},            # per-subreddit counts
    "last_login_user": None,
}

def _log(level: str, event: str, **kwargs):
    entry = {"level": level, "event": event, "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())}
    entry.update(kwargs)
    STATE["logs"].append(entry)
    # keep last ~2000 lines
    if len(STATE["logs"]) > 2000:
        STATE["logs"] = STATE["logs"][-2000:]


# -------------------- Heuristics / Presets --------------------

PROHIBIT_PATTERNS = [
    "no referrals", "no referral", "no codes", "no promo codes",
    "no self-promotion", "no self promotion", "referrals not allowed",
    "no affiliate", "no affiliate links"
]
MEGATHREAD_REQUIRED_PATTERNS = [
    "referrals only in", "referrals allowed only in",
    "post referrals only in", "megathread", "weekly thread"
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
}

# -------------------- Reddit client --------------------

def build_reddit(read_only=False) -> praw.Reddit:
    kwargs = dict(
        client_id=REDDIT_CLIENT_ID,
        client_secret=REDDIT_CLIENT_SECRET,
        user_agent=DEFAULT_USER_AGENT,
        redirect_uri=REDDIT_REDIRECT_URI,
        ratelimit_seconds=5,
    )
    if STATE["refresh_token"] and not read_only:
        kwargs["refresh_token"] = STATE["refresh_token"]
    return praw.Reddit(**kwargs)

def is_megathread_title(title: str) -> bool:
    t = (title or "").lower()
    return "megathread" in t or ("weekly" in t and "thread" in t)

def rules_disallow_referrals(subreddit) -> (bool, bool, str):
    """Return (disallowed, megathread_only, memo)."""
    try:
        text_blobs = []
        for r in subreddit.rules():
            text_blobs.append(f"{r.short_name or ''} {r.description or ''}")
        about = subreddit.description or ""
        text_blobs.append(about)
        blob = " ".join(text_blobs).lower()

        disallow = any(pat in blob for pat in PROHIBIT_PATTERNS)
        mega_only = any(pat in blob for pat in MEGATHREAD_REQUIRED_PATTERNS)
        return disallow, mega_only, "rules_checked"
    except Exception as e:
        return False, False, f"rules_error:{e!r}"

def find_candidate_threads(reddit, brand_terms: List[str], generic_terms: List[str],
                           allowlist: List[str], days_back: int):
    """Yield (sub_name, submission, disallow, mega_only)"""
    cutoff = time.time() - days_back * 86400
    seen = set()

    # 1) Search allowlisted subs first
    for s in allowlist:
        try:
            sr = reddit.subreddit(s)
            disallow, mega_only, _ = rules_disallow_referrals(sr)
            q_terms = brand_terms + generic_terms
            query = " OR ".join([f'title:"{t}"' for t in q_terms if t])
            for subm in sr.search(query or "referral", sort="new", time_filter="year", limit=50):
                if subm.created_utc < cutoff: continue
                if (s, subm.id) in seen: continue
                seen.add((s, subm.id))
                title = (subm.title or "").lower()
                if any(k in title for k in ["referral", "referrals", "promo", "code", "coupon", "discount", "megathread"]):
                    yield (s, subm, disallow, mega_only)
        except Exception as e:
            _log("warn", "allowlist_search_error", sub=s, error=repr(e))

    # 2) Discovery by brand/generic terms
    discovery = list(dict.fromkeys(brand_terms + generic_terms))
    for q in discovery:
        try:
            for sr in reddit.subreddits.search(q, limit=25):
                s = sr.display_name
                if any(s.lower() == a.lower() for a in allowlist):
                    continue
                disallow, mega_only, _ = rules_disallow_referrals(sr)
                query = " OR ".join([f'title:"{t}"' for t in (brand_terms + generic_terms) if t])
                try:
                    for subm in sr.search(query or "referral", sort="new", time_filter="year", limit=25):
                        if subm.created_utc < cutoff: continue
                        if (s, subm.id) in seen: continue
                        seen.add((s, subm.id))
                        title = (subm.title or "").lower()
                        if any(k in title for k in ["referral", "referrals", "promo", "code", "coupon", "discount", "megathread"]):
                            yield (s, subm, disallow, mega_only)
                except Exception as inner:
                    _log("warn", "subreddit_search_error", sub=s, error=repr(inner))
        except Exception as e:
            _log("warn", "discovery_error", query=q, error=repr(e))

# -------------------- Copy variation engine --------------------

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

def generate_variant(base: str, brand: str, code: str, link: str, discount: int,
                     tone: str, emoji_level: str, add_disclaimer: bool) -> str:
    parts = []
    if base.strip():
        parts.append(base.strip())

    opener = spin_template(random.choice(OPENERS), brand, code, link, discount)
    cta = spin_template(random.choice(CTA_TEMPLATES), brand, code, link, discount)
    closer = spin_template(random.choice(CLOSERS), brand, code, link, discount)

    if tone == "concise":
        msg = f"{cta}\n\n{link}"
    elif tone == "friendly":
        msg = f"{opener}\n\n{cta}\n\n{closer}"
    else:
        msg = (
            f"{opener}\n\n{cta}\n\n"
            f"If you don’t see the code field, sign up first, then add it {spin_piece('at_checkout')}. {closer}"
        )

    parts.append(msg)

    if add_disclaimer:
        parts.append("Mods: if this isn’t allowed here, please remove — no worries.")

    final = "\n\n".join(parts)

    if emoji_level == "low":
        if random.random() < 0.35:
            final += " 🙂"
    elif emoji_level == "normal":
        if random.random() < 0.2:
            final += " 🚚"

    return final.strip()

# -------------------- Worker --------------------

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
        except prawcore.exceptions.OAuthException as e:
            _log("error", "auth_failed", reason="OAuthException", detail=str(e))
            STATE["running"] = False
            return
        except prawcore.exceptions.PrawcoreException as e:
            _log("error", "auth_failed", reason="PrawcoreException", detail=str(e))
            STATE["running"] = False
            return

        # --- Inputs ---
        base_message = (config.get("message") or "").strip()
        brand = (config.get("brand") or "").strip()
        code = (config.get("ref_code") or "").strip()
        link = (config.get("ref_link") or "").strip()
        try:
            discount = int(config.get("discount", "0"))
        except Exception:
            discount = 0

        tone = config.get("tone", "friendly")
        emoji_level = config.get("emoji_level", "low")
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

        # Start NOW, end after duration_minutes
        duration_minutes = int(config.get("duration_minutes", 60))
        start_at = dt.datetime.utcnow()
        end_at = start_at + dt.timedelta(minutes=duration_minutes)

        only_megathreads = bool(config.get("only_megathreads", True))
        dry_run = bool(config.get("dry_run", False))

        # --- Main loop ---
        while True:
            if STATE["stop_requested"]:
                _log("warn", "stop_requested")
                break

            now = dt.datetime.utcnow()
            if now > end_at:
                _log("info", "end_time_reached")
                break

            # Gather candidates
            candidates = []
            for s, submission, disallow, mega_only in find_candidate_threads(
                reddit, brand_terms, generic_terms, allowlist, days_back
            ):
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

            s, submission = random.choice(candidates)
            seen_targets[f"{s}_{submission.id}"] = True

            try:
                title = submission.title

                # Build message (auto-generate when base is empty)
                if base_message:
                    msg = generate_variant(base_message, brand, code, link, discount, tone, emoji_level, add_disclaimer)
                else:
                    seed = f"{brand} referral"
                    msg = generate_variant(seed, brand, code, link, discount, tone, emoji_level, add_disclaimer)

                if dry_run:
                    _log("info", "dry_run_match", sub=s, title=title, url=f"https://www.reddit.com{submission.permalink}")
                else:
                    reply = submission.reply(msg)
                    posted += 1
                    per_sub_counts[s] = per_sub_counts.get(s, 0) + 1
                    STATE["summary"][s] = per_sub_counts[s]
                    _log("success", "comment_posted", sub=s, title=title,
                         comment_id=getattr(reply, "id", "?"),
                         url=f"https://www.reddit.com{getattr(reply, 'permalink', '')}")

                if posted >= max_total_posts and not dry_run:
                    _log("info", "total_cap_reached", count=posted)
                    break

            except Exception as e:
                _log("error", "post_failed", sub=s, title=title, error=repr(e))

            delay = cadence_seconds + random.randint(0, jitter)
            _log("info", "sleep", seconds=delay)
            time.sleep(delay)

    finally:
        STATE["running"] = False
        _log("info", "job_done", running=False)


# -------------------- Routes --------------------

@app.route("/")
def index():
    presets = sorted(PRESETS.keys())
    use_oauth_ready = bool(REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET and REDDIT_REDIRECT_URI)
    return render_template(
        "index.html",
        presets=presets,
        use_oauth_ready=use_oauth_ready,
        logged_in=bool(STATE["refresh_token"]),
        user=STATE.get("last_login_user")
    )

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
    # Allow empty message; worker will auto-generate
    for k in ["message", "brand", "ref_code", "ref_link", "discount"]:
        data.setdefault(k, "")

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
        "stop_requested": STATE["stop_requested"],
        "logs": STATE["logs"],
        "summary": STATE["summary"],
        "user": STATE.get("last_login_user"),
        "logged_in": bool(STATE["refresh_token"]),
    })

@app.route("/export.csv")
def export_csv():
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["ts","level","event","sub","title","url","comment_id","error","seconds","user","count"]
    )
    writer.writeheader()
    for row in STATE["logs"]:
        writer.writerow({k: row.get(k, "") for k in writer.fieldnames})
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
