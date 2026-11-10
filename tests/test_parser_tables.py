"""V1.2+ 表格恢复回归：OCR Markdown 表格与普通文本区分。"""
from __future__ import annotations

import unittest

from alun.app.parser import _markdown_tables, _table_markdown


class TableRecoveryTest(unittest.TestCase):
    def test_ocr_markdown_table_is_recovered(self):
        text = """2.5 什么场景适合 Agent

| 更适合 | 不适合或应谨慎 |
| --- | --- |
| 非结构化输入 | 固定规则 |
| 跨系统闭环 | 单次查询 |
"""
        blocks = _markdown_tables(text)
        self.assertEqual(len(blocks), 1)
        self.assertIn("| 更适合 | 不适合或应谨慎 |", blocks[0])
        self.assertIn("非结构化输入", blocks[0])

    def test_plain_pipe_text_is_not_a_table(self):
        self.assertEqual(_markdown_tables("a | b\nsingle line without table"), [])
        self.assertEqual(_table_markdown([["a"]]), "")


if __name__ == "__main__":
    unittest.main()
