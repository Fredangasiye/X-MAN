"""Vercel serverless version of the Lifestyle X bot (no AI/LLM calls)."""
import base64, hashlib, html, json, os, random, re, urllib.parse, urllib.request, uuid
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS = json.loads((ROOT / "data/questions.json").read_text())

def request(url, method="GET", body=None, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=25) as response:
        return json.loads(response.read())

def kv(*command):
    url, token = os.environ["KV_REST_API_URL"], os.environ["KV_REST_API_TOKEN"]
    result = request(url, "POST", list(command), {"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    return result.get("result")

def get_json(key, default):
    value = kv("GET", key)
    return json.loads(value) if value else default

def set_json(key, value): kv("SET", key, json.dumps(value, separators=(",", ":")))

def x(path, method="GET", payload=None):
    token = os.environ["X_USER_ACCESS_TOKEN"]
    return request("https://api.x.com" + path, method, payload,
                   {"Authorization": "Bearer " + token, "Content-Type": "application/json"})

def compact(text, limit=270): return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"
def today(): return date.today().isoformat()
def enabled(): return kv("GET", "bot:enabled") != "false"
def log(kind, text):
    item = {"at": datetime.now(timezone.utc).isoformat(), "kind": kind, "text": text}
    entries = get_json("bot:activity", [])[:49]; entries.insert(0, item); set_json("bot:activity", entries)

def choose_question():
    history = get_json("bot:history", [])
    recent = {row["id"] for row in history if row["at"] > (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()}
    return random.choice([q for q in QUESTIONS if q["id"] not in recent] or QUESTIONS)

def post_question(question):
    suffix = "\n\nVote below." if question["format"] == "poll" else "\n\nReply with your answer."
    payload = {"text": compact(question["text"], 280 - len(suffix)) + suffix}
    if question["format"] == "poll": payload["poll"] = {"options": question["options"], "duration_minutes": 1440}
    result = x("/2/tweets", "POST", payload)["data"]
    history = get_json("bot:history", []); history.append({"id": question["id"], "at": datetime.now(timezone.utc).isoformat(), "x_id": result["id"]}); set_json("bot:history", history[-500:])
    log("question", payload["text"]); return result["id"]

def candidate(question):
    terms = question["keywords"][:3] or ["survey"]
    query = "(" + " OR ".join(terms) + ") -is:retweet lang:en"
    data = x("/2/tweets/search/recent?" + urllib.parse.urlencode({"query": query, "max_results": 20, "tweet.fields": "author_id,entities"}))
    me = x("/2/users/me?user.fields=username")["data"]
    found = []
    for post in data.get("data", []):
        mentions = post.get("entities", {}).get("mentions", [])
        can_reply = post.get("author_id") == me["id"] or any(m.get("username", "").lower() == me.get("username", "").lower() for m in mentions)
        item = {"id": uuid.uuid4().hex, "source_id": post["id"], "source": post["text"], "question_id": question["id"], "can_reply": can_reply}
        set_json("bot:candidate:" + item["id"], item); found.append(item)
        if len(found) == 5: break
    set_json("bot:candidates", found); return found

def action(candidate_id, action_name):
    item = get_json("bot:candidate:" + candidate_id, None)
    if not item: raise ValueError("Candidate has expired.")
    if action_name == "reply":
        if not item["can_reply"]: raise ValueError("X permits a reply only when you were mentioned or authored the source post.")
        question = next(q for q in QUESTIONS if q["id"] == item["question_id"])
        x("/2/tweets", "POST", {"text": compact("Quick question: " + question["text"]), "reply": {"in_reply_to_tweet_id": item["source_id"]}})
    else:
        user_id = x("/2/users/me")["data"]["id"]
        if action_name == "like": x(f"/2/users/{user_id}/likes", "POST", {"tweet_id": item["source_id"]})
        elif action_name == "repost": x(f"/2/users/{user_id}/retweets", "POST", {"tweet_id": item["source_id"]})
        elif action_name == "quote":
            question = next(q for q in QUESTIONS if q["id"] == item["question_id"])
            x("/2/tweets", "POST", {"text": compact("Quick question: " + question["text"]), "quote_tweet_id": item["source_id"]})
        else: raise ValueError("Unknown action.")
    log(action_name, item["source"]); return action_name.capitalize() + " completed."

def dashboard(message=""):
    candidates, activity = get_json("bot:candidates", []), get_json("bot:activity", [])
    def card(item):
        reply = f'<button name=action value=reply>Reply</button>' if item["can_reply"] else '<small>Reply requires a mention.</small>'
        return f'<article><p>{html.escape(item["source"])}</p><form method=post action=/action><input type=hidden name=id value="{item["id"]}">{reply}<button name=action value=quote>Quote</button><button name=action value=like>Like</button><button name=action value=repost>Repost</button></form></article>'
    cards = "".join(card(i) for i in candidates) or "<p>No candidates yet.</p>"
    rows = "".join(f'<li><b>{html.escape(i["kind"])}</b> — {html.escape(i["text"])}</li>' for i in activity) or "<li>No activity yet.</li>"
    state = "ACTIVE" if enabled() else "PAUSED"
    notice = f'<p class=notice>{html.escape(message)}</p>' if message else ""
    return f'''<!doctype html><title>X question bot</title><style>body{{max-width:780px;margin:36px auto;font:16px system-ui;color:#17212b}}button{{margin:4px;padding:8px 12px}}article{{border:1px solid #ccd6dd;padding:12px;margin:12px 0}}.notice{{background:#e8f5e9;padding:10px}}</style><h1>X question bot</h1><p><b>{state}</b> · Vercel cloud dashboard</p>{notice}<form method=post action=/toggle><button>{'Pause' if enabled() else 'Resume'} posting</button></form><form method=post action=/post><button>Post a random question now</button></form><form method=post action=/find><button>Find candidate posts</button></form><h2>Candidate post actions</h2>{cards}<h2>Activity</h2><ul>{rows}</ul>'''

class handler(BaseHTTPRequestHandler):
    def authenticated(self):
        if self.path.endswith("/cron"):
            return self.headers.get("Authorization") == "Bearer " + os.environ.get("CRON_SECRET", "")
        password = os.environ.get("DASHBOARD_PASSWORD", "")
        expected = "Basic " + base64.b64encode(("admin:" + password).encode()).decode()
        return bool(password) and self.headers.get("Authorization") == expected
    def do_GET(self):
        if self.path.endswith("/cron"):
            if not self.authenticated(): return self.respond("Forbidden", 403, "text/plain")
            key = "bot:cron:" + today()
            if enabled() and not kv("GET", key):
                post_question(choose_question()); kv("SET", key, "done")
            return self.respond('{"ok":true}', 200, "application/json")
        if not self.authenticated(): return self.challenge()
        self.respond(dashboard())
    def do_POST(self):
        if not self.authenticated(): return self.challenge()
        length = int(self.headers.get("Content-Length", 0)); form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        try:
            if self.path.endswith("/toggle"):
                kv("SET", "bot:enabled", "false" if enabled() else "true"); message = "Posting setting updated."
            elif self.path.endswith("/post"): message = "Posted X post " + post_question(choose_question()) + "."
            elif self.path.endswith("/find"): message = f"Found {len(candidate(choose_question()))} candidate posts."
            elif self.path.endswith("/action"): message = action(form["id"][0], form["action"][0])
            else: message = "Unknown action."
        except Exception as exc: message = str(exc)[:500]
        self.respond(dashboard(message))
    def challenge(self):
        self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="X bot"'); self.end_headers()
    def respond(self, text, status=200, content_type="text/html; charset=utf-8"):
        data = text.encode(); self.send_response(status); self.send_header("Content-Type", content_type); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def log_message(self, *args): pass
