"""Conversation persistence checks using only temporary, synthetic text."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from shu.memory import ConversationMemory, MAX_TEXT_LENGTH, MemoryClosedError


class ConversationMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.memory = ConversationMemory(self.directory)
        self.addCleanup(self.memory.close)

    def test_messages_and_unicode_survive_restart_in_commit_order(self):
        originals = [("user", "黍，先看一下\n游戏界面 🌾 ' \" ; --"),
                     ("system", "观察成功，尚未操作"),
                     ("assistant", "我看到了主页。\n还需要确认你这次的目标。")]
        identifiers = [self.memory.append(role, text)["id"] for role, text in originals]
        self.memory.close()
        reopened = ConversationMemory(self.directory)
        self.addCleanup(reopened.close)
        loaded = reopened.load_messages(10)
        self.assertEqual([(m["role"], m["content"]) for m in loaded], originals)
        self.assertEqual([m["id"] for m in loaded], identifiers)
        self.assertTrue(all(m["created_at"].endswith("+00:00") for m in loaded))

    def test_visible_limit_is_latest_chronological_and_returns_detached_records(self):
        for number in range(5):
            self.memory.append("user", f"消息 {number}")
        records = self.memory.load_messages(2)
        self.assertEqual([m["content"] for m in records], ["消息 3", "消息 4"])
        records[0]["content"] = "本地修改"
        self.assertEqual(self.memory.load_messages(2)[0]["content"], "消息 3")

    def test_model_context_keeps_complete_turns_and_excludes_status_and_pending_request(self):
        self.memory.append("assistant", "独立问候，不进入模型恢复")
        for number in range(4):
            self.memory.append("user", f"用户 {number}")
            self.memory.append("system", f"内部诊断 {number}")
            self.memory.append("assistant", f"回复 {number}")
        self.memory.append("user", "中断的请求，尚未收到回复")
        self.assertEqual(self.memory.model_messages(2), [
            {"role": "user", "content": "用户 2"},
            {"role": "assistant", "content": "回复 2"},
            {"role": "user", "content": "用户 3"},
            {"role": "assistant", "content": "回复 3"},
        ])
        self.assertEqual(len(self.memory.load_messages(100)), 14)

    def test_multiple_texts_in_a_turn_are_never_split_by_turn_limit(self):
        for role, text in [("user", "第一轮"), ("assistant", "第一轮回答"),
                           ("user", "第二轮问题"), ("user", "第二轮补充"),
                           ("assistant", "第二轮回答"), ("assistant", "第二轮补充回答")]:
            self.memory.append(role, text)
        self.assertEqual([m["content"] for m in self.memory.model_messages(1)],
                         ["第二轮问题", "第二轮补充", "第二轮回答", "第二轮补充回答"])

    def test_status_only_and_unanswered_histories_do_not_become_model_context(self):
        self.memory.append("system", "网络失败")
        self.memory.append("user", "尚未回答的问题")
        self.assertEqual(self.memory.model_messages(), [])
        self.assertEqual(len(self.memory.load_messages()), 2)

    def test_non_text_payloads_and_invalid_roles_are_rejected_without_writes(self):
        for role in ("tool", "developer", "reasoning", None):
            with self.subTest(role=role), self.assertRaises(ValueError):
                self.memory.append(role, "not stored")
        for payload in ({"reasoning_content": "hidden"}, {"tool_calls": ["pending"]},
                        [{"type": "image_url", "image_url": "data:fixture"}], b"bytes"):
            with self.subTest(payload=payload), self.assertRaises(TypeError):
                self.memory.append("assistant", payload)
        for payload in ("", " \n\t", "embedded\x00null", "x" * (MAX_TEXT_LENGTH + 1)):
            with self.subTest(length=len(payload)), self.assertRaises(ValueError):
                self.memory.append("user", payload)
        self.assertEqual(self.memory.load_messages(), [])

    def test_identifiable_credentials_are_redacted_before_persistence(self):
        tokens = ["sk-synthetic-secret-1234567890", "synthetic-bearer-1234567890",
                  "synthetic-env-key-1234567890", "synthetic-quoted-value"]
        text = (f"Key {tokens[0]}\nAuthorization: Bearer {tokens[1]}\n"
                f"DEEPSEEK_API_KEY={tokens[2]}\nAPI_KEY=\"{tokens[3]}\"")
        self.memory.append("user", text)
        self.memory.append("assistant", "配置说明已记录为可见文本")
        stored = self.memory.load_messages()
        for token in tokens:
            self.assertNotIn(token, str(stored))
            self.assertNotIn(token, str(self.memory.model_messages()))
        with closing(sqlite3.connect(self.memory.database_path)) as connection:
            persisted = connection.execute("SELECT content FROM conversation_messages").fetchall()
        self.assertTrue(all(token not in str(persisted) for token in tokens))

    def test_exact_secrets_stay_in_memory_and_are_redacted_from_database(self):
        token = "unprefixed-synthetic-credential-value"
        self.memory.set_secrets([token])
        self.memory.append("user", f"不要记录 {token}")
        self.assertNotIn(token, self.memory.load_messages()[0]["content"])
        with closing(sqlite3.connect(self.memory.database_path)) as connection:
            rows = connection.execute("SELECT content FROM conversation_messages").fetchall()
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        self.assertNotIn(token, str(rows))
        self.assertFalse(any("secret" in name for (name,) in tables))

    def test_failed_write_rolls_back_trigger_side_effects(self):
        with closing(sqlite3.connect(self.memory.database_path)) as connection:
            connection.executescript("""
                CREATE TABLE audit(value TEXT);
                CREATE TRIGGER reject_fixture BEFORE INSERT ON conversation_messages
                WHEN NEW.content = 'reject fixture'
                BEGIN
                    INSERT INTO audit(value) VALUES ('must rollback');
                    SELECT RAISE(ABORT, 'synthetic write failure');
                END;
            """)
        self.memory.append("user", "已提交")
        with self.assertRaises(sqlite3.IntegrityError):
            self.memory.append("assistant", "reject fixture")
        self.assertEqual([m["content"] for m in self.memory.load_messages()], ["已提交"])
        with closing(sqlite3.connect(self.memory.database_path)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)
        self.memory.append("assistant", "失败之后仍能继续写入")
        self.assertEqual(len(self.memory.load_messages()), 2)

    def test_parallel_appends_have_unique_order_and_survive_reopen(self):
        with ThreadPoolExecutor(max_workers=4) as workers:
            records = list(workers.map(lambda n: self.memory.append("user", f"并发 {n}"), range(30)))
        self.assertEqual(len({record["id"] for record in records}), 30)
        loaded = self.memory.load_messages(100)
        self.assertEqual([m["id"] for m in loaded], sorted(m["id"] for m in loaded))
        self.assertEqual({m["content"] for m in loaded}, {f"并发 {n}" for n in range(30)})

    def test_clear_removes_context_and_history_without_reusing_identifiers(self):
        original = self.memory.append("user", "之前的问题")
        self.memory.append("assistant", "之前的回答")
        self.memory.clear()
        self.assertEqual(self.memory.load_messages(), [])
        self.assertEqual(self.memory.model_messages(), [])
        self.assertGreater(self.memory.append("user", "新会话")["id"], original["id"])

    def test_argument_validation_and_close_are_predictable(self):
        for value in (0, -1, True, 1.5, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.memory.load_messages(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.memory.model_messages(value)
        self.memory.close()
        self.memory.close()
        for method, args in ((self.memory.append, ("user", "不会写入")),
                             (self.memory.load_messages, ()), (self.memory.model_messages, ()),
                             (self.memory.clear, ())):
            with self.assertRaises(MemoryClosedError):
                method(*args)


if __name__ == "__main__":
    unittest.main()
