#!/usr/bin/env python3
"""Rank every Terminal Trove tool by GitHub stars and emit a static HTML page.

Terminal Trove publishes no star counts, so the data is stitched from four
sources:

  1. /search?q=*        Typesense JSON, 10 requests -> the whole catalogue
                        (slug, tagline, language, license, OS, preview image)
  2. /categories/<slug>/ 73 pages -> authoritative tool -> category mapping
  3. /<slug>/           each tool page's JSON-LD carries the GitHub repo URL,
                        which appears in no API
  4. GitHub GraphQL     stars, forks, archived state, batched 50 repos/query

Usage:
    scripts/terminaltrove-stars.py [-o out.html] [--refresh] [--limit N]

Needs a GitHub token: $GITHUB_TOKEN, or an authenticated `gh` CLI.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SITE = "https://terminaltrove.com"
SITEMAP = f"{SITE}/sitemap-index.xml"
SEARCH = f"{SITE}/search?q=*&per_page=100&page={{page}}"
USER_AGENT = "terminaltrove-stars/1.0 (+personal ranking script)"
CACHE = Path(
    os.environ.get("TT_CACHE", Path.home() / ".cache" / "terminaltrove-stars")
)

# Single-segment URLs that are site chrome, not tools. Anything else is only
# treated as a tool if its page actually carries SoftwareApplication JSON-LD.
NOT_TOOLS = {
    "about", "explore", "list", "new", "categories", "blog", "newsletter",
    "terminals", "compare", "ai-coding-agents", "tool-of-the-week", "sponsors",
    "post", "privacy", "terms", "feeds", "language", "search", "submit", "rss",
}

# github.com/<owner>/<repo> where <owner> is a real account, not a site feature.
NOT_OWNERS = {"sponsors", "topics", "features", "about", "orgs", "apps",
              "collections", "marketplace", "settings", "login", "explore"}


def fetch(url: str, *, retries: int = 3) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
                raise
            if attempt == retries - 1:
                raise
            time.sleep(2**attempt)
    raise RuntimeError("unreachable")


def cached_fetch(url: str, refresh: bool = False) -> str:
    CACHE.mkdir(parents=True, exist_ok=True)
    key = re.sub(r"[^A-Za-z0-9]+", "_", url).strip("_")[:120]
    path = CACHE / f"{key}.html"
    if path.exists() and not refresh:
        return path.read_text(encoding="utf-8")
    body = fetch(url)
    path.write_text(body, encoding="utf-8")
    return body


def catalogue(refresh: bool = False) -> list[dict]:
    """Page through the site's Typesense endpoint; q=* matches every tool.

    per_page is clamped to 100 server-side, so the whole catalogue is ~10 calls.
    """
    docs: dict[str, dict] = {}
    for page in range(1, 40):
        # Always live: a cached index would never surface newly added tools.
        payload = json.loads(fetch(SEARCH.format(page=page)))
        hits = payload.get("hits", [])
        for hit in hits:
            doc = hit.get("document") or {}
            if doc.get("slug"):
                docs[doc["slug"]] = doc
        if len(hits) < 100 or len(docs) >= payload.get("found", 0):
            break
    return [docs[k] for k in sorted(docs)]


def category_slugs(refresh: bool = False) -> list[str]:
    index = cached_fetch(SITEMAP, refresh)
    out = set()
    for sub in re.findall(r"<loc>([^<]+)</loc>", index):
        for loc in re.findall(r"<loc>([^<]+)</loc>", cached_fetch(sub, refresh)):
            m = re.match(rf"{re.escape(SITE)}/categories/([^/]+)/?$", loc)
            if m:
                out.add(m.group(1))
    return sorted(out)


def category_map(
    valid: set[str], refresh: bool, workers: int
) -> dict[str, list[str]]:
    """Invert the category pages into tool -> [category].

    Tool membership is whatever flat link on the page is a known slug, which
    filters out site chrome without trusting a hand-kept blocklist.
    """
    slugs = category_slugs(refresh)
    mapping: dict[str, set[str]] = {}

    def work(cat: str) -> tuple[str, list[str]]:
        try:
            page = fetch(f"{SITE}/categories/{cat}/")  # live, same reason
        except Exception as exc:
            print(f"  ! category {cat}: {exc}", file=sys.stderr)
            return cat, []
        found = re.findall(r'href="/([a-z0-9][a-z0-9._-]*)/"', page)
        return cat, [s for s in set(found) if s in valid]

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for cat, tools in pool.map(work, slugs):
            for slug in tools:
                mapping.setdefault(slug, set()).add(cat)

    print(f"  {len(slugs)} categories over {len(mapping)} tools", file=sys.stderr)
    return {k: sorted(v) for k, v in mapping.items()}


def strip_tags(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def repo_from(url: str) -> str | None:
    m = re.match(r"https?://(?:www\.)?github\.com/([^/#?]+)/([^/#?]+)", url or "")
    if not m:
        return None
    owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2))
    if owner.lower() in NOT_OWNERS or not repo:
        return None
    return f"{owner}/{repo}"


def parse_tool(page: str, url: str) -> dict | None:
    """Pull the SoftwareApplication node out of the page's JSON-LD."""
    for block in re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S
    ):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        nodes = data.get("@graph", [data]) if isinstance(data, dict) else data
        for node in nodes if isinstance(nodes, list) else [nodes]:
            if not isinstance(node, dict):
                continue
            types = node.get("@type", "")
            types = types if isinstance(types, list) else [types]
            if "SoftwareApplication" not in types:
                continue
            links = node.get("sameAs") or []
            links = links if isinstance(links, list) else [links]
            help_url = (node.get("softwareHelp") or {}).get("url", "")
            repo = next(
                (r for r in (repo_from(u) for u in [*links, help_url]) if r), None
            )
            cats = node.get("applicationCategory") or []
            cats = cats if isinstance(cats, list) else [cats]
            return {
                "name": node.get("name") or url.strip("/").rsplit("/", 1)[-1],
                "url": url,
                "repo": repo,
                "description": strip_tags(node.get("description", "")),
                "categories": [c for c in cats if isinstance(c, str)],
            }
    return None


def collect_tools(slugs: list[str], refresh: bool, workers: int) -> dict[str, dict]:
    """Fetch each tool page for the one field no API exposes: the repo URL."""
    out: dict[str, dict] = {}
    done = 0

    def work(slug: str) -> tuple[str, dict | None]:
        url = f"{SITE}/{slug}/"
        try:
            return slug, parse_tool(cached_fetch(url, refresh), url)
        except Exception as exc:  # a dead page must not sink the run
            print(f"  ! {url}: {exc}", file=sys.stderr)
            return slug, None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for slug, result in pool.map(work, slugs):
            done += 1
            if done % 200 == 0:
                print(f"  ...{done}/{len(slugs)} pages", file=sys.stderr)
            if result:
                out[slug] = result
    return out


def download_previews(
    rows: list[dict], outdir: Path, base: Path, width: int, workers: int
) -> tuple[int, int]:
    """Vendor every preview locally and repoint rows at the relative copy.

    GIFs are left untouched so the animated demos keep animating; stills are
    downscaled through sips when it is available (macOS), otherwise kept as-is.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    have_sips = bool(width) and subprocess.run(
        ["which", "sips"], capture_output=True
    ).returncode == 0

    def work(row: dict) -> tuple[dict, str | None, int]:
        url = row.get("preview") or ""
        if not url:
            return row, None, 0
        ext = url.rsplit(".", 1)[-1].lower()
        ext = ext if ext in ("png", "jpg", "jpeg", "gif") else "png"
        dest = outdir / f"{row['slug']}.{ext}"
        try:
            if not dest.exists():
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    dest.write_bytes(resp.read())
            if have_sips and ext != "gif":
                small = outdir / f"{row['slug']}.jpg"
                done = subprocess.run(
                    ["sips", "-Z", str(width), "-s", "format", "jpeg",
                     "-s", "formatOptions", "60", str(dest), "--out", str(small)],
                    capture_output=True,
                )
                if done.returncode == 0 and small.stat().st_size:
                    if small != dest:
                        dest.unlink(missing_ok=True)
                    dest = small
            return row, dest.name, dest.stat().st_size
        except Exception as exc:
            print(f"  ! preview {row['slug']}: {exc}", file=sys.stderr)
            return row, None, 0

    got = total = 0
    rel = outdir.relative_to(base) if outdir.is_relative_to(base) else outdir
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for row, name, size in pool.map(work, rows):
            if name:
                row["preview"] = f"{rel}/{name}"
                got += 1
                total += size
            else:
                row["preview"] = ""
            if got and got % 200 == 0:
                print(f"  ...{got}/{len(rows)} previews", file=sys.stderr)
    return got, total


def github_token() -> str:
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    try:
        out = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, timeout=15
        )
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    sys.exit("No GitHub token. Set $GITHUB_TOKEN or run `gh auth login`.")


REPO_FIELDS = """
    stargazerCount forkCount isArchived
    description homepageUrl pushedAt
    primaryLanguage { name }
    licenseInfo { spdxId }
    nameWithOwner
"""


def fetch_stars(repos: list[str], token: str, batch: int = 50) -> dict[str, dict]:
    """Batch repos into aliased GraphQL queries; missing repos come back null."""
    stats: dict[str, dict] = {}
    for start in range(0, len(repos), batch):
        chunk = repos[start : start + batch]
        parts = []
        for i, full in enumerate(chunk):
            owner, name = full.split("/", 1)
            parts.append(
                f'r{i}: repository(owner: {json.dumps(owner)}, '
                f"name: {json.dumps(name)}) {{ {REPO_FIELDS} }}"
            )
        query = "query {" + "\n".join(parts) + "}"
        req = urllib.request.Request(
            "https://api.github.com/graphql",
            data=json.dumps({"query": query}).encode(),
            headers={
                "Authorization": f"bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = json.load(resp)
                break
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 2:
                    print(f"  ! GraphQL batch failed: {exc}", file=sys.stderr)
                    payload = {"data": {}}
                    break
                time.sleep(2**attempt + 1)

        for i, full in enumerate(chunk):
            node = (payload.get("data") or {}).get(f"r{i}")
            if node:
                stats[full] = node
        print(
            f"  ...{min(start + batch, len(repos))}/{len(repos)} repos",
            file=sys.stderr,
        )
    return stats


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Terminal Trove — ranked by GitHub stars</title>
<style>
  :root {
    color-scheme: light dark;
    --bg:      #fafbfb;
    --panel:   #ffffff;
    --fg:      #101619;
    --muted:   #5a666d;
    --faint:   #8a959b;
    --line:    #e1e7e9;
    --accent:  #0d7d8c;
    --bar:     #0d7d8c;
    --bar-bg:  #e6edee;
    --warn:    #9a6a1c;
    --chip:    #eef2f3;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg:     #0e1418;
      --panel:  #131b20;
      --fg:     #dde5e8;
      --muted:  #8b9aa1;
      --faint:  #64737a;
      --line:   #222e34;
      --accent: #5cc6d6;
      --bar:    #2f7f8c;
      --bar-bg: #1b262c;
      --warn:   #c79544;
      --chip:   #1a242a;
    }
  }
  :root[data-theme="light"] {
    --bg:#fafbfb; --panel:#ffffff; --fg:#101619; --muted:#5a666d; --faint:#8a959b;
    --line:#e1e7e9; --accent:#0d7d8c; --bar:#0d7d8c; --bar-bg:#e6edee;
    --warn:#9a6a1c; --chip:#eef2f3;
  }
  :root[data-theme="dark"] {
    --bg:#0e1418; --panel:#131b20; --fg:#dde5e8; --muted:#8b9aa1; --faint:#64737a;
    --line:#222e34; --accent:#5cc6d6; --bar:#2f7f8c; --bar-bg:#1b262c;
    --warn:#c79544; --chip:#1a242a;
  }

  * { box-sizing: border-box; }
  html { -webkit-text-size-adjust: 100%; }
  body {
    margin: 0; background: var(--bg); color: var(--fg);
    font: 15px/1.5 ui-sans-serif, -apple-system, "Segoe UI", Roboto, system-ui, sans-serif;
  }
  .mono {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
  }
  .wrap { max-width: 1220px; margin: 0 auto; padding: 2.5rem 1.25rem 5rem; }

  header { display: flex; flex-direction: column; gap: .5rem; margin-bottom: 1.75rem; }
  .eyebrow {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-size: .72rem; letter-spacing: .16em; text-transform: uppercase;
    color: var(--accent);
  }
  h1 {
    margin: 0; font-size: clamp(1.6rem, 3.4vw, 2.1rem);
    letter-spacing: -.025em; font-weight: 650; text-wrap: balance;
  }
  .lede { margin: 0; color: var(--muted); font-size: .92rem; max-width: 62ch; }
  .lede a { color: var(--accent); }

  .stats {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 1px; background: var(--line); border: 1px solid var(--line);
    border-radius: 10px; overflow: hidden; margin: 1.5rem 0;
  }
  .stat { background: var(--panel); padding: .8rem 1rem; }
  .stat b {
    display: block; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 1.3rem; font-weight: 600; letter-spacing: -.02em;
    font-variant-numeric: tabular-nums;
  }
  .stat span {
    font-size: .7rem; letter-spacing: .1em; text-transform: uppercase; color: var(--faint);
  }

  .toolbar {
    position: sticky; top: 0; z-index: 5; display: flex; gap: .55rem; flex-wrap: wrap;
    padding: .85rem 0; background: var(--bg); border-bottom: 1px solid var(--line);
  }
  input[type=search], select {
    padding: .5rem .7rem; border: 1px solid var(--line); border-radius: 7px;
    background: var(--panel); color: var(--fg); font: inherit; font-size: .875rem;
  }
  input[type=search] { flex: 1 1 240px; min-width: 0; }
  input[type=search]:focus-visible, select:focus-visible, th:focus-visible, a:focus-visible {
    outline: 2px solid var(--accent); outline-offset: 2px;
  }
  #count {
    margin-left: auto; align-self: center; color: var(--faint);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .78rem; font-variant-numeric: tabular-nums;
  }

  .tablewrap { overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; min-width: 840px; }
  th {
    text-align: left; padding: .85rem .7rem .5rem; cursor: pointer; user-select: none;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .68rem; letter-spacing: .12em; text-transform: uppercase;
    color: var(--faint); font-weight: 500; white-space: nowrap;
    border-bottom: 1px solid var(--line);
  }
  th:hover { color: var(--fg); }
  th[aria-sort] { color: var(--accent); }
  th .car { opacity: .55; }
  td { padding: .6rem .7rem; border-bottom: 1px solid var(--line); vertical-align: top; }
  tbody tr:hover { background: var(--chip); }

  .rank {
    text-align: right; color: var(--faint);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .78rem; font-variant-numeric: tabular-nums; width: 3.5rem;
  }
  .starcell { width: 12rem; }
  .starnum {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .85rem; font-variant-numeric: tabular-nums; font-weight: 600;
    display: block; text-align: right;
  }
  .track { height: 3px; border-radius: 2px; background: var(--bar-bg); margin-top: .3rem; }
  .fill { height: 100%; border-radius: 2px; background: var(--bar); }

  .name { font-weight: 600; letter-spacing: -.01em; }
  .name a { color: var(--fg); text-decoration: none; }
  .name a:hover { color: var(--accent); text-decoration: underline; }
  .desc { color: var(--muted); font-size: .82rem; line-height: 1.45; max-width: 52ch; margin-top: .15rem; }
  .lang {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .78rem; color: var(--muted); white-space: nowrap;
  }
  .repo a {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .78rem; color: var(--accent); text-decoration: none;
    word-break: break-all;
  }
  .repo a:hover { text-decoration: underline; }
  .tag {
    display: inline-block; margin-left: .4rem; padding: 0 .32rem;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .64rem; letter-spacing: .06em; text-transform: uppercase;
    color: var(--warn); border: 1px solid currentColor; border-radius: 3px;
    vertical-align: 1px;
  }
  tbody tr.head { cursor: pointer; }
  tbody tr.head.open { background: var(--chip); }
  .twist {
    display: inline-block; width: .85em; color: var(--faint);
    transition: transform .15s ease;
  }
  tr.head.open .twist { transform: rotate(90deg); color: var(--accent); }
  @media (prefers-reduced-motion: reduce) { .twist { transition: none; } }

  tr.detail > td { padding: 0; border-bottom: 1px solid var(--line); background: var(--panel); }
  .panel { display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr); gap: 1.25rem; padding: 1.1rem 1.2rem 1.4rem; }
  @media (max-width: 760px) { .panel { grid-template-columns: 1fr; } }
  .shot {
    margin: 0; border: 1px solid var(--line); border-radius: 8px;
    overflow: hidden; background: var(--bar-bg); min-height: 90px;
  }
  .shot img { display: block; width: 100%; height: auto; }
  .shot figcaption {
    padding: .4rem .6rem; font-size: .7rem; color: var(--faint);
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    border-top: 1px solid var(--line);
  }
  .noshot { padding: 1.6rem .8rem; text-align: center; color: var(--faint); font-size: .8rem; line-height: 1.5; }
  .noshot a { color: var(--accent); }
  .tagline { margin: 0 0 .8rem; font-size: .95rem; line-height: 1.45; }
  .meta { display: grid; grid-template-columns: auto 1fr; gap: .35rem .9rem; font-size: .8rem; margin-bottom: .9rem; }
  .meta dt {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .68rem; letter-spacing: .1em; text-transform: uppercase;
    color: var(--faint); white-space: nowrap; padding-top: .12rem;
  }
  .meta dd { margin: 0; color: var(--fg); font-variant-numeric: tabular-nums; }
  .chips { display: flex; flex-wrap: wrap; gap: .3rem; }
  .chip {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: .7rem; padding: .12rem .42rem; border-radius: 4px;
    background: var(--chip); border: 1px solid var(--line); color: var(--muted);
    cursor: pointer;
  }
  .chip:hover { color: var(--accent); border-color: var(--accent); }
  .empty { padding: 3rem 1rem; text-align: center; color: var(--faint); }
  footer {
    margin-top: 2rem; padding-top: 1rem; border-top: 1px solid var(--line);
    color: var(--faint); font-size: .78rem; line-height: 1.6;
  }
  footer a { color: var(--accent); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">Terminal Trove index</div>
    <h1>Every tool, ranked by GitHub stars</h1>
    <p class="lede">
      __TOOLCOUNT__ tools from <a href="https://terminaltrove.com/" target="_blank" rel="noopener">terminaltrove.com</a>
      resolved to a GitHub repository and sorted by star count. Click a row to
      preview the tool, or a column heading to re-sort.
    </p>
  </header>

  <section class="stats">
    <div class="stat"><b>__TOOLCOUNT__</b><span>Tools ranked</span></div>
    <div class="stat"><b>__TOTALSTARS__</b><span>Stars combined</span></div>
    <div class="stat"><b>__MEDIAN__</b><span>Median stars</span></div>
    <div class="stat"><b>__TOPLANG__</b><span>Most common language</span></div>
    <div class="stat"><b>__CATCOUNT__</b><span>Categories</span></div>
    <div class="stat"><b>__GENERATED__</b><span>Generated</span></div>
  </section>

  <div class="toolbar">
    <input id="q" type="search" placeholder="Filter by name, description, language, category…" aria-label="Filter tools">
    <select id="lang" aria-label="Filter by language"><option value="">All languages</option></select>
    <select id="cat" aria-label="Filter by category"><option value="">All categories</option></select>
    <span id="count"></span>
  </div>

  <div class="tablewrap">
    <table>
      <thead><tr>
        <th data-k="rank" tabindex="0" scope="col">#</th>
        <th data-k="stars" tabindex="0" scope="col">Stars</th>
        <th data-k="name" tabindex="0" scope="col">Tool</th>
        <th data-k="language" tabindex="0" scope="col">Language</th>
        <th data-k="repo" tabindex="0" scope="col">Repository</th>
      </tr></thead>
      <tbody id="rows"></tbody>
    </table>
  </div>
  <div class="empty" id="empty" hidden>No tool matches that filter.</div>

  <footer>
    Stars read from the GitHub GraphQL API at generation time; Terminal Trove itself
    publishes no star data. __SKIPPED__
  </footer>
</div>

<script>
const DATA = __DATA__;
DATA.forEach((d, i) => { d.rank = i + 1; });

const $ = id => document.getElementById(id);
const MAXLOG = Math.log(Math.max(...DATA.map(d => d.stars), 1) + 1);
let sortKey = "stars", sortDir = -1;

const fill = (sel, values) => {
  [...new Set(values)].sort((a, b) => a.localeCompare(b))
    .forEach(v => sel.add(new Option(v, v)));
};
fill($("lang"), DATA.map(d => d.language).filter(Boolean));
fill($("cat"), DATA.flatMap(d => d.categories || []));

const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const open = new Set();

function detail(d) {
  const shot = d.preview
    ? '<figure class="shot"><img loading="lazy" alt="' + esc(d.name) + ' screenshot" '
      + 'src="' + esc(d.preview) + '" '
      + "onerror=\"this.closest('.shot').innerHTML='<div class=\\'noshot\\'>Preview blocked here &mdash; "
      + "<a href=\\'" + esc(d.url) + "\\' target=\\'_blank\\' rel=\\'noopener\\'>view on Terminal Trove</a></div>'\">"
      + '<figcaption>' + (d.preview.endsWith(".gif") ? "animated demo" : "screenshot") + '</figcaption></figure>'
    : '<div class="shot"><div class="noshot">No preview published</div></div>';

  const row = (k, v) => v ? '<dt>' + k + '</dt><dd>' + v + '</dd>' : "";
  const chips = (d.categories || []).map(c =>
    '<button class="chip" data-cat="' + esc(c) + '">' + esc(c) + '</button>').join("");

  return '<div class="panel">' + shot + '<div>'
    + (d.tagline ? '<p class="tagline">' + esc(d.tagline) + '</p>' : "")
    + '<dl class="meta">'
    +   row("Stars", d.stars.toLocaleString())
    +   row("Forks", (d.forks || 0).toLocaleString())
    +   row("Language", esc(d.language))
    +   row("License", esc(d.license))
    +   row("Runs on", (d.os || []).map(esc).join(", "))
    +   row("Last push", esc(d.pushed_at))
    + '</dl>'
    + (chips ? '<div class="chips">' + chips + '</div>' : "")
    + '</div></div>';
}

function render() {
  const q = $("q").value.trim().toLowerCase();
  const lang = $("lang").value, cat = $("cat").value;

  const rows = DATA.filter(d => {
    if (lang && d.language !== lang) return false;
    if (cat && !(d.categories || []).includes(cat)) return false;
    if (!q) return true;
    return [d.name, d.description, d.tagline, d.language, d.repo, ...(d.categories || [])]
      .join(" ").toLowerCase().includes(q);
  }).sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    const cmp = (typeof x === "number" && typeof y === "number")
      ? x - y : String(x ?? "").localeCompare(String(y ?? ""));
    return cmp * sortDir;
  });

  $("count").textContent = rows.length + " / " + DATA.length;
  $("empty").hidden = rows.length > 0;

  $("rows").innerHTML = rows.map(d => {
    const w = (Math.log(d.stars + 1) / MAXLOG) * 100;
    const on = open.has(d.slug);
    return '<tr class="head' + (on ? " open" : "") + '" data-slug="' + esc(d.slug) + '"'
      + ' tabindex="0" role="button" aria-expanded="' + on + '">'
      + '<td class="rank"><span class="twist">&#9656;</span> ' + d.rank + '</td>'
      + '<td class="starcell"><span class="starnum">' + d.stars.toLocaleString() + '</span>'
      +   '<div class="track"><div class="fill" style="width:' + w.toFixed(1) + '%"></div></div></td>'
      + '<td><div class="name">' + esc(d.name)
      +   (d.archived ? '<span class="tag">archived</span>' : '') + '</div>'
      +   '<div class="desc">' + esc(d.description) + '</div></td>'
      + '<td class="lang">' + esc(d.language || "—") + '</td>'
      + '<td class="repo"><a href="https://github.com/' + esc(d.repo)
      +   '" target="_blank" rel="noopener">' + esc(d.repo) + '</a></td>'
      + '</tr>'
      + (on ? '<tr class="detail"><td colspan="5">' + detail(d) + '</td></tr>' : "");
  }).join("");
}

$("rows").addEventListener("click", e => {
  const chip = e.target.closest(".chip");
  if (chip) {
    $("cat").value = chip.dataset.cat;
    render();
    return;
  }
  if (e.target.closest("a")) return;          // let repo links through
  const tr = e.target.closest("tr.head");
  if (!tr) return;
  const slug = tr.dataset.slug;
  open.has(slug) ? open.delete(slug) : open.add(slug);
  render();
});

$("rows").addEventListener("keydown", e => {
  if (e.key !== "Enter" && e.key !== " ") return;
  const tr = e.target.closest("tr.head");
  if (!tr) return;
  e.preventDefault();
  const slug = tr.dataset.slug;
  open.has(slug) ? open.delete(slug) : open.add(slug);
  render();
});

document.querySelectorAll("th[data-k]").forEach(th => {
  const activate = () => {
    const k = th.dataset.k;
    sortDir = k === sortKey ? -sortDir : (k === "stars" ? -1 : 1);
    sortKey = k;
    document.querySelectorAll("th[data-k]").forEach(o => {
      o.removeAttribute("aria-sort");
      o.querySelector(".car")?.remove();
    });
    th.setAttribute("aria-sort", sortDir === 1 ? "ascending" : "descending");
    th.insertAdjacentHTML("beforeend",
      '<span class="car"> ' + (sortDir === 1 ? "↑" : "↓") + '</span>');
    render();
  };
  th.onclick = activate;
  th.onkeydown = e => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); activate(); }
  };
});
["q", "lang", "cat"].forEach(id => { $(id).oninput = render; });
render();
</script>
</body>
</html>
"""


def render(rows: list[dict], missing: list[dict], generated: str) -> str:
    stars = sorted(r["stars"] for r in rows)
    langs: dict[str, int] = {}
    for r in rows:
        if r["language"]:
            langs[r["language"]] = langs.get(r["language"], 0) + 1
    top_lang = max(langs.items(), key=lambda kv: kv[1])[0] if langs else "—"
    skipped = (
        f"{len(missing)} catalogued tools were skipped because they have no "
        "public GitHub repository." if missing else ""
    )
    subs = {
        "__DATA__": json.dumps(rows, ensure_ascii=False),
        "__TOOLCOUNT__": f"{len(rows):,}",
        "__TOTALSTARS__": f"{sum(stars):,}",
        "__MEDIAN__": f"{stars[len(stars) // 2]:,}" if stars else "0",
        "__TOPLANG__": html.escape(top_lang),
        "__CATCOUNT__": f"{len({c for r in rows for c in r['categories']})}",
        "__GENERATED__": html.escape(generated),
        "__SKIPPED__": html.escape(skipped),
    }
    page = PAGE
    for key, value in subs.items():
        page = page.replace(key, value)
    return page


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--out", default="terminaltrove-stars.html")
    ap.add_argument("--refresh", action="store_true",
                    help="re-download pages instead of using the cache")
    ap.add_argument("--limit", type=int, help="only process the first N tools")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--json", help="also write the raw ranking as JSON")
    ap.add_argument(
        "--previews", nargs="?", const="previews", metavar="DIR",
        help="download preview images next to the HTML and link them relatively "
             "(needed for GitHub Pages / offline use)",
    )
    ap.add_argument(
        "--preview-width", type=int, default=560, metavar="PX",
        help="downscale still previews to this width (0 keeps originals); "
             "GIFs are never touched",
    )
    args = ap.parse_args()

    print("Reading catalogue (/search?q=*)…", file=sys.stderr)
    docs = catalogue(args.refresh)
    if args.limit:
        docs = docs[: args.limit]
    print(f"  {len(docs)} tools in the collection", file=sys.stderr)
    slugs = [d["slug"] for d in docs]

    print("Mapping categories…", file=sys.stderr)
    cats = category_map(set(slugs), args.refresh, args.workers)

    print("Fetching tool pages for repo links…", file=sys.stderr)
    parsed = collect_tools(slugs, args.refresh, args.workers)

    with_repo = {s: p for s, p in parsed.items() if p["repo"]}
    missing = [d for d in docs if d["slug"] not in with_repo]
    repos = sorted({p["repo"] for p in with_repo.values()})
    print(f"Fetching stars for {len(repos)} repos…", file=sys.stderr)
    stats = fetch_stars(repos, github_token())

    rows = []
    for doc in docs:
        slug = doc["slug"]
        tool = with_repo.get(slug)
        node = stats.get(tool["repo"]) if tool else None
        if not node:
            if tool:
                missing.append(doc)
            continue
        rows.append({
            "name": doc.get("name") or slug,
            "slug": slug,
            "url": f"{SITE}/{slug}/",
            "repo": node.get("nameWithOwner") or tool["repo"],
            "stars": node.get("stargazerCount", 0),
            "forks": node.get("forkCount", 0),
            # Terminal Trove's own label is more editorial than GitHub's
            # primaryLanguage guess, so it wins when present.
            "language": doc.get("language") or
                        (node.get("primaryLanguage") or {}).get("name", ""),
            "license": ", ".join(doc.get("license") or []) or
                       (node.get("licenseInfo") or {}).get("spdxId", ""),
            "os": doc.get("operating_systems") or [],
            "preview": doc.get("preview") or "",
            "archived": bool(node.get("isArchived")),
            "pushed_at": (node.get("pushedAt") or "")[:10],
            "tagline": doc.get("tagline") or "",
            "description": (tool["description"] if tool else "")
                           or (node.get("description") or ""),
            "categories": cats.get(slug, []),
        })

    # Two tools can point at one repo (forks, renames); keep the best-named entry.
    rows.sort(key=lambda r: (-r["stars"], r["name"].lower()))
    seen: set[str] = set()
    rows = [r for r in rows if not (r["repo"] in seen or seen.add(r["repo"]))]

    out = Path(args.out)
    if args.previews:
        base = out.parent if out.parent.as_posix() else Path(".")
        target = Path(args.previews)
        if not target.is_absolute():
            target = base / target
        print(f"Downloading previews to {target}…", file=sys.stderr)
        got, size = download_previews(
            rows, target, base, args.preview_width, args.workers
        )
        print(f"  {got} previews, {size / 1_048_576:.1f} MB on disk", file=sys.stderr)

    generated = time.strftime("%Y-%m-%d")
    out.write_text(render(rows, missing, generated), encoding="utf-8")
    if args.json:
        Path(args.json).write_text(
            json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    print(f"\nWrote {args.out} — {len(rows)} tools ranked.", file=sys.stderr)
    if missing:
        print(f"{len(missing)} tools had no resolvable GitHub repo.", file=sys.stderr)
    for r in rows[:10]:
        print(f"  {r['stars']:>7,}  {r['name']:<18} {r['repo']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
