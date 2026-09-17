"""Codex 账户用量载荷的无副作用规范化。"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Mapping


_WEEK_MINUTES = 7 * 24 * 60
_MAX_UNIX_SECONDS = 253_402_300_799


def normalize_account(
    payload: Mapping[str, Any] | Any,
    *,
    observed_at: datetime,
    current_account_id: str,
    now: datetime,
    max_age_seconds: int | float = 120,
) -> dict[str, Any]:
    """规范化已读取的账户限额快照。

    仅当 ``payload.accountId`` 与当前账户一致时才返回计划、限额和重置卡；函数不保存
    原始载荷，也不访问账户或凭据。``observed_at`` 和 ``now`` 必须携带时区。
    """
    _aware(observed_at, "observed_at")
    _aware(now, "now")
    if not _positive_finite(max_age_seconds):
        raise ValueError("max_age_seconds 必须为正的有限数值")
    observed_at_utc = observed_at.astimezone(UTC)
    base = {
        "identityMatch": False,
        "observedAt": _iso_utc(observed_at_utc),
        "freshness": "unavailable",
        "plan": None,
        "limits": {},
        "resetCredits": None,
        "subscriptionExpiresAt": None,
        "subscriptionSource": "unavailable",
    }
    if not isinstance(payload, Mapping) or not isinstance(current_account_id, str) or not current_account_id:
        return base
    if payload.get("accountId") != current_account_id:
        return base

    age_seconds = (now.astimezone(UTC) - observed_at_utc).total_seconds()
    base["identityMatch"] = True
    base["freshness"] = "live" if 0 <= age_seconds <= max_age_seconds else "stale"
    buckets = _buckets(payload)
    normalized_limits = {bucket_id: _normalize_bucket(bucket_id, bucket) for bucket_id, bucket in buckets.items()}
    base["limits"] = normalized_limits
    base["plan"] = _plan(normalized_limits)
    reset_credits = payload.get("rateLimitResetCredits")
    if isinstance(reset_credits, Mapping):
        base["resetCredits"] = _nonnegative_int(reset_credits.get("availableCount"))
    return base


def _buckets(payload: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    values = payload.get("rateLimitsByLimitId")
    if isinstance(values, Mapping):
        return {
            bucket_id: bucket
            for bucket_id, bucket in values.items()
            if isinstance(bucket_id, str) and bucket_id and isinstance(bucket, Mapping)
        }
    legacy = payload.get("rateLimits")
    if isinstance(legacy, Mapping):
        bucket_id = legacy.get("limitId")
        if isinstance(bucket_id, str) and bucket_id:
            return {bucket_id: legacy}
    return {}


def _normalize_bucket(bucket_id: str, bucket: Mapping[str, Any]) -> dict[str, Any]:
    plan = bucket.get("planType")
    return {
        "limitId": bucket.get("limitId") if isinstance(bucket.get("limitId"), str) else bucket_id,
        "limitName": bucket.get("limitName") if isinstance(bucket.get("limitName"), str) else None,
        "plan": plan if isinstance(plan, str) and plan else None,
        "primary": _normalize_window(bucket.get("primary")),
        "secondary": _normalize_window(bucket.get("secondary")),
    }


def _normalize_window(window: Any) -> dict[str, Any] | None:
    if not isinstance(window, Mapping):
        return None
    used_percent = _finite_number(window.get("usedPercent"))
    duration = _positive_integer(window.get("windowDurationMins"))
    reset_seconds = _unix_seconds(window.get("resetsAt"))
    return {
        "usedPercent": used_percent,
        "remainingPercent": _remaining_percent(used_percent),
        "windowDurationMins": duration,
        "windowKind": "week" if duration == _WEEK_MINUTES else "window",
        "resetsAt": _iso_from_unix(reset_seconds),
    }


def _plan(limits: Mapping[str, Mapping[str, Any]]) -> str | None:
    values = {bucket["plan"] for bucket in limits.values() if bucket["plan"] is not None}
    return next(iter(values)) if len(values) == 1 else None


def _remaining_percent(used_percent: int | float | None) -> int | float | None:
    if used_percent is None:
        return None
    return max(0, min(100, 100 - used_percent))


def _finite_number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except OverflowError:
        return None
    return value


def _positive_finite(value: Any) -> bool:
    return _finite_number(value) is not None and value > 0


def _positive_integer(value: Any) -> int | None:
    number = _finite_number(value)
    if number is None or number <= 0 or int(number) != number:
        return None
    return int(number)


def _nonnegative_int(value: Any) -> int | None:
    number = _finite_number(value)
    if number is None or number < 0 or int(number) != number:
        return None
    return int(number)


def _unix_seconds(value: Any) -> int | None:
    number = _finite_number(value)
    if number is None or number < 0 or int(number) != number or number > _MAX_UNIX_SECONDS:
        return None
    return int(number)


def _iso_from_unix(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    try:
        return _iso_utc(datetime.fromtimestamp(seconds, UTC))
    except (OverflowError, OSError, ValueError):
        return None


def _iso_utc(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _aware(value: Any, label: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} 必须为带时区的 datetime")
