#!/usr/bin/env python3
"""Dependency-free dashboard and scheduler for the lifestyle X poll bot."""
from __future__ import annotations

import html
import json
import os
import random
import re
import sqlite3
import subprocess
import threading
import time
import urllib.parse
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DB = DATA / "bot.sqlite3"
QUESTIONS = json.loads((DATA / "questions.json").read_text())
LOCK = threading.Lock()

def load_env():
    path = ROOT / ".env"
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_env()

def setting(name, default=""):
    return os.getenv(name, default)

def preference(name, default=""):
    """Dashboard-managed settings take precedence; .env remains the baseline."""
    with connect() as con:
        row = con.execute("select value from settings where key=?", ("preference_" + name,)).fetchone()
    return row["value"] if row else setting(name, default)

def save_preference(name, value):
    with connect() as con:
        con.execute("insert or replace into settings(key,value) values(?,?)", ("preference_" + name, value))

def init_db():
    DATA.mkdir(exist_ok=True)
    with connect() as con:
        con.executescript("""
        create table if not exists posts (
          id integer primary key, question_id text not null, text text not null,
          x_post_id text, mode text not null, created_at text not null
        );
        create table if not exists candidates (
          id integer primary key, source_post_id text unique not null, author text,
          source_text text not null, question_id text not null, reply_text text not null,
          score integer not null, status text not null default 'pending', created_at text not null
        );
        create table if not exists settings (key text primary key, value text not null);
        """)
        try:
            con.execute("alter table candidates add column can_reply integer not null default 0")
        except sqlite3.OperationalError:
            pass

def connect():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def question_by_id(question_id):
    return next(q for q in QUESTIONS if q["id"] == question_id)

def choose_question():
    cutoff = (datetime.now() - timedelta(days=90)).isoformat()
    with connect() as con:
        used = {r[0] for r in con.execute("select question_id from posts where created_at > ?", (cutoff,))}
    pool = [q for q in QUESTIONS if q["id"] not in used] or QUESTIONS
    return random.choice(pool)

def trim(text, limit=270):
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"

def post_text(q):
    suffix = "\n\nVote below." if q["format"] == "poll" else "\n\nReply with your answer."
    return trim(q["text"], 280 - len(suffix)) + suffix

def x_request(path, method="GET", payload=None):
    token = setting("X_USER_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("X_USER_ACCESS_TOKEN is not set")
    # macOS's system curl uses the operating system certificate store. This avoids
    # a common certificate-chain issue in framework-installed Python builds.
    command = ["curl", "-sS", "--max-time", "25", "-X", method,
               "-H", f"Authorization: Bearer {token}",
               "-H", "Content-Type: application/json",
               "-w", "\n%{http_code}", "https://api.x.com" + path]
    if payload is not None:
        command.extend(["--data", json.dumps(payload)])
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise RuntimeError("Could not reach the X API: " + completed.stderr.strip())
    body, _, status = completed.stdout.rpartition("\n")
    if not status.startswith("2"):
        try:
            detail = json.loads(body).get("detail") or json.loads(body).get("title")
        except json.JSONDecodeError:
            detail = body[:500]
        raise RuntimeError(f"X API {status}: {detail or 'request failed'}")
    return json.loads(body)

def publish(q, reply_to=None):
    if preference("ENABLED", "true").lower() != "true":
        raise RuntimeError("Publishing is paused in the dashboard.")
    text = post_text(q) if not reply_to else trim("Quick question: " + q["text"])
    payload = {"text": text}
    if reply_to:
        payload["reply"] = {"in_reply_to_tweet_id": reply_to}
    elif q["format"] == "poll":
        payload["poll"] = {"options": q["options"], "duration_minutes": 1440}
    dry = setting("DRY_RUN", "true").lower() != "false"
    result = {"data": {"id": "dry-run"}} if dry else x_request("/2/tweets", "POST", payload)
    with connect() as con:
        con.execute("insert into posts(question_id,text,x_post_id,mode,created_at) values(?,?,?,?,?)",
                    (q["id"], text, result["data"]["id"], "reply" if reply_to else "poll", datetime.now().isoformat()))
    return result["data"]["id"], text

def find_candidates():
    if setting("DRY_RUN", "true").lower() != "false":
        return 0, "Live X search is disabled in dry-run mode."
    q = choose_question()
    terms = q["keywords"][:3] or ["lifestyle"]
    query = "(" + " OR ".join(terms) + ") -is:retweet lang:en"
    me = x_request("/2/users/me?user.fields=username")["data"]
    data = x_request("/2/tweets/search/recent?" + urllib.parse.urlencode({"query": query, "max_results": 20, "tweet.fields": "author_id,created_at,entities"}))
    added = 0
    with connect() as con:
        for post in data.get("data", []):
            lower = post["text"].lower()
            matches = [question for question in QUESTIONS if any(k.lower() in lower for k in question["keywords"])]
            if not matches or added >= 5:
                continue
            match = random.choice(matches)
            mentions = post.get("entities", {}).get("mentions", [])
            can_reply = post.get("author_id") == me["id"] or any(m.get("username", "").lower() == me.get("username", "").lower() for m in mentions)
            reply = trim("Quick question: " + match["text"])
            try:
                con.execute("insert into candidates(source_post_id,author,source_text,question_id,reply_text,score,can_reply,created_at) values(?,?,?,?,?,?,?,?)",
                            (post["id"], post.get("author_id", ""), post["text"], match["id"], reply, len(match["keywords"]), int(can_reply), datetime.now().isoformat()))
                added += 1
            except sqlite3.IntegrityError:
                pass
    return added, f"Added {added} candidate replies from a recent X search."

def act_on_candidate(candidate, action):
    if action == "reply":
        if not candidate["can_reply"]:
            raise RuntimeError("X only allows this reply when @FredandRods is mentioned or authored the source post. Use Quote instead.")
        return publish(question_by_id(candidate["question_id"]), candidate["source_post_id"])
    me = x_request("/2/users/me")["data"]["id"]
    if action == "like":
        result = x_request(f"/2/users/{me}/likes", "POST", {"tweet_id": candidate["source_post_id"]})
        return result["data"].get("liked", True), "Liked the source post."
    if action == "repost":
        result = x_request(f"/2/users/{me}/retweets", "POST", {"tweet_id": candidate["source_post_id"]})
        return result["data"].get("retweeted", True), "Reposted the source post."
    if action == "quote":
        q = question_by_id(candidate["question_id"])
        text = trim("Quick question: " + q["text"])
        result = x_request("/2/tweets", "POST", {"text": text, "quote_tweet_id": candidate["source_post_id"]})
        return result["data"]["id"], "Published a quote post."
    raise RuntimeError("Unknown action.")

def today_schedule():
    key = "scheduled_at_" + date.today().isoformat()
    with connect() as con:
        row = con.execute("select value from settings where key=?", (key,)).fetchone()
        if row:
            return datetime.fromisoformat(row["value"])
        start = datetime.strptime(preference("POST_WINDOW_START", "09:00"), "%H:%M").time()
        end = datetime.strptime(preference("POST_WINDOW_END", "18:00"), "%H:%M").time()
        base = datetime.combine(date.today(), start)
        seconds = max(0, int((datetime.combine(date.today(), end) - base).total_seconds()))
        when = base + timedelta(seconds=random.randint(0, seconds))
        con.execute("insert into settings(key,value) values(?,?)", (key, when.isoformat()))
        return when

def scheduled_worker():
    while True:
        try:
            now = datetime.now()
            due = today_schedule()
            with connect() as con:
                done = con.execute("select 1 from settings where key=?", ("attempted_" + date.today().isoformat(),)).fetchone()
            if now >= due and not done:
                try:
                    q = choose_question()
                    publish(q)
                    outcome = "posted"
                except Exception as exc:
                    outcome = "failed: " + str(exc)[:300]
                    print("Scheduled post not sent:", exc, flush=True)
                with connect() as con:
                    con.execute("insert or replace into settings(key,value) values(?,?)", ("attempted_" + date.today().isoformat(), outcome))
        except Exception as exc:
            print("Scheduler:", exc, flush=True)
        time.sleep(60)

def test_connection():
    response = x_request("/2/users/me?user.fields=username")
    return "Connected to @" + response["data"].get("username", "your X account") + "."

def page(message=""):
    with connect() as con:
        posts = con.execute("select * from posts order by id desc limit 10").fetchall()
        candidates = con.execute("select * from candidates where status='pending' order by id desc limit 10").fetchall()
        attempted = con.execute("select value from settings where key=?", ("attempted_" + date.today().isoformat(),)).fetchone()
    live = setting("DRY_RUN", "true").lower() == "false"
    enabled = preference("ENABLED", "true").lower() == "true"
    start = preference("POST_WINDOW_START", "09:00")
    end = preference("POST_WINDOW_END", "18:00")
    state = "ACTIVE" if enabled else "PAUSED"
    status_color = "#e8f5e9" if enabled else "#fff3cd"
    today = attempted["value"] if attempted else "Not attempted yet"
    post_rows = "".join(f"<tr><td>{html.escape(r['created_at'][:16])}</td><td>{html.escape(r['mode'])}</td><td>{html.escape(r['text'])}</td><td>{html.escape(r['x_post_id'] or '')}</td></tr>" for r in posts) or "<tr><td colspan=4>No posts yet.</td></tr>"
    def candidate_card(r):
        candidate_id = r["id"]
        reply = (f"<form method=post action='/candidate-action'><input type=hidden name=id value='{candidate_id}'><input type=hidden name=action value=reply><button>Publish Reply</button></form>"
                 if r["can_reply"] else "<small>Reply unavailable: X requires that you are mentioned or authored the post.</small>")
        controls = "".join([
            reply,
            f"<form method=post action='/candidate-action'><input type=hidden name=id value='{candidate_id}'><input type=hidden name=action value=quote><button>Quote Post</button></form>",
            f"<form method=post action='/candidate-action'><input type=hidden name=id value='{candidate_id}'><input type=hidden name=action value=like><button>Like</button></form>",
            f"<form method=post action='/candidate-action'><input type=hidden name=id value='{candidate_id}'><input type=hidden name=action value=repost><button>Repost</button></form>",
        ])
        return f"<article><p><b>Source:</b> {html.escape(r['source_text'])}</p><p><b>Question text:</b> {html.escape(r['reply_text'])}</p><div class=actions>{controls}</div></article>"
    cand_rows = "".join(candidate_card(r) for r in candidates) or "<p>No pending candidates.</p>"
    return f"""<!doctype html><meta charset=utf-8><title>Lifestyle X poll bot</title><style>body{{max-width:850px;margin:40px auto;font:16px system-ui;color:#17212b}}button{{padding:9px 12px;margin:4px 8px 4px 0}}article,fieldset{{border:1px solid #ccd6dd;padding:12px;margin:12px 0}}table{{width:100%;border-collapse:collapse}}td{{border-bottom:1px solid #ddd;padding:8px;vertical-align:top}}form{{display:inline}}.notice{{background:#e8f5e9;padding:10px}}.status{{padding:10px;background:{status_color}}} input{{padding:6px}}</style><h1>Lifestyle X poll bot</h1><p class=status><b>{state}</b> · {'LIVE' if live else 'DRY RUN'} · Next poll: {today_schedule().strftime('%H:%M')} · Today: {html.escape(today)}</p>{'<p class=notice>'+html.escape(message)+'</p>' if message else ''}<form method=post action='/toggle'><button>{'Pause all publishing' if enabled else 'Resume publishing'}</button></form><form method=post action='/test-connection'><button>Test X connection</button></form><form method=post action='/post-now'><button>Post next question now</button></form><form method=post action='/find-replies'><button>Find candidate posts</button></form><fieldset><legend><b>Schedule</b></legend><form method=post action='/save-schedule'>Post between <input name=start type=time value='{html.escape(start)}'> and <input name=end type=time value='{html.escape(end)}'> <button>Save schedule</button></form><small>The app chooses one randomized time inside this window. Changes apply from tomorrow; use “Post next question now” for today.</small></fieldset><h2>Candidate post actions</h2>{cand_rows}<h2>Recent activity</h2><table><tr><th>Time</th><th>Type</th><th>Text</th><th>X ID</th></tr>{post_rows}</table>"""

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.respond(page())
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        try:
            if self.path == "/post-now":
                q = choose_question(); post_id, text = publish(q); msg = f"{'Would post' if post_id == 'dry-run' else 'Posted'}: {text}"
            elif self.path == "/toggle":
                next_state = "false" if preference("ENABLED", "true").lower() == "true" else "true"
                save_preference("ENABLED", next_state)
                msg = "Publishing " + ("resumed." if next_state == "true" else "paused.")
            elif self.path == "/test-connection":
                msg = test_connection()
            elif self.path == "/save-schedule":
                start, end = form.get("start", [""])[0], form.get("end", [""])[0]
                if not start or not end or start >= end:
                    raise RuntimeError("Use valid times, with the end time after the start time.")
                datetime.strptime(start, "%H:%M"); datetime.strptime(end, "%H:%M")
                save_preference("POST_WINDOW_START", start); save_preference("POST_WINDOW_END", end)
                msg = "Schedule saved for future daily polls."
            elif self.path == "/find-replies":
                _, msg = find_candidates()
            elif self.path == "/candidate-action":
                candidate_id = int(form["id"][0])
                action = form["action"][0]
                with connect() as con: row = con.execute("select * from candidates where id=? and status='pending'", (candidate_id,)).fetchone()
                if not row: raise RuntimeError("Candidate is no longer available")
                _, msg = act_on_candidate(row, action)
                past_tense = {"reply": "published", "quote": "quoted", "like": "liked", "repost": "reposted"}[action]
                with connect() as con: con.execute("update candidates set status=? where id=?", (past_tense, candidate_id))
            else: msg = "Unknown action."
        except Exception as exc:
            msg = str(exc)
        self.respond(page(msg))
    def respond(self, body):
        encoded = body.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)
    def log_message(self, fmt, *args): print(fmt % args)

if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduled_worker, daemon=True).start()
    print("Open http://127.0.0.1:8787  (Ctrl-C to stop)")
    ThreadingHTTPServer(("127.0.0.1", 8787), Handler).serve_forever()
