"""Tests for build.py. Run: python -m unittest discover tests"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import feedparser
import yaml

import build
from build import Item, Story

NOW = datetime(2026, 11, 21, 16, 0, tzinfo=timezone.utc)
TZ = ZoneInfo("America/New_York")


def item(title, url, source="Outlet", category=0, order=0, hours_ago=1.0, summary="", window=36):
    return Item(title, url, summary, NOW - timedelta(hours=hours_ago), source, category, order, window)


def filler(n=40, start=1000):
    """Unrelated items so word rarity (IDF) behaves like a real build of a few hundred items."""
    return [item(f"Unrelated report number {i} about subject{i} and matter{i}",
                 f"https://filler.example/{i}", source=f"Filler{i % 7}", order=start + i)
            for i in range(n)]


def cfg(**overrides):
    base = {
        "title": "Test Digest", "timezone": "America/New_York", "lookback_hours": 36,
        "max_per_category": 10, "max_per_source": 4, "show_first": 5,
        "top_stories": {"min_outlets": 3, "max": 5},
        "categories": [{"name": "Science", "feeds": []}, {"name": "Tech", "feeds": []},
                       {"name": "AI", "feeds": []}],
    }
    base.update(overrides)
    return base


class TextHelpers(unittest.TestCase):
    def test_clean_text_strips_tags_and_entities(self):
        self.assertEqual(build.clean_text("<p>Fish &amp; chips</p>\n<b>now</b>"), "Fish & chips now")

    def test_truncate_cuts_at_word_with_ellipsis(self):
        out = build.truncate("word " * 100, limit=22)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 23)
        self.assertNotIn("wor…", out)
        self.assertEqual(build.truncate("short"), "short")

    def test_url_key_ignores_query_www_and_trailing_slash(self):
        self.assertEqual(build.url_key("https://www.Example.com/a/b/?utm=1#x"),
                         build.url_key("http://example.com/a/b"))

    def test_sig_words_drops_stopwords_and_stems_plurals(self):
        self.assertEqual(build.sig_words("The stars and the Galaxies"), {"star", "galaxie"})
        self.assertIn("glass", build.sig_words("glass"))  # -ss isn't treated as a plural

    def test_word_regex_whole_words_only(self):
        rx = build.word_regex(["AI", "A.I.", "machine learning"])
        self.assertTrue(rx.search("New AI model"))
        self.assertTrue(rx.search("the A.I. boom"))
        self.assertTrue(rx.search("Machine Learning at scale"))
        self.assertFalse(rx.search("He said hi"))  # 'ai' inside 'said'
        self.assertIsNone(build.word_regex([]))
        self.assertIsNone(build.word_regex(None))

    def test_word_regex_punctuated_phrase(self):
        self.assertTrue(build.word_regex(["Pointed!"]).search("Pointed! Bloomberg's Weekly Quiz"))


class Summaries(unittest.TestCase):
    def test_nature_boilerplate_removed(self):
        s = "Nature, Published online: 20 November 2026; doi:10.1038/d41586-026-03812-7 Scholars say this."
        self.assertEqual(build.clean_summary(s, "Some headline"), "Scholars say this.")

    def test_hacker_news_stub_dropped(self):
        self.assertEqual(build.clean_summary("Article URL: https://x Comments URL: https://y", "T"), "")

    def test_summary_repeating_headline_dropped(self):
        self.assertEqual(build.clean_summary("Big news today. More words.", "Big news today"), "")

    def test_long_summary_truncated(self):
        self.assertLessEqual(len(build.clean_summary("x " * 300, "T")), build.SUMMARY_LEN + 1)


GOOGLE_NEWS_RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Troops mobilise along the border - France 24</title>
<link>https://news.google.com/rss/articles/abc</link>
<pubDate>Sat, 21 Nov 2026 14:00:00 GMT</pubDate>
<description>&lt;a href="x"&gt;Troops mobilise along the border&lt;/a&gt; France 24</description>
<source url="https://www.france24.com">France 24</source></item>
<item><title>No date here</title><link>https://example.com/x</link></item>
<item><title>Updated only</title><link>https://example.com/u</link>
<atom:updated xmlns:atom="http://www.w3.org/2005/Atom">2026-11-21T10:00:00Z</atom:updated></item>
</channel></rss>"""


class ReadEntry(unittest.TestCase):
    def setUp(self):
        self.entries = feedparser.parse(GOOGLE_NEWS_RSS).entries

    def test_google_news_credits_publisher_and_strips_suffix(self):
        title, link, summary, when, source = build.read_entry(
            self.entries[0], {"name": "Google News", "publisher_from_entry": True})
        self.assertEqual(title, "Troops mobilise along the border")
        self.assertEqual(source, "France 24")
        self.assertEqual(summary, "")  # only repeated the headline
        self.assertEqual(when, datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc))

    def test_publisher_allowlist(self):
        feed = {"name": "Google News", "publisher_from_entry": True}
        self.assertIsNotNone(build.read_entry(self.entries[0], {**feed, "publishers": ["france 24"]}))
        self.assertIsNone(build.read_entry(self.entries[0], {**feed, "publishers": ["Reuters"]}))

    def test_without_option_keeps_feed_name(self):
        title, *_, source = build.read_entry(self.entries[0], {"name": "Google News"})
        self.assertEqual(source, "Google News")
        self.assertTrue(title.endswith("- France 24"))

    def test_entry_without_date_is_skipped(self):
        self.assertIsNone(build.read_entry(self.entries[1], {"name": "X"}))


class FriendlyTime(unittest.TestCase):
    now_local = datetime(2026, 11, 21, 11, 0, tzinfo=TZ)  # a Saturday

    def fmt(self, *args):
        return build.friendly_time(datetime(*args, tzinfo=TZ), self.now_local)

    def test_today_yesterday_weekday_date(self):
        self.assertEqual(self.fmt(2026, 11, 21, 9, 5), "9:05 AM")
        self.assertEqual(self.fmt(2026, 11, 21, 0, 5), "12:05 AM")
        self.assertEqual(self.fmt(2026, 11, 20, 22, 0), "Yesterday 10:00 PM")
        self.assertEqual(self.fmt(2026, 11, 17, 8, 0), "Tue 8:00 AM")
        # a week ago: same weekday as today, so show the date instead
        self.assertEqual(self.fmt(2026, 11, 14, 22, 0), "Nov 14")


class Editions(unittest.TestCase):
    def test_scheduled_runs_get_the_right_name_even_when_late(self):
        # 7 AM, noon and 5 PM EST runs (8 AM, 1 PM, 6 PM in EDT), each up to ~45 min late
        for hour, name in ((7, "Morning"), (8, "Morning"), (12, "Midday"), (13, "Midday"),
                           (17, "Evening"), (18, "Evening")):
            self.assertEqual(build.edition_name(hour), name, hour)


class Dedupe(unittest.TestCase):
    def test_same_url_merges_even_with_different_query(self):
        stories = build.dedupe([
            item("Headline one", "https://a.com/story?x=1", source="A", order=0),
            item("Different words entirely", "https://www.a.com/story/", source="B", order=1),
        ] + filler())
        merged = [s for s in stories if len(s.items) == 2]
        self.assertEqual(len(merged), 1)

    def test_differently_worded_headlines_from_two_outlets_merge(self):
        a = item("Mathematician says AI solved the Riemann hypothesis", "https://n.com/1",
                 source="Nature", order=0,
                 summary="A mathematician claims an AI-assisted proof of the Riemann hypothesis.")
        b = item("AI claimed a solution to the 167-year-old Riemann hypothesis", "https://t.com/2",
                 source="TechCrunch", category=1, order=1,
                 summary="A mathematician says AI helped prove the Riemann hypothesis.")
        stories = build.dedupe([a, b] + filler())
        story = next(s for s in stories if s.lead is a)
        self.assertIs(story.items[1], b)
        self.assertEqual(story.also, ["TechCrunch"])

    def test_same_outlet_similar_headlines_stay_separate(self):
        a = item("Riemann hypothesis proof claimed by mathematician", "https://n.com/1", source="Nature")
        b = item("Riemann hypothesis proof explained by mathematician", "https://n.com/2",
                 source="Nature", order=1)
        stories = build.dedupe([a, b] + filler())
        self.assertFalse(any(len(s.items) > 1 for s in stories))

    def test_identical_headline_same_outlet_merges(self):
        # same article via the outlet's feed and via a Google News redirect
        a = item("The TPLF’s Setbacks Do Not Determine Tigray’s Future", "https://ei.com/tplf",
                 source="Ethiopia Insight")
        b = item("The TPLF's setbacks do not determine Tigray's future", "https://news.google.com/x",
                 source="Ethiopia Insight", order=1)
        stories = build.dedupe([a, b] + filler())
        self.assertEqual(len(next(s for s in stories if s.lead is a).items), 2)

    def test_unrelated_headlines_stay_separate(self):
        a = item("Giant star swallows neighbour", "https://a.com/1", source="A")
        b = item("Senate passes farm bill", "https://b.com/2", source="B", order=1)
        stories = build.dedupe([a, b] + filler())
        self.assertFalse(any(len(s.items) > 1 for s in stories))

    def test_lead_is_earliest_in_config_order(self):
        late = item("Story", "https://x.com/s", source="Late", order=5)
        early = item("Story", "https://x.com/s", source="Early", order=1)
        story = build.dedupe([late, early])[0]
        self.assertEqual(story.lead.source, "Early")

    def test_tie_one_outlet_two_feeds_goes_to_narrower_category(self):
        # The Verge main feed (Tech, big) and The Verge AI feed (AI, small) carry the same URL.
        items = [item("Verge AI story", "https://verge.com/ai1", source="The Verge", category=1, order=0),
                 item("Verge AI story", "https://verge.com/ai1", source="The Verge", category=2, order=50)]
        items += [item(f"Tech filler {i}", f"https://t.com/{i}", category=1, order=10 + i) for i in range(5)]
        story = next(s for s in build.dedupe(items) if len(s.items) == 2)
        self.assertEqual(story.category, 2)

    def test_tie_different_outlets_goes_to_lead_category(self):
        a = item("Story", "https://x.com/s", source="ScienceDaily", category=0, order=0)
        b = item("Story", "https://x.com/s", source="Ars", category=1, order=1)
        extra = [item(f"Science filler {i}", f"https://s.com/{i}", category=0, order=10 + i)
                 for i in range(5)]  # Science is the bigger category
        story = next(s for s in build.dedupe([a, b] + extra) if len(s.items) == 2)
        self.assertEqual(story.category, 0)

    def test_majority_category_wins(self):
        items = [item("Story", "https://x.com/s", source=src, category=c, order=o)
                 for src, c, o in (("A", 0, 0), ("B", 1, 1), ("C", 1, 2))]
        self.assertEqual(build.dedupe(items)[0].category, 1)


class Rank(unittest.TestCase):
    def story(self, title, source, category=0, hours_ago=1.0, outlets=1, order=0, window=36):
        s = Story(items=[item(title, f"https://{source}.com/{title}", source=source, category=category,
                              hours_ago=hours_ago, order=order, window=window)])
        for i in range(outlets - 1):
            s.items.append(item(title, f"https://o{i}.com/{title}", source=f"Other{i}", order=order))
        s.url_keys = [build.url_key(s.items[0].url)]
        s.category = category
        return s

    def test_top_stories_pulled_out_of_categories(self):
        big = self.story("big", "A", outlets=3)
        small = self.story("small", "B")
        top, per_cat = build.rank([big, small], cfg(), NOW)
        self.assertEqual(top, [big])
        self.assertNotIn(big, per_cat[0])
        self.assertIn(small, per_cat[0])

    def test_caps_per_category_and_per_source(self):
        stories = [self.story(f"s{i}", "Same", order=i) for i in range(6)]
        stories += [self.story(f"o{i}", f"Src{i}", order=10 + i) for i in range(10)]
        _, per_cat = build.rank(stories, cfg(), NOW)
        self.assertEqual(len(per_cat[0]), 10)
        self.assertEqual(sum(1 for s in per_cat[0] if s.lead.source == "Same"), 4)

    def test_category_can_override_source_cap(self):
        c = cfg(categories=[{"name": "Science", "max_per_source": 2, "feeds": []}])
        stories = [self.story(f"s{i}", "Same", order=i) for i in range(5)]
        _, per_cat = build.rank(stories, c, NOW)
        self.assertEqual(len(per_cat[0]), 2)

    def test_recency_uses_each_feeds_window(self):
        short = self.story("short", "A", hours_ago=30, window=36)
        long = self.story("long", "B", hours_ago=30, window=96)
        build.rank([short, long], cfg(), NOW)
        self.assertGreater(long.score, short.score)

    def test_more_outlets_rank_higher(self):
        one = self.story("one", "A", hours_ago=1)
        two = self.story("two", "B", hours_ago=5, outlets=2)
        _, per_cat = build.rank([one, two], cfg(), NOW)
        self.assertEqual(per_cat[0][0], two)


class Following(unittest.TestCase):
    def test_matches_follow_words_sorted_by_score(self):
        a = Story(items=[item("Federal Reserve holds rates", "https://a.com/1")], score=1.0)
        b = Story(items=[item("Unrelated", "https://b.com/2", summary="the federal reserve said")], score=2.0)
        c = Story(items=[item("Nothing here", "https://c.com/3")], score=3.0)
        self.assertEqual(build.following([a, b, c], {"follow": ["Federal Reserve"]}), [b, a])
        self.assertEqual(build.following([a, b, c], {}), [])


class StateFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [mock.patch.object(build, "SEEN", self.tmp / "seen.json"),
                   mock.patch.object(build, "HEALTH", self.tmp / "health.json")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def stories(self, *urls):
        out = []
        for u in urls:
            s = Story(items=[item("t", u)])
            s.url_keys = [build.url_key(u)]
            out.append(s)
        return out

    def test_first_build_marks_nothing_new(self):
        stories = self.stories("https://a.com/1", "https://a.com/2")
        build.mark_new(stories, NOW)
        self.assertFalse(any(s.new for s in stories))
        self.assertTrue(build.SEEN.exists())
        self.assertEqual(stories[0].first_seen, NOW)

    def test_later_build_marks_only_unseen(self):
        build.mark_new(self.stories("https://a.com/1"), NOW)
        later = NOW + timedelta(hours=10)
        old, fresh = self.stories("https://a.com/1", "https://a.com/2")
        build.mark_new([old, fresh], later)
        self.assertFalse(old.new)
        self.assertTrue(fresh.new)
        self.assertEqual(old.first_seen, NOW)  # keeps the original first-seen time

    def test_prunes_entries_older_than_a_week(self):
        build.SEEN.write_text(json.dumps({"urls": {
            "old.com/x": (NOW - timedelta(days=8)).isoformat(),
            "recent.com/y": (NOW - timedelta(days=2)).isoformat()}}))
        build.mark_new([], NOW)
        urls = json.loads(build.SEEN.read_text())["urls"]
        self.assertEqual(list(urls), ["recent.com/y"])

    def test_health_counts_consecutive_failures(self):
        c = {"categories": [{"name": "S", "feeds": [{"name": "F", "url": "u"}]}]}
        build.update_health(c, {"u": (None, "HTTP 500")}, NOW)
        build.update_health(c, {"u": (None, "HTTP 500")}, NOW)
        self.assertEqual(json.loads(build.HEALTH.read_text())["feeds"]["u"]["fails"], 2)
        build.update_health(c, {"u": (object(), "")}, NOW)
        feed = json.loads(build.HEALTH.read_text())["feeds"]["u"]
        self.assertEqual(feed["fails"], 0)
        self.assertEqual(feed["error"], "")


class Render(unittest.TestCase):
    def test_highlight_escapes_html_and_marks_follow_words(self):
        hl = build.make_env({"follow": ["Fed"]}).filters["hl"]
        out = str(hl("<script>x</script> The Fed meets"))
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("<mark>Fed</mark>", out)

    def test_full_render(self):
        tmp = Path(tempfile.mkdtemp())
        c = cfg(follow=["Riemann"])
        stories = []
        for i in range(8):
            s = Story(items=[item(f"Science story {i} <b>", f"https://s.com/{i}", source=f"S{i}", order=i)])
            s.url_keys, s.category, s.first_seen, s.new = [f"s.com/{i}"], 0, NOW, i == 0
            stories.append(s)
        top = Story(items=[item("Riemann proof", "https://t.com/r", source="A"),
                           item("Riemann proof", "https://u.com/r", source="B")])
        top.url_keys, top.category, top.first_seen = ["t.com/r"], 2, NOW
        with mock.patch.object(build, "SITE", tmp / "site"), \
                mock.patch.object(build, "ARCHIVE", tmp / "archive"), \
                mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            ctx = build.build_context(c, [top], [top], [stories, [], []], NOW)
            editions = build.save_archive(ctx, c, NOW)
            build.render(c, ctx, editions)
            page = (tmp / "site" / "index.html").read_text(encoding="utf-8")
            archive = (tmp / "site" / "archive" / "index.html").read_text(encoding="utf-8")
            self.assertTrue((tmp / "site" / "archive" / f"{ctx['edition_id']}.html").exists())
            self.assertTrue((tmp / "site" / "sw.js").exists())

        ids = re.findall(r'\bid="([^"]+)"', page)
        self.assertEqual(len(ids), len(set(ids)), "duplicate HTML ids")
        for slug in ("top-stories", "following", "science", "tech", "ai"):
            self.assertIn(slug, ids)
        self.assertIn("Show 3 more", page)
        self.assertIn("&lt;b&gt;", page)  # titles are escaped
        self.assertIn("<mark>Riemann</mark>", page)
        self.assertIn("Also covered by <b>B</b>", page)
        self.assertEqual(ctx["total_new"], 1)
        self.assertIn(f"{ctx['edition']} · ", archive)  # NOW is 11 AM Eastern: Midday


class Briefing(unittest.TestCase):
    def test_skipped_without_api_key(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            self.assertEqual(build.briefings({"briefing": {"enabled": True}}, []), {})

    def test_skipped_when_disabled(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"}):
            self.assertEqual(build.briefings({"briefing": {"enabled": False}}, []), {})


class ConfigFile(unittest.TestCase):
    def test_config_is_valid(self):
        c = yaml.safe_load((build.ROOT / "config.yaml").read_text(encoding="utf-8"))
        for key in ("title", "timezone", "lookback_hours", "max_per_category", "max_per_source"):
            self.assertIn(key, c)
        ZoneInfo(c["timezone"])
        slugs = [build.slugify(cat["name"]) for cat in c["categories"]]
        self.assertEqual(len(slugs), len(set(slugs)), "duplicate category names")
        for reserved in ("top", "top-stories", "following"):
            self.assertNotIn(reserved, slugs)
        for cat in c["categories"]:
            self.assertTrue(cat["feeds"], f"{cat['name']} has no feeds")
            for feed in cat["feeds"]:
                self.assertTrue(feed.get("name") and feed.get("url", "").startswith("http"), feed)
                if feed.get("filter"):
                    self.assertTrue(cat.get("keywords"), f"{feed['name']} filters but {cat['name']} has no keywords")


if __name__ == "__main__":
    unittest.main()
