"""账户概览规范化的纯函数测试。"""

from __future__ import annotations

import math
import unittest
from datetime import UTC, datetime, timedelta

from codex_workbench.accounts import normalize_account


NOW = datetime(2026, 9, 11, 8, 0, tzinfo=UTC)
ACCOUNT_ID = "account-current"


def payload() -> dict:
    """返回不含任何真实账户数据的模拟多桶载荷。"""
    return {
        "accountId": ACCOUNT_ID,
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "limitName": "Codex",
                "planType": "plus",
                "primary": {"usedPercent": 25, "windowDurationMins": 10080, "resetsAt": 1_789_113_600},
                "secondary": {"usedPercent": 50, "windowDurationMins": 60, "resetsAt": 1_789_027_200},
            },
            "other": {
                "limitId": "other",
                "limitName": "Other",
                "planType": "plus",
                "primary": {"usedPercent": 120, "windowDurationMins": 15, "resetsAt": 1_789_027_200},
                "secondary": None,
            },
        },
        "rateLimitResetCredits": {"availableCount": 2, "credits": {"balance": 999, "expiresAt": 1_789_113_600}},
    }


class NormalizeAccountTest(unittest.TestCase):
    """验证限额、身份隔离、时效和缺失字段处理。"""

    def normalize(self, value: object, observed_at: datetime = NOW) -> dict:
        """以当前模拟账户和时钟调用被测函数。"""
        return normalize_account(value, observed_at=observed_at, current_account_id=ACCOUNT_ID, now=NOW)

    def test_multiple_buckets_and_primary_week_are_preserved(self) -> None:
        """primary/secondary 保持原字段，周窗口由 10080 分钟而非字段位置识别。"""
        normalized = self.normalize(payload())
        self.assertTrue(normalized["identityMatch"])
        self.assertEqual("live", normalized["freshness"])
        self.assertEqual("plus", normalized["plan"])
        self.assertEqual({"codex", "other"}, set(normalized["limits"]))
        self.assertEqual("week", normalized["limits"]["codex"]["primary"]["windowKind"])
        self.assertEqual("window", normalized["limits"]["codex"]["secondary"]["windowKind"])
        self.assertEqual(75, normalized["limits"]["codex"]["primary"]["remainingPercent"])
        self.assertEqual(0, normalized["limits"]["other"]["primary"]["remainingPercent"])
        self.assertIsNone(normalized["limits"]["other"]["secondary"])
        self.assertTrue(normalized["limits"]["codex"]["primary"]["resetsAt"].endswith("Z"))

    def test_missing_fields_remain_unknown_and_credits_do_not_become_reset_cards(self) -> None:
        """缺字段保持 None，不能用工作区 credits.balance 或 expiresAt 填充重置卡或订阅。"""
        value = payload()
        value["rateLimitsByLimitId"]["codex"].pop("secondary")
        value["rateLimitResetCredits"] = {"credits": {"balance": 7, "expiresAt": 1_789_113_600}}
        normalized = self.normalize(value)
        self.assertIsNone(normalized["limits"]["codex"]["secondary"])
        self.assertIsNone(normalized["resetCredits"])
        self.assertIsNone(normalized["subscriptionExpiresAt"])
        self.assertEqual("unavailable", normalized["subscriptionSource"])

    def test_invalid_numbers_and_millisecond_dates_are_unknown(self) -> None:
        """布尔、NaN、无穷、非法时长和毫秒时间戳不得伪装为零或有效日期。"""
        value = payload()
        primary = value["rateLimitsByLimitId"]["codex"]["primary"]
        primary.update({"usedPercent": True, "windowDurationMins": math.nan, "resetsAt": 1_789_113_600_000})
        secondary = value["rateLimitsByLimitId"]["codex"]["secondary"]
        secondary.update({"usedPercent": math.inf, "windowDurationMins": True, "resetsAt": "unknown"})
        normalized = self.normalize(value)
        for window in (normalized["limits"]["codex"]["primary"], normalized["limits"]["codex"]["secondary"]):
            self.assertIsNone(window["usedPercent"])
            self.assertIsNone(window["remainingPercent"])
            self.assertIsNone(window["windowDurationMins"])
            self.assertIsNone(window["resetsAt"])

    def test_stale_observation_remains_attributed_but_not_live(self) -> None:
        """匹配账户的过期快照仍可显示，但 freshness 必须为 stale。"""
        normalized = self.normalize(payload(), NOW - timedelta(seconds=121))
        self.assertTrue(normalized["identityMatch"])
        self.assertEqual("stale", normalized["freshness"])
        self.assertEqual("plus", normalized["plan"])

    def test_identity_mismatch_redacts_other_account_data(self) -> None:
        """缺失或错配 accountId 时不能暴露另一账户的计划、限额或重置卡。"""
        value = payload()
        value["accountId"] = "account-other"
        normalized = self.normalize(value)
        self.assertFalse(normalized["identityMatch"])
        self.assertEqual("unavailable", normalized["freshness"])
        self.assertIsNone(normalized["plan"])
        self.assertEqual({}, normalized["limits"])
        self.assertIsNone(normalized["resetCredits"])

    def test_reset_credit_uses_available_count_only(self) -> None:
        """availableCount 是重置卡数量的唯一来源，credits 详情不能覆盖它。"""
        normalized = self.normalize(payload())
        self.assertEqual(2, normalized["resetCredits"])

    def test_extremely_large_number_is_unknown(self) -> None:
        """异常大整数不导致整个账户面板加载失败。"""
        value = payload()
        value["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = 10 ** 1000
        normalized = self.normalize(value)
        self.assertIsNone(normalized["limits"]["codex"]["primary"]["remainingPercent"])


if __name__ == "__main__":
    unittest.main()
