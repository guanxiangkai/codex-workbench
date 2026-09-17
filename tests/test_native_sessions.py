"""原生线程公开 RPC 契约的合成测试，不启动 CLI 或读取真实会话。"""

from __future__ import annotations

import unittest
from typing import Any

from codex_workbench.native_sessions import (
    NativeSessionClient,
    NativeSessionError,
    name_token,
    name_token_matches,
)
from codex_workbench.titles import safe_display_title


class FakeNativeSessionClient(NativeSessionClient):
    """绕过进程启动，仅记录公开 RPC 调用。"""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = replies
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if method not in self.METHODS:
            raise NativeSessionError("forbidden", "不支持该原生会话操作")
        self.calls.append((method, params or {}))
        if not self.replies:
            return {}
        return self.replies.pop(0)


class NativeSessionClientTests(unittest.TestCase):
    thread_id = "00000000-0000-4000-8000-000000000001"

    def response(self, *, name: str | None = "旧名称", state: str = "idle") -> dict[str, Any]:
        return {"thread": {"id": self.thread_id, "name": name, "status": {"type": state}}}

    def test_set_name_uses_public_method_then_treats_readback_as_authority(self) -> None:
        client = FakeNativeSessionClient([{}, self.response(name="原生已确认")])
        result = client.set_name(self.thread_id, "工作台请求名")
        self.assertEqual("原生已确认", result["name"])
        self.assertEqual([
            ("thread/name/set", {"threadId": self.thread_id, "name": "工作台请求名"}),
            ("thread/read", {"threadId": self.thread_id, "includeTurns": False}),
        ], client.calls)

    def test_read_discards_preview_and_turns_from_summary_response(self) -> None:
        """即使假 RPC 返回历史片段，客户端也只输出名称、身份和状态。"""
        response = self.response()
        response["thread"].update({
            "preview": "合成的首条消息摘要",
            "turns": [{"items": [{"text": "合成的历史正文"}]}],
        })
        client = FakeNativeSessionClient([response])
        self.assertEqual({
            "id": self.thread_id, "name": "旧名称", "status": {"type": "idle"},
        }, client.read(self.thread_id))
        self.assertEqual([
            ("thread/read", {"threadId": self.thread_id, "includeTurns": False}),
        ], client.calls)

    def test_read_preserves_explicit_null_name(self) -> None:
        """原生明确返回 null 时保留空值，不能冒充缺失的协议字段。"""
        client = FakeNativeSessionClient([self.response(name=None)])
        result = client.read(self.thread_id)
        self.assertIn("name", result)
        self.assertIsNone(result["name"])

    def test_read_rejects_missing_name(self) -> None:
        """名称缺失意味着无法建立可靠的改名前提。"""
        response = self.response()
        del response["thread"]["name"]
        client = FakeNativeSessionClient([response])
        with self.assertRaises(NativeSessionError) as caught:
            client.read(self.thread_id)
        self.assertEqual("protocol", caught.exception.code)

    def test_resume_gate_only_rejects_observed_active_threads(self) -> None:
        client = FakeNativeSessionClient([self.response(state="active")])
        with self.assertRaises(NativeSessionError) as caught:
            client.resume_gate(self.thread_id)
        self.assertEqual("thread_active", caught.exception.code)
        for state, observability in (("notLoaded", "not_loaded"), ("systemError", "status_error"), ("idle", "confirmed_idle")):
            with self.subTest(state=state):
                client = FakeNativeSessionClient([self.response(state=state)])
                result = client.resume_gate(self.thread_id)
                self.assertEqual(observability, result["resume_observability"])

    def test_rejects_non_public_rpc_and_malformed_status(self) -> None:
        client = FakeNativeSessionClient([self.response(state="mystery")])
        with self.assertRaises(NativeSessionError) as caught:
            client.read(self.thread_id)
        self.assertEqual("protocol", caught.exception.code)
        with self.assertRaises(NativeSessionError) as caught:
            client.request("thread/resume", {"threadId": self.thread_id})
        self.assertEqual("forbidden", caught.exception.code)


class NativeNameTokenTests(unittest.TestCase):
    """名称令牌必须依据原始值和线程身份，而非净化后的显示名。"""

    thread_id = "synthetic-thread-one"

    def test_name_tokens_distinguish_null_empty_and_whitespace(self) -> None:
        """不同原始空值不能因显示层归一化而误判为相同名称。"""
        names = (None, "", " ", "\t", "  ")
        tokens = [name_token(self.thread_id, name) for name in names]
        self.assertEqual(len(names), len(set(tokens)))
        for original, token in zip(names, tokens):
            for observed in names:
                with self.subTest(original=original, observed=observed):
                    self.assertEqual(
                        original == observed,
                        name_token_matches(token, self.thread_id, observed),
                    )

    def test_name_token_is_bound_to_thread_identity(self) -> None:
        """相同名称的另一线程不能复用当前线程的写入前提。"""
        token = name_token(self.thread_id, "同名会话")
        self.assertTrue(name_token_matches(token, self.thread_id, "同名会话"))
        other_id = "synthetic-thread-two"
        self.assertNotEqual(token, name_token(other_id, "同名会话"))
        self.assertFalse(name_token_matches(token, other_id, "同名会话"))

    def test_names_with_same_safe_title_have_different_tokens(self) -> None:
        """净化会抹去差异，令牌仍须发现原始名称已经被修改。"""
        first = "请修复任务列表，token=synthetic-first-value"
        second = "请修复任务列表，token=synthetic-second-value"
        self.assertEqual(safe_display_title(first), safe_display_title(second))
        token = name_token(self.thread_id, first)
        self.assertNotEqual(token, name_token(self.thread_id, second))
        self.assertTrue(name_token_matches(token, self.thread_id, first))
        self.assertFalse(name_token_matches(token, self.thread_id, second))
        self.assertFalse(name_token_matches(token, self.thread_id, safe_display_title(first)))

    def test_invalid_expected_token_cannot_authorize_name_change(self) -> None:
        """缺失、类型不符或畸形令牌必须返回不匹配，不能抛出编码异常。"""
        for expected in (None, "", 123, [], "非 ASCII 令牌", "n1:invalid"):
            with self.subTest(expected=expected):
                self.assertFalse(name_token_matches(expected, self.thread_id, "会话名称"))
