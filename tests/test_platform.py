"""V2.0 平台层单元测试：SQL 占位符翻译、embedding 稳定性。"""
from __future__ import annotations

import unittest

from alun.app.db import _pg_sql
from alun.platform.vector_store import _vec


class PlatformTest(unittest.TestCase):
    def test_pg_placeholder_translation_skips_literals(self):
        sql = "SELECT '?' AS mark, id FROM t WHERE name=? AND note=?"
        self.assertEqual(_pg_sql(sql), "SELECT '?' AS mark, id FROM t WHERE name=%s AND note=%s")

    def test_embedding_is_deterministic_and_normalized(self):
        a = _vec("GRPO 优势估计")
        b = _vec("GRPO 优势估计")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 384)
        norm = sum(x * x for x in a) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=6)


if __name__ == "__main__":
    unittest.main()
