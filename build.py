"""Build the news digest: fetch RSS -> filter -> dedupe -> rank -> render site/."""

from __future__ import annotations

import html
import json
import re
import shutil
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import feedparser
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
SEEN = ROOT / "data" / "seen.json"
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


@dataclass
class Story:
    items: list[Item] = field(default_factory=list)
    url_keys: set[str] = field(default_factory=set)
    words: list[set[str]] = field(default_factory=list)
    category: int = 0
    score: float = 0.0
    new: bool = False

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


# ---------------------------------------------------------------- helpers

def log(msg: str) -> None:
    print(msg, flush=True)


def word_regex(words: list[str]) -> re.Pattern | None:
    words = [w for w in (words or []) if str(w).strip()]
    if not words:
        return None
    alts = "|".join(re.escape(str(w).strip()) for w in sorted(words, key=len, reverse=True))
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


def sig_words(title: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", title.lower().replace("’", "").replace("'", ""))
    return {w for w in words if w not in STOPWORDS and len(w) > 1}


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


def similar(a: set[str], b: set[str]) -> bool:
    shared = len(a & b)
    return shared >= 3 and shared / len(a | b) >= 0.5


# ---------------------------------------------------------------- pipeline

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
            reason = str(parsed.get("bozo_exception") or "no entries")
            return url, None, reason
        return url, parsed, ""
    except Exception as exc:  # one bad feed never breaks the build
        return url, None, f"{type(exc).__name__}: {exc}"


def collect(cfg: dict, cutoff: datetime, now: datetime) -> list[Item]:
    mute = word_regex(cfg.get("mute") or [])
    urls = {f["url"] for c in cfg["categories"] for f in c["feeds"]}
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = {u: (p, err) for u, p, err in pool.map(fetch, sorted(urls))}

    items: list[Item] = []
    order = 0
    for ci, cat in enumerate(cfg["categories"]):
        kw = word_regex(cat.get("keywords") or [])
        for feed in cat["feeds"]:
            parsed, err = results[feed["url"]]
            label = f"[{cat['name']}] {feed['name']}"
            if parsed is None:
                log(f"  FAIL {label}: {err}")
                continue
            kept = 0
            for entry in parsed.entries:
                title = clean_text(entry.get("title", ""))
                link = (entry.get("link") or "").strip()
                when = entry_time(entry)
                if not title or not link or when is None:
                    continue
                if when < cutoff or when > now + timedelta(hours=1):
                    continue
                summary = clean_text(entry.get("summary") or entry.get("description") or "")
                if summary.startswith("Article URL:") or norm(summary).startswith(norm(title)):
                    summary = ""
                summary = truncate(summary)
                text = f"{title} {summary}"
                if feed.get("filter") and (kw is None or not kw.search(text)):
                    continue
                if mute and mute.search(text):
                    continue
                items.append(Item(title, link, summary, when, feed["name"], ci, order))
                order += 1
                kept += 1
            log(f"  ok   {label}: {kept}/{len(parsed.entries)} kept")
    return items


def dedupe(items: list[Item]) -> list[Story]:
    stories: list[Story] = []
    by_url: dict[str, Story] = {}
    for it in sorted(items, key=lambda i: i.order):
        key = url_key(it.url)
        words = sig_words(it.title)
        story = by_url.get(key)
        if story is None:
            story = next((s for s in stories if any(similar(words, w) for w in s.words)), None)
        if story is None:
            story = Story()
            stories.append(story)
        story.items.append(it)
        story.url_keys.add(key)
        story.words.append(words)
        by_url[key] = story

    # Category = where most of the story's items came from. Ties go to the narrower
    # category (fewer items this build, e.g. The Verge AI feed vs. The Verge main feed),
    # then to the earlier category in config.
    size: dict[int, int] = {}
    for it in items:
        size[it.category] = size.get(it.category, 0) + 1
    for s in stories:
        counts: dict[int, int] = {}
        for it in s.items:
            counts[it.category] = counts.get(it.category, 0) + 1
        s.category = min(counts, key=lambda c: (-counts[c], size[c], c))
    return stories


def rank(stories: list[Story], cfg: dict, now: datetime) -> list[list[Story]]:
    window = cfg["lookback_hours"] * 3600
    for s in stories:
        age = (now - s.lead.published).total_seconds()
        recency = max(0.0, min(2.0, 2.0 * (1 - age / window)))
        s.score = recency + 0.75 * len(s.also)

    per_cat: list[list[Story]] = [[] for _ in cfg["categories"]]
    for s in sorted(stories, key=lambda s: (-s.score, s.lead.order)):
        bucket = per_cat[s.category]
        if len(bucket) >= cfg["max_per_category"]:
            continue
        if sum(1 for b in bucket if b.lead.source == s.lead.source) >= cfg["max_per_source"]:
            continue
        bucket.append(s)
    return per_cat


def mark_new(stories: list[Story], now: datetime) -> None:
    first_build = not SEEN.exists()
    seen: dict[str, str] = {}
    if not first_build:
        try:
            seen = json.loads(SEEN.read_text(encoding="utf-8")).get("urls", {})
        except (ValueError, OSError):
            seen = {}

    for s in stories:
        s.new = not first_build and not any(k in seen for k in s.url_keys)

    stamp = now.isoformat(timespec="seconds")
    for s in stories:
        for k in s.url_keys:
            seen.setdefault(k, stamp)
    horizon = now - timedelta(days=SEEN_DAYS)
    seen = {k: v for k, v in seen.items() if datetime.fromisoformat(v) >= horizon}

    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(
        json.dumps({"urls": dict(sorted(seen.items()))}, indent=0, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def clock(dt: datetime) -> str:
    return f"{dt.strftime('%I').lstrip('0')}:{dt.strftime('%M %p')}"


def friendly_time(dt: datetime, now_local: datetime) -> str:
    days = (now_local.date() - dt.date()).days
    if days <= 0:
        return clock(dt)
    if days == 1:
        return f"Yesterday {clock(dt)}"
    return f"{dt.strftime('%a')} {clock(dt)}"


def render(cfg: dict, per_cat: list[list[Story]], now: datetime) -> None:
    tz = ZoneInfo(cfg["timezone"])
    now_local = now.astimezone(tz)

    sections = []
    for cat, stories in zip(cfg["categories"], per_cat):
        cards = [
            {
                "title": s.lead.title,
                "url": s.lead.url,
                "source": s.lead.source,
                "time": friendly_time(s.lead.published.astimezone(tz), now_local),
                "iso": s.lead.published.isoformat(timespec="minutes"),
                "also": s.also,
                "summary": s.lead.summary,
                "new": s.new,
            }
            for s in stories
        ]
        slug = re.sub(r"[^a-z0-9]+", "-", cat["name"].lower()).strip("-")
        sections.append(
            {"name": cat["name"], "slug": slug, "stories": cards,
             "new": sum(c["new"] for c in cards)}
        )

    env = Environment(
        loader=FileSystemLoader(TEMPLATES),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    page = env.get_template("digest.html").render(
        title=cfg.get("title", "News Digest"),
        updated=f"{now_local.strftime('%a %b')} {now_local.day}, {clock(now_local)}",
        total_new=sum(s["new"] for s in sections),
        sections=sections,
    )

    if SITE.exists():
        shutil.rmtree(SITE)
    shutil.copytree(STATIC, SITE)
    (SITE / "index.html").write_text(page, encoding="utf-8")
    log(f"Wrote {SITE / 'index.html'} ({len(page.encode()) / 1024:.1f} KB)")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    cfg = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=cfg["lookback_hours"])

    log("Fetching feeds…")
    items = collect(cfg, cutoff, now)
    stories = dedupe(items)
    per_cat = rank(stories, cfg, now)
    mark_new(stories, now)
    log(f"{len(items)} items -> {len(stories)} stories -> "
        f"{sum(len(c) for c in per_cat)} shown")
    for cat, bucket in zip(cfg["categories"], per_cat):
        log(f"  {cat['name']}: {len(bucket)} ({sum(s.new for s in bucket)} new)")
    render(cfg, per_cat, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
