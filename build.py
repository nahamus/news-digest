"""Build the news digest: fetch RSS -> filter -> dedupe -> rank -> render site/."""

from __future__ import annotations

import html
import json
import math
import os
import re
import shutil
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import feedparser
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
DATA = ROOT / "data"
SEEN = DATA / "seen.json"
HEALTH = DATA / "health.json"
ARCHIVE = DATA / "archive"
TEMPLATES = ROOT / "templates"
STATIC = ROOT / "static"
SITE = ROOT / "site"

TIMEOUT = 20
SUMMARY_LEN = 220
SEEN_DAYS = 7
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

STOPWORDS = set(
    """a an and are as at be been but by for from has have he her his how i in into is it
    its of on or our over says say she so than that the their them they this to up was we
    were what when where which who why will with you your after about new more not can
    just now out all its first""".split()
)


@dataclass
class Item:
    title: str
    url: str
    summary: str
    published: datetime
    source: str
    category: int  # index into config categories
    order: int  # global config order (category, feed, entry)
    window: float  # this feed's lookback, in hours


@dataclass
class Story:
    items: list[Item] = field(default_factory=list)
    url_keys: list[str] = field(default_factory=list)
    category: int = 0
    score: float = 0.0
    new: bool = False
    first_seen: datetime | None = None

    @property
    def lead(self) -> Item:
        return self.items[0]

    @property
    def also(self) -> list[str]:
        out: list[str] = []
        for it in self.items[1:]:
            if it.source != self.lead.source and it.source not in out:
                out.append(it.source)
        return out

    @property
    def outlets(self) -> int:
        return 1 + len(self.also)


# ---------------------------------------------------------------- helpers

def log(msg: str) -> None:
    print(msg, flush=True)


def word_regex(words: list[str] | None) -> re.Pattern | None:
    words = [str(w).strip() for w in (words or []) if str(w).strip()]
    if not words:
        return None
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)


def clean_text(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def truncate(text: str, limit: int = SUMMARY_LEN) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(",;:.-–— ")
    return cut + "…"


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower().replace("’", "'")).strip()


def sig_words(text: str) -> set[str]:
    """Significant words, lightly stemmed (plural -s dropped) so 'stars' matches 'star'."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower().replace("’", "").replace("'", "")):
        if w in STOPWORDS or len(w) < 2:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def url_key(url: str) -> str:
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    return host + parts.path.rstrip("/")


def entry_time(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc)
    return None


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data, indent: int | None = 0) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=indent, ensure_ascii=False) + "\n", encoding="utf-8")


# ---------------------------------------------------------------- fetch + health

def fetch(url: str) -> tuple[str, feedparser.FeedParserDict | None, str]:
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
            },
        )
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read()
        parsed = feedparser.parse(body)
        if not parsed.entries:
            return url, None, str(parsed.get("bozo_exception") or "no entries")
        return url, parsed, ""
    except Exception as exc:  # one bad feed never breaks the build
        return url, None, f"{type(exc).__name__}: {exc}"


def update_health(cfg: dict, results: dict, now: datetime) -> None:
    """Track consecutive failures per feed URL in data/health.json (read by feed_alerts.py)."""
    old = load_json(HEALTH, {}).get("feeds", {})
    feeds = {}
    for cat in cfg["categories"]:
        for feed in cat["feeds"]:
            url = feed["url"]
            if url in feeds:
                continue
            prev = old.get(url, {})
            _, err = results[url]
            ok = not err
            feeds[url] = {
                "name": feed["name"],
                "category": cat["name"],
                "fails": 0 if ok else prev.get("fails", 0) + 1,
                "error": "" if ok else err[:300],
                "last_ok": now.isoformat(timespec="seconds") if ok else prev.get("last_ok", ""),
            }
    save_json(HEALTH, {"feeds": feeds}, indent=1)


# ---------------------------------------------------------------- pipeline

def clean_summary(summary: str, title: str) -> str:
    # journal boilerplate, e.g. "Nature, Published online: 20 November 2026; doi:10.1038/…"
    summary = re.sub(r"^[^.]{0,60}Published online:[^;]*;\s*doi:\S+\s*", "", summary)
    # Hacker News "Article URL: … Comments URL: …", or a summary that just repeats the headline
    if summary.startswith("Article URL:") or norm(summary).startswith(norm(title)):
        return ""
    return truncate(summary)


def read_entry(entry, feed: dict) -> tuple[str, str, str, datetime, str] | None:
    """Normalize one feed entry to (title, link, summary, published, source), or None."""
    title = clean_text(entry.get("title", ""))
    link = (entry.get("link") or "").strip()
    when = entry_time(entry)
    if not title or not link or when is None:
        return None
    source = feed["name"]
    if feed.get("publisher_from_entry"):
        # Aggregators like Google News: credit the real outlet, and drop the
        # " - Outlet" suffix they append to every headline.
        publisher = clean_text((entry.get("source") or {}).get("title", ""))
        allowed = feed.get("publishers")
        if allowed and publisher.casefold() not in {p.casefold() for p in allowed}:
            return None
        if publisher:
            source = publisher
            suffix = f" - {publisher}"
            if title.endswith(suffix) and len(title) > len(suffix):
                title = title[: -len(suffix)].rstrip()
    summary = clean_text(entry.get("summary") or entry.get("description") or "")
    return title, link, clean_summary(summary, title), when, source


def collect(cfg: dict, now: datetime) -> list[Item]:
    mute = word_regex(cfg.get("mute"))
    global_skip = cfg.get("skip_titles") or []
    default_window = cfg["lookback_hours"]
    urls = {f["url"] for c in cfg["categories"] for f in c["feeds"]}
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = {u: (p, err) for u, p, err in pool.map(fetch, sorted(urls))}
    update_health(cfg, results, now)

    items: list[Item] = []
    order = 0
    for ci, cat in enumerate(cfg["categories"]):
        kw = word_regex(cat.get("keywords"))
        for feed in cat["feeds"]:
            parsed, err = results[feed["url"]]
            label = f"[{cat['name']}] {feed['name']}"
            if parsed is None:
                log(f"  FAIL {label}: {err}")
                continue
            window = float(feed.get("lookback_hours", default_window))
            cutoff = now - timedelta(hours=window)
            skip = word_regex(global_skip + (feed.get("skip") or []))
            kept = skipped = 0
            for entry in parsed.entries:
                read = read_entry(entry, feed)
                if read is None:
                    continue
                title, link, summary, when, source = read
                if when < cutoff or when > now + timedelta(hours=1):
                    continue
                if skip and skip.search(title):
                    skipped += 1
                    continue
                text = f"{title} {summary}"
                if feed.get("filter") and (kw is None or not kw.search(text)):
                    continue
                if mute and mute.search(text):
                    continue
                items.append(Item(title, link, summary, when, source, ci, order, window))
                order += 1
                kept += 1
            note = f", {skipped} skipped" if skipped else ""
            log(f"  ok   {label}: {kept}/{len(parsed.entries)} kept{note}")
    return items


# Two items from different outlets are the same story when their words overlap enough,
# weighting rare words (names, places, "Riemann") far above common ones (IDF over this
# build's items). Tuned on real feeds: >= 0.25 overall plus distinctive shared headline
# words keeps false merges rare while catching differently-worded headlines.
MATCH_SIM = 0.25
MATCH_TITLE_WEIGHT = 7.0


def dedupe(items: list[Item]) -> list[Story]:
    items = sorted(items, key=lambda i: i.order)
    title_words = [sig_words(it.title) for it in items]
    all_words = [tw | sig_words(" ".join(it.summary.split()[:30]))
                 for it, tw in zip(items, title_words)]
    df: dict[str, int] = {}
    for ws in all_words:
        for w in ws:
            df[w] = df.get(w, 0) + 1
    n = len(items)
    idf = {w: math.log(n / c) for w, c in df.items()}
    weight = lambda ws: sum(idf[w] for w in ws)  # noqa: E731
    totals = [weight(ws) for ws in all_words]

    norm_titles = [norm(it.title) for it in items]

    def same_story(i: int, j: int) -> bool:
        # Identical headline: the same article reached us twice (e.g. an outlet's own feed
        # and a Google News redirect link), so merge even within one outlet.
        if norm_titles[i] and norm_titles[i] == norm_titles[j]:
            return True
        # Otherwise never merge an outlet with itself: its similar headlines are separate pieces.
        if items[i].source == items[j].source:
            return False
        if weight(title_words[i] & title_words[j]) < MATCH_TITLE_WEIGHT:
            return False
        denom = min(totals[i], totals[j]) or 1
        return weight(all_words[i] & all_words[j]) / denom >= MATCH_SIM

    stories: list[Story] = []
    owner: list[Story] = []  # story for each item index
    by_url: dict[str, Story] = {}
    for i, it in enumerate(items):
        key = url_key(it.url)
        story = by_url.get(key)
        if story is None:
            story = next((owner[j] for j in range(i) if same_story(i, j)), None)
        if story is None:
            story = Story()
            stories.append(story)
        story.items.append(it)
        if key not in story.url_keys:
            story.url_keys.append(key)
        owner.append(story)
        by_url[key] = story

    # Category = where most of the story's items came from. On a tie:
    # - one outlet in two of its own feeds (The Verge main vs. The Verge AI): the narrower
    #   category (fewer items this build) wins, since the topical feed is more specific;
    # - different outlets: the lead item's category wins;
    # then the earlier category in config.
    size: dict[int, int] = {}
    for it in items:
        size[it.category] = size.get(it.category, 0) + 1
    for s in stories:
        counts: dict[int, int] = {}
        for it in s.items:
            counts[it.category] = counts.get(it.category, 0) + 1
        one_outlet = len({it.source for it in s.items}) == 1
        lead_cat = s.lead.category
        s.category = min(counts, key=lambda c: (
            -counts[c], size[c] if one_outlet else (c != lead_cat), size[c], c))
    return stories


def rank(stories: list[Story], cfg: dict, now: datetime) -> tuple[list[Story], list[list[Story]]]:
    """Score stories; return (top stories, per-category lists). Top stories are not repeated."""
    for s in stories:
        age_h = (now - s.lead.published).total_seconds() / 3600
        recency = max(0.0, min(2.0, 2.0 * (1 - age_h / s.lead.window)))
        s.score = recency + 0.75 * len(s.also)
    ordered = sorted(stories, key=lambda s: (-s.score, s.lead.order))

    top_cfg = cfg.get("top_stories") or {}
    min_outlets = int(top_cfg.get("min_outlets", 3))
    top = [s for s in ordered if s.outlets >= min_outlets][: int(top_cfg.get("max", 5))]
    top_ids = {id(s) for s in top}

    per_cat: list[list[Story]] = [[] for _ in cfg["categories"]]
    for s in ordered:
        if id(s) in top_ids:
            continue
        cat = cfg["categories"][s.category]
        bucket = per_cat[s.category]
        cap = cat.get("max_per_source", cfg["max_per_source"])
        if len(bucket) >= cfg["max_per_category"]:
            continue
        if sum(1 for b in bucket if b.lead.source == s.lead.source) >= cap:
            continue
        bucket.append(s)
    return top, per_cat


def following(stories: list[Story], cfg: dict) -> list[Story]:
    rx = word_regex(cfg.get("follow"))
    if rx is None:
        return []
    hits = [s for s in stories if rx.search(f"{s.lead.title} {s.lead.summary}")]
    hits.sort(key=lambda s: (-s.score, s.lead.order))
    return hits[: int(cfg.get("follow_max", 8))]


def mark_new(stories: list[Story], now: datetime) -> None:
    first_build = not SEEN.exists()
    seen: dict[str, str] = {} if first_build else load_json(SEEN, {}).get("urls", {})

    for s in stories:
        s.new = not first_build and not any(k in seen for k in s.url_keys)

    stamp = now.isoformat(timespec="seconds")
    for s in stories:
        for k in s.url_keys:
            seen.setdefault(k, stamp)
        s.first_seen = min(datetime.fromisoformat(seen[k]) for k in s.url_keys)
    horizon = now - timedelta(days=SEEN_DAYS)
    seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v) >= horizon}
    save_json(SEEN, {"urls": dict(sorted(seen.items()))})


# ---------------------------------------------------------------- AI briefing (optional)

BRIEFING_SYSTEM = """You write the short briefing that opens each section of a personal news digest.
For each category you get the headlines (with blurbs and how many outlets covered each story).
Write 2-3 plain sentences per category saying what matters today: name the most important
developments and connect related stories. Neutral, factual tone. Use only the information
given - do not add facts, numbers or context that are not in the headlines and blurbs.
No markdown, no bullet points, no preamble."""


def has_claude_access() -> bool:
    """An API key, or keyless Workload Identity Federation (set up by the workflow)."""
    env = os.environ
    return bool(env.get("ANTHROPIC_API_KEY")) or bool(
        env.get("ANTHROPIC_FEDERATION_RULE_ID") and env.get("ANTHROPIC_ORGANIZATION_ID"))


def briefings(cfg: dict, blocks: list[dict]) -> dict[str, str]:
    """One Claude call for all categories. Skipped unless enabled and Claude access is set up."""
    bcfg = cfg.get("briefing") or {}
    if not bcfg.get("enabled") or not has_claude_access():
        return {}
    try:
        import anthropic
    except ImportError:
        log("Briefing skipped: anthropic package not installed")
        return {}

    parts = []
    for b in blocks:
        if b["kind"] != "category" or not b["stories"]:
            continue
        lines = [f"## {b['name']}"]
        for st in b["stories"]:
            covered = f" [{1 + len(st['also'])} outlets]" if st["also"] else ""
            lines.append(f"- {st['title']}{covered}: {st['summary']}")
        parts.append("\n".join(lines))
    if not parts:
        return {}

    schema = {
        "type": "object",
        "properties": {
            "briefings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"category": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["category", "text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["briefings"],
        "additionalProperties": False,
    }
    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=bcfg.get("model", "claude-opus-5-5"),
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=BRIEFING_SYSTEM,
            output_config={
                "effort": bcfg.get("effort", "low"),
                "format": {"type": "json_schema", "schema": schema},
            },
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
        )
        if response.stop_reason != "end_turn":
            log(f"Briefing skipped: stop_reason={response.stop_reason}")
            return {}
        text = next(b.text for b in response.content if b.type == "text")
        data = json.loads(text)
        out = {item["category"].strip(): item["text"].strip() for item in data["briefings"]}
        log(f"Briefing: {len(out)} categories ({response.usage.input_tokens} in / "
            f"{response.usage.output_tokens} out tokens)")
        return out
    except anthropic.APIStatusError as exc:
        log(f"Briefing skipped: API error {exc.status_code}: {exc.message}")
    except anthropic.APIConnectionError as exc:
        log(f"Briefing skipped: connection error: {exc}")
    except (ValueError, KeyError, StopIteration) as exc:
        log(f"Briefing skipped: unexpected response: {exc}")
    except anthropic.AnthropicError as exc:  # e.g. sign-in (credentials) failures
        log(f"Briefing skipped: {type(exc).__name__}: {exc}")
    return {}


# ---------------------------------------------------------------- render

def clock(dt: datetime) -> str:
    return f"{dt.strftime('%I').lstrip('0')}:{dt.strftime('%M %p')}"


def friendly_time(dt: datetime, now_local: datetime) -> str:
    days = (now_local.date() - dt.date()).days
    if days <= 0:
        return clock(dt)
    if days == 1:
        return f"Yesterday {clock(dt)}"
    if days < 6:
        return f"{dt.strftime('%a')} {clock(dt)}"
    return f"{dt.strftime('%b')} {dt.day}"  # a weekday name would be ambiguous a week out


def edition_name(hour: int) -> str:
    """Scheduled runs land ~7-8 AM, ~12-1 PM and ~5-6 PM local (cron can start late)."""
    if hour < 11:
        return "Morning"
    if hour < 15:
        return "Midday"
    return "Evening"


def card(s: Story, cfg: dict, tz: ZoneInfo, now_local: datetime) -> dict:
    return {
        "title": s.lead.title,
        "url": s.lead.url,
        "key": s.url_keys[0],
        "source": s.lead.source,
        "category": cfg["categories"][s.category]["name"],
        "cat_idx": s.category % 6,
        "time": friendly_time(s.lead.published.astimezone(tz), now_local),
        "iso": s.lead.published.isoformat(timespec="minutes"),
        "seen": int(s.first_seen.timestamp()) if s.first_seen else 0,
        "also": s.also,
        "summary": s.lead.summary,
        "new": s.new,
    }


def make_env(cfg: dict) -> Environment:
    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    rx = word_regex(cfg.get("follow"))

    def highlight(text: str) -> Markup:
        # Escape first, then wrap matches, so the output stays safe HTML. (A follow word
        # containing & < > " won't highlight, since it's matched against escaped text.)
        safe = str(escape(text))
        if rx is None:
            return Markup(safe)
        return Markup(rx.sub(lambda m: f"<mark>{m.group(0)}</mark>", safe))

    env.filters["hl"] = highlight
    return env


def build_context(cfg, top, follows, per_cat, now) -> dict:
    tz = ZoneInfo(cfg["timezone"])
    now_local = now.astimezone(tz)
    mk = lambda s: card(s, cfg, tz, now_local)  # noqa: E731

    blocks = []
    if top:
        blocks.append({"kind": "top", "name": "Top Stories", "slug": "top-stories", "color": "top",
                       "stories": [mk(s) for s in top]})
    if follows:
        blocks.append({"kind": "follow", "name": "Following", "slug": "following", "color": "follow",
                       "stories": [mk(s) for s in follows]})
    for i, (cat, stories) in enumerate(zip(cfg["categories"], per_cat)):
        blocks.append({"kind": "category", "name": cat["name"], "slug": slugify(cat["name"]),
                       "color": str(i % 6), "stories": [mk(s) for s in stories]})
    for b in blocks:
        b["new"] = sum(c["new"] for c in b["stories"])

    brief = briefings(cfg, blocks)
    for b in blocks:
        b["briefing"] = brief.get(b["name"], "")

    total_new = len({c["key"] for b in blocks for c in b["stories"] if c["new"]})
    edition = edition_name(now_local.hour)
    return {
        "title": cfg.get("title", "News Digest"),
        "updated": f"{now_local.strftime('%a %b')} {now_local.day}, {clock(now_local)}",
        "dateline": f"{now_local.strftime('%A, %B')} {now_local.day}",
        "updated_time": clock(now_local),
        "edition": edition,
        "date_id": f"{now_local:%Y-%m-%d}",
        "built": int(now.timestamp()),
        "total_new": total_new,
        "show_first": int(cfg.get("show_first", 5)),
        "blocks": blocks,
    }


def save_archive(ctx: dict, cfg: dict, now: datetime) -> list[dict]:
    """Store this edition as its day's entry and return all kept days, newest first.

    One file per day: each build overwrites the day's file, so a day ends up holding its
    last edition (Evening when that build runs). Keeps today plus archive_days - 1 days.
    """
    ARCHIVE.mkdir(parents=True, exist_ok=True)
    save_json(ARCHIVE / f"{ctx['date_id']}.json", ctx, indent=None)
    keep_days = max(1, int(cfg.get("archive_days", 15)))
    today = date.fromisoformat(ctx["date_id"])
    horizon = (today - timedelta(days=keep_days - 1)).isoformat()
    editions = []
    for path in sorted(ARCHIVE.glob("*.json"), reverse=True):
        if path.stem[:10] < horizon:
            path.unlink()
            continue
        editions.append({"id": path.stem, "path": path})
    return editions


def render(cfg: dict, ctx: dict, editions: list[dict]) -> None:
    env = make_env(cfg)
    if SITE.exists():
        shutil.rmtree(SITE)
    shutil.copytree(STATIC, SITE)

    page = env.get_template("digest.html").render(**ctx, base="", is_archive=False)
    (SITE / "index.html").write_text(page, encoding="utf-8")
    log(f"Wrote site/index.html ({len(page.encode()) / 1024:.1f} KB)")

    out = SITE / "archive"
    out.mkdir()
    listing = []
    for ed in editions:
        old = load_json(ed["path"], None)
        if not old:
            continue
        old_page = env.get_template("digest.html").render(**old, base="../", is_archive=True)
        (out / f"{ed['id']}.html").write_text(old_page, encoding="utf-8")
        listing.append({"id": ed["id"], "dateline": old["dateline"], "edition": old["edition"],
                        "count": len({c["key"] for b in old["blocks"] for c in b["stories"]})})
    index = env.get_template("archive.html").render(
        title=ctx["title"], editions=listing, base="../", is_archive=True)
    (out / "index.html").write_text(index, encoding="utf-8")
    log(f"Wrote {len(listing)} archived editions")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)

    log("Fetching feeds…")
    items = collect(cfg, now)
    stories = dedupe(items)
    mark_new(stories, now)
    top, per_cat = rank(stories, cfg, now)
    follows = following(stories, cfg)
    log(f"{len(items)} items -> {len(stories)} stories -> "
        f"{len(top)} top + {sum(len(c) for c in per_cat)} in categories, {len(follows)} followed")
    for cat, bucket in zip(cfg["categories"], per_cat):
        log(f"  {cat['name']}: {len(bucket)} ({sum(s.new for s in bucket)} new)")

    ctx = build_context(cfg, top, follows, per_cat, now)
    editions = save_archive(ctx, cfg, now)
    render(cfg, ctx, editions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
