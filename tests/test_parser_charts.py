"""V2.0 图表数据提取回归：模型 JSON 规范化与 Markdown 表。"""
from __future__ import annotations

import unittest

from alun.app.parser import _normalise_chart


class ChartExtractionTest(unittest.TestCase):
    def test_bar_chart_is_normalized(self):
        block = _normalise_chart({
            "is_chart": True, "chart_type": "bar", "title": "Sales by Year",
            "x_label": "Year", "y_label": "Revenue", "unit": "million",
            "categories": ["2021", "2022", "2023"],
            "series": [{"name": "Revenue", "values": [12, 25, 18]}],
            "confidence": 0.98,
        }, page=2, fallback_title="图 1")
        self.assertIsNotNone(block)
        self.assertEqual(block["chart_type"], "bar")
        self.assertEqual(block["series"][0]["values"], [12.0, 25.0, 18.0])
        self.assertIn("| 2021 | 12 |", block["markdown"])
        self.assertEqual(block["data"]["title"], "Sales by Year")

    def test_non_chart_is_rejected(self):
        self.assertIsNone(_normalise_chart({"is_chart": False}, 1, "图 1"))
        self.assertIsNone(_normalise_chart({"is_chart": True, "series": []}, 1, "图 1"))


if __name__ == "__main__":
    unittest.main()
