# FindWord

Counts how many times a word occurs across all pages of a website.
Counting is case-insensitive and reports the exact-case variants
separately (e.g. `Adoption: 491, adoption: 86`), with a per-page
breakdown.

## Hosted version (Vercel)

The web UI lives in [api/index.py](api/index.py) and is deployed as a
Vercel serverless function.

- **aibusinessschool.com** is pre-indexed: its full crawl (51 pages,
  browser-rendered) is bundled in [cache/](cache/), so searches are instant.
- Other **static** sites are scanned live on first search (up to 30
  pages within the serverless time budget) and cached in `/tmp` for the
  lifetime of the function instance.
- **JavaScript-rendered (SPA) sites cannot be scanned in the hosted
  version** — serverless functions cannot run a browser. Pre-index them
  locally instead (see below) and commit the resulting
  `cache/<domain>.json`.

## Local tools

Install: `pip install -r requirements-local.txt` then
`python -m playwright install chromium`.

- `python webapp.py` — full-featured local web UI at
  http://127.0.0.1:5000. Detects SPA sites automatically and renders
  them with headless Chromium; caches every scanned site under
  `cache/`. Committing a cache file makes that site pre-indexed in the
  hosted version too.
- `python word_counter.py <url> <word...>` — CLI version with more
  options (`--case-sensitive`, `--no-word-boundary`, `--include-html`,
  `--output report.csv`, `--render always|never|auto`).

## Deployment

Deployed with the Vercel CLI (`vercel --prod`) or by connecting this
repo to a Vercel project. `vercel.json` routes all paths to the Flask
app and bundles `cache/**` into the function.
