"""默认账户的本机路由检查；只读非秘密配置与有界健康接口。"""
from __future__ import annotations

import http.client
import json
from pathlib import Path
import tomllib


def gateway_endpoint(data_dir: Path) -> str:
    """返回可用本机端点；路由缺失时拒绝执行，不静默直连旧账户。"""
    root = Path(data_dir) / "model-gateway"
    try:
        settings = json.loads((root / "settings.json").read_text())
        status = json.loads((root / "status.json").read_text())
        port = settings.get("port")
        if (type(port) is not int or not 1 <= port <= 65535
                or status.get("ready") is not True or status.get("port") != port
                or status.get("provider") != "workbench_gateway"):
            raise ValueError("invalid_gateway_status")
        client = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        try:
            client.request("GET", "/health")
            response = client.getresponse()
            body = json.loads(response.read(4097))
            if response.status != 200 or body.get("ready") is not True or body.get("protocol") != 1:
                raise ValueError("gateway_not_ready")
        finally:
            client.close()
    except (OSError, ValueError, TypeError, AttributeError, http.client.HTTPException):
        raise ValueError("默认账户路由未就绪，无法执行；请先恢复工作台账户网关") from None
    return f"http://127.0.0.1:{port}/v1"


def default_routing_status(data_dir: Path, config_path: Path | None = None) -> dict:
    """偏好保存和原生接入分别报告，不把数据库写成功当作切换已生效。"""
    try:
        endpoint = gateway_endpoint(data_dir)
    except ValueError:
        return {"routing_ready": False, "routing_reason": "gateway_unavailable"}
    try:
        config = tomllib.loads((config_path or Path.home() / ".codex/config.toml").read_text())
        provider = config.get("model_providers", {}).get("workbench_gateway", {})
        if (config.get("model_provider") != "workbench_gateway"
                or provider.get("base_url", "").rstrip("/") != endpoint
                or provider.get("wire_api") != "responses"
                or provider.get("supports_websockets") is not False):
            return {"routing_ready": False, "routing_reason": "native_gateway_inactive"}
    except (OSError, ValueError, TypeError, AttributeError):
        return {"routing_ready": False, "routing_reason": "native_gateway_inactive"}
    return {"routing_ready": True, "routing_reason": None}
