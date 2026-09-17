#!/usr/bin/env python3
"""仅供隔离集成检查的模拟 CLI；不连接服务或读取认证。"""
import json
import sys
import time
import uuid


def emit(value):
    print(json.dumps(value), flush=True)


if "app-server" in sys.argv:
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        if method == "initialize":
            result = {"userAgent": "fixture"}
        elif method == "account/read":
            result = {"account": {"type": "chatgpt", "email": "demo@example.invalid", "planType": "pro"}}
        elif method == "account/rateLimits/read":
            result = {"rateLimitsByLimitId": {"codex": {"planType": "pro", "primary": {"usedPercent": 35, "windowDurationMins": 10080, "resetsAt": 1790000000}}}, "rateLimitResetCredits": {"availableCount": 2}}
        else:
            emit({"id": request["id"], "error": {"code": -32601, "message": "Fixture does not implement this method"}})
            continue
        emit({"id": request["id"], "result": result})
elif "exec" in sys.argv:
    prompt = sys.stdin.read()
    emit({"type": "thread.started", "thread_id": str(uuid.uuid4())})
    emit({"type": "turn.started"})
    time.sleep(0.2)
    emit({"type": "item.completed", "item": {"type": "agent_message", "text": "模拟执行已完成；收到本次规则与资源引用。"}})
    emit({"type": "turn.completed"})
else:
    raise SystemExit(2)
