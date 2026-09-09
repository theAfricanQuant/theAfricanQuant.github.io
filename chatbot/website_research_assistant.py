#!/usr/bin/env python3
"""Separate public Website Research Assistant.

This service is intentionally independent of the SisengAI support chatbot and
its ingestion routes. Run it on its own origin, e.g. research.sisengai.com.
"""
import argparse, hashlib, ipaddress, json, os, socket, sqlite3, time, urllib.parse, urllib.robotparser
from collections import defaultdict, deque
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

def load_env():
    for path in (Path.home() / ".hermes/.env", Path(".env")):
        if path.exists():
            for raw in path.read_text().splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_env()
MODEL = os.environ.get("WEBSITE_RESEARCH_MODEL", os.environ.get("CHATBOT_MODEL", "deepseek-v4-flash"))
BASE_URL = os.environ.get("WEBSITE_RESEARCH_BASE_URL", os.environ.get("CHATBOT_BASE_URL", "https://opencode.ai/zen/go/v1"))
API_KEY = os.environ.get("WEBSITE_RESEARCH_API_KEY") or os.environ.get("OPENCODE_GO_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENCODE_ZEN_API_KEY") or ""
DATA_DIR = Path(os.environ.get("WEBSITE_RESEARCH_DATA", Path.home() / "sisengai/website-research/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "research.sqlite"
UA = "SisengAI Website Research Assistant/1.0 (+https://www.sisengai.com)"
MAX_BYTES, MAX_CHARS, TTL = 750000, 28000, 86400
LIMITS, BUCKETS = {"analyse": 4, "chat": 20}, defaultdict(lambda: defaultdict(deque))

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def init_db():
    conn = db()
    conn.executescript("""CREATE TABLE IF NOT EXISTS research_sessions (
      session_id TEXT PRIMARY KEY, source_url TEXT NOT NULL, title TEXT NOT NULL,
      source_text TEXT NOT NULL, brief_json TEXT NOT NULL, expires_at INTEGER NOT NULL
    ); CREATE INDEX IF NOT EXISTS research_session_expiry ON research_sessions(expires_at);""")
    conn.commit()
    conn.close()

def client_ip(request):
    # Configure the reverse proxy to overwrite this header before trusting it.
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")

def rate_limit(request, action):
    now, bucket = time.monotonic(), BUCKETS[client_ip(request)][action]
    while bucket and now - bucket[0] >= 3600:
        bucket.popleft()
    if len(bucket) >= LIMITS[action]:
        raise HTTPException(429, "Hourly limit reached. Please try again later.")
    bucket.append(now)

def normalise_url(value):
    value = value.strip()
    if not value:
        raise HTTPException(400, "Paste a website address first.")
    if "://" not in value:
        value = "https://" + value
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise HTTPException(400, "Use a complete public http or https website address.")
    if parsed.username or parsed.password or parsed.port not in {None, 80, 443}:
        raise HTTPException(400, "Use a normal public website address without credentials or a custom port.")
    host = parsed.hostname.rstrip(".").lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise HTTPException(400, "Local or private network addresses are not allowed.")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))

def assert_public_host(host):
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise HTTPException(400, "That website address could not be resolved.") from exc
    if not addresses:
        raise HTTPException(400, "That website address could not be resolved.")
    for address in addresses:
        try:
            allowed = ipaddress.ip_address(address).is_global
        except ValueError as exc:
            raise HTTPException(400, "That website address is invalid.") from exc
        if not allowed:
            raise HTTPException(400, "Local, private, and reserved network addresses are not allowed.")

def robots_allows(url):
    parts = urllib.parse.urlsplit(url)
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/robots.txt", "", "")))
    try:
        parser.read()
    except Exception:
        return True
    return parser.can_fetch(UA, url)

def fetch_public_html(value):
    """Follow a short redirect chain and check every destination hostname."""
    current = normalise_url(value)
    for _ in range(4):
        parts = urllib.parse.urlsplit(current)
        assert_public_host(parts.hostname or "")
        if not robots_allows(current):
            raise HTTPException(403, "This website's robots policy does not allow analysis of that page.")
        try:
            response = requests.get(current, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"}, timeout=(5, 18), stream=True, allow_redirects=False)
        except requests.RequestException as exc:
            raise HTTPException(422, "That website could not be reached right now.") from exc
        if response.is_redirect or response.is_permanent_redirect:
            location = response.headers.get("location")
            response.close()
            if not location:
                raise HTTPException(422, "The website returned an incomplete redirect.")
            current = normalise_url(urllib.parse.urljoin(current, location))
            continue
        if response.status_code >= 400:
            code = response.status_code
            response.close()
            raise HTTPException(422, "The website returned HTTP " + str(code) + ".")
        if "html" not in response.headers.get("content-type", "").lower():
            response.close()
            raise HTTPException(422, "That address did not return an HTML web page.")
        chunks, used = [], 0
        try:
            for chunk in response.iter_content(chunk_size=16384):
                used += len(chunk)
                if used > MAX_BYTES:
                    raise HTTPException(422, "That page is too large for the free website brief.")
                chunks.append(chunk)
            encoding = response.encoding or "utf-8"
        finally:
            response.close()
        return current, b"".join(chunks).decode(encoding, errors="replace")
    raise HTTPException(422, "That website redirected too many times.")

def extract_page(html):
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "nav", "footer", "header", "form"]):
        tag.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else "Website brief"
    root, blocks = soup.find("main") or soup.body or soup, []
    for element in root.find_all(["h1", "h2", "h3", "p", "li"]):
        text = " ".join(element.get_text(" ", strip=True).split())
        if 25 <= len(text) <= 900 and text not in blocks:
            blocks.append(text)
    source = "\n".join(blocks)
    if len(source) < 180:
        raise HTTPException(422, "There was not enough readable public content on that page to make a useful brief.")
    return title[:180], source[:MAX_CHARS]

def model_call(system, user, max_tokens):
    if not API_KEY:
        raise HTTPException(503, "The research assistant is not configured yet. Please try again later.")
    try:
        response = requests.post(BASE_URL.rstrip("/") + "/chat/completions", headers={"Authorization": "Bearer " + API_KEY}, json={"model": MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.25, "max_tokens": max_tokens}, timeout=55)
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except (requests.RequestException, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(503, "The research assistant could not complete that request. Please try again shortly.") from exc

def create_brief(source_url, title, source):
    system = """Create a practical, source-grounded website brief for a business owner.
Use only the public page text. Return JSON only:
{"summary":"one concise paragraph","offer":["up to 3 bullets"],"audience":["up to 3 bullets"],"clarity":["up to 3 observations"],"opportunity":{"title":"one AI or automation opportunity","detail":"two concise sentences"},"questions":["three questions"]}
Avoid hype, private-data suggestions, guarantees, and claims about unseen pages."""
    raw = model_call(system, "PAGE TITLE: " + title + "\nSOURCE URL: " + source_url + "\n\nPUBLIC PAGE TEXT:\n" + source, 800)
    candidate, fence = raw.strip(), chr(96) * 3
    if candidate.startswith(fence):
        candidate = candidate.split("\n", 1)[-1].rsplit(fence, 1)[0].strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return {"summary": raw[:1200], "offer": [], "audience": [], "clarity": ["The page was read, but the structured brief needs another try."], "opportunity": {"title": "Review the visitor journey", "detail": "Identify the first repeated visitor question and make the answer easy to find."}, "questions": []}

class AnalyseRequest(BaseModel):
    url: str = Field(max_length=2048)

class ChatRequest(BaseModel):
    session_id: str = Field(min_length=16, max_length=80)
    message: str = Field(min_length=2, max_length=900)

def create_app():
    init_db()
    app = FastAPI(title="SisengAI Website Research Assistant", version="1.0")
    origins = [item.strip() for item in os.environ.get("WEBSITE_RESEARCH_ORIGINS", "https://www.sisengai.com,https://sisengai.com,http://127.0.0.1:22222").split(",") if item.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["content-type"])

    @app.get("/health")
    def health():
        return {"ok": True, "service": "website-research", "model": MODEL, "key_configured": bool(API_KEY)}

    @app.post("/analyse")
    def analyse(payload: AnalyseRequest, request: Request):
        rate_limit(request, "analyse")
        source_url, html = fetch_public_html(payload.url)
        title, source = extract_page(html)
        brief, now = create_brief(source_url, title, source), int(time.time())
        session_id = hashlib.sha256((source_url + str(now) + os.urandom(16).hex()).encode()).hexdigest()[:32]
        conn = db()
        conn.execute("DELETE FROM research_sessions WHERE expires_at < ?", (now,))
        conn.execute("INSERT INTO research_sessions(session_id,source_url,title,source_text,brief_json,expires_at) VALUES(?,?,?,?,?,?)", (session_id, source_url, title, source, json.dumps(brief), now + TTL))
        conn.commit()
        conn.close()
        return {"session_id": session_id, "source_url": source_url, "title": title, "brief": brief, "expires_in_hours": 24}

    @app.post("/chat")
    def chat(payload: ChatRequest, request: Request):
        rate_limit(request, "chat")
        conn = db()
        row = conn.execute("SELECT source_url,title,source_text,expires_at FROM research_sessions WHERE session_id=?", (payload.session_id,)).fetchone()
        conn.close()
        if not row or row["expires_at"] < int(time.time()):
            raise HTTPException(404, "This research session has expired. Analyse the website again to continue.")
        system = """You are the SisengAI Website Research Assistant. Answer only from supplied public page text.
Be concise and precise. State when the page lacks evidence. Do not browse, infer private data, or claim an action was performed."""
        answer = model_call(system, "SOURCE URL: " + row["source_url"] + "\nPAGE TITLE: " + row["title"] + "\nPUBLIC PAGE TEXT:\n" + row["source_text"] + "\n\nVISITOR QUESTION: " + payload.message, 500)
        return {"answer": answer, "source_url": row["source_url"]}

    @app.get("/", response_class=HTMLResponse)
    def root():
        return "<h1>SisengAI Website Research Assistant</h1><p>Use the public page at sisengai.com/free-website-brief.</p>"

    return app

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    uvicorn.run(create_app(), host=args.host, port=args.port)
