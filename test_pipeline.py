import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from collect_market_data import build_record, request_window
from market_brief import CN, Flash, Quote
from send_brief import render


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.end = datetime(2026, 11, 7, 8, tzinfo=CN)
        self.request = {"version": 1, "request_id": "test-request-123", "edition": "2026-11-07-am", "window_end": self.end.isoformat()}

    def test_rejects_wrong_or_future_window(self):
        with self.assertRaises(ValueError):
            request_window({**self.request, "edition": "2026-11-07-pm"}, self.end)
        with self.assertRaises(ValueError):
            request_window(self.request, self.end - timedelta(minutes=1))
        with self.assertRaises(ValueError):
            request_window({**self.request, "window_end": "2026-11-07T08:00:00+00:00"}, self.end)

    @patch("collect_market_data.collect")
    def test_collect_preserves_unclassified_events_and_timestamps(self, collect):
        event = Flash("未列入固定规则的事件", "可供模型判断的新事实", self.end - timedelta(hours=1),
                      "财联社", "https://example.com/item", 1)
        quote = Quote(102.3, self.end - timedelta(minutes=5), "https://example.com/quote", 102.0)
        collect.return_value = ([event, event], {"美元指数": quote}, [], ["现货暂缺"], 3)
        record = build_record(self.request, "test-key", self.end)
        self.assertEqual(record["edition"], self.request["edition"])
        self.assertEqual(record["request_id"], self.request["request_id"])
        self.assertEqual(len(record["news"]), 1)
        self.assertEqual(record["news"][0]["title"], event.title)
        self.assertEqual(record["quotes"]["美元指数"]["at"], quote.at.isoformat())
        self.assertEqual(record["warnings"], ["现货暂缺"])
        self.assertNotIn("test-key", json.dumps(record))

    def test_feishu_card_supports_complete_ai_report(self):
        message, = render({"version": 2, "edition": "2026-11-07-am",
                           "content": "风险偏好：分化\n\n[来源](https://example.com/)\n\n展望：待确认"})
        content = message["card"]["elements"][0]["text"]["content"]
        self.assertIn("风险偏好", content)
        self.assertIn("展望", content)
        with self.assertRaises(ValueError):
            render({"version": 2, "edition": "bad", "content": "test"})
        with self.assertRaises(ValueError):
            render({"version": 2, "edition": "2026-11-07-am", "content": "文" * 5000})


if __name__ == "__main__":
    unittest.main()
