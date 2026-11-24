# News Digest

A personal news digest built from public RSS feeds by GitHub Actions three times a day
(7:17 AM, 12:17 PM and 5:17 PM Eastern) and published to GitHub Pages:
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
  `lookback_hours` (also per feed), `top_stories`, `archive_days` (one entry per day, default 15 days).

## AI briefing

Claude writes the briefing during each build. Access is keyless (Workload Identity
Federation): there is no API key to store or renew. Each run gets a short-lived GitHub
token, and the Anthropic SDK exchanges it for API access through a federation rule set up
in the Claude Console (Settings → Workload identity federation). The rule accepts only
`nahamus/news-digest` on `main`. The rule, organization, service account and workspace
IDs are in [digest.yml](.github/workflows/digest.yml). They are identifiers, not secrets.

To turn it off, set `briefing: enabled: false` in `config.yaml`, or archive the rule in
the Console. If sign-in fails, the build still publishes, just without the briefing (the
run log says why).

## Feed health

If a feed fails `alert_after_failures` builds in a row (default 3), the workflow opens a
GitHub issue labeled `feed-health`, so GitHub emails you. The issue closes itself when the
feed recovers.

## Schedule

GitHub's built-in `schedule` trigger never fired for this account, so a free
[cron-job.org](https://cron-job.org) job starts the builds instead. At 7:17, 12:17 and
17:17 (America/New_York, so daylight saving is handled) it sends:

```
POST https://api.github.com/repos/nahamus/news-digest/actions/workflows/digest.yml/dispatches
Authorization: Bearer <token>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
Content-Type: application/json

{"ref":"main"}
```

A success returns 204. The token is a fine-grained GitHub token limited to this repo with
**Actions: Read and write** only. **It expires**: when it does, cron-job.org emails a
failure. Create a new token with the same settings and paste it into the job's
`Authorization` header.

## Build now

Actions tab → **Build digest** → **Run workflow**, or:

```
gh workflow run digest.yml -R nahamus/news-digest
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
