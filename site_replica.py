#!/usr/bin/env python3
"""
site-replica — turn any website's content into a beautiful, self-contained
static HTML replica. Built for SisengAI's "website makeover" service.

Usage:
    python3 site_replica.py https://example.com --out ./out --pages 5
    python3 site_replica.py --urls urls.txt --out ./out

Features:
  - Fetches one or more pages, follows internal links (bounded)
  - Extracts site name, nav, headings, body text, images, brand color
  - Emits a modern, responsive, self-contained HTML site (no external deps)
  - Generates a manifest.json of what it extracted (for review/QA)
"""
import argparse
import json
import re
import sys
import urllib.parse
from collections import OrderedDict
from pathlib import Path

import requests
from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0 (SisengAI Replica Bot; +https://sisengai.com)"}
TIMEOUT = 20

# ---------- extraction ----------

def norm_url(base: str, href: str) -> str | None:
    u = urllib.parse.urljoin(base, href)
    u = urllib.parse.urldefrag(u)[0].strip()
    if not u.startswith(("http://", "https://")):
        return None
    return u


def fetch(url: str) -> BeautifulSoup | None:
    try:
        r = requests.get(url, headers=UA, timeout=TIMEOUT)
        r.raise_for_status()
        if "text/html" not in r.headers.get("content-type", ""):
            return None
        return BeautifulSoup(r.text, "lxml")
    except Exception:
        return None


def extract_brand_color(soup: BeautifulSoup) -> str | None:
    """Best-effort detection of the site's primary brand color from CSS."""
    candidates = []
    for style in soup.find_all("style"):
        for m in re.finditer(r"(?:--primary|--brand|--accent)[^:]*:\s*(#[0-9a-fA-F]{3,8})", style.get_text()):
            candidates.append(m.group(1))
    for el in soup.select('[style*="background"]'):
        m = re.search(r"background(?:-color)?\s*:\s*(#[0-9a-fA-F]{3,8})", el.get("style", ""))
        if m:
            candidates.append(m.group(1))
    # most common color wins
    if candidates:
        return max(set(candidates), key=candidates.count)
    return None


def clean_text(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def extract_page(url: str, soup: BeautifulSoup, base: str) -> dict:
    title = soup.title.get_text(strip=True) if soup.title else url
    meta = soup.find("meta", attrs={"name": "description"})
    desc = meta.get("content", "").strip() if meta else ""

    # nav links (top N unique)
    nav = []
    seen = set()
    for a in soup.find_all("a"):
        txt = clean_text(a.get_text())
        href = a.get("href")
        if not txt or not href:
            continue
        target = norm_url(base, href)
        if target and target not in seen:
            seen.add(target)
            nav.append({"text": txt, "url": target})
        if len(nav) >= 20:
            break

    # main content: heading + following paragraphs, grouped into sections
    sections = []
    cur = None
    main = soup.find("main") or soup.body or soup
    for el in main.find_all(["h1", "h2", "h3", "p", "li"]):
        if el.name in ("h1", "h2", "h3"):
            cur = {"heading": clean_text(el.get_text()), "level": int(el.name[1]), "body": []}
            sections.append(cur)
        elif el.name in ("p", "li") and cur is not None:
            t = clean_text(el.get_text())
            if len(t) > 20:  # skip nav crumbs / junk
                cur["body"].append(t)
    sections = [s for s in sections if s["body"]]

    # images
    images = []
    for img in main.find_all("img"):
        src = img.get("src") or img.get("data-src")
        if not src:
            continue
        src = urllib.parse.urljoin(base, src)
        if src.startswith(("http://", "https://")):
            images.append({"src": src, "alt": img.get("alt", "")})

    return {
        "url": url,
        "title": title,
        "description": desc,
        "brand_color": extract_brand_color(soup),
        "nav": nav[:20],
        "sections": sections,
        "images": images[:20],
    }


# ---------- rendering ----------

TPL = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
:root{{color-scheme:light;--brand:{brand};--ink:#16181d;--muted:#5b6270;--bg:#ffffff;--card:#f6f7f9;--line:#e6e8ee}}
@media (prefers-color-scheme:dark){{:root{{--ink:#f2f4f8;--muted:#9aa3b2;--bg:#0d1117;--card:#161b22;--line:#232a34}}}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;color:var(--ink);background:var(--bg);line-height:1.65}}
.wrap{{max-width:900px;margin:0 auto;padding:24px 20px 80px}}
nav{{position:sticky;top:0;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);padding:12px 20px;overflow-x:auto;white-space:nowrap}}
nav a{{color:var(--ink);text-decoration:none;font-weight:600;margin-right:18px;font-size:14px}}
nav a:hover{{color:var(--brand)}}
h1{{font-size:clamp(1.8rem,4.5vw,2.8rem);line-height:1.2;margin:0 0 8px}}
h2{{font-size:1.5rem;margin:40px 0 10px;padding-bottom:6px;border-bottom:2px solid var(--brand)}}
h3{{font-size:1.15rem;margin:26px 0 8px}}
p{{color:var(--muted)}}
.hero{{padding:48px 0 16px}}
.hero p.desc{{font-size:1.15rem;max-width:640px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 22px;margin:16px 0}}
.img-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px;margin:20px 0}}
.img-grid img{{width:100%;border-radius:10px;border:1px solid var(--line)}}
.tag{{display:inline-block;background:color-mix(in srgb,var(--brand) 14%,transparent);color:var(--brand);border-radius:999px;padding:2px 12px;font-size:12px;font-weight:700;margin:0 6px 8px 0}}
footer{{border-top:1px solid var(--line);margin-top:60px;padding:20px 0;color:var(--muted);font-size:13px}}
@media print{{nav{{display:none}}}}
</style>
</head>
<body>
<nav>{navlinks}</nav>
<div class="wrap">
<div class="hero">
<h1>{title}</h1>
<p class="desc">{description}</p>
</div>
{sections}
{images}
<footer>Replica generated by SisengAI &middot; source: <span style="word-break:break-all">{source}</span></footer>
</div>
</body>
</html>
"""

SECTION_TPL = """<h2>{heading}</h2>
{paragraphs}
"""


def render_page(page: dict) -> str:
    navlinks = "".join(
        f'<a href="{p["url"]}">{html_escape(p["text"])}</a>' for p in page["nav"]
    )
    sections = ""
    for s in page["sections"]:
        if s["level"] == 1:
            # main title already rendered in hero; keep it as intro
            continue
        paras = "\n".join(f"<p>{html_escape(t)}</p>" for t in s["body"])
        sections += SECTION_TPL.format(heading=html_escape(s["heading"]), paragraphs=paras)
    images = ""
    if page["images"]:
        imgs = "".join(
            f'<img src="{html_escape(i["src"])}" alt="{html_escape(i["alt"])}" loading="lazy">'
            for i in page["images"]
        )
        images = f'<h2>Gallery</h2><div class="img-grid">{imgs}</div>'
    brand = page.get("brand_color") or "#D4720A"
    return TPL.format(
        title=html_escape(page["title"]),
        description=html_escape(page["description"]) or " ",
        navlinks=navlinks,
        sections=sections,
        images=images,
        source=html_escape(page["url"]),
        brand=brand,
    )


def html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------- driver ----------

def main():
    ap = argparse.ArgumentParser(description="Build a beautiful HTML replica of a website")
    ap.add_argument("url", nargs="?", help="starting URL")
    ap.add_argument("--urls", help="file with one URL per line")
    ap.add_argument("--out", default="./replica", help="output directory")
    ap.add_argument("--pages", type=int, default=6, help="max internal pages to fetch")
    args = ap.parse_args()

    if args.urls:
        seeds = [l.strip() for l in open(args.urls) if l.strip()]
    elif args.url:
        seeds = [args.url]
    else:
        ap.error("provide a URL or --urls file")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    queue = list(seeds)
    visited: OrderedDict[str, dict] = OrderedDict()
    base_host = urllib.parse.urlparse(seeds[0]).netloc

    while queue and len(visited) < args.pages:
        url = queue.pop(0)
        if url in visited:
            continue
        soup = fetch(url)
        if soup is None:
            print(f"[skip] {url}", file=sys.stderr)
            continue
        page = extract_page(url, soup, url)
        visited[url] = page
        print(f"[ok] {url} — {page['title']}")
        # enqueue internal links (same host, html)
        for p in page["nav"]:
            if urllib.parse.urlparse(p["url"]).netloc == base_host and p["url"] not in visited:
                queue.append(p["url"])

    # write pages
    index_pages = []
    for i, (url, page) in enumerate(visited.items()):
        slug = f"page-{i}.html" if i else "index.html"
        (out / slug).write_text(render_page(page))
        index_pages.append({"slug": slug, "title": page["title"], "url": url, "sections": len(page["sections"]), "images": len(page["images"])})

    # manifest
    (out / "manifest.json").write_text(json.dumps({"pages": index_pages}, indent=2))

    print(f"\nDone. {len(visited)} pages → {out}/")
    print(json.dumps(index_pages, indent=2))


if __name__ == "__main__":
    main()
