"""隔离模型探测消费者：凭据仅从 stdin 进入，stdout 只输出安全判定。"""
import json
import re
import sys
from pathlib import Path


def decode_credential(payload: bytes, expected_base_url: str | None = None) -> str | None:
    """接受单个 API Key 或包含唯一 API Key 字段的保险库条目。"""
    if expected_base_url is not None and not isinstance(expected_base_url, str):
        raise ValueError("credential_format")
    if len(payload) > 16384:
        raise ValueError("credential_format")
    value = payload.decode("utf-8").strip()
    if not value:
        return None
    if value.startswith("{"):
        record = json.loads(value)
        if not isinstance(record, dict):
            raise ValueError("credential_format")
        target = record.get("target")
        if isinstance(target, dict) and isinstance(target.get("base_url"), str) and expected_base_url is not None:
            if target["base_url"].strip().rstrip("/") != expected_base_url.strip().rstrip("/"):
                raise ValueError("credential_target")
        candidates = {record[key] for key in ("api_key", "apiKey", "API_KEY", "APP_KEY", "token", "access_token", "value")
                      if isinstance(record.get(key), str) and record[key]}
        if len(candidates) != 1:
            raise ValueError("credential_format")
        value = candidates.pop().strip()
    if value.lower().startswith("bearer "):
        value = value[7:].strip()
    if not re.fullmatch(r"[\x21-\x7e]{1,8192}", value) or value.startswith("-----BEGIN"):
        raise ValueError("credential_format")
    return value


def main() -> None:
    """只使用经过父进程白名单导出的模型配置，不读环境变量里的登录或 API Key。"""
    try:
        config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        token = decode_credential(sys.stdin.buffer.read(16385), expected_base_url=config.get("base_url"))
        if config.get("credential_ref") and token is None:
            raise ValueError("credential_format")
    except (ValueError, UnicodeDecodeError, OSError, IndexError, TypeError) as error:
        if isinstance(error, ValueError) and str(error) == "credential_target":
            result = {"success": False, "code": "credential_target", "message": "保存的 Key 不属于当前接口，请重新配置 Key"}
        else:
            result = {"success": False, "code": "credential_format", "message": "模型配置或保险库条目格式无效；请使用单个 API Key 条目"}
    else:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        try:
            from codex_workbench.model_probe import probe_model
            result = probe_model(config, token=token, timeout=15)
        except Exception:
            result = {"success": False, "code": "probe_error", "message": "模型验证未能完成，请稍后重试"}
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
