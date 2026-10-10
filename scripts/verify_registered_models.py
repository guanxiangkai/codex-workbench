#!/usr/bin/env python3
"""通过 vault exec-stdin 一次性验证登记的 MiniMax 或智谱模型。

示例：vault exec-stdin example.minimax.api-key \\
  python scripts/verify_registered_models.py --resources-dir /path/resources \\
  --data-dir /path/data --provider minimax --credential-id example.minimax.api-key
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_workbench.model_catalog import configured_models
from codex_workbench.model_probe_worker import decode_credential
from codex_workbench.registered_model_verifier import (record_results, verify_model_with_review,
                                                        write_metadata, write_reviews)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources-dir", required=True, type=Path)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--provider", required=True, choices=("minimax", "bigmodel"))
    parser.add_argument("--credential-id", required=True)
    parser.add_argument("--model-id", action="append", dest="model_ids")
    parser.add_argument("--timeout", type=float, default=30)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 60:
        parser.error("--timeout 必须在 1 到 60 秒")
    try:
        selected = [model for model in configured_models(args.resources_dir / "models" / "catalog.json")
                    if model.get("provider_id") == args.provider and model.get("credential_id") == args.credential_id]
        if args.model_ids:
            requested = set(args.model_ids)
            selected = [model for model in selected if model["id"] in requested]
            if requested != {model["id"] for model in selected}:
                raise ValueError("model_selection")
        if not selected:
            raise ValueError("model_selection")
        # stdin 是唯一凭据入口；不读取环境变量、文件或命令行中任何密钥。
        token = decode_credential(sys.stdin.buffer.read(16385), expected_base_url=None)
        if token is None:
            raise ValueError("credential_format")
    except (OSError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        for model in locals().get("selected", []):
            print(json.dumps({"model_id": model["id"], "success": False, "status": "failed", "error_code": "credential_format"}, ensure_ascii=False))
        return 2
    # Resume an already accepted video task instead of submitting (and charging for)
    # another generation on a focused --model-id rerun.
    try:
        previous = json.loads((args.data_dir / "registered-model-verifications.json").read_text(encoding="utf-8"))
        pending = {item.get("model_id"): item.get("video_task_id") for item in previous.get("results", [])
                   if isinstance(item, dict) and not item.get("success")
                   and isinstance(item.get("video_task_id"), str)}
    except (OSError, ValueError, json.JSONDecodeError, AttributeError):
        pending = {}
    for model in selected:
        task_id = pending.get(model["id"])
        if isinstance(task_id, str):
            model["_verification_task_id"] = task_id
    outcomes = [verify_model_with_review(model, token, timeout=args.timeout, include_diagnostics=True) for model in selected]
    results = [result for result, _, _ in outcomes]
    metadata_results = [{**result, **diagnostic} for result, _, diagnostic in outcomes]
    reviews = {result["model_id"]: review for result, review, _ in outcomes if result["success"] and review}
    write_metadata(args.data_dir, metadata_results)
    if reviews:
        write_reviews(args.data_dir, reviews)
    record_results(args.data_dir, selected, results)
    for result in results:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if all(result["success"] or result["status"] == "skipped" for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
