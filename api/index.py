"""Word counter — Vercel serverless app.

Serves instant word counts from pre-indexed site caches bundled in the
repo (cache/*.json). Can also live-scan static sites within the
serverless time budget; JavaScript-rendered (SPA) sites cannot be
scanned here and must be pre-indexed locally (see webapp.py).
"""

import json
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request

DEFAULT_SITE = "https://www.aibusinessschool.com"
BUNDLED_DIR = Path(__file__).resolve().parent.parent / "cache"
TMP_DIR = Path("/tmp/findword_cache")

# Live-scan budget (must fit the serverless maxDuration)
SCAN_MAX_PAGES = 30
SCAN_TIME_BUDGET = 40          # seconds
SCAN_MAX_DEPTH = 4

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 FindWordBot/1.0")
SKIP_TAGS = {"script", "style", "noscript", "template"}
SKIP_EXTENSIONS = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp", ".ico",
    ".css", ".js", ".zip", ".rar", ".mp4", ".mp3", ".avi", ".mov",
    ".woff", ".woff2", ".ttf", ".eot", ".xml", ".json", ".doc", ".docx",
    ".xls", ".xlsx", ".ppt", ".pptx",
}
SPA_TEXT_THRESHOLD = 300

app = Flask(__name__)

# --- URL helpers -----------------------------------------------------------


def normalize_url(url):
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{path}" + (f"?{parsed.query}" if parsed.query else "")


def same_domain(url, root_netloc):
    netloc = urlparse(url).netloc.lower()
    root = root_netloc.lower()
    return netloc == root or netloc == f"www.{root}" or f"www.{netloc}" == root


def canonicalize(url, root_netloc):
    parsed = urlparse(url)
    if parsed.netloc.lower() != root_netloc.lower() and same_domain(url, root_netloc):
        url = url.replace(f"//{parsed.netloc}", f"//{root_netloc}", 1)
    return url


def is_crawlable(url):
    path = urlparse(url).path.lower()
    return not any(path.endswith(ext) for ext in SKIP_EXTENSIONS)


def site_key(url):
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def normalize_site_input(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/"


def resolve_site(raw):
    start_url = normalize_site_input(raw)
    if not start_url:
        return None, None
    return site_key(start_url), start_url


def cache_filename(key):
    return re.sub(r"[^a-z0-9.-]", "_", key) + ".json"


# --- Cache lookup ----------------------------------------------------------


def load_site(key):
    """Returns cache dict {crawled_at, pages, source} or None."""
    for directory, source in ((BUNDLED_DIR, "pre-indexed"), (TMP_DIR, "live scan")):
        f = directory / cache_filename(key)
        if f.exists():
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                data["source"] = source
                return data
            except Exception:
                continue
    return None


# --- Live scan (static sites only) ----------------------------------------


def extract_visible_text(soup):
    for tag in soup.find_all(SKIP_TAGS):
        tag.decompose()
    return soup.get_text(separator=" ")


def scan_site(start_url):
    """Limited synchronous crawl. Returns pages list; raises on failure."""
    root_netloc = urlparse(start_url).netloc
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    deadline = time.monotonic() + SCAN_TIME_BUDGET
    queue = [(canonicalize(normalize_url(start_url), root_netloc), 0)]
    visited = set()
    pages = []

    while queue and len(visited) < SCAN_MAX_PAGES and time.monotonic() < deadline:
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        try:
            resp = session.get(url, timeout=8)
        except requests.RequestException:
            continue
        if resp.status_code != 200 or "text/html" not in resp.headers.get("Content-Type", ""):
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        text = extract_visible_text(soup)

        # First page decides whether the site is scannable at all
        if not pages and len(text.strip()) < SPA_TEXT_THRESHOLD \
                and len(soup.find_all("a", href=True)) < 3:
            raise RuntimeError(
                "This site is JavaScript-rendered and cannot be scanned by the "
                "hosted version. It must be pre-indexed (see project README).")

        title = soup.title.get_text(strip=True) if soup.title else ""
        pages.append({"url": url, "title": title, "text": text})

        if depth < SCAN_MAX_DEPTH:
            for a in soup.find_all("a", href=True):
                link = canonicalize(normalize_url(urljoin(url, a["href"])), root_netloc)
                if (link.startswith("http") and same_domain(link, root_netloc)
                        and is_crawlable(link) and link not in visited):
                    queue.append((link, depth + 1))

    if not pages:
        raise RuntimeError("No pages could be fetched from this site.")
    return pages


# --- API -------------------------------------------------------------------


@app.get("/api/status")
def api_status():
    key, _ = resolve_site(request.args.get("site"))
    if not key:
        return jsonify({"error": "Invalid site URL."}), 400
    data = load_site(key)
    if not data:
        return jsonify({"status": "empty", "page_count": 0, "crawled_at": None})
    return jsonify({"status": "ready", "page_count": len(data["pages"]),
                    "crawled_at": data.get("crawled_at"),
                    "source": data.get("source")})


@app.post("/api/scan")
def api_scan():
    key, start_url = resolve_site(request.args.get("site"))
    if not key:
        return jsonify({"error": "Invalid site URL."}), 400
    if (BUNDLED_DIR / cache_filename(key)).exists():
        data = load_site(key)
        return jsonify({"status": "ready", "page_count": len(data["pages"]),
                        "crawled_at": data.get("crawled_at"),
                        "source": "pre-indexed"})
    try:
        pages = scan_site(start_url)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 422
    crawled_at = datetime.utcnow().strftime("%d.%m.%Y %H:%M UTC")
    TMP_DIR.mkdir(parents=True, exist_ok=True)
    (TMP_DIR / cache_filename(key)).write_text(
        json.dumps({"crawled_at": crawled_at, "pages": pages}, ensure_ascii=False),
        encoding="utf-8")
    return jsonify({"status": "ready", "page_count": len(pages),
                    "crawled_at": crawled_at, "source": "live scan"})


@app.get("/api/count")
def api_count():
    key, _ = resolve_site(request.args.get("site"))
    if not key:
        return jsonify({"error": "Invalid site URL."}), 400
    word = (request.args.get("word") or "").strip()
    whole_word = request.args.get("whole_word", "1") != "0"
    if not word:
        return jsonify({"error": "Word cannot be empty."}), 400
    data = load_site(key)
    if not data:
        return jsonify({"error": "This site has not been scanned yet."}), 409

    escaped = re.escape(word)
    if whole_word:
        escaped = rf"\b{escaped}\b"
    pattern = re.compile(escaped, re.IGNORECASE)

    variants = Counter()
    per_page = []
    for p in data["pages"]:
        matches = pattern.findall(p["text"])
        if matches:
            variants.update(matches)
            path = urlparse(p["url"]).path or "/"
            per_page.append({"url": p["url"], "title": p["title"],
                             "path": path, "count": len(matches)})
    per_page.sort(key=lambda r: -r["count"])

    return jsonify({
        "word": word,
        "total": sum(variants.values()),
        "variants": [{"text": v, "count": c} for v, c in variants.most_common()],
        "pages": per_page,
        "page_count": len(data["pages"]),
        "crawled_at": data.get("crawled_at"),
    })


# --- UI --------------------------------------------------------------------

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Word Counter</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --text:#1a1d21; --muted:#6b7280;
          --accent:#2563eb; --border:#e5e7eb; }
  * { box-sizing:border-box; margin:0; }
  body { font-family:'Segoe UI',system-ui,sans-serif; background:var(--bg);
         color:var(--text); min-height:100vh; padding:2rem 1rem; }
  .wrap { max-width:860px; margin:0 auto; }
  h1 { font-size:1.5rem; margin-bottom:1.25rem; }
  .card { background:var(--card); border:1px solid var(--border);
          border-radius:12px; padding:1.25rem; margin-bottom:1rem; }
  .row { display:flex; gap:.6rem; flex-wrap:wrap; margin-bottom:.6rem; }
  label.field { flex:1; min-width:220px; font-size:.78rem; color:var(--muted); }
  input[type=text] { width:100%; margin-top:.25rem; padding:.65rem .9rem;
      font-size:1rem; border:1px solid var(--border); border-radius:8px; }
  input[type=text]:focus { outline:2px solid var(--accent); border-color:transparent; }
  .actions { display:flex; gap:.6rem; align-items:flex-end; }
  button { padding:.65rem 1.2rem; font-size:1rem; border:none; border-radius:8px;
           background:var(--accent); color:#fff; cursor:pointer; }
  button:disabled { opacity:.5; cursor:default; }
  .opt { display:flex; align-items:center; gap:.4rem; font-size:.85rem;
         color:var(--muted); margin-top:.6rem; }
  .meta { font-size:.82rem; color:var(--muted); margin-top:.8rem; }
  .total { font-size:2.6rem; font-weight:700; }
  .total small { font-size:1rem; font-weight:400; color:var(--muted); }
  .variants { display:flex; gap:.5rem; flex-wrap:wrap; margin-top:.5rem; }
  .chip { background:#eff6ff; color:#1d4ed8; border-radius:999px;
          padding:.2rem .7rem; font-size:.85rem; }
  table { width:100%; border-collapse:collapse; margin-top:.75rem; font-size:.9rem; }
  th, td { text-align:left; padding:.5rem .6rem; border-bottom:1px solid var(--border); }
  th { color:var(--muted); font-weight:600; font-size:.8rem;
       text-transform:uppercase; letter-spacing:.03em; }
  td.num, th.num { text-align:right; white-space:nowrap; }
  td a { color:var(--accent); text-decoration:none; word-break:break-all; }
  .status { font-size:.9rem; color:var(--muted); }
  .err { color:#b91c1c; }
  .hidden { display:none; }
  .note { font-size:.78rem; color:var(--muted); margin-top:.9rem; }
  .spin { display:inline-block; width:14px; height:14px; border:2px solid var(--border);
          border-top-color:var(--accent); border-radius:50%;
          animation:s 0.8s linear infinite; vertical-align:-2px; }
  @keyframes s { to { transform:rotate(360deg); } }
</style>
</head>
<body>
<div class="wrap">
  <h1>Word Counter</h1>

  <div class="card">
    <form id="f">
      <div class="row">
        <label class="field">Site URL
          <input type="text" id="site" value="{{ default_site }}"
                 placeholder="https://www.example.com">
        </label>
        <label class="field">Word
          <input type="text" id="word" placeholder="Word to search (e.g. adoption)" autofocus>
        </label>
        <div class="actions">
          <button type="submit" id="btn">Count</button>
        </div>
      </div>
    </form>
    <label class="opt">
      <input type="checkbox" id="whole" checked>
      Match whole word only (uncheck to also count derivatives like "adoptions")
    </label>
    <div class="meta"><span id="status" class="status"></span></div>
    <div class="note">Counting is case-insensitive. New sites are scanned on first
      search (static sites only, up to {{ max_pages }} pages);
      aibusinessschool.com is pre-indexed in full.</div>
  </div>

  <div class="card hidden" id="result">
    <div class="total"><span id="total"></span> <small id="totalLabel"></small></div>
    <div class="variants" id="variants"></div>
    <table>
      <thead><tr><th>Page</th><th class="num">Count</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function siteValue() { return $('site').value.trim(); }
function setStatus(html) { $('status').innerHTML = html; }
function setError(msg) { setStatus('<span class="err">' + esc(msg) + '</span>'); }

async function refreshStatus() {
  const site = siteValue();
  if (!site) { setStatus('Enter a site URL to begin.'); return null; }
  const r = await fetch('/api/status?site=' + encodeURIComponent(site));
  const st = await r.json();
  if (!r.ok) { setError(st.error); return null; }
  if (st.status === 'ready') {
    setStatus(st.page_count + ' pages indexed (' + (st.source || '') +
              ') · Last scan: ' + (st.crawled_at || '-'));
  } else {
    setStatus('This site has not been scanned yet — it will be scanned on your first search.');
  }
  return st;
}

async function ensureScanned() {
  const st = await refreshStatus();
  if (!st) return false;
  if (st.status === 'ready') return true;
  setStatus('<span class="spin"></span> Scanning site... this can take up to a minute.');
  const r = await fetch('/api/scan?site=' + encodeURIComponent(siteValue()), {method: 'POST'});
  const d = await r.json();
  if (!r.ok) { setError(d.error); return false; }
  setStatus(d.page_count + ' pages indexed (' + (d.source || '') +
            ') · Last scan: ' + (d.crawled_at || '-'));
  return true;
}

async function doCount(word) {
  const whole = $('whole').checked ? '1' : '0';
  const r = await fetch('/api/count?site=' + encodeURIComponent(siteValue()) +
                        '&word=' + encodeURIComponent(word) + '&whole_word=' + whole);
  const d = await r.json();
  if (d.error) { setError(d.error); return; }
  $('result').classList.remove('hidden');
  $('total').textContent = d.total;
  $('totalLabel').textContent =
    'occurrences of "' + d.word + '" (searched across ' + d.page_count + ' pages)';
  $('variants').innerHTML = d.variants
    .map(v => '<span class="chip">' + esc(v.text) + ': ' + v.count + '</span>').join('');
  $('rows').innerHTML = d.pages.map(p => {
    const label = p.path === '/' ? 'Homepage' : esc(p.path);
    return '<tr><td><a href="' + esc(p.url) + '" target="_blank">' + label +
           '</a></td><td class="num">' + p.count + '</td></tr>';
  }).join('') || '<tr><td colspan="2">Not found on any page.</td></tr>';
}

$('f').addEventListener('submit', async ev => {
  ev.preventDefault();
  const word = $('word').value.trim();
  if (!word || !siteValue()) return;
  $('btn').disabled = true;
  try {
    if (await ensureScanned()) await doCount(word);
  } catch (e) {
    setError('Request failed: ' + e.message);
  } finally {
    $('btn').disabled = false;
  }
});

$('site').addEventListener('change', () => {
  $('result').classList.add('hidden');
  refreshStatus();
});

refreshStatus();
</script>
</body>
</html>"""


@app.get("/")
def index():
    return render_template_string(PAGE, default_site=DEFAULT_SITE,
                                  max_pages=SCAN_MAX_PAGES)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=False)
