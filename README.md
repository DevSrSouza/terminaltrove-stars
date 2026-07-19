# terminaltrove-stars

[Terminal Trove](https://terminaltrove.com/) catalogues ~965 terminal tools but
publishes no star counts and offers no way to sort by popularity. This builds
that missing view: every catalogued tool with a resolvable GitHub repository,
ranked by stars, with previews and the site's own categories.

A GitHub Action rebuilds and republishes the page every second day.

## How the data is assembled

No single endpoint has everything, so four sources are stitched together.

| # | Source | Gives |
|---|--------|-------|
| 1 | `/search?q=*` | Typesense JSON. `q=*` matches the whole collection; `per_page` is clamped to 100, so ~10 requests returns every tool with its slug, tagline, language, license, OS list and preview image. |
| 2 | `/categories/<slug>/` | 73 pages, each listing its full membership in one request. Inverted into a tool → category map. |
| 3 | `/<slug>/` | The repository URL, which appears in **no** API. Each tool page embeds schema.org JSON-LD whose `sameAs` carries the GitHub link. |
| 4 | GitHub GraphQL | Stars, forks, archived state, last push — batched 50 repos per query, so ~19 requests instead of ~930. |

Parsing JSON-LD rather than scraping markup means a CSS or layout change on
Terminal Trove does not break this.

Tool membership on category pages is resolved by intersecting the flat links
with the known slug set from step 1, so site chrome cannot leak in without a
hand-maintained blocklist.

## Running it locally

```bash
./terminaltrove-stars.py -o index.html --json data.json
```

Needs a GitHub token for step 4 — `$GITHUB_TOKEN`, or an authenticated
[`gh`](https://cli.github.com/) CLI, which is picked up automatically.

| Flag | Effect |
|------|--------|
| `--out FILE` | Output HTML (default `terminaltrove-stars.html`) |
| `--json FILE` | Also write the ranking as raw JSON |
| `--previews [DIR]` | Download preview images and link them relatively, for offline use |
| `--preview-width PX` | Downscale stills when vendoring (default 560, `0` keeps originals; GIFs are never touched) |
| `--refresh` | Re-download tool pages instead of using the cache |
| `--limit N` | Only process the first N tools |
| `--workers N` | Concurrent fetches (default 8) |

Tool pages are cached under `~/.cache/terminaltrove-stars`, so re-runs only hit
GitHub for fresh star counts. The catalogue and category pages are always
fetched live so newly added tools appear.

## Previews

The published page links preview images straight from Terminal Trove's CDN,
which serves them without hotlink protection. `--previews` exists for the
offline case: it vendors the images locally and rewrites the links, downscaling
the stills while leaving the animated GIF demos intact.

## Caveats

- Star counts are a snapshot from the moment the page was generated.
- Tools with no public GitHub repository are excluded and counted in the footer.
- Terminal Trove treats `linux`, `macos`, `windows`, `bsd` and `cross-platform`
  as categories alongside functional ones like `file-manager`, so they dominate
  the category filter.

## Licence

MIT.
