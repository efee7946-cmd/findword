"""Word counter web UI.

Enter a site URL and a word; the app crawls the site once, caches the
page texts (one cache file per site under cache/), and every search runs
instantly against the cache. "Re-scan site" refreshes the cache.

Run:
    python webapp.py
    -> http://127.0.0.1:5000
"""

import json
import re
import threading
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, request

from word_counter import (canonicalize, extract_visible_text, is_crawlable,
                          make_fetcher, normalize_url, same_domain)

DEFAULT_SITE = "https://www.aibusinessschool.com"
CACHE_DIR = Path(__file__).parent / "cache"
LEGACY_CACHE = Path(__file__).parent / "site_cache.json"
MAX_PAGES = 200
MAX_DEPTH = 6

app = Flask(__name__)

# --- Site cache registry ---------------------------------------------------

sites_lock = threading.Lock()
sites = {}          # key -> {status, pages_done, page_count, crawled_at,
                    #         error, start_url, pages}
active_crawl = None  # key of the crawl currently running, if any


def site_key(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def normalize_site_input(raw: str):
    """Turns user input into a start URL, or None if invalid."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    parsed = urlparse(raw)
    if not parsed.netloc or "." not in parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/"


def public_state(entry):
    return {k: entry[k] for k in ("status", "pages_done", "page_count",
                                  "crawled_at", "error", "start_url")}


def new_entry(start_url):
    return {"status": "empty", "pages_done": 0, "page_count": 0,
            "crawled_at": None, "error": None, "start_url": start_url,
            "pages": []}


def cache_path(key: str) -> Path:
    safe = re.sub(r"[^a-z0-9.-]", "_", key)
    return CACHE_DIR / f"{safe}.json"


def load_caches():
    CACHE_DIR.mkdir(exist_ok=True)

    # Migrate the old single-site cache file if present
    if LEGACY_CACHE.exists():
        try:
            data = json.loads(LEGACY_CACHE.read_text(encoding="utf-8"))
            if data.get("pages"):
                url = data["pages"][0]["url"]
                key = site_key(url)
                if not cache_path(key).exists():
                    parsed = urlparse(url)
                    data["start_url"] = f"{parsed.scheme}://{parsed.netloc}/"
                    cache_path(key).write_text(
                        json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    for f in CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            key = site_key(data["pages"][0]["url"])
            entry = new_entry(data.get("start_url") or data["pages"][0]["url"])
            entry.update(status="ready", pages=data["pages"],
                         page_count=len(data["pages"]),
                         crawled_at=data.get("crawled_at"))
            sites[key] = entry
        except Exception:
            continue


def crawl_site(key: str, start_url: str):
    """Crawls one site and caches page texts. Runs in its own thread."""
    global active_crawl
    root_netloc = urlparse(start_url).netloc
    new_pages = []
    try:
        fetcher = make_fetcher("auto", start_url)
        try:
            queue = [(canonicalize(normalize_url(start_url), root_netloc), 0)]
            visited = set()
            while queue and len(visited) < MAX_PAGES:
                url, depth = queue.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                try:
                    html = fetcher.fetch(url)
                except Exception:
                    continue
                soup = BeautifulSoup(html, "lxml")
                title = soup.title.get_text(strip=True) if soup.title else ""
                text = extract_visible_text(soup)
                new_pages.append({"url": url, "title": title, "text": text})
                with sites_lock:
                    sites[key]["pages_done"] = len(new_pages)
                if depth < MAX_DEPTH:
                    for a in soup.find_all("a", href=True):
                        link = canonicalize(normalize_url(urljoin(url, a["href"])), root_netloc)
                        if (link.startswith("http") and same_domain(link, root_netloc)
                                and is_crawlable(link) and link not in visited):
                            queue.append((link, depth + 1))
        finally:
            fetcher.close()

        if not new_pages:
            raise RuntimeError("No pages could be fetched from this site.")

        crawled_at = datetime.now().strftime("%d.%m.%Y %H:%M")
        cache_path(key).write_text(
            json.dumps({"start_url": start_url, "crawled_at": crawled_at,
                        "pages": new_pages}, ensure_ascii=False),
            encoding="utf-8")
        with sites_lock:
            sites[key].update(status="ready", pages=new_pages,
                              page_count=len(new_pages), crawled_at=crawled_at,
                              error=None)
    except Exception as exc:
        with sites_lock:
            sites[key].update(status="error", error=str(exc))
    finally:
        with sites_lock:
            active_crawl = None


def start_crawl(key: str, start_url: str):
    """Returns (started, error_message)."""
    global active_crawl
    with sites_lock:
        if active_crawl == key:
            return False, None                      # already scanning this site
        if active_crawl is not None:
            return False, ("Another site is currently being scanned. "
                           "Please wait for it to finish.")
        entry = sites.get(key) or new_entry(start_url)
        entry.update(status="crawling", pages_done=0, error=None,
                     start_url=start_url)
        sites[key] = entry
        active_crawl = key
    threading.Thread(target=crawl_site, args=(key, start_url), daemon=True).start()
    return True, None


def resolve_site(raw):
    """Returns (key, start_url) or (None, None) for invalid input."""
    start_url = normalize_site_input(raw)
    if not start_url:
        return None, None
    return site_key(start_url), start_url


# --- API -------------------------------------------------------------------

@app.get("/api/status")
def api_status():
    key, start_url = resolve_site(request.args.get("site"))
    if not key:
        return jsonify({"error": "Invalid site URL."}), 400
    with sites_lock:
        entry = sites.get(key)
        if not entry:
            return jsonify({**new_entry(start_url), "pages": None})
        return jsonify(public_state(entry))


@app.post("/api/scan")
def api_scan():
    site = request.args.get("site") or request.form.get("site")
    if not site:
        site = (request.get_json(silent=True) or {}).get("site")
    key, start_url = resolve_site(site)
    if not key:
        return jsonify({"error": "Invalid site URL."}), 400
    started, err = start_crawl(key, start_url)
    if err:
        return jsonify({"error": err}), 409
    with sites_lock:
        return jsonify({"started": started, **public_state(sites[key])})


@app.get("/api/count")
def api_count():
    key, _ = resolve_site(request.args.get("site"))
    if not key:
        return jsonify({"error": "Invalid site URL."}), 400
    word = (request.args.get("word") or "").strip()
    whole_word = request.args.get("whole_word", "1") != "0"
    if not word:
        return jsonify({"error": "Word cannot be empty."}), 400
    with sites_lock:
        entry = sites.get(key)
        if not entry or entry["status"] != "ready":
            return jsonify({"error": "This site has not been scanned yet."}), 409
        snapshot = list(entry["pages"])
        crawled_at = entry["crawled_at"]

    escaped = re.escape(word)
    if whole_word:
        escaped = rf"\b{escaped}\b"
    pattern = re.compile(escaped, re.IGNORECASE)

    variants = Counter()
    per_page = []
    for p in snapshot:
        matches = pattern.findall(p["text"])
        if matches:
            variants.update(matches)
            path = urlparse(p["url"]).path or "/"
            per_page.append({"url": p["url"], "title": p["title"],
                             "path": path, "count": len(matches)})
    per_page.sort(key=lambda r: -r["count"])
    total = sum(variants.values())

    return jsonify({
        "word": word,
        "total": total,
        "variants": [{"text": v, "count": c} for v, c in variants.most_common()],
        "pages": per_page,
        "page_count": len(snapshot),
        "crawled_at": crawled_at,
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
  button.ghost { background:transparent; color:var(--accent);
                 border:1px solid var(--accent); }
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
          <button type="button" class="ghost" id="refresh">Re-scan site</button>
        </div>
      </div>
    </form>
    <label class="opt">
      <input type="checkbox" id="whole" checked>
      Match whole word only (uncheck to also count derivatives like "adoptions")
    </label>
    <div class="meta"><span id="status" class="status"></span></div>
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
let pendingWord = null;
let pollTimer = null;

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}

function siteValue() { return $('site').value.trim(); }

async function poll() {
  clearTimeout(pollTimer);
  const site = siteValue();
  if (!site) { $('status').textContent = 'Enter a site URL to begin.'; return; }
  let st;
  try {
    const r = await fetch('/api/status?site=' + encodeURIComponent(site));
    st = await r.json();
    if (!r.ok) { $('status').innerHTML = '<span class="err">' + esc(st.error) + '</span>'; return; }
  } catch (e) { pollTimer = setTimeout(poll, 3000); return; }

  $('refresh').disabled = st.status === 'crawling';
  if (st.status === 'crawling') {
    $('status').innerHTML =
      '<span class="spin"></span> Scanning site... ' + st.pages_done + ' pages fetched';
    pollTimer = setTimeout(poll, 1500);
  } else if (st.status === 'ready') {
    $('status').textContent =
      st.page_count + ' pages cached · Last scan: ' + (st.crawled_at || '-');
    if (pendingWord) { const w = pendingWord; pendingWord = null; doCount(w); }
  } else if (st.status === 'error') {
    pendingWord = null;
    $('status').innerHTML = '<span class="err">Scan error: ' + esc(st.error || '') + '</span>';
  } else {
    $('status').textContent = 'This site has not been scanned yet — it will be scanned on your first search.';
  }
}

async function startScan() {
  const site = siteValue();
  if (!site) return;
  const r = await fetch('/api/scan?site=' + encodeURIComponent(site), {method: 'POST'});
  const d = await r.json();
  if (!r.ok) {
    pendingWord = null;
    $('status').innerHTML = '<span class="err">' + esc(d.error) + '</span>';
    return;
  }
  poll();
}

async function doCount(word) {
  const site = siteValue();
  $('btn').disabled = true;
  const whole = $('whole').checked ? '1' : '0';
  const r = await fetch('/api/count?site=' + encodeURIComponent(site) +
                        '&word=' + encodeURIComponent(word) + '&whole_word=' + whole);
  const d = await r.json();
  $('btn').disabled = false;
  if (d.error) { $('status').innerHTML = '<span class="err">' + esc(d.error) + '</span>'; return; }
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
  const site = siteValue();
  if (!word || !site) return;
  const r = await fetch('/api/status?site=' + encodeURIComponent(site));
  const st = await r.json();
  if (!r.ok) { $('status').innerHTML = '<span class="err">' + esc(st.error) + '</span>'; return; }
  if (st.status === 'ready') {
    doCount(word);
  } else if (st.status === 'crawling') {
    pendingWord = word; poll();
  } else {
    pendingWord = word; startScan();
  }
});

$('refresh').addEventListener('click', startScan);
$('site').addEventListener('change', () => { $('result').classList.add('hidden'); poll(); });

poll();
</script>
</body>
</html>"""


@app.get("/")
def index():
    return render_template_string(PAGE, default_site=DEFAULT_SITE)


load_caches()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
