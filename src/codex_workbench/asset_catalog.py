"""已安装技能的有界只读素材目录。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import stat
import xml.etree.ElementTree as element_tree
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit


_MAX_DEPTH = 5
_MAX_PER_SKILL = 200
_MAX_ASSETS = 1_000
_MAX_TEXT_BYTES = 128 * 1024
_MAX_IMAGE_BYTES = 1024 * 1024
_FONT_SUFFIXES = frozenset({".ttf", ".woff", ".woff2"})
_TEXT_SUFFIXES = frozenset({".md", ".txt", ".css", ".html", ".json"})
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".svg"})
_METADATA_SUFFIXES = frozenset({".woff", ".woff2", ".ttf", ".pptx", ".docx", ".xlsx", ".pdf"})
_ALLOWED_SUFFIXES = _TEXT_SUFFIXES | _IMAGE_SUFFIXES | _METADATA_SUFFIXES
_FORBIDDEN_SUFFIXES = frozenset({
    ".env", ".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der", ".pub", ".ppk", ".jks", ".kdb", ".keystore",
})
_SECRET_NAME = re.compile(r"(?:^|[._-])(auth|key|secret|token|credential|password|passwd|dotenv)(?:[._-]|$)", re.IGNORECASE)
_SVG_TAGS = frozenset({"svg", "g", "path", "circle", "rect", "line", "polyline", "polygon", "ellipse", "title", "desc"})
_SVG_ATTRIBUTES = frozenset({
    "xmlns", "viewBox", "width", "height", "fill", "stroke", "stroke-width", "stroke-linecap", "stroke-linejoin",
    "stroke-miterlimit", "opacity", "fill-opacity", "stroke-opacity", "d", "cx", "cy", "r", "rx", "ry", "x", "y",
    "x1", "x2", "y1", "y2", "points", "transform", "id", "class",
})


class AssetCatalogError(ValueError):
    """素材不存在、已漂移或违反只读安全边界时抛出的错误。"""


@dataclass(frozen=True)
class _FileState:
    """一次安全读取前后的文件身份。"""

    real_path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class _Asset:
    """内部索引条目；绝对路径不属于公开响应。"""

    id: str
    skill_id: str
    skill_name: str
    root: Path
    relative_path: str
    kind: str
    name: str
    license: str | None
    source_url: str | None
    sha256: str | None


class AssetCatalog:
    """仅从已发现的技能根目录列出模板、字体和静态素材。

    ``skill_sources`` 返回由技能发现器授权的 ``id``、``name``、``path`` 项；本类不接受
    客户端路径，也不会扫描技能根目录以外的位置或访问网络。
    """

    def __init__(self, skill_sources: Callable[[], Iterable[dict[str, Any]]]) -> None:
        """保存技能发现回调；调用回调本身不产生文件写入。"""
        self._skill_sources = skill_sources

    def list(self) -> dict[str, Any]:
        """返回受上限约束的公开元数据，响应中不含内容和绝对路径。"""
        records, truncated = self._records()
        return {"assets": [self._public_asset(record) for record in records], "truncated": truncated}

    def detail(self, asset_id: str) -> dict[str, Any]:
        """返回单项的安全预览，未知或伪造 ID 不可用于探测本机路径。"""
        if not isinstance(asset_id, str) or not asset_id:
            raise AssetCatalogError("unknown asset id")
        records, _ = self._records()
        asset = next((record for record in records if record.id == asset_id), None)
        if asset is None:
            raise AssetCatalogError("unknown asset id")
        state = self._state(asset.root, asset.relative_path)
        if state is None:
            raise AssetCatalogError("asset is no longer available")
        public = self._public_asset(asset, state)
        suffix = PurePosixPath(asset.relative_path).suffix.lower()
        if (suffix in _METADATA_SUFFIXES and suffix not in _FONT_SUFFIXES) or public["status"] == "too_large":
            return {"asset": public, "preview": {"kind": "metadata"}}
        content = self._read_stable(asset, state)
        if asset.sha256 and hashlib.sha256(content).hexdigest().lower() != asset.sha256.lower():
            public["status"] = "integrity_failed"
            return {"asset": public, "preview": {"kind": "metadata"}}
        if suffix in _FONT_SUFFIXES:
            try:
                preview = _font_preview(content)
            except (OSError, ValueError):
                public["status"] = "unpreviewable"
                return {"asset": public, "preview": {"kind": "metadata"}}
            return {"asset": public, "preview": {"kind": "image", "data_uri": preview}}
        if suffix == ".svg":
            if not _safe_svg(content):
                public["status"] = "unpreviewable"
                return {"asset": public, "preview": {"kind": "metadata"}}
            return {"asset": public, "preview": {"kind": "image", "data_uri": _data_uri("image/svg+xml", content)}}
        if suffix in _IMAGE_SUFFIXES:
            mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}[suffix]
            return {"asset": public, "preview": {"kind": "image", "data_uri": _data_uri(mime, content)}}
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            public["status"] = "unpreviewable"
            return {"asset": public, "preview": {"kind": "metadata"}}
        return {"asset": public, "preview": {"kind": "text", "text": text}}

    def _records(self) -> tuple[list[_Asset], bool]:
        records: list[_Asset] = []
        truncated = False
        try:
            sources = list(self._skill_sources())
        except (OSError, TypeError, ValueError):
            raise AssetCatalogError("技能资源来源暂时不可用") from None
        normalized: list[tuple[str, str, Path]] = []
        for source in sources:
            if not isinstance(source, dict):
                continue
            skill_id, name, skill_path = source.get("id"), source.get("name"), source.get("path")
            if not all(isinstance(value, str) and value for value in (skill_id, name, skill_path)):
                continue
            path = Path(skill_path)
            if path.name != "SKILL.md" or not path.is_file():
                continue
            normalized.append((skill_id, name, path.parent.resolve()))
        for skill_id, name, root in sorted(normalized, key=lambda item: (item[0], item[1])):
            if len(records) >= _MAX_ASSETS:
                return records, True
            skill_records, skill_truncated = self._skill_records(skill_id, name, root, _MAX_ASSETS - len(records))
            records.extend(skill_records)
            truncated = truncated or skill_truncated
        return records, truncated

    def _skill_records(self, skill_id: str, skill_name: str, root: Path, remaining: int) -> tuple[list[_Asset], bool]:
        catalog = root / "assets" / "catalog.json"
        catalog_state = self._state(root, "assets/catalog.json")
        if catalog_state is not None:
            entries = self._catalog_entries(root, catalog_state)
        else:
            entries = ((path, {}) for path in self._walk_assets(root))
        records: list[_Asset] = []
        truncated = False
        for relative, metadata in entries:
            if len(records) >= min(_MAX_PER_SKILL, remaining):
                truncated = True
                break
            candidate = _clean_relative(relative)
            if candidate is None or not candidate.startswith("assets/") or any(x.startswith(".") for x in PurePosixPath(candidate).parts) or _is_secret(candidate):
                continue
            state = self._state(root, candidate)
            if state is None:
                continue
            suffix = PurePosixPath(candidate).suffix.lower()
            if suffix not in _ALLOWED_SUFFIXES:
                continue
            digest = metadata.get("sha256") if isinstance(metadata.get("sha256"), str) else None
            records.append(_Asset(
                id=_asset_id(skill_id, candidate), skill_id=skill_id, skill_name=skill_name[:160], root=root,
                relative_path=candidate, kind=_kind(metadata.get("kind"), suffix),
                name=(metadata.get("name") if isinstance(metadata.get("name"), str) else Path(candidate).stem)[:160],
                license=metadata["license"][:500] if isinstance(metadata.get("license"), str) else None,
                source_url=_source_url(metadata), sha256=digest if _valid_hash(digest) else None,
            ))
        return records, truncated

    def _catalog_entries(self, root:Path, before:_FileState) -> Iterable[tuple[str, dict[str, Any]]]:
        try:
            if before.size>1024*1024:raise AssetCatalogError('资源清单过大')
            fd=os.open(before.real_path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            with os.fdopen(fd,'rb') as stream:
                opened=os.fstat(stream.fileno())
                if (opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns)!=(before.device,before.inode,before.size,before.mtime_ns):raise AssetCatalogError('资源清单已变化')
                raw=stream.read(1024*1024+1)
            if len(raw)>1024*1024 or self._state(root,'assets/catalog.json')!=before:raise AssetCatalogError('资源清单已变化')
            payload=json.loads(raw)
        except (OSError,UnicodeDecodeError,json.JSONDecodeError):
            raise AssetCatalogError('资源清单无法读取') from None
        assets=payload.get('assets') if isinstance(payload,dict) else None
        if not isinstance(assets,list):raise AssetCatalogError('资源清单格式无效')
        return ((entry['path'],entry) for entry in assets if isinstance(entry,dict) and isinstance(entry.get('path'),str))

    def _walk_assets(self, root: Path) -> Iterable[str]:
        assets = root / "assets"
        if not assets.is_dir() or assets.is_symlink():
            return ()
        found: list[str] = []
        inspected=0
        for directory, directories, filenames in os.walk(assets, followlinks=False):
            inspected+=len(directories)+len(filenames)
            if inspected>4000:raise AssetCatalogError("资源目录过大，请提供素材清单")
            current = Path(directory)
            directories[:] = [name for name in directories if not name.startswith(".") and not (current / name).is_symlink()]
            relative_dir = current.relative_to(assets)
            if len(relative_dir.parts) >= _MAX_DEPTH:
                directories[:] = []
            for filename in filenames:
                path = current / filename
                relative = path.relative_to(root).as_posix()
                if len(PurePosixPath(relative).parts) <= _MAX_DEPTH:
                    found.append(relative)
        return sorted(found)

    def _state(self, root: Path, relative: str) -> _FileState | None:
        try:
            real_root = root.resolve(strict=True)
            path = root / relative
            real_path = path.resolve(strict=True)
            if not real_path.is_relative_to(real_root):
                return None
            details = real_path.stat()
            if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
                return None
            return _FileState(real_path, details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)
        except OSError:
            return None

    def _read_stable(self, asset: _Asset, before: _FileState) -> bytes:
        limit = _MAX_IMAGE_BYTES if PurePosixPath(asset.relative_path).suffix.lower() in _IMAGE_SUFFIXES | _FONT_SUFFIXES else _MAX_TEXT_BYTES
        if before.size > limit:
            raise AssetCatalogError("asset is too large to preview")
        try:
            fd=os.open(before.real_path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
            with os.fdopen(fd,'rb') as file:
                opened=os.fstat(file.fileno())
                if (opened.st_dev,opened.st_ino,opened.st_size,opened.st_mtime_ns)!=(before.device,before.inode,before.size,before.mtime_ns):
                    raise AssetCatalogError('asset changed before reading')
                content = file.read(limit + 1)
        except OSError as error:
            raise AssetCatalogError("asset cannot be read") from error
        after = self._state(asset.root, asset.relative_path)
        if len(content) > limit or after != before:
            raise AssetCatalogError("asset changed while reading")
        return content

    def _public_asset(self, asset: _Asset, state: _FileState | None = None) -> dict[str, Any]:
        state = state or self._state(asset.root, asset.relative_path)
        status = "available" if state else "unavailable"
        suffix = PurePosixPath(asset.relative_path).suffix.lower()
        if state and ((suffix in _IMAGE_SUFFIXES | _FONT_SUFFIXES and state.size > _MAX_IMAGE_BYTES) or (suffix not in _METADATA_SUFFIXES and suffix not in _IMAGE_SUFFIXES and state.size > _MAX_TEXT_BYTES)):
            status = "too_large"
        return {"id": asset.id, "name": asset.name, "kind": asset.kind, "skill_id": asset.skill_id,
                "skill_name": asset.skill_name, "relative_path": asset.relative_path, "size": state.size if state else None,
                "license": asset.license, "source_url": asset.source_url, "status": status, "updated_at": state.mtime_ns / 1_000_000_000 if state else None}


def _clean_relative(value: str) -> str | None:
    if "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _is_secret(relative: str) -> bool:
    name = PurePosixPath(relative).name.lower()
    return name in {".env", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"} or Path(name).suffix in _FORBIDDEN_SUFFIXES or bool(_SECRET_NAME.search(name))


def _asset_id(skill_id: str, relative: str) -> str:
    return "asset_" + hashlib.sha256((skill_id + "\0" + relative).encode("utf-8")).hexdigest()[:32]


def _valid_hash(value: str | None) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fA-F]{64}", value))


def _source_url(metadata: dict[str, Any]) -> str | None:
    for field in ("source_url", "source"):
        value = metadata.get(field)
        if isinstance(value,str) and value:
            if value.startswith('original:'):return '原创资源'
            try:
                parsed=urlsplit(value)
                if len(value)<=2048 and parsed.scheme in ('http','https') and parsed.hostname and not any((parsed.username,parsed.password,parsed.query,parsed.fragment)):return value
            except ValueError:pass
    return None


def _kind(value: Any, suffix: str) -> str:
    if isinstance(value, str) and value:
        return value[:64]
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _METADATA_SUFFIXES:
        return "template" if suffix in {".pptx", ".docx", ".xlsx", ".pdf"} else "font"
    return "text"


def _font_preview(content: bytes) -> str:
    """使用资源字体绘制字形样本；只返回内存中的图片，不安装字体或写文件。"""
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGBA", (560, 240), (255, 255, 255, 0))
    draw = ImageDraw.Draw(image)
    for text, size, y in (("Aa Bb Cc", 66, 74), ("0123456789", 34, 170)):
        font = ImageFont.truetype(io.BytesIO(content), size)
        draw.text((280, y), text, font=font, anchor="mm", fill="#263448")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return _data_uri("image/png", output.getvalue())


def _data_uri(mime: str, content: bytes) -> str:
    return "data:" + mime + ";base64," + base64.b64encode(content).decode("ascii")


def _safe_svg(content: bytes) -> bool:
    if len(content) > _MAX_TEXT_BYTES:
        return False
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return False
    lowered = text.lower()
    if "<!doctype" in lowered or "<!entity" in lowered or "<?xml-stylesheet" in lowered:
        return False
    try:
        root = element_tree.fromstring(text)
    except element_tree.ParseError:
        return False
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""
        if tag not in _SVG_TAGS:
            return False
        for raw_name, value in node.attrib.items():
            name = raw_name.rsplit("}", 1)[-1]
            if name not in _SVG_ATTRIBUTES or raw_name.startswith("{") or name.lower().startswith("on"):
                return False
            normalized = value.lower().replace(" ", "")
            if any(part in normalized for part in ("url(", "javascript:", "data:", "http:", "https:", "file:", "//")):
                return False
    return root.tag.rsplit("}", 1)[-1] == "svg"
