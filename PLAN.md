# News Digest — Project Plan

A personal news digest. You choose categories (Science, Tech, AI, Finance, Politics…) and
the app collects stories from public RSS feeds, sorts and dedupes them, and publishes a
lightweight static page 4x a day. The page is installable on a phone as a home-screen app.

## Requirements (agreed)

- **Daily-digest style.** Checked a couple of times a day, not a live feed.
- **Nothing runs on the PC.** All builds run on GitHub Actions.
- **No new external services.** Only GitHub, which we already have (repo in the `nahamus` org,
  `gh` CLI logged in as `nahamus`). We use public RSS feeds with no API keys.
- **Light on the phone.** One static HTML page with inline CSS, no images, and no JS
  except a tiny service-worker registration. No background activity.
- **Installable.** A PWA (manifest + icons + service worker) for "Add to Home Screen" and
  offline viewing of the last digest.

## Architecture

```
cron-job.org (7:17 AM, 10:17 AM, 1:17 PM & 5:17 PM Eastern) → workflow_dispatch API
  └─ GitHub Actions
  └─ build.py: fetch RSS → filter → categorize → dedupe → rank → render
       ├─ commits data/seen.json   (story history, keeps the repo "active")
       └─ deploys site/ to GitHub Pages (via Pages artifact; site/ is not committed)
            └─ phone opens https://nahamus.github.io/news-digest/
```

Why we don't fetch in the browser: most news sites block cross-site requests for their
feeds (CORS), so that would need a third-party proxy. It would also make the phone do all
the work on every open.

## Repo layout

```
news-digest/
├── PLAN.md                      # this file
├── README.md                    # short usage: edit config, trigger a build, install on phone
├── config.yaml                  # categories, feeds, keywords, mutes (the only file normally edited)
├── build.py                     # the build script
├── requirements.txt             # feedparser, pyyaml, jinja2, tzdata
├── templates/digest.html        # page template (mobile-first, dark/light)
├── static/                      # copied into site/ as-is
│   ├── manifest.webmanifest
│   ├── sw.js                    # network-first, falls back to cache when offline
│   ├── icon-192.png
│   └── icon-512.png
├── data/seen.json               # generated; committed by the workflow
├── site/                        # generated; git-ignored
└── .github/workflows/digest.yml
```

## config.yaml (design + starter feeds)

- `title`, `timezone: America/New_York`
- `lookback_hours: 36`: ignore older stories
- `max_per_category: 10`, `max_per_source: 4`: keeps one outlet from dominating a category
- `mute: [...]`: drop stories containing these words anywhere
- `categories`: in display order. Each has `name`, optional `keywords`, and `feeds`.
  A feed with `filter: true` keeps only items that match the category's keywords, so
  broad feeds can feed narrow categories.

Starter feeds (all public RSS, to be verified on the first local run):

| Category | Feeds |
|---|---|
| Science | ScienceDaily (all), Nature, Phys.org, Quanta, NASA news releases |
| Tech | Ars Technica, The Verge, TechCrunch, Wired, Hacker News (hnrss, ≥150 pts) |
| AI | MIT Tech Review AI, The Verge AI, TechCrunch AI, HN ≥100 pts *(filter)*, Ars Technica *(filter)* |
| Finance | CNBC top news, MarketWatch top stories, Bloomberg markets, Yahoo Finance |
| Politics | NPR Politics, Politico, The Hill, Guardian US Politics, BBC World |

AI keywords: AI, A.I., artificial intelligence, LLM(s), OpenAI, Anthropic, Claude,
ChatGPT, GPT, Gemini, DeepMind, Copilot, machine learning, deep learning, neural network,
chatbot, generative, Llama, Mistral.

## build.py pipeline

1. **Load config**, compute cutoff = now − `lookback_hours`.
2. **Fetch** all feeds in parallel (thread pool, 20s timeout, browser-like User-Agent).
   If a feed fails, log it and skip it. One bad feed never breaks the build.
3. **Normalize** each entry: strip HTML, unescape, truncate the summary to ~220 chars, and
   drop summaries that just repeat the headline. Use `published` or `updated` (UTC).
4. **Filter**: apply the `filter: true` keyword match and the global `mute` list
   (word-boundary, case-insensitive regex).
5. **Dedupe into stories**: merge items with the same URL (query string ignored) or similar
   headlines (≥3 shared significant words and Jaccard ≥ 0.5). The lead item comes from the
   earliest feed in config order. Other outlets are shown as "also: X, Y".
   A story's category is the one most of its items belong to. Ties go to the earlier
   category in config.
6. **Rank**: score = recency (0–2, linear over the lookback window) + 0.75 × number of
   additional outlets covering the story. Fill each category up to the caps.
7. **New markers**: a story is "new" if none of its URLs appear in `data/seen.json`.
   On the very first build nothing is marked new. Record all URLs and prune entries older
   than 7 days.
8. **Render** `templates/digest.html` → `site/index.html` and copy `static/` → `site/`.
   Times are shown in the configured timezone ("3:15 PM", "Yesterday 9:02 PM", "Tue 8:00 AM").
   Avoid `%-I`, because it doesn't work on Windows.

## The page

- A header with the title, "Updated Sat Nov 21, 7:02 AM" and the total new count.
- Sticky category chips (anchor links, no JS), each showing that category's new count.
- One section per category. Each story card has a headline link, source, time, an
  optional "also: …" line, a 1–2 line blurb and a **New** badge.
- Colors are CSS tokens on `:root`, with dark mode via `prefers-color-scheme`. System
  fonts, readable without zooming, no horizontal scroll.
- `<head>`: viewport, theme-color, manifest, apple-touch-icon, and
  apple-mobile-web-app-capable. **All paths relative**, because Pages serves under
  `/news-digest/`.
- Target size is under ~150 KB.

## GitHub Actions workflow (`.github/workflows/digest.yml`)

- Triggers:
  - `workflow_dispatch`: manual "build now", and the timed builds. A cron-job.org job
    POSTs to the dispatch API at 7:17 / 10:17 / 13:17 / 17:17 America/New_York, using a
    fine-grained token (this repo, Actions read/write only). Editions are named
    Morning / Midday / Evening by local hour.
  - No `schedule` trigger: GitHub's never fired for this repo or for fresh test repos on
    the same account (Nov 2026), despite active workflows and permissive org settings.
  - `push` to `main` touching config.yaml, build.py, templates/**, static/**.
- Permissions: `contents: write`, `pages: write`, `id-token: write`. Use a concurrency
  group so builds don't overlap.
- Steps: checkout → setup-python 3.12 (pip cache) → install requirements → `python build.py`
  → commit `data/seen.json` if it changed (bot identity) → configure-pages →
  upload-pages-artifact (`site/`) → deploy-pages. Use the `github-pages` environment.

## Setup steps (one time)

1. Create a local venv, install requirements, and run `python build.py` to verify feeds.
   Open `site/index.html` locally to review. (This is a one-off check during
   development, not a recurring process.)
2. Generate the icons (192/512 PNG). A one-off script is fine and the PNGs get committed.
3. `git init`, then create a **public** repo `nahamus/news-digest` with `gh repo create` and push.
   Free Pages requires a public repo, so the config and feed choices will be visible.
   **Confirm with the user before creating or pushing the repo.**
4. Repo Settings → Pages → Source: **GitHub Actions** (or set it via `gh api`).
5. Run the workflow manually once and open the Pages URL on the phone.
6. Phone: open the URL → Share/menu → **Add to Home Screen**.

## Known caveats

- **Timer token expiry:** the cron-job.org job's GitHub token expires. cron-job.org
  emails on failure. Renew the token and update the job's `Authorization` header.
- Some feeds may change URLs or block bots over time. Failures are logged in the Actions
  run, so check there if a source goes quiet.
- Paywalled outlets (Bloomberg, etc.) may link to articles you can't read in full.

## Roadmap

| Phase | Scope |
|---|---|
| **1. MVP** ✅ | Everything above: config, build, page, PWA, workflow, deploy |
| **2. Polish** ✅ | Per-category "show more" via `<details>`, headline skip lists, more Science sources, IDF-weighted story matching, Top Stories, feed-health issues, "new since last visit". Ongoing: tune feeds/keywords with use |
| **3. Optional AI** ✅ | An edition briefing at the top of the page (overview + one line per section), written by Claude during the build. Keyless access via Workload Identity Federation (no API key stored) |
| **4. Extras** ✅ | YouTube/Substack feeds (Reddit blocks bots), followed-topic highlighting, 15-day archive (one entry per day, its last edition) |

## Open decisions

- Final category list and any region focus (currently US + world politics, US markets).
- Digest times (currently 7:17 AM, 10:17 AM, 1:17 PM and 5:17 PM Eastern, set in cron-job.org).
