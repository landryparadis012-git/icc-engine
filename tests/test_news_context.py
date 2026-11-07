import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "engine"))

from news_context import format_context, parse_github_event


class NewsContextTests(unittest.TestCase):
    def payload(self, **overrides):
        p = {
            "reason": "post-news scan",
            "currency": "USD",
            "event": "Consumer Price Index",
            "event_ts": 1000,
            "actual": "3.1%",
            "forecast": "3.0%",
            "previous": "2.9%",
        }
        p.update(overrides)
        return {"action": "icc.run", "client_payload": p}

    def test_accepts_fresh_post_news_event(self):
        ctx = parse_github_event(self.payload(), now=1100)
        self.assertEqual(ctx["currency"], "USD")
        self.assertEqual(ctx["event"], "Consumer Price Index")

    def test_rejects_non_news_dispatch(self):
        event = self.payload()
        event["client_payload"]["reason"] = "sweep self-heal"
        self.assertIsNone(parse_github_event(event, now=1100))

    def test_rejects_missing_actual_event_time(self):
        event = self.payload()
        del event["client_payload"]["event_ts"]
        self.assertIsNone(parse_github_event(event, now=1100))

    def test_rejects_stale_and_far_future_events(self):
        self.assertIsNone(parse_github_event(self.payload(), now=1000 + 2701))
        self.assertIsNone(parse_github_event(self.payload(), now=1000 - 121))


    def test_accepts_pre_news_guard_up_to_45_minutes_before_release(self):
        event = {
            "action": "icc.news_guard",
            "client_payload": {
                "reason": "pre-news guard",
                "currency": "USD",
                "event": "Nonfarm Payrolls",
                "event_ts": 2000,
            },
        }
        ctx = parse_github_event(event, now=1000)
        self.assertEqual(ctx["kind"], "guard")
        self.assertEqual(ctx["block_until"], 2090)
        self.assertEqual(ctx["key"], "2000:Nonfarm Payrolls")

    def test_rejects_expired_pre_news_guard(self):
        event = {
            "action": "icc.news_guard",
            "client_payload": {
                "reason": "pre-news guard",
                "currency": "USD",
                "event": "Nonfarm Payrolls",
                "event_ts": 1000,
            },
        }
        self.assertIsNone(parse_github_event(event, now=1121))

    def test_context_only_attaches_to_relevant_symbols(self):
        ctx = parse_github_event(self.payload(), now=1100)
        self.assertIn("POST-NEWS CONTEXT", format_context(ctx, "XAUUSD"))
        self.assertIn("actual 3.1%", format_context(ctx, "GBPUSD"))
        self.assertFalse(__import__("news_context").is_relevant({"currency": "EUR"}, "GBPUSD"))

    def test_context_is_annotation_not_trade_direction(self):
        ctx = parse_github_event(self.payload(), now=1100)
        line = format_context(ctx, "XAUUSD")
        self.assertIn("not a direction signal", line)
        self.assertNotIn("BUY", line)
        self.assertNotIn("SELL", line)


if __name__ == "__main__":
    unittest.main()
