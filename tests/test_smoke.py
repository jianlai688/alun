"""A论 冒烟测试：解析、知识点、规划与令牌。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from alun.app.knowledge import extract_knowledge
from alun.app.parser import _table_markdown, parse_document
from alun.app.planner import build_plan
from alun.app.security import decode_token, hash_password, issue_token, verify_password
from alun.app.url_import import _ArticleParser


SAMPLE = """# 面向低资源场景的检索增强生成方法研究

摘要：针对低资源场景下大语言模型事实性不足的问题，本文提出一种两阶段混合检索方法。该方法先以稀疏检索扩大召回，再以稠密检索与重排序精排。实验表明，该方法在自建数据集上 F1 达到 95.8%，较基线提升 6.2 个百分点。

关键词：检索增强生成；低资源；混合检索

## 1 引言

低资源场景中的专业文档往往篇幅长、术语密集。现有方法难以兼顾召回率与准确率，因此需要一种结构化的检索策略。

## 2 方法

### 2.1 两阶段混合检索

两阶段混合检索是指先使用 BM25 进行候选文本块召回，再使用向量检索与重排序模型进行精排。该方法能够在不增加标注成本的前提下提升关键信息定位精度。

### 2.2 强化微调

本文采用 GRPO 对基座模型进行强化微调，奖励由答案准确性、思维链质量与原文可追溯性三个维度组成。

## 3 实验

实验在自建电力文档数据集上进行。结果表明，两阶段混合检索使 F1 提升 6.2 个百分点，验证了方法的有效性。

## 4 结论

本文提出的方法在低资源专业文档抽取任务上取得了稳定提升。未来仍有待研究跨文档场景下的检索一致性。
"""


class SmokeTest(unittest.TestCase):
    def test_parse_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sample.md"
            path.write_text(SAMPLE, encoding="utf-8")
            doc = parse_document(path)
            self.assertIn("检索增强生成", doc.title)
            self.assertGreaterEqual(doc.stats["section_count"], 3)
            self.assertGreater(doc.text_chars, 100)
            kps, evs = extract_knowledge("doc_test", doc.nodes)
            self.assertGreater(len(kps), 0)
            self.assertGreater(len(evs), 0)
            self.assertTrue(all(kp["evidence_ids"] for kp in kps))

    def test_planner(self):
        nodes = [
            {"node_id": "n1", "parent_id": None, "node_type": "section", "title": "2 方法", "order_index": 2, "level": 1},
            {"node_id": "n2", "parent_id": None, "node_type": "section", "title": "3 实验", "order_index": 3, "level": 1},
        ]
        kps = [
            {"kp_id": "k1", "node_id": "n1", "name": "两阶段混合检索", "kp_type": "method", "importance": 0.9, "difficulty": 3},
            {"kp_id": "k2", "node_id": "n2", "name": "实验结果", "kp_type": "data", "importance": 0.8, "difficulty": 3},
        ]
        ev = {
            "k1": [{"evidence_id": "e1", "strength": "strong", "page_no": 1, "quote": "先召回后精排。"}],
            "k2": [{"evidence_id": "e2", "strength": "strong", "page_no": 2, "quote": "F1 达到 95.8%。"}],
        }
        plan = build_plan(nodes=nodes, kps=kps, evidence_by_kp=ev, mode="guided", question_count=5)
        self.assertGreaterEqual(plan["total"], 2)
        self.assertTrue(plan["items"])

    def test_question_type_filter(self):
        nodes = [
            {"node_id": "n1", "parent_id": None, "node_type": "section", "title": "2 方法", "order_index": 2, "level": 1},
            {"node_id": "n2", "parent_id": None, "node_type": "section", "title": "3 实验", "order_index": 3, "level": 1},
        ]
        kps = [
            {"kp_id": "k1", "node_id": "n1", "name": "方法", "kp_type": "method", "importance": 0.9, "difficulty": 3},
            {"kp_id": "k2", "node_id": "n2", "name": "结果", "kp_type": "data", "importance": 0.8, "difficulty": 3},
        ]
        ev = {"k1": [{"evidence_id": "e1", "strength": "strong", "page_no": 1, "quote": "方法。"}],
              "k2": [{"evidence_id": "e2", "strength": "strong", "page_no": 2, "quote": "结果。"}]}
        plan = build_plan(nodes=nodes, kps=kps, evidence_by_kp=ev, mode="guided", question_count=5,
                          question_types=["true_false"])
        self.assertTrue(plan["items"])
        self.assertTrue(all(item["q_type"] == "true_false" for item in plan["items"]))

    def test_token(self):
        token, ttl = issue_token("usr_test", "tnt_test", "documents:read", role="admin", username="tester")
        payload = decode_token(token)
        self.assertEqual(payload["sub"], "usr_test")
        self.assertEqual(payload["tenant_id"], "tnt_test")
        self.assertEqual(payload["role"], "admin")
        self.assertEqual(payload["username"], "tester")
        self.assertGreater(ttl, 0)

    def test_password_hash(self):
        encoded = hash_password("demo123")
        self.assertTrue(verify_password("demo123", encoded))
        self.assertFalse(verify_password("wrong", encoded))

    def test_web_article_markdown(self):
        parser = _ArticleParser()
        parser.feed("""<html><head><title>示例网页</title></head><body>
        <nav>导航噪声</nav><h1>研究问题</h1><p>这是网页的正文段落。</p>
        <table><tr><th>方法</th><th>结果</th></tr><tr><td>A</td><td>0.9</td></tr></table>
        </body></html>""")
        md = parser.markdown()
        self.assertIn("# 示例网页", md)
        self.assertIn("# 研究问题", md)
        self.assertNotIn("导航噪声", md)
        self.assertIn("| 方法 | 结果 |", md)

    def test_table_markdown(self):
        md = _table_markdown([["方法", "准确率"], ["基线", "0.82"], ["本文", "0.92"]])
        self.assertIn("| 方法 | 准确率 |", md)
        self.assertIn("| 本文 | 0.92 |", md)


if __name__ == "__main__":
    unittest.main()
