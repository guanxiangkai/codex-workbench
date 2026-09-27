"""定位桌面应用自带的官方 CLI；仅兼容已知的应用目录布局迁移。"""
from pathlib import Path
import os


_APP_CLI_PATHS = tuple(
    Path('/Applications') / app / 'Contents/Resources' / relative
    for app in ('ChatGPT.app', 'Codex.app')
    for relative in ('codex-cli/CodexCLI.app/Contents/MacOS/codex', 'codex')
)


def resolve_gateway_cli(configured: str | Path | None = None) -> Path:
    """自定义路径失效时明确失败；仅旧官方布局可自动寻找同应用的新布局。"""
    target = Path(configured) if configured else None
    if target and target.is_file() and os.access(target, os.X_OK):
        return target
    if target and target not in _APP_CLI_PATHS:
        raise ValueError('configured_codex_cli_unavailable')
    for candidate in _APP_CLI_PATHS:
        if target and candidate.parts[2] != target.parts[2]:
            continue
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise ValueError('bundled_codex_cli_unavailable')
