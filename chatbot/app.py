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
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    HAS_SKLEARN = True
except ImportError:
    HAS_SKLEARN = False

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
    for el in main.find_all(["h1", "h2", "h3", "p", "li"]):
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
        # crude fallback: keyword overlap
        qw = set(query.lower().split())
        scored = []
        for r in rows:
            tw = set(r["text"].lower().split())
            scored.append((len(qw & tw), dict(r)))
        scored.sort(key=lambda x: -x[0])
        return [r for s, r in scored[:top_k] if s > 0]
    corpus = [r["text"] for r in rows]
    v = TfidfVectorizer(stop_words="english", ngram_range=(1, 2)).fit_transform(corpus + [query])
    sim = cosine_similarity(v[-1], v[:-1]).flatten()
    top = sim.argsort()[-top_k:][::-1]
    return [{"url": rows[i]["url"], "text": rows[i]["text"], "score": float(sim[i])} for i in top if sim[i] > 0.01]


# ---- generation --------------------------------------------------------

def generate(system: str, user: str) -> str:
    if not API_KEY:
        return "⚠️ Chatbot backend not configured (no API key). Add OPENCODE_GO_API_KEY to ~/.hermes/.env."
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
        "max_tokens": 500,
    }
    r = requests.post(f"{BASE_URL}/chat/completions", headers={"Authorization": f"Bearer {API_KEY}"}, json=payload, timeout=60)
    r.raise_for_status()
    data = r.json()
    return data["choices"][0]["message"]["content"].strip()


def answer(bot_id: str, question: str) -> dict:
    c = db()
    bot = c.execute("SELECT * FROM bots WHERE bot_id=?", (bot_id,)).fetchone()
    c.close()
    if not bot:
        raise KeyError(bot_id)
    ctx = retrieve(bot_id, question)
    if not ctx:
        return {"answer": "I don't have enough content from this site to answer that yet.", "sources": []}
    context = "\n\n".join(f"[{i+1}] {x['text']}" for i, x in enumerate(ctx))
    sys_prompt = (
        f"{bot['system_prompt']}\n\n"
        "Answer the visitor's question using ONLY the context below. "
        "Be concise and friendly. If the context doesn't contain the answer, say so. "
        "Cite sources by number like [1].\n\n"
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
            "INSERT INTO bots(bot_id,name,system_prompt,brand_color,urls) VALUES(?,?,?,?,?) "
            "ON CONFLICT(bot_id) DO UPDATE SET name=excluded.name, system_prompt=excluded.system_prompt, "
            "brand_color=excluded.brand_color, urls=excluded.urls",
            (req.bot_id, req.name, req.system_prompt, req.brand_color, json.dumps(req.urls)),
        )
        c.commit()
        c.close()
        return {"ok": True, "bot_id": req.bot_id, "chunks": n}

    @app.post("/chat")
    def chat_api(req: ChatReq):
        try:
            return answer(req.bot_id, req.message)
        except KeyError:
            raise HTTPException(404, "unknown bot_id")

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
  var d = document.createElement("div");
  d.id = "sisengai-chat";
  d.innerHTML = '<button id="s-btn" style="position:fixed;right:18px;bottom:18px;z-index:99999;width:56px;height:56px;border-radius:50%;border:none;background:'+BRAND+';color:#fff;font-size:26px;cursor:pointer;box-shadow:0 4px 16px rgba(0,0,0,.25)">💬</button>'+
    '<div id="s-box" style="display:none;position:fixed;right:18px;bottom:84px;z-index:99999;width:340px;max-width:90vw;height:440px;background:#fff;border-radius:14px;box-shadow:0 8px 40px rgba(0,0,0,.3);overflow:hidden;font-family:system-ui,sans-serif">'+
    '<div style="background:'+BRAND+';color:#fff;padding:12px 16px;font-weight:700">Ask us anything</div>'+
    '<div id="s-msgs" style="height:330px;overflow-y:auto;padding:14px;font-size:14px;background:#f7f8fa"></div>'+
    '<div style="display:flex;border-top:1px solid #eee"><input id="s-in" placeholder="Type a question…" style="flex:1;border:none;padding:12px;font-size:14px;outline:none"><button id="s-send" style="border:none;background:'+BRAND+';color:#fff;padding:0 16px;font-weight:700">Send</button></div></div>';
  document.body.appendChild(d);
  function add(who, txt){ var m=document.createElement('div'); m.style.cssText='margin:6px 0;padding:8px 12px;border-radius:12px;max-width:85%;'+(who==='u'?'margin-left:auto;background:'+BRAND+';color:#fff':'background:#fff;border:1px solid #eee'); m.textContent=txt; document.getElementById('s-msgs').appendChild(m); document.getElementById('s-msgs').scrollTop=1e9; }
  document.getElementById('s-btn').onclick=function(){var b=document.getElementById('s-box');b.style.display=b.style.display==='none'?'block':'none';};
  function send(){ var q=document.getElementById('s-in').value.trim(); if(!q)return; add('u',q); document.getElementById('s-in').value=''; var t=document.createElement('div');t.id='s-typing';t.style.cssText='margin:6px 0;color:#999;font-size:12px';t.textContent='…';document.getElementById('s-msgs').appendChild(t);
    fetch(API+'/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({bot_id:BOT_ID,message:q})})
    .then(function(r){return r.json()}).then(function(j){var tp=document.getElementById('s-typing');if(tp)tp.remove();add('a',j.answer||'Sorry, try again.');}).catch(function(){var tp=document.getElementById('s-typing');if(tp)tp.remove();add('a','Error reaching assistant.');}); }
  document.getElementById('s-send').onclick=send; document.getElementById('s-in').addEventListener('keydown',function(e){if(e.key==='Enter')send();});
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
