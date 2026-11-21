# News Digest

A personal news digest built from public RSS feeds by GitHub Actions twice a day
(~7 AM and ~5 PM Eastern) and published to GitHub Pages:
https://nahamus.github.io/news-digest/

## What's on the page

- **Top Stories**: stories that 3+ outlets are covering, pulled out of their categories.
- **Following**: stories mentioning your `follow` topics, highlighted wherever they appear.
- **Categories**: the first 5 stories, with the rest behind "Show more". Each category
  can open with a 2–3 sentence AI briefing (see below).
- **New** markers show what's new since your last visit on that device (or since the
  last build, on a first visit).
- **Archive**: past editions from the last 30 days, linked in the footer.

## Change what you see

Edit `config.yaml`. Pushing a change to `main` rebuilds the site automatically.

- `categories`: feeds per category. Any RSS/Atom feed works, including YouTube channels
  and Substack newsletters (examples in the file). Reddit blocks automated requests.
- `follow`: topics to highlight and collect in Following.
- `skip_titles` / a feed's `skip`: drop filler by headline (quizzes, coupons, podcasts…).
- `mute`: drop stories mentioning a word anywhere.
- `max_per_category`, `max_per_source` (also per category), `show_first`,
  `lookback_hours` (also per feed), `top_stories`, `archive_days`.

## AI briefing (optional)

Off until you add an Anthropic API key to the repo; costs roughly a few cents per build.

1. Create a key at https://console.anthropic.com
2. `gh secret set ANTHROPIC_API_KEY -R nahamus/news-digest` and paste the key.

To turn it off again, delete the secret or set `briefing: enabled: false`.

## Feed health

If a feed fails `alert_after_failures` builds in a row (default 3), the workflow opens a
GitHub issue labeled `feed-health`, so GitHub emails you. The issue closes itself when the
feed recovers.

## Build now

Actions tab → **Build digest** → **Run workflow**, or:

```
gh workflow run build.yml -R nahamus/news-digest
```

## Build locally (optional)

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python build.py
```

Run the tests with `.venv\Scripts\python -m unittest discover tests` (the workflow runs
them before every build, and a failure stops the deploy).

Then open `site/index.html`. The log lists each feed and whether it failed.
A local build also writes to `data/`; run `git checkout data` and delete any new files
there afterwards so you don't commit local history.

## Install on your phone

Open the Pages URL → Share (iOS) or ⋮ menu (Android) → **Add to Home Screen**.
The last digest stays readable offline.
