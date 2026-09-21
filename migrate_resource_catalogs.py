"""一次性把公开资源目录从聚合 catalog 拆为逐条 JSON；默认只预检。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from codex_workbench.model_catalog import configured_models
from codex_workbench.other_accounts import other_accounts


@dataclass(frozen=True)
class Plan:
    name: str
    catalog: Path
    entries: tuple[dict, ...]
    projection: object


def _document(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: dict) -> None:
    handle = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".workbench-", suffix=".tmp", delete=False)
    try:
        with handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _filename(index: int, identifier: str) -> str:
    """ID 是展示标识，不作为路径；稳定摘要避免路径穿越和文件名冲突。"""
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()[:20]
    return f"{index:04d}-{digest}.json"


def _plan(resources: Path, name: str) -> Plan:
    directory = resources / name
    catalog = directory / "catalog.json"
    if not catalog.is_file():
        return Plan(name, catalog, (), [] if name == "models" else {"providers": [], "accounts": []})
    active = [entry for entry in directory.glob("*.json") if entry != catalog]
    legacy = directory / "legacy" / "catalog.json"
    if active or legacy.exists():
        raise ValueError(f"{name} 已存在分片或恢复文件，停止以避免覆盖")
    raw = _document(catalog)
    if name == "models":
        projection = configured_models(catalog)
        entries = tuple({"version": 1, "model": item} for item in raw["models"])
    else:
        projection = other_accounts(catalog)
        entries = tuple(
            {"version": 1, "provider": {"id": provider["id"], "name": provider["name"]}, "account": account}
            for provider in raw["providers"] for account in provider["accounts"]
        )
    return Plan(name, catalog, entries, projection)


def _stage(resources: Path, plans: tuple[Plan, ...]) -> Path:
    staging = Path(tempfile.mkdtemp(prefix=".workbench-resource-migration-", dir=resources.parent))
    try:
        for plan in plans:
            directory = staging / plan.name
            directory.mkdir()
            for index, entry in enumerate(plan.entries, start=1):
                item = entry.get("model", entry.get("account"))
                _write(directory / _filename(index, item["id"]), entry)
            projected = configured_models(directory / "catalog.json") if plan.name == "models" else other_accounts(directory / "catalog.json")
            if projected != plan.projection:
                raise ValueError(f"{plan.name} 分片预演与原目录投影不一致")
        return staging
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def apply(resources: Path, plans: tuple[Plan, ...]) -> None:
    """先在隔离目录验证投影，再落盘；失败恢复 catalog 并删除本次生成的分片。"""
    staging = _stage(resources, plans)
    written: list[Path] = []
    moved: list[tuple[Path, Path]] = []
    legacy_dirs: list[Path] = []
    try:
        for plan in plans:
            if not plan.catalog.is_file():
                continue
            for fragment in sorted((staging / plan.name).glob("*.json")):
                target = plan.catalog.parent / fragment.name
                _write(target, _document(fragment))
                written.append(target)
        for plan in plans:
            if not plan.catalog.is_file():
                continue
            legacy = plan.catalog.parent / "legacy"
            legacy.mkdir()
            legacy_dirs.append(legacy)
            target = legacy / "catalog.json"
            os.replace(plan.catalog, target)
            moved.append((plan.catalog, target))
    except BaseException:
        for original, backup in reversed(moved):
            if backup.exists():
                os.replace(backup, original)
        for path in written:
            path.unlink(missing_ok=True)
        for directory in reversed(legacy_dirs):
            directory.rmdir()
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources-dir", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="写入分片并将旧 catalog 移到 legacy/；默认仅预检")
    parser.add_argument("--runtime-stopped", action="store_true", help="确认工作台运行时已停止；--apply 的必填保护")
    args = parser.parse_args()
    plans = tuple(_plan(args.resources_dir, name) for name in ("models", "accounts"))
    staging = _stage(args.resources_dir, plans)
    shutil.rmtree(staging, ignore_errors=True)
    print("; ".join(f"{plan.name}: {len(plan.entries)} 条" for plan in plans))
    if not args.apply:
        return
    if not args.runtime_stopped:
        parser.error("--apply 需要 --runtime-stopped，避免运行时读取迁移中的双份目录")
    apply(args.resources_dir, plans)


if __name__ == "__main__":
    main()
