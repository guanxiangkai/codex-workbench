#!/usr/bin/env python3
"""按需验证更新兼容性，只使用临时目录和本机合成模型，不接管或启动桌面。"""

import argparse
import asyncio
from datetime import UTC, datetime
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'tests'))
from codex_workbench.native_compatibility import METHODS, evaluate_upgrade, file_digest, protocol_contract
from native_protocol_probe import probe


def save_new(path: Path, value: dict):
    """只创建新验收文件，保留所有既有基线和历史证据。"""
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def main() -> int:
    """生成当前官方 schema、比较基线并跑隔离行为；原生未验收返回退出码 2。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=Path('/Applications/ChatGPT.app'))
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--baseline', type=Path)
    group.add_argument('--record-baseline', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    options = parser.parse_args()
    cli = options.app / 'Contents/Resources/codex'
    archive = options.app / 'Contents/Resources/app.asar'
    with (options.app / 'Contents/Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    artifacts = {'cli_sha256': file_digest(cli), 'desktop_sha256': file_digest(archive),
                 'desktop_version': info['CFBundleShortVersionString'], 'desktop_build': info['CFBundleVersion']}
    version = subprocess.run([str(cli), '--version'], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    with tempfile.TemporaryDirectory(prefix='workbench-schema-') as temporary:
        directory = Path(temporary)
        for name, arguments in [('stable', []), ('experimental', ['--experimental'])]:
            subprocess.run([str(cli), 'app-server', 'generate-json-schema', *arguments, '--out', str(directory / name)],
                           capture_output=True, check=True, timeout=30)
        observed = protocol_contract(directory / 'experimental')
        stable = json.loads((directory / 'stable/ClientRequest.json').read_text())
        stable_methods = {method for variant in stable.get('oneOf', [])
                          for method in variant.get('properties', {}).get('method', {}).get('enum', [])}
        if options.record_baseline:
            baseline = observed
        else:
            baseline = json.loads(options.baseline.read_text())['contract']
        result = asyncio.run(probe(cli))
    unchanged = artifacts['cli_sha256'] == file_digest(cli) and artifacts['desktop_sha256'] == file_digest(archive)
    decision = evaluate_upgrade(baseline, observed, result['checks'], artifacts=artifacts)
    if not unchanged:
        decision['native_routing_ready'] = False
        decision['artifact_changed_during_probe'] = True
    report = {'observed_at': datetime.now(UTC).isoformat(), 'artifacts': artifacts, 'cli_version': version,
              'required_methods_outside_stable_surface': sorted(METHODS - stable_methods),
              'baseline_is_current_version': bool(options.record_baseline),
              'protocol_probe': result, 'decision': decision,
              'not_verified': ['ChatGPT account switching', 'Native New Task and prewarming',
                               'Native tool callbacks', 'Native inactive archive', 'Future Desktop releases'],
              'side_effects': 'Temporary synthetic threads only; no official login access, desktop changes or model charges'}
    if options.record_baseline and unchanged:
        save_new(options.record_baseline, {'artifacts': artifacts, 'cli_version': version, 'contract': observed})
    save_new(options.report, report)
    print(json.dumps({'cli_version': version, 'decision': decision,
                      'protocol_checks_passed': result['protocol_checks_passed'],
                      'experimental_methods': report['required_methods_outside_stable_surface'],
                      'report': str(options.report)}, ensure_ascii=False))
    return 0 if decision['native_routing_ready'] else 2


if __name__ == '__main__':
    raise SystemExit(main())
