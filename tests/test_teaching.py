"""V1.2 教学闭环核心回归：固定题卷会话、结构版本快照/回滚。"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from types import SimpleNamespace

from alun.app import assignments, db, sessions, versions


class TeachingCoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_path = db.DB_PATH
        db.DB_PATH = os.path.join(self.tmp.name, "test.db")
        db.init_db()
        now = db.iso()
        with db.db() as conn:
            conn.execute(
                "INSERT INTO document (doc_id, tenant_id, user_id, title, status, page_count, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                ("doc_t", "tnt_t", "usr_t", "测试论文", "ready", 10, now, now),
            )
            for uid, name, role in (("usr_t", "teacher1", "teacher"), ("usr_s", "student1", "user")):
                conn.execute(
                    "INSERT INTO app_user (user_id, username, password_hash, role, display_name, tenant_id, status, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (uid, name, "x", role, name, "tnt_t", "active", now),
                )
            conn.execute(
                "INSERT INTO question_set (set_id, doc_id, tenant_id, title, difficulty, question_ids, question_count, created_by, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                ("set_t", "doc_t", "tnt_t", "测试题卷", "mixed", db.dump([f"q_{i}" for i in range(6)]), 6, "usr_t", now),
            )
            for i in range(6):
                conn.execute(
                    "INSERT INTO node (node_id, doc_id, parent_id, level, node_type, order_no, title, text,"
                    " page_start, page_end, path, char_count, order_index) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"nd_{i}", "doc_t", None, 1, "section", i + 1, f"第 {i+1} 节", "正文", i + 1, i + 1,
                     f"/{i}", 2, i),
                )
                conn.execute(
                    "INSERT INTO question (q_id, doc_id, kp_id, node_id, q_type, bloom_level, difficulty, stem,"
                    " options, answer, rubric, evidence, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (f"q_{i}", "doc_t", "", f"nd_{i}", "single_choice", "理解", 3, f"第 {i+1} 题",
                     db.dump([{"key": "A", "text": "选项"}]), db.dump({"value": ["A"]}), "[]", "[]", now),
                )

    def tearDown(self):
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_fixed_paper_session_uses_exact_questions(self):
        s = sessions.create_session(
            doc_id="doc_t", user_id="usr_t", tenant_id="tnt_t",
            question_count=6, question_ids=[f"q_{i}" for i in range(6)],
        )
        self.assertEqual(s["total_questions"], 6)
        turn = sessions.next_question(s["session_id"], "tnt_t")
        self.assertEqual(turn["question"]["q_id"], "q_0")
        self.assertEqual(turn["question"]["stem"], "第 1 题")

    def test_assignment_assigns_and_starts_fixed_paper(self):
        teacher = SimpleNamespace(user_id="usr_t", role="teacher", tenant_id="tnt_t", username="teacher1")
        student = SimpleNamespace(user_id="usr_s", role="user", tenant_id="tnt_t", username="student1")
        created = assignments.create_assignment(teacher, "tnt_t", {"paper_id": "set_t", "assignee_id": "usr_s"})
        self.assertEqual(created["status"], "assigned")
        started = assignments.start_assignment(created["assignment_id"], student)
        self.assertEqual(started["session"]["total_questions"], 6)
        row = db.fetch_one("SELECT status, session_id FROM paper_assignment WHERE assignment_id=?", (created["assignment_id"],))
        self.assertEqual(row["status"], "in_progress")
        self.assertTrue(row["session_id"])

    def test_structure_snapshot_and_restore(self):
        snap = versions.snapshot("doc_t", "测试快照", "usr_t")
        self.assertIsNotNone(snap)
        with db.db() as conn:
            conn.execute("UPDATE node SET title='被改坏的标题' WHERE node_id='nd_0'")
        result = versions.restore("doc_t", 1, "usr_t")
        self.assertEqual(result["restored_from"], 1)
        row = db.fetch_one("SELECT title FROM node WHERE node_id='nd_0'")
        self.assertEqual(row["title"], "第 1 节")


if __name__ == "__main__":
    unittest.main()
