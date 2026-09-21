#!/usr/bin/env python3
"""Separate public Website Research Assistant.

This service is intentionally independent of the SisengAI support chatbot and
its ingestion routes. Run it on its own origin, e.g. research.sisengai.com.
"""
import argparse, hashlib, ipaddress, json, os, re, socket, sqlite3, time, urllib.parse, urllib.robotparser
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import uvicorn

try:
    import markdown as _markdown
    import nh3
    HAS_MARKDOWN = True
except Exception:
    _markdown = nh3 = None
    HAS_MARKDOWN = False

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


def _research_key() -> str:
    """API key for the research service. Nous Portal tokens are read fresh from
    auth.json (never refreshed here — single-use refresh tokens are Hermes-only);
    otherwise use the env key matching the base URL."""
    if "nousresearch" in BASE_URL:
        from nous_token import get_nous_token
        return get_nous_token()
    if "openrouter" in BASE_URL:
        return os.environ.get("OPENROUTER_API_KEY", "")
    return (os.environ.get("WEBSITE_RESEARCH_API_KEY")
            or os.environ.get("OPENCODE_GO_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or os.environ.get("OPENCODE_ZEN_API_KEY") or "")


API_KEY = _research_key()
DATA_DIR = Path(os.environ.get("WEBSITE_RESEARCH_DATA", Path.home() / "sisengai/website-research/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "research.sqlite"
UA = "SisengAI Website Research Assistant/1.0 (+https://www.sisengai.com)"
MAX_BYTES, MAX_CHARS, TTL = 750000, 28000, 86400

# --- site-level indexing -------------------------------------------------
# The assistant indexes the SITE the visitor names, not just the page they
# pasted: a homepage is rarely where the answer lives.
MAX_PAGES = int(os.environ.get("WEBSITE_RESEARCH_MAX_PAGES", "25"))
MAX_PAGE_CHARS = 6000          # per page, kept for retrieval
SITE_CONTEXT_CHARS = 18000     # how much page text one question may see
SITE_CACHE_SECONDS = int(os.environ.get("WEBSITE_RESEARCH_CACHE_MINUTES", "5")) * 60
SKIP_EXT = (".pdf", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js",
            ".ico", ".xml", ".mp4", ".mp3", ".wav", ".woff", ".woff2", ".csv", ".xlsx", ".docx", ".rss")
STOPWORDS = set("the and for with that this what does about from your you are its it was were has have how who when where which their there then than into onto over under not but can could would should may might will also any all more most some such only very says say tell page pages site website mention mentions much many get got use used using is of to in on at as by or if do did".split())


def origin_of(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _dedupe(urls):
    out, seen = [], set()
    for url in urls:
        key = url.rstrip("/")
        if key and key not in seen:
            seen.add(key)
            out.append(url)
    return out
LIMITS, BUCKETS = {"analyse": 30, "chat": 200}, defaultdict(lambda: defaultdict(deque))

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
    ); CREATE INDEX IF NOT EXISTS research_session_expiry ON research_sessions(expires_at);
    CREATE TABLE IF NOT EXISTS site_cache (
      origin TEXT PRIMARY KEY, pages_json TEXT NOT NULL, fetched_at INTEGER NOT NULL
    );""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(research_sessions)")}
    if "pages_json" not in columns:
        conn.execute("ALTER TABLE research_sessions ADD COLUMN pages_json TEXT")
    conn.commit()
    conn.close()

def client_ip(request):
    # Configure the reverse proxy to overwrite this header before trusting it.
    forwarded = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return forwarded or (request.client.host if request.client else "unknown")


def origin_allowed(origin, allowed_origins):
    """Allow a missing Origin (curl, server-to-server) but reject browsers from
    unapproved origins. Mirrors the /contact endpoint's CSRF-style protection —
    the parent app's wildcard CORS must not let foreign sites drive /research."""
    if not origin:
        return True
    return origin.rstrip("/") in allowed_origins

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

_ROBOTS_CACHE: dict = {}

def robots_allows(url):
    """Cached per origin — crawling 25 pages must not mean 25 robots.txt fetches."""
    parts = urllib.parse.urlsplit(url)
    origin = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    if origin not in _ROBOTS_CACHE:
        parser = urllib.robotparser.RobotFileParser()
        parser.set_url(origin + "/robots.txt")
        try:
            parser.read()
            _ROBOTS_CACHE[origin] = parser
        except Exception:
            _ROBOTS_CACHE[origin] = None
    parser = _ROBOTS_CACHE[origin]
    return True if parser is None else parser.can_fetch(UA, url)

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

def sitemap_pages(start_url):
    """The site's own page list, if it publishes one (Quarto, WordPress, etc.)."""
    origin = origin_of(start_url)
    found = []
    for path in ("/sitemap.xml", "/blog/sitemap.xml", "/sitemap_index.xml"):
        try:
            response = requests.get(origin + path, headers={"User-Agent": UA}, timeout=(5, 15))
            if response.status_code == 200:
                for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", response.text):
                    if loc.startswith(origin) and not loc.lower().endswith(SKIP_EXT):
                        found.append(loc.split("#")[0])
        except requests.RequestException:
            continue
    return _dedupe(found)[:MAX_PAGES]


def link_pages(page_url, html):
    """Same-host links on a page — the fallback when a site has no sitemap."""
    soup = BeautifulSoup(html, "lxml")
    host = urllib.parse.urlsplit(page_url).netloc
    found = []
    for anchor in soup.find_all("a", href=True):
        target = urllib.parse.urljoin(page_url, anchor["href"]).split("#")[0]
        parts = urllib.parse.urlsplit(target)
        if parts.scheme in {"http", "https"} and parts.netloc == host and not parts.path.lower().endswith(SKIP_EXT):
            found.append(urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, "")))
    return _dedupe(found)[:MAX_PAGES]


def read_page(url):
    """One page, with every public-host / robots / size check the single-page
    path already applied. Returns None when the page is unusable."""
    try:
        final_url, html = fetch_public_html(url)
        title, text = extract_page(html)
        return {"url": final_url, "title": title, "text": text[:MAX_PAGE_CHARS]}
    except HTTPException as exc:
        print(f"[crawl skip] {url}: {exc.detail}")
    except Exception as exc:
        print(f"[crawl skip] {url}: {exc}")
    return None


def crawl_site(start_url):
    """Read the whole site: the named page plus up to MAX_PAGES-1 neighbours.

    Sitemap first (so a new blog post is included with no configuration), the
    page's own links second. Pages are fetched in parallel and every one of them
    still passes the robots and public-host checks.
    """
    start_url, html = fetch_public_html(start_url)
    title, text = extract_page(html)
    pages = [{"url": start_url, "title": title, "text": text[:MAX_PAGE_CHARS]}]
    targets = [u for u in sitemap_pages(start_url) if u.rstrip("/") != start_url.rstrip("/")]
    if not targets:
        targets = [u for u in link_pages(start_url, html) if u.rstrip("/") != start_url.rstrip("/")]
    targets = targets[: max(0, MAX_PAGES - 1)]
    if targets:
        with ThreadPoolExecutor(max_workers=5) as pool:
            for page in pool.map(read_page, targets):
                if page and page["text"].strip():
                    pages.append(page)
    return start_url, pages


def site_digest(pages):
    listing = "PAGES ON THIS SITE (" + str(len(pages)) + "): " + "; ".join(
        f"{p['title']} — {p['url']}" for p in pages)
    body = "\n\n".join(f"PAGE: {p['title']} — {p['url']}\n{p['text']}" for p in pages)
    return (listing + "\n\n" + body)[:MAX_CHARS]


def relevant_pages(pages, question, budget=SITE_CONTEXT_CHARS):
    """Pick the pages worth showing the model for this question.

    Word-overlap retrieval over the whole indexed site, so a question about
    Lua/Torch reaches the blog post that covers it instead of the homepage.
    Returns (text blocks, page urls used).
    """
    words = [w for w in re.findall(r"[a-z0-9']{3,}", question.lower()) if w not in STOPWORDS]
    scored = []
    for page in pages:
        low = page["text"].lower()
        scored.append((sum(low.count(w) for w in words), page))
    scored.sort(key=lambda item: -item[0])
    blocks, urls, used = [], [], 0
    for hits, page in scored:
        if hits == 0 and blocks:
            break                      # nothing matched: stop padding the prompt
        block = f"PAGE: {page['title']} — {page['url']}\n{page['text']}"
        if used + len(block) > budget:
            block = block[: max(0, budget - used)]
        if len(block.strip()) < 40:
            break
        blocks.append(block)
        urls.append(page["url"])
        used += len(block)
        if used >= budget:
            break
    if not blocks:
        blocks = [f"PAGE: {p['title']} — {p['url']}\n{p['text'][:4000]}" for p in pages[:2]]
        urls = [p["url"] for p in pages[:2]]
        blocks.append("(The visitor's words matched no indexed page. Say plainly that the site does not cover it, "
                      "and name what the site does cover.)")
    return blocks, urls


FALLBACK_MODELS = [m.strip() for m in os.environ.get("WEBSITE_RESEARCH_FALLBACK_MODELS", os.environ.get("CHATBOT_FALLBACK_MODELS", "")).split(",") if m.strip()]


def model_call(system, user, max_tokens):
    if not API_KEY and "openrouter" not in BASE_URL and "nousresearch" not in BASE_URL:
        raise HTTPException(503, "The research assistant is not configured yet. Please try again later.")
    models = [MODEL] + FALLBACK_MODELS
    last_error = None
    for attempt in range(2):  # retry whole chain once to ride out free-tier blips
        for candidate in models:
            # route: primary → configured backend (Nous token read fresh per
            # call so expiry self-heals); fallbacks → OpenRouter free models
            if "nousresearch" in BASE_URL and candidate == MODEL:
                from nous_token import get_nous_token
                base, key = BASE_URL, get_nous_token()
            elif candidate != MODEL:
                base, key = "https://openrouter.ai/api/v1", os.environ.get("OPENROUTER_API_KEY", "")
            else:
                base, key = BASE_URL, API_KEY
            if not key:
                continue
            try:
                response = requests.post(base.rstrip("/") + "/chat/completions", headers={"Authorization": "Bearer " + key}, json={"model": candidate, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.25, "max_tokens": max_tokens}, timeout=55)
                response.raise_for_status()
                content = response.json()["choices"][0]["message"].get("content")
                if not content or not content.strip():  # some free models return null content
                    raise ValueError("empty model response")
                return content.strip()
            except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = exc
                continue
    raise HTTPException(503, "The research assistant could not complete that request. Please try again shortly.") from last_error

def _normalize_lists(text: str) -> str:
    """Insert blank lines before list markers so CommonMark renders them as lists."""
    if not text:
        return text
    out, prev = [], ""
    for line in text.splitlines():
        stripped = line.lstrip()
        if re.match(r"^([-*+]|\d+[.)])\s+", stripped) and not re.match(r"^\s{4,}", line):
            if prev and prev.strip():
                out.append("")
        out.append(line)
        prev = line
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


def _clean_brief(candidate):
    """Parse the model's JSON brief, tolerating code fences and models that
    double-encode the JSON inside a string field (e.g. summary='```json\n{...}')."""
    candidate, fence = candidate.strip(), chr(96) * 3
    if candidate.startswith(fence):
        candidate = candidate.split("\n", 1)[-1].rsplit(fence, 1)[0].strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def create_brief(source_url, title, source, page_count=1):
    system = """Create a practical, source-grounded website brief for a business owner.
Use ONLY the public page text of this one site (several of its pages may be included). Return JSON only:
{"summary":"one concise paragraph","offer":["up to 3 bullets"],"audience":["up to 3 bullets"],"clarity":["up to 3 observations"],"opportunity":{"title":"one AI or automation opportunity","detail":"two concise sentences"},"questions":["three questions"]}
No explanations, no markdown, no format description — output the JSON object and nothing else.
Avoid hype, private-data suggestions, guarantees, and claims about unseen pages. Cover nothing that is not in the supplied page text — no weather, sports, politics, or outside facts."""
    raw = model_call(system, "SITE TITLE: " + title + "\nSITE URL: " + source_url + "\nPAGES READ: " + str(page_count)
                     + "\n\nPUBLIC PAGE TEXT:\n" + source, 800)
    brief = _clean_brief(raw)
    if brief is None and '"summary"' in raw:
        # Model double-encoded the JSON inside a string field: extract the inner fenced block.
        inner = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.S)
        if inner:
            brief = _clean_brief(inner.group(1))
    if brief is None:
        return {"summary": raw[:1200], "offer": [], "audience": [], "clarity": ["The page was read, but the structured brief needs another try."], "opportunity": {"title": "Review the visitor journey", "detail": "Identify the first repeated visitor question and make the answer easy to find."}, "questions": []}
    if isinstance(brief.get("summary"), str) and brief["summary"].lstrip().startswith(chr(96) * 3):
        inner = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", brief["summary"], re.S)
        if inner:
            inner_brief = _clean_brief(inner.group(1))
            if isinstance(inner_brief, dict):
                brief = {**inner_brief, **{k: v for k, v in brief.items() if k != "summary"}}
    return brief

class AnalyseRequest(BaseModel):
    url: str = Field(max_length=2048)
    force: bool = False  # skip the site cache and re-read every page (Reset / explicit re-index)

class ChatRequest(BaseModel):
    session_id: str = Field(min_length=16, max_length=80)
    message: str = Field(min_length=2, max_length=900)

def create_app():
    init_db()
    app = FastAPI(title="SisengAI Website Research Assistant", version="1.0")
    origins = [item.strip() for item in os.environ.get("WEBSITE_RESEARCH_ORIGINS", "https://www.sisengai.com,https://sisengai.com,http://127.0.0.1:22222").split(",") if item.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"], allow_headers=["content-type"])

    def require_origin(request: Request) -> None:
        if not origin_allowed(request.headers.get("origin"), origins):
            raise HTTPException(403, "Origin not allowed.")

    @app.get("/health")
    def health(request: Request):
        require_origin(request)
        return {"ok": True, "service": "website-research", "model": MODEL, "key_configured": bool(API_KEY)}

    @app.post("/analyse")
    def analyse(payload: AnalyseRequest, request: Request):
        require_origin(request)
        rate_limit(request, "analyse")
        now = int(time.time())
        origin = origin_of(normalise_url(payload.url))
        conn = db()
        cached = conn.execute("SELECT pages_json, fetched_at FROM site_cache WHERE origin=?", (origin,)).fetchone()
        if cached and not payload.force and now - cached["fetched_at"] < SITE_CACHE_SECONDS:
            pages = json.loads(cached["pages_json"])
            print(f"[analyse] {origin}: reusing a {len(pages)}-page index ({now - cached['fetched_at']}s old)")
        else:
            conn.close()
            start_url, pages = crawl_site(payload.url)
            conn = db()
            conn.execute("INSERT INTO site_cache(origin,pages_json,fetched_at) VALUES(?,?,?) "
                         "ON CONFLICT(origin) DO UPDATE SET pages_json=excluded.pages_json, fetched_at=excluded.fetched_at",
                         (origin, json.dumps(pages), now))
            print(f"[analyse] {origin}: indexed {len(pages)} pages")
        start_url = pages[0]["url"]
        title = pages[0]["title"]
        digest = site_digest(pages)
        brief = create_brief(start_url, title, digest, len(pages))
        session_id = hashlib.sha256((start_url + str(now) + os.urandom(16).hex()).encode()).hexdigest()[:32]
        conn.execute("DELETE FROM research_sessions WHERE expires_at < ?", (now,))
        conn.execute("INSERT INTO research_sessions(session_id,source_url,title,source_text,brief_json,pages_json,expires_at) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (session_id, start_url, title, digest, json.dumps(brief), json.dumps(pages), now + TTL))
        conn.commit()
        conn.close()
        return {"session_id": session_id, "source_url": start_url, "title": title, "pages": len(pages),
                "page_list": [{"title": page["title"], "url": page["url"]} for page in pages],
                "brief": brief, "expires_in_hours": 24}

    @app.post("/chat")
    def chat(payload: ChatRequest, request: Request):
        require_origin(request)
        rate_limit(request, "chat")
        conn = db()
        row = conn.execute("SELECT source_url,title,source_text,brief_json,pages_json,expires_at FROM research_sessions WHERE session_id=?",
                           (payload.session_id,)).fetchone()
        conn.close()
        if not row or row["expires_at"] < int(time.time()):
            raise HTTPException(404, "This research session has expired. Analyse the website again to continue.")
        if row["pages_json"]:
            pages = json.loads(row["pages_json"])
        else:
            pages = [{"url": row["source_url"], "title": row["title"], "text": row["source_text"]}]
        blocks, urls = relevant_pages(pages, payload.message)
        listing = "INDEXED PAGES ON THIS SITE (" + str(len(pages)) + "): " + "; ".join(
            page["title"] + " — " + page["url"] for page in pages[:40])
        system = ("You are a strict website analyst answering about ONE website. Answer ONLY from the indexed pages of "
                  "that site supplied below — no outside knowledge, no general facts, and nothing about weather, sports, "
                  "politics, or any other topic unless it is literally written in that text. If none of the indexed pages "
                  "cover what is asked, say so plainly and name the closest thing the site does cover; never guess and "
                  "never imply knowledge of a page you were not shown. Name the page you used (its title or path) in your "
                  "answer. Be concise and precise. Never browse, never infer private data, never claim an action was performed.")
        user = ("SITE: " + row["source_url"] + "\n" + listing + "\n\nMOST RELEVANT PAGE TEXT:\n"
                + "\n\n".join(blocks) + "\n\nVISITOR QUESTION: " + payload.message)
        answer = model_call(system, user, 500)
        return {"answer": answer, "answer_html": render_markdown(answer), "source_url": row["source_url"],
                "sources": urls, "pages": len(pages)}

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
