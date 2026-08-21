#!/usr/bin/env python3
"""
SisengAI Chatbot — self-hosted RAG chatbot (freemium product).

A customer drops one <script> tag on their site; their visitors ask
questions; we answer from the customer's own site content.

Free tier: the demo running on sisengai.com (a live lead magnet).
Paid tier: a custom bot per client — their URLs ingested, their
branding, their widget.

Run:
    python3 app.py            # serves on :8000
    python3 app.py --port 8080

Env (all optional, read from ~/.hermes/.env if present):
    CHATBOT_MODEL       LLM model name        (default deepseek-v4-flash)
    OPENCODE_GO_API_KEY API key for opencode  (fallback OPENAI_API_KEY)
    CHATBOT_BASE_URL    OpenAI-compatible URL (default opencode zen)
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

try:
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, HTMLResponse, PlainTextResponse
    from pydantic import BaseModel
    import uvicorn
    HAS_FASTAPI = True
except ImportError:
    HAS_FASTAPI = False

try:
    from sklearn.feature_extraction.text import TfidfVectorizer, ENGLISH_STOP_WORDS
    from sklearn.metrics.pairwise import cosine_similarity
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

try:
    import markdown as _markdown
    import nh3
    HAS_MARKDOWN = True
except ImportError:
    HAS_MARKDOWN = False

# ---- config ------------------------------------------------------------

def load_env():
    for p in (Path.home() / ".hermes/.env", Path(".env")):
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

load_env()

MODEL = os.environ.get("CHATBOT_MODEL", "deepseek-v4-flash")
BASE_URL = os.environ.get("CHATBOT_BASE_URL", "https://opencode.ai/zen/go/v1")
API_KEY = os.environ.get("OPENCODE_GO_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENCODE_ZEN_API_KEY") or ""

DATA_DIR = Path(os.environ.get("CHATBOT_DATA", Path.home() / "sisengai/chatbot/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bots.sqlite"

UA = {"User-Agent": "Mozilla/5.0 (SisengAI Chatbot; +https://sisengai.com)"}


# ---- storage (sqlite, one table per concern) --------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS bots (
        bot_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        system_prompt TEXT NOT NULL DEFAULT 'You are a helpful assistant.',
        brand_color TEXT,
        aliases TEXT NOT NULL DEFAULT '{}',
        urls TEXT NOT NULL DEFAULT '[]',
        created_at TEXT DEFAULT (datetime('now'))
    );
    CREATE TABLE IF NOT EXISTS chunks (
        bot_id TEXT NOT NULL,
        chunk_id TEXT NOT NULL,
        url TEXT,
        text TEXT NOT NULL,
        PRIMARY KEY (bot_id, chunk_id)
    );
    """)
    cols = [r["name"] for r in c.execute("PRAGMA table_info(bots)").fetchall()]
    if "aliases" not in cols:
        c.execute("ALTER TABLE bots ADD COLUMN aliases TEXT NOT NULL DEFAULT '{}'")
    c.commit()
    c.close()


# ---- ingestion ---------------------------------------------------------

def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def scrape(url: str) -> list[dict]:
    """Fetch a page and return heading/paragraph chunks."""
    r = requests.get(url, headers=UA, timeout=25)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()
    main = soup.find("main") or soup.body or soup
    chunks, cur = [], None
    for el in main.find_all(["h1", "h2", "h3", "p", "li", "pre", "blockquote"]):
        if el.name in ("h1", "h2", "h3"):
            cur = {"heading": clean_text(el.get_text()), "text": ""}
            chunks.append(cur)
        elif cur is not None:
            t = clean_text(el.get_text())
            if len(t) > 20:
                cur["text"] = (cur["text"] + " " + t).strip()
    return [{"text": f"{c['heading']}. {c['text']}"} for c in chunks if len(c["text"]) > 40]


def ingest(bot_id: str, urls: list[str]) -> int:
    c = db()
    c.execute("DELETE FROM chunks WHERE bot_id=?", (bot_id,))
    n = 0
    for url in urls:
        try:
            for ch in scrape(url):
                cid = hashlib.md5(f"{bot_id}:{url}:{n}".encode()).hexdigest()[:16]
                c.execute("INSERT OR REPLACE INTO chunks VALUES (?,?,?,?)", (bot_id, cid, url, ch["text"]))
                n += 1
        except Exception as e:
            print(f"[ingest skip] {url}: {e}")
    c.commit()
    c.close()
    return n


# ---- retrieval ---------------------------------------------------------

def retrieve(bot_id: str, query: str, top_k: int = 4) -> list[dict]:
    c = db()
    rows = c.execute("SELECT url, text FROM chunks WHERE bot_id=?", (bot_id,)).fetchall()
    c.close()
    if not rows:
        return []
    if not HAS_SKLEARN:
        # crude fallback: keyword overlap + substring/prefix match
        q = query.lower()
        qw = set(re.findall(r"[a-z0-9]+", q))
        scored = []
        for r in rows:
            low = r["text"].lower()
            tw = set(re.findall(r"[a-z0-9]+", low))
            overlap = len(qw & tw)
            # nickname/partial-word boost: 'rick' must still match 'ricky'
            partial = sum(1 for t in qw if len(t) >= 3 and t in low)
            scored.append((overlap + partial, dict(r)))
        scored.sort(key=lambda x: -x[0])
        return [r for s, r in scored[:top_k] if s > 0]
    corpus = [r["text"] for r in rows]

    def ranked(sim, floor):
        out = []
        for i in sim.argsort()[-top_k:][::-1]:
            if float(sim[i]) > floor:
                out.append({"url": rows[i]["url"], "text": rows[i]["text"], "score": float(sim[i])})
        return out

    # Stage 1: word n-grams (semantic / phrase matching).
    v = TfidfVectorizer(stop_words="english", ngram_range=(1, 2)).fit_transform(corpus + [query])
    sim = cosine_similarity(v[-1], v[:-1]).flatten()
    best = float(sim.max()) if sim.size else 0.0
    if best >= 0.03:
        return ranked(sim, 0.01)

    # Stage 2: character n-grams — catches nicknames, typos and partial words,
    # so "who is rick" still finds the "Ricky Macharm" chunks. Stopwords are
    # stripped from the query first (sklearn ignores stop_words for char_wb),
    # so filler words ("what does ... do") don't dilute the name signal.
    tokens = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in ENGLISH_STOP_WORDS]
    q_clean = " ".join(tokens) if tokens else query.lower()
    vc = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5)).fit_transform(corpus + [q_clean])
    sim = cosine_similarity(vc[-1], vc[:-1]).flatten()
    return ranked(sim, 0.06)


# ---- generation --------------------------------------------------------

def generate(system: str, user: str, temperature: float = 0.3) -> str:
    if not API_KEY:
        return "⚠️ Chatbot backend not configured (no API key). Add OPENCODE_GO_API_KEY to ~/.hermes/.env."
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": 500,
    }
    r = requests.post(f"{BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {API_KEY}"}, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def _topics(bot_id: str, max_n: int = 6) -> str:
    """Short human-readable list of what a bot can talk about (from chunk headings)."""
    c = db()
    rows = c.execute("SELECT text FROM chunks WHERE bot_id=?", (bot_id,)).fetchall()
    c.close()
    topics, seen = [], set()
    for r in rows:
        head = re.split(r"[.\n]", r["text"])[0].strip()
        head = re.sub(r"^[\W_]+", "", head).strip()
        if 2 < len(head) < 70 and head.lower() not in seen:
            topics.append(head)
            seen.add(head.lower())
        if len(topics) >= max_n:
            break
    return ", ".join(topics) if topics else "questions about this site"


_GREETINGS = {
    "hi", "hii", "hiii", "hey", "heyy", "heyyy", "hello", "hallo", "howdy", "yo",
    "hiya", "sup", "greetings", "hola", "namaste", "welcome", "good morning",
    "good afternoon", "good evening", "good day", "hey there", "hi there",
    "hello there", "what's up", "whats up", "wassup",
}
_GREET_STARTS = {"hi", "hey", "hello", "hallo", "howdy", "hiya", "yo"}


def _is_greeting(q: str) -> bool:
    t = re.sub(r"[^a-z'\s]", " ", q.lower())
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return False
    if t in _GREETINGS:
        return True
    parts = t.split()
    return 1 <= len(parts) <= 3 and parts[0] in _GREET_STARTS


def _alias_note(q: str, aliases: dict) -> str:
    if not aliases:
        return ""
    ql = q.lower()
    for alias, canonical in aliases.items():
        if re.search(rf"\b{re.escape(str(alias).lower())}\b", ql) and str(canonical).lower() not in ql:
            return (
                f"\nNote: the visitor wrote \"{alias}\". The correct name is \"{canonical}\". "
                "If your answer mentions this person, gently and naturally clarify the correct name."
            )
    return ""


def _greet(bot_id: str, bot, question: str) -> str:
    topics = _topics(bot_id)
    prompt = (
        f"{bot['system_prompt']}\n\n"
        f"A visitor just greeted you: \"{question}\".\n"
        "Greet them back warmly and briefly (1–2 short sentences), then offer to help "
        "them find what they need on this site. Vary your wording each time. "
        f"Things you can help with: {topics}.\n"
        "No citations, no invented facts."
    )
    return generate(prompt, question, temperature=0.7)


def _normalize_lists(text: str) -> str:
    """Insert blank lines before bullet lists so CommonMark parsers recognize them.

    LLM output often writes a lead-in line directly above a `- ` list with no
    blank line; strict Markdown then treats the bullets as literal text.
    """
    bullet = re.compile(r"^[ \t]*[-*+][ \t]+")
    out = []
    for line in text.split("\n"):
        if bullet.match(line) and out and out[-1].strip() and not bullet.match(out[-1]):
            out.append("")
        out.append(line)
    return "\n".join(out)


def render_markdown(text: str) -> str:
    """Convert the model's Markdown answer to sanitized HTML for the widget."""
    if not text or not HAS_MARKDOWN:
        return text
    html = _markdown.markdown(_normalize_lists(text), extensions=["fenced_code", "tables", "sane_lists"])
    return nh3.clean(
        html,
        tags={"p", "br", "strong", "em", "code", "pre", "ul", "ol", "li", "a",
              "h1", "h2", "h3", "h4", "blockquote", "table", "thead", "tbody",
              "tr", "th", "td", "hr"},
        attributes={"a": {"href", "title"}, "th": {"align"}, "td": {"align"}},
    )


def answer(bot_id: str, question: str) -> dict:
    c = db()
    bot = c.execute("SELECT * FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
    c.close()
    if not bot:
        raise KeyError(bot_id)

    if _is_greeting(question):
        return {"answer": _greet(bot_id, bot, question), "sources": []}

    aliases = json.loads(bot["aliases"] or "{}")
    alias_note = _alias_note(question, aliases)

    ctx = retrieve(bot_id, question)
    if not ctx:
        # No matching content — respond like a human, not a canned refusal.
        topics = _topics(bot_id)
        fallback = (
            f"{bot['system_prompt']}\n\n"
            f"A visitor asked: \"{question}\"\n\n"
            "You could not find anything on this website that answers that. "
            "Reply the way a friendly, real person would — do NOT use a fixed or "
            "robotic phrase, and vary your wording from one reply to the next. "
            "Follow these rules:\n"
            "- If the question is clearly outside this website's scope (e.g. the "
            "weather, news, general-knowledge or personal questions), politely say you "
            "only help with this site's content and gently steer them back.\n"
            "- If it might relate to the site but you lack the details, ask one short, "
            "warm clarifying question, or offer a couple of things you can help with.\n"
            f"- Topics you can help with: {topics}.\n"
            "Keep it to 1–3 short sentences, no invented facts, no citations."
            f"{alias_note}"
        )
        return {"answer": generate(fallback, question, temperature=0.7), "sources": []}
    context = "\n\n".join(f"[{i+1}] {x['text']}" for i, x in enumerate(ctx))
    sys_prompt = (
        f"{bot['system_prompt']}\n\n"
        "Answer the visitor's question using ONLY the context below. "
        "Be concise, warm and friendly. If the context doesn't fully cover it, "
        "say so briefly and offer to help with something related. "
        "Cite sources by number like [1]."
        f"{alias_note}\n\n"
        f"CONTEXT:\n{context}"
    )
    ans = generate(sys_prompt, question)
    sources = list({x["url"] for x in ctx})
    return {"answer": ans, "sources": sources}


# ---- API / app ---------------------------------------------------------

class IngestReq(BaseModel):
    bot_id: str
    name: str = "Chatbot"
    urls: list[str]
    system_prompt: str = "You are a helpful customer-service assistant."
    brand_color: str = "#D4720A"
    aliases: dict = {}


class ChatReq(BaseModel):
    bot_id: str
    message: str


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="SisengAI Chatbot", version="1.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.get("/health")
    def health():
        return {"ok": True, "model": MODEL, "key_configured": bool(API_KEY), "sklearn": HAS_SKLEARN}

    @app.post("/ingest")
    def ingest_api(req: IngestReq):
        n = ingest(req.bot_id, req.urls)
        c = db()
        c.execute(
            "INSERT INTO bots(bot_id,name,system_prompt,brand_color,aliases,urls) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(bot_id) DO UPDATE SET name=excluded.name, system_prompt=excluded.system_prompt, "
            "brand_color=excluded.brand_color, aliases=excluded.aliases, urls=excluded.urls",
            (req.bot_id, req.name, req.system_prompt, req.brand_color, json.dumps(req.aliases), json.dumps(req.urls)),
        )
        c.commit()
        c.close()
        return {"ok": True, "bot_id": req.bot_id, "chunks": n}

    @app.post("/chat")
    def chat_api(req: ChatReq):
        try:
            res = answer(req.bot_id, req.message)
        except KeyError:
            raise HTTPException(404, "unknown bot_id")
        res["answer_html"] = render_markdown(res["answer"])
        return res

    @app.get("/widget/{bot_id}.js")
    def widget(bot_id: str):
        c = db()
        bot = c.execute("SELECT * FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
        c.close()
        if not bot:
            raise HTTPException(404, "unknown bot_id")
        color = bot["brand_color"] or "#D4720A"
        return PlainTextResponse(WIDGET_JS.replace("__BOT_ID__", bot_id).replace("__BRAND__", color),
                                 media_type="application/javascript")

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse(ADMIN_HTML)

    return app


WIDGET_JS = r"""
(function(){
  var SCRIPT = document.currentScript;
  var API = window.__SISENG_API__ || (SCRIPT && SCRIPT.src ? SCRIPT.src.slice(0, SCRIPT.src.indexOf("/widget/")) : "https://sisengai.com");
  var BOT_ID = "__BOT_ID__", BRAND = "__BRAND__";
  var INK = "#18181b", LINE = "#e4e4e7", MSG_BG = "#f4f4f5", MUTED = "#71717a";

  var host = document.createElement("div");
  host.id = "sisengai-chat";
  var root = host.attachShadow({ mode: "open" });

  var style = document.createElement("style");
  style.textContent =
    ":host{position:static;display:block;z-index:2147483000;color-scheme:light}" +
    "*{box-sizing:border-box;margin:0;padding:0;font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif}" +
    ".s-btn{position:fixed;right:18px;bottom:18px;width:58px;height:58px;border-radius:50%;border:2px solid #fff;background:" + BRAND + ";color:#fff;font-size:26px;line-height:1;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.4)}" +
    ".s-box{display:none;position:fixed;right:18px;bottom:88px;width:340px;max-width:calc(100vw - 36px);height:460px;max-height:calc(100vh - 130px);background:#fff;color:" + INK + ";border:1px solid #d4d4d8;border-radius:14px;box-shadow:0 16px 60px rgba(0,0,0,.5);overflow:hidden;flex-direction:column}" +
    ".s-head{background:" + BRAND + ";color:#fff;padding:13px 16px;font-weight:700;font-size:15px;flex:0 0 auto}" +
    ".s-msgs{flex:1 1 auto;overflow-y:auto;padding:14px;font-size:14px;line-height:1.45;background:" + MSG_BG + ";color:" + INK + "}" +
    ".s-msg{max-width:85%;margin:6px 0;padding:9px 13px;border-radius:12px;overflow-wrap:break-word;white-space:pre-wrap}" +
    ".s-msg.u{margin-left:auto;background:" + BRAND + ";color:#fff}" +
    ".s-msg.a{background:#fff;color:" + INK + ";border:1px solid " + LINE + ";white-space:normal}" +
    ".s-msg.a p{margin:0 0 8px}.s-msg.a p:last-child{margin-bottom:0}" +
    ".s-msg.a ul,.s-msg.a ol{margin:2px 0 8px;padding-left:20px}" +
    ".s-msg.a li{margin:2px 0}.s-msg.a li>ul,.s-msg.a li>ol{margin-bottom:0}" +
    ".s-msg.a strong{font-weight:700}.s-msg.a em{font-style:italic}" +
    ".s-msg.a code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12.5px;background:" + MSG_BG + ";border:1px solid " + LINE + ";padding:1px 5px;border-radius:4px}" +
    ".s-msg.a pre{background:" + MSG_BG + ";border:1px solid " + LINE + ";border-radius:8px;padding:10px 12px;margin:8px 0;overflow-x:auto;white-space:pre}" +
    ".s-msg.a pre code{background:none;border:none;padding:0;font-size:12.5px;display:block}" +
    ".s-msg.a a{color:" + BRAND + ";text-decoration:underline}" +
    ".s-msg.a h1,.s-msg.a h2,.s-msg.a h3,.s-msg.a h4{margin:8px 0 4px;font-weight:700;font-size:1.05em}" +
    ".s-msg.a blockquote{border-left:3px solid " + LINE + ";margin:6px 0;padding:2px 0 2px 10px;color:" + MUTED + "}" +
    ".s-msg.a hr{border:none;border-top:1px solid " + LINE + ";margin:8px 0}" +
    ".s-typing{color:" + MUTED + ";font-size:12px;margin:6px 0}" +
    ".s-bar{display:flex;flex:0 0 auto;border-top:1px solid " + LINE + ";background:#fff}" +
    ".s-in{flex:1;border:none;outline:none;padding:13px 14px;font-size:14px;background:#fff;color:" + INK + ";caret-color:" + BRAND + "}" +
    ".s-in::placeholder{color:" + MUTED + "}" +
    ".s-send{border:none;background:" + BRAND + ";color:#fff;padding:0 18px;font-weight:700;font-size:14px;cursor:pointer}" +
    ".s-send:hover{filter:brightness(1.08)}";

  root.appendChild(style);

  var btn = document.createElement("button");
  btn.className = "s-btn";
  btn.type = "button";
  btn.setAttribute("aria-label", "Open chat");
  btn.textContent = "💬";

  var box = document.createElement("div");
  box.className = "s-box";
  box.innerHTML =
    '<div class="s-head">Ask us anything</div>' +
    '<div class="s-msgs"></div>' +
    '<div class="s-bar"><input class="s-in" placeholder="Type a question…" aria-label="Your question" /><button class="s-send" type="button">Send</button></div>';

  root.appendChild(btn);
  root.appendChild(box);
  document.body.appendChild(host);

  var msgs = box.querySelector(".s-msgs");
  var input = box.querySelector(".s-in");
  var sendBtn = box.querySelector(".s-send");

  function add(who, txt, html) {
    var m = document.createElement("div");
    m.className = "s-msg " + (who === "u" ? "u" : "a");
    if (html) { m.innerHTML = html; } else { m.textContent = txt; }
    msgs.appendChild(m);
    msgs.scrollTop = msgs.scrollHeight;
  }

  btn.addEventListener("click", function () {
    var open = box.style.display === "flex";
    box.style.display = open ? "none" : "flex";
    if (!open) { input.focus(); }
  });

  function send() {
    var q = input.value.trim();
    if (!q) return;
    add("u", q);
    input.value = "";
    var t = document.createElement("div");
    t.className = "s-typing";
    t.textContent = "…";
    msgs.appendChild(t);
    fetch(API + "/chat", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ bot_id: BOT_ID, message: q }) })
      .then(function (r) { return r.json(); })
      .then(function (j) { if (t.parentNode) t.remove(); add("a", j.answer || "Sorry, try again.", j.answer_html); })
      .catch(function () { if (t.parentNode) t.remove(); add("a", "Error reaching assistant."); });
  }

  sendBtn.addEventListener("click", send);
  input.addEventListener("keydown", function (e) { if (e.key === "Enter") send(); });
})();
"""

ADMIN_HTML = """<!DOCTYPE html><html><head><title>SisengAI Chatbot Admin</title><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{font-family:system-ui,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;background:#0d1117;color:#e6edf3}
h1{color:#f0f6fc} label{display:block;margin:14px 0 4px;font-weight:600;font-size:14px}
input,textarea{width:100%;padding:10px;border:1px solid #30363d;border-radius:8px;background:#161b22;color:#e6edf3;font-family:inherit}
button{background:#D4720A;color:#fff;border:none;padding:12px 20px;border-radius:8px;font-weight:700;cursor:pointer;margin-top:16px}
code{background:#161b22;padding:2px 6px;border-radius:4px}</style></head><body>
<h1>SisengAI Chatbot — Admin</h1>
<p>Create a bot, then paste the widget script into the client's site.</p>
<label>Bot ID (slug)</label><input id="bid" placeholder="my-client">
<label>Bot name</label><input id="name" placeholder="ACME Support">
<label>URLs to ingest (one per line)</label><textarea id="urls" rows="4" placeholder="https://client.com&#10;https://client.com/faq"></textarea>
<label>System prompt</label><textarea id="prompt" rows="2">You are a helpful customer-service assistant.</textarea>
<label>Brand color</label><input id="color" value="#D4720A">
<button onclick="create()">Create &amp; Ingest</button>
<pre id="out" style="white-space:pre-wrap;background:#161b22;padding:14px;border-radius:8px;margin-top:16px"></pre>
<script>
async function create(){
  var urls=document.getElementById('urls').value.split('\\n').map(s=>s.trim()).filter(Boolean);
  var body={bot_id:document.getElementById('bid').value,name:document.getElementById('name').value,urls:urls,system_prompt:document.getElementById('prompt').value,brand_color:document.getElementById('color').value};
  var r=await fetch('/ingest',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  var j=await r.json();
  var bid=body.bot_id;
  document.getElementById('out').textContent = JSON.stringify(j,null,2)+"\\n\\nWIDGET SNIPPET (paste in client <head>):\\n<script src=\\"/widget/"+bid+".js\\"><\\/script>";
}
</script></body></html>"""


def main():
    if not HAS_FASTAPI:
        print("Missing dependencies. Run:  uv add fastapi uvicorn pydantic scikit-learn beautifulsoup4 lxml requests")
        raise SystemExit(1)
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()
    print(f"SisengAI Chatbot on http://{args.host}:{args.port}  (model={MODEL}, key={'yes' if API_KEY else 'NO'})")
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
