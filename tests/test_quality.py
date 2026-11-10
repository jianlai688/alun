"""V1.1 质量评测单元测试：结构指标、标题规则与黄金集校验。"""
from __future__ import annotations

import unittest

from alun.app.parser import Line, _merge_heading_lines, _merge_two_lines, heading_of
from alun.quality import golden
from alun.quality import metrics as m


def line(text: str, *, page: int = 1, size: float = 10.5, bold: bool = False,
         x0: float = 60.0, y0: float = 100.0, x1: float = 260.0) -> Line:
    return Line(text, page, size, bold, x0, y0, x1, y0 + 10, x1 - x0)


class StructureMetricTest(unittest.TestCase):
    def test_perfect_match(self):
        gold = [{"title": "1 引言", "level": 1, "page": 1}, {"title": "1.1 方法", "level": 2, "page": 2}]
        pred = [{"title": "1 引言", "level": 1, "page_start": 1},
                {"title": "1.1 方法", "level": 2, "page_start": 2}]
        result = m.structure_metrics(gold, pred)
        self.assertEqual(result["f1"], 1.0)
        self.assertEqual(result["boundary_accuracy"], 1.0)
        self.assertEqual(result["level_accuracy"], 1.0)

    def test_wrapped_title_prefix(self):
        gold = [{"title": "1 CLIP 语义引导与 Mamba-KAN 协同的医学图像分割模型", "level": 1, "page": 3}]
        pred = [{"title": "1 CLIP 语义引导与 Mamba-KAN 协同的医", "level": 1, "page_start": 3}]
        result = m.structure_metrics(gold, pred)
        self.assertEqual(result["matched"], 1)

    def test_missing_and_extra(self):
        gold = [{"title": "1 引言", "level": 1, "page": 1}]
        pred = [{"title": "1 引言", "level": 1, "page_start": 1},
                {"title": "1 HW", "level": 1, "page_start": 2}]
        result = m.structure_metrics(gold, pred)
        self.assertEqual(result["precision"], 0.5)
        self.assertEqual(result["recall"], 1.0)
        self.assertEqual(len(result["extra"]), 1)


class HeadingRuleTest(unittest.TestCase):
    def test_title_starting_with_duo_is_not_rejected(self):
        self.assertIsNotNone(heading_of(line("2.2.4 多维度可验证奖励机制"), 10.5))

    def test_ascii_only_level_one_is_rejected(self):
        self.assertIsNone(heading_of(line("1 HW", size=12.3, x0=208), 10.6))

    def test_long_part_line_is_rejected(self):
        self.assertIsNone(heading_of(line("第二部分注重于关键特征的筛选,该分支通过注"), 10.6))

    def test_number_and_title_merge(self):
        a = line("1", page=4, size=12.0, x0=60, y0=70, x1=69)
        b = line("相关工作", page=4, size=12.0, x0=81, y0=70, x1=132)
        merged = _merge_two_lines(a, b, 9.0)
        self.assertIsNotNone(merged)
        self.assertEqual(merged.text, "1 相关工作")

    def test_title_before_number_merge(self):
        title = line("M3UCD 数据集上的实验结果", page=10, size=9.0, x0=81, y0=215, x1=196)
        number = line("4.2", page=10, size=9.0, x0=60, y0=215, x1=73)
        merged = _merge_heading_lines([title, number], 9.0)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].text, "4.2 M3UCD 数据集上的实验结果")


class GoldenTest(unittest.TestCase):
    def test_golden_v1_loads(self):
        bundle = golden.load_version("golden-v1")
        self.assertGreaterEqual(len(bundle["cases"]), 6)
        for case in bundle["cases"]:
            self.assertEqual(golden.validate_case(case), [])
            self.assertGreater(len(case["headings"]), 0)


if __name__ == "__main__":
    unittest.main()
