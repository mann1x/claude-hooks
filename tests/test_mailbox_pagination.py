"""Mailbox listings are pages: bounded, filterable, and navigable.

A session that has used the mailbox for weeks holds hundreds of
messages, and a listing that returned all of them was a wall of text.
These pin the paging contract end to end — store, tools and the hook
announcements — against a real SQLite file.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from claude_hooks.mailbox import filters  # noqa: E402
from claude_hooks.mailbox.filters import (  # noqa: E402
    FilterError, like_pattern, page_size, parse_when, split_terms,
)
from tests.test_mailbox import StoreHarness  # noqa: E402


def _backdate(store, mid: int, when: datetime) -> None:
    store._connect().execute(
        "UPDATE session_messages SET created_at = ? WHERE id = ?",
        (when.astimezone(timezone.utc).isoformat(), mid))
    store._connect().commit()


class FilterParsingTests(unittest.TestCase):
    NOW = datetime(2026, 9, 28, 12, 0, tzinfo=timezone.utc)

    def test_relative_ages(self):
        self.assertEqual(parse_when("3d", now=self.NOW), self.NOW - timedelta(days=3))
        self.assertEqual(parse_when("12h", now=self.NOW), self.NOW - timedelta(hours=12))
        self.assertEqual(parse_when("30m", now=self.NOW), self.NOW - timedelta(minutes=30))
        self.assertEqual(parse_when("2w", now=self.NOW), self.NOW - timedelta(weeks=2))

    def test_explicit_zone_is_honoured(self):
        self.assertEqual(parse_when("2026-09-27T14:30+02:00"),
                         datetime(2026, 9, 27, 12, 30, tzinfo=timezone.utc))
        self.assertEqual(parse_when("2026-09-27T14:30Z"),
                         datetime(2026, 9, 27, 14, 30, tzinfo=timezone.utc))

    def test_bare_date_as_until_includes_the_day(self):
        start = parse_when("2026-09-27")
        end = parse_when("2026-09-27", end=True)
        self.assertEqual(end - start, timedelta(days=1))

    def test_nonsense_is_a_fixable_error(self):
        with self.assertRaises(FilterError) as cm:
            parse_when("last tuesday")
        self.assertIn("3d", str(cm.exception))

    def test_empty_is_no_filter(self):
        self.assertIsNone(parse_when(""))
        self.assertIsNone(parse_when(None))

    def test_terms_keep_quoted_phrases(self):
        self.assertEqual(split_terms('bcache "wipe superblock" fix'),
                         ["bcache", "wipe superblock", "fix"])
        self.assertEqual(split_terms('unbalanced "quote'),
                         ["unbalanced", '"quote'])

    def test_like_wildcards_are_escaped(self):
        self.assertEqual(like_pattern("50%_Off"), "%50\\%\\_off%")

    def test_page_size_is_clamped(self):
        self.assertEqual(page_size(None), filters.DEFAULT_PAGE_SIZE)
        self.assertEqual(page_size(10_000), filters.MAX_PAGE_SIZE)
        self.assertEqual(page_size(0), 1)


class _Loaded(StoreHarness):
    """``me`` holds 45 messages from three senders."""

    N = 45

    def setUp(self):
        super().setUp()
        self.register("me", "solidpc", sid="mine")
        senders = ("alpha", "beta", "gamma")
        for i in range(self.N):
            self.store.send("me", f"subject {i:02d}",
                            f"body {i} " + ("bcache" if i % 5 == 0 else "misc"),
                            from_alias=senders[i % 3])

    def page(self, **kw):
        return self.store.inbox_page(alias="me", session_id="mine",
                                     host="solidpc", **kw)


class StorePagingTests(_Loaded):

    def test_pages_cover_everything_once(self):
        seen = []
        for n in (1, 2, 3):
            pg = self.page(page=n, page_size=20)
            self.assertEqual(pg.total, self.N)
            seen += [m["id"] for m in pg.rows]
        self.assertEqual(len(seen), self.N)
        self.assertEqual(len(set(seen)), self.N, "no overlap between pages")
        self.assertEqual(self.page(page=3, page_size=20).pages, 3)

    def test_past_the_end_is_empty_with_the_total(self):
        pg = self.page(page=9, page_size=20)
        self.assertEqual(pg.rows, [])
        self.assertEqual(pg.total, self.N)

    def test_unread_queue_is_oldest_first(self):
        rows = self.page(page_size=3).rows
        self.assertEqual([m["subject"] for m in rows],
                         ["subject 00", "subject 01", "subject 02"])

    def test_history_is_newest_first(self):
        rows = self.page(page_size=2, include_read=True).rows
        self.assertEqual([m["subject"] for m in rows],
                         ["subject 44", "subject 43"])

    def test_keyword_matches_subject_body_and_sender(self):
        self.assertEqual(self.page(query="bcache").total, 9)
        self.assertEqual(self.page(query="BCACHE").total, 9, "case-insensitive")
        self.assertEqual(self.page(query="gamma").total, 15)
        self.assertEqual(self.page(query="bcache gamma").total, 3, "terms AND")

    def test_like_wildcards_in_a_query_are_literal(self):
        self.assertEqual(self.page(query="%").total, 0)
        self.assertEqual(self.page(query="_").total, 0)

    def test_sender_filter(self):
        pg = self.page(sender="beta")
        self.assertEqual(pg.total, 15)
        self.assertTrue(all(m["from_alias"] == "beta" for m in pg.rows))
        self.assertEqual(self.page(sender="Beta@solidpc").total, 15)

    def test_date_window(self):
        rows = self.store.inbox(alias="me", session_id="mine", host="solidpc")
        old = datetime(2026, 1, 10, 12, tzinfo=timezone.utc)
        for m in rows[:5]:
            _backdate(self.store, m["id"], old)
        self.assertEqual(self.page(until=old + timedelta(days=1)).total, 5)
        self.assertEqual(self.page(since=old + timedelta(days=1)).total, self.N - 5)

    def test_count_matches_the_page_total(self):
        self.assertEqual(self.store.inbox_count(alias="me", session_id="mine",
                                                host="solidpc"), self.N)


class ToolPagingTests(_Loaded):

    def setUp(self):
        super().setUp()
        from claude_hooks.mailbox.tools import MailboxTools
        self.tools = MailboxTools(self.store, alias="me", session_id="mine",
                                  host="solidpc")

    def list(self, **args):
        return self.tools.call("mailbox-list", args)

    def test_default_is_one_bounded_page(self):
        out = self.list()
        self.assertEqual(len(re.findall(r"^  #\d+", out, re.M)),
                         filters.DEFAULT_PAGE_SIZE)
        self.assertIn(f"1–20 of {self.N}", out)
        self.assertIn("page 1 of 3", out)

    def test_footer_gives_the_exact_next_call(self):
        out = self.list(query="misc", limit=10)
        nxt = re.search(r"More: mailbox-list (\{.*\}) for the next page", out)
        self.assertIsNotNone(nxt, out)
        args = json.loads(nxt.group(1))
        self.assertEqual(args, {"query": "misc", "limit": 10, "page": 2})
        again = self.list(**args)
        self.assertIn("11–20 of 36", again)

    def test_last_page_has_no_footer(self):
        out = self.list(page=3)
        self.assertIn("41–45 of 45", out)
        self.assertNotIn("More:", out)

    def test_filter_is_named_in_the_header(self):
        out = self.list(query="bcache", **{"from": "alpha"})
        self.assertIn("matching 'bcache'", out)
        self.assertIn("from alpha", out)
        self.assertIn("3 unread message(s)", out)

    def test_no_match_says_what_was_searched(self):
        out = self.list(query="nothing-like-this")
        self.assertIn("No messages", out)
        self.assertIn("nothing-like-this", out)

    def test_past_the_end_says_how_many_pages(self):
        out = self.list(page=7)
        self.assertIn("past the end", out)
        self.assertIn("3 page(s)", out)

    def test_bad_filters_come_back_as_prose(self):
        self.assertIn("Could not read", self.list(since="yesterday-ish"))
        self.assertIn("order must be", self.list(order="random"))
        self.assertIn("is not before", self.list(since="1d", until="3d"))

    def test_sent_is_paged_and_filtered_too(self):
        from claude_hooks.mailbox.tools import MailboxTools
        alpha = MailboxTools(self.store, alias="alpha", session_id="a",
                             host="solidpc")
        out = alpha.call("mailbox-sent", {"limit": 5})
        self.assertIn("1–5 of 15", out)
        self.assertIn("More: mailbox-sent", out)
        out = alpha.call("mailbox-sent", {"query": "bcache"})
        self.assertIn("matching 'bcache'", out)
        self.assertEqual(len(re.findall(r"^  #\d+", out, re.M)), 3)

    def test_catalog_advertises_the_paging_arguments(self):
        from claude_hooks.mailbox.tools import tool_catalog
        by = {t["name"]: t["inputSchema"]["properties"] for t in tool_catalog()}
        for key in ("page", "limit", "query", "since", "until", "order", "from"):
            self.assertIn(key, by["mailbox-list"])
        for key in ("page", "limit", "query", "since", "until", "to"):
            self.assertIn(key, by["mailbox-sent"])


class AnnouncementCapTests(_Loaded):

    def test_render_summarises_beyond_the_page(self):
        from claude_hooks.mailbox.announce import render
        pg = self.page(page_size=filters.ANNOUNCE_MAX)
        block = render(pg.rows, alias="me", host="solidpc", total=pg.total)
        self.assertEqual(block.count("\n- `"), filters.ANNOUNCE_MAX)
        self.assertIn(f"**{self.N} unread**", block)
        self.assertIn(f"…and {self.N - filters.ANNOUNCE_MAX} more", block)

    def test_announce_block_fetches_one_page(self):
        from unittest import mock
        from claude_hooks.mailbox import hook
        from claude_hooks.mailbox.tools import MailboxTools
        tools = MailboxTools(self.store, alias="me", session_id="mine",
                             host="solidpc")
        with mock.patch.object(hook, "_tools", return_value=tools):
            block = hook.announce_block(
                event={"session_id": "mine"},
                config={"hooks": {"mailbox": {"enabled": True}}},
                providers=[])
        self.assertEqual(block.count("\n- `"), filters.ANNOUNCE_MAX)
        self.assertIn("more. `mailbox-list` pages", block)

    def test_stop_nudge_lists_ten_but_claims_the_batch(self):
        from unittest import mock
        from claude_hooks.hooks import stop
        from claude_hooks.mailbox import hook
        rows = self.store.inbox(alias="me", session_id="mine", host="solidpc")
        claimed = []

        def claim(sid, ids):
            claimed.extend(ids)
            return list(ids)
        with mock.patch.object(hook, "unread_messages", return_value=rows), \
                mock.patch.object(hook, "claim_nudge", side_effect=claim):
            reason = stop._mailbox_nudge_reason(
                {"session_id": "mine"}, {"hooks": {"mailbox": {}}}, [])
        self.assertIn(f"You have {self.N} unread", reason)
        self.assertEqual(len(re.findall(r"^- #\d+", reason, re.M)),
                         filters.ANNOUNCE_MAX)
        self.assertIn(f"…and {self.N - filters.ANNOUNCE_MAX} more", reason)
        self.assertEqual(len(claimed), self.N, "one nudge for the whole batch")


if __name__ == "__main__":
    unittest.main()
