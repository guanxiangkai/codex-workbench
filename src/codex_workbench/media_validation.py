"""媒体的有界、无解码结构检查；通过不代表可播放、像素完整或生成质量合格。

仅使用标准库，不打开路径或分配解压后的媒体。调用方仍负责大小、快照摘要、
路径及 MIME 边界。结构依据 ID3v2、RFC 3119/9639、W3C PNG/GIF、ITU T.81、
Microsoft RIFF、Google WebP、Xiph Ogg 和 Apple QuickTime 的格式定义。
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator


def valid_media(value: bytes, suffix: str, media: str) -> bool:
    """按扩展名检查必要容器字段和非空媒体负载；不做编解码或内容质量判定。

    PNG/JPEG/GIF/WebP 不解码像素；MP3 不校验熵编码、bit reservoir 或音频 CRC；
    FLAC 不解析每个子帧；Ogg 不解码音频包；M4A 不核验样本表及偏移。
    """
    validators = _IMAGES if media == "image" else _AUDIO if media == "audio" else {}
    validator = validators.get(suffix.lower())
    if validator is None or not isinstance(value, bytes) or not value:
        return False
    try:
        return validator(value)
    except (ValueError, IndexError, struct.error):
        return False


def _chunks(value: bytes | memoryview, start: int = 0) -> Iterator[tuple[bytes, memoryview]]:
    """读取 RIFF 子块，未知块可跳过，但长度和奇数字节填充必须完整。"""
    view = memoryview(value)
    while start < len(view):
        if start + 8 > len(view):
            raise ValueError("truncated chunk")
        size = int.from_bytes(view[start + 4:start + 8], "little")
        end = start + 8 + size
        padded = end + (size & 1)
        if padded > len(view):
            raise ValueError("truncated chunk payload")
        yield bytes(view[start:start + 4]), view[start + 8:end]
        start = padded


def _riff(value: bytes, kind: bytes) -> Iterator[tuple[bytes, memoryview]]:
    if (len(value) < 12 or value[:4] != b"RIFF" or value[8:12] != kind
            or int.from_bytes(value[4:8], "little") + 8 != len(value)):
        raise ValueError("invalid RIFF size")
    return _chunks(value, 12)


def _wav(value: bytes) -> bool:
    block_size = 0
    pcm = False
    data_size = 0
    for kind, payload in _riff(value, b"WAVE"):
        if kind == b"fmt ":
            if block_size or len(payload) < 16:
                return False
            encoding, channels, rate, byte_rate, block_size, bits = struct.unpack_from("<HHIIHH", payload)
            if not all((encoding, channels, rate, byte_rate, block_size)):
                return False
            if len(payload) > 16 and (len(payload) < 18 or 18 + int.from_bytes(payload[16:18], "little") > len(payload)):
                return False
            pcm = encoding in {1, 3}
            if encoding == 0xFFFE:
                if len(payload) < 40 or int.from_bytes(payload[16:18], "little") < 22:
                    return False
                pcm = bytes(payload[24:40]) in {
                    b"\x01\0\0\0\0\0\x10\0\x80\0\0\xaa\0\x38\x9b\x71",
                    b"\x03\0\0\0\0\0\x10\0\x80\0\0\xaa\0\x38\x9b\x71",
                }
            if pcm and (not bits or block_size != channels * ((bits + 7) // 8) or byte_rate != rate * block_size):
                return False
        elif kind == b"data":
            if not block_size or (pcm and len(payload) % block_size):
                return False
            data_size += len(payload)
    return bool(block_size and data_size >= block_size)


def _id3_end(value: bytes, start: int) -> int:
    header = value[start:start + 10]
    if len(header) != 10 or header[3] not in {2, 3, 4} or header[4] == 255 or any(byte & 128 for byte in header[6:10]):
        raise ValueError("invalid ID3 header")
    if header[5] & {2: 0x3F, 3: 0x1F, 4: 0x0F}[header[3]]:
        raise ValueError("reserved ID3 flags")
    size = 0
    for byte in header[6:10]:
        size = (size << 7) | byte
    end = start + 10 + size
    if header[3] == 4 and header[5] & 0x10:
        if value[end:end + 10] != b"3DI" + header[3:]:
            raise ValueError("truncated ID3 footer")
        end += 10
    if end > len(value):
        raise ValueError("truncated ID3 tag")
    return end


def _mp3_header(value: bytes, start: int) -> tuple[int, int, int, int, int, int] | None:
    if start + 4 > len(value):
        return None
    header = int.from_bytes(value[start:start + 4], "big")
    version, layer, bitrate, rate = (header >> 19) & 3, (header >> 17) & 3, (header >> 12) & 15, (header >> 10) & 3
    if header >> 21 != 0x7FF or version == 1 or layer != 1 or bitrate == 15 or rate == 3 or header & 3 == 2:
        return None
    sample_rate = (44100, 48000, 32000)[rate] // {3: 1, 2: 2, 0: 4}[version]
    kbps = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320) if version == 3
            else (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160))[bitrate]
    padding = (header >> 9) & 1
    length = (144000 if version == 3 else 72000) * kbps // sample_rate + padding if bitrate else 0
    mono = (header >> 6) & 3 == 3
    side_size = (17 if mono else 32) if version == 3 else (9 if mono else 17)
    overhead = 4 + (0 if header & 0x10000 else 2) + side_size
    return length, overhead, padding, version, sample_rate, bitrate


def _mp3(value: bytes) -> bool:
    """ID3 是元数据；必须另有完整 Layer III 帧，不能用标签或同步字节证明音频。"""
    start, end, frames = 0, len(value), 0
    while value[start:start + 3] == b"ID3":
        start = _id3_end(value, start)
    if end - start >= 128 and value[end - 128:end - 125] == b"TAG":
        end -= 128
    # APEv2 是常见尾部标签；大小包含 footer，不包含可选 header。
    if end - start >= 32 and value[end - 32:end - 24] == b"APETAGEX":
        size = int.from_bytes(value[end - 20:end - 16], "little")
        flags = int.from_bytes(value[end - 12:end - 8], "little")
        if size < 32 or size > end - start:
            return False
        end -= size
        if flags & 0x80000000:
            if end - start < 32 or value[end - 32:end - 24] != b"APETAGEX":
                return False
            end -= 32
    free_size = 0
    while start < end:
        if value[start:start + 3] == b"ID3":
            start = _id3_end(value, start)
            continue
        header = _mp3_header(value, start)
        if header is None:
            return False
        length, overhead, padding, version, rate, bitrate = header
        if not bitrate:
            if not free_size:
                following = value.find(b"\xff", start + overhead + 1, end)
                while following != -1:
                    candidate = _mp3_header(value, following)
                    if candidate and candidate[3:] == (version, rate, 0):
                        candidate_size = following - start - padding
                        cursor = start
                        # 辅助数据也能含同步位；候选必须沿完整帧链抵达末尾。
                        while cursor < end:
                            frame = _mp3_header(value, cursor)
                            if not frame or frame[3:] != (version, rate, 0) or candidate_size + frame[2] <= frame[1]:
                                break
                            cursor += candidate_size + frame[2]
                        if cursor == end:
                            free_size = candidate_size
                            break
                    following = value.find(b"\xff", following + 1, end)
            length = free_size + padding if free_size else 0
        if length <= overhead or start + length > end:
            return False
        # Xing/Info/VBRI 索引帧自身不作为非空音频证据。
        if value[start + overhead:start + overhead + 4] not in {b"Xing", b"Info"} and value[start + 36:start + 40] != b"VBRI":
            frames += 1
        start += length
    return start == end and frames > 0


def _png(value: bytes) -> bool:
    if value[:8] != b"\x89PNG\r\n\x1a\n":
        return False
    start, image_bytes, color, palette = 8, 0, -1, False
    while start + 12 <= len(value):
        size = int.from_bytes(value[start:start + 4], "big")
        kind = value[start + 4:start + 8]
        end = start + 12 + size
        if end > len(value) or zlib.crc32(memoryview(value)[start + 4:end - 4]) != int.from_bytes(value[end - 4:end], "big"):
            return False
        payload = memoryview(value)[start + 8:end - 4]
        if start == 8 and kind != b"IHDR":
            return False
        if kind == b"IHDR":
            if start != 8 or size != 13:
                return False
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if not width or not height or depth not in depths.get(color, ()) or compression or filtering or interlace > 1:
                return False
        elif kind == b"PLTE":
            if not size or size % 3 or size > 768:
                return False
            palette = True
        elif kind == b"IDAT":
            image_bytes += size
        elif kind == b"IEND":
            return size == 0 and image_bytes > 0 and (color != 3 or palette) and end == len(value)
        start = end
    return False


def _jpeg(value: bytes) -> bool:
    if value[:2] != b"\xff\xd8":
        return False
    start, frame, scan_bytes, height = 2, False, 0, 0
    in_scan = False
    while start < len(value):
        if in_scan:
            marker = value.find(b"\xff", start)
            if marker == -1:
                return False
            scan_bytes += marker - start
            start = marker
        if value[start] != 255:
            return False
        while start < len(value) and value[start] == 255:
            start += 1
        if start == len(value):
            return False
        kind = value[start]
        start += 1
        if in_scan and (kind == 0 or 0xD0 <= kind <= 0xD7):
            scan_bytes += kind == 0
            continue
        if kind == 0xD9:
            return frame and height > 0 and scan_bytes > 0
        if kind == 1:  # TEM 是无长度标记。
            continue
        if kind in {0, 0xD8} or 0xD0 <= kind <= 0xD7 or start + 2 > len(value):
            return False
        size = int.from_bytes(value[start:start + 2], "big")
        end = start + size
        if size < 2 or end > len(value):
            return False
        if kind in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            if size < 8 or not value[start + 7] or size != 8 + 3 * value[start + 7] or not int.from_bytes(value[start + 5:start + 7], "big"):
                return False
            height = int.from_bytes(value[start + 3:start + 5], "big")
            frame = True
        elif kind == 0xDA:
            if not frame or size < 6 or not value[start + 2] or size != 6 + 2 * value[start + 2]:
                return False
        elif kind == 0xDC:  # DNL 可在首个扫描后补充原本为零的高度。
            if size != 4:
                return False
            height = int.from_bytes(value[start + 2:end], "big")
        in_scan = kind == 0xDA or (kind == 0xDC and in_scan)
        start = end
    return False


def _gif(value: bytes) -> bool:
    if len(value) < 13 or value[:6] not in {b"GIF87a", b"GIF89a"} or not all(struct.unpack_from("<HH", value, 6)):
        return False
    start = 13 + (3 * (2 << (value[10] & 7)) if value[10] & 128 else 0)
    images = 0
    while start < len(value):
        kind = value[start]
        start += 1
        if kind == 0x3B:
            return images > 0
        is_image = kind == 0x2C
        if is_image:
            if start + 9 > len(value) or not all(struct.unpack_from("<HH", value, start + 4)):
                return False
            flags = value[start + 8]
            start += 9 + (3 * (2 << (flags & 7)) if flags & 128 else 0)
            if start >= len(value) or not 2 <= value[start] <= 8:
                return False
            start += 1
        elif kind == 0x21:
            start += 1  # 扩展标签，后面同样是带终止符的数据子块。
        else:
            return False
        payload_size = 0
        while start < len(value) and value[start]:
            size = value[start]
            payload_size += size
            start += 1 + size
        if start >= len(value):
            return False
        start += 1
        if is_image:
            if not payload_size:
                return False
            images += 1
    return False


def _webp_frame(kind: bytes, payload: memoryview) -> bool:
    if kind == b"VP8 ":
        return (len(payload) > 10 and not payload[0] & 1 and bytes(payload[3:6]) == b"\x9d\x01\x2a"
                and 0 < int.from_bytes(payload[:3], "little") >> 5 <= len(payload) - 10
                and int.from_bytes(payload[6:8], "little") & 0x3FFF > 0 and int.from_bytes(payload[8:10], "little") & 0x3FFF > 0)
    return kind == b"VP8L" and len(payload) > 5 and payload[0] == 0x2F and not payload[4] & 0xE0


def _webp(value: bytes) -> bool:
    frames = 0
    for kind, payload in _riff(value, b"WEBP"):
        if kind in {b"VP8 ", b"VP8L"}:
            if not _webp_frame(kind, payload):
                return False
            frames += 1
        elif kind == b"VP8X" and len(payload) != 10:
            return False
        elif kind == b"ANMF":
            if len(payload) <= 16:
                return False
            nested = list(_chunks(payload, 16))
            if not any(_webp_frame(code, frame) for code, frame in nested):
                return False
            frames += 1
    return frames > 0


def _boxes(value: bytes | memoryview) -> Iterator[tuple[bytes, memoryview]]:
    view, start = memoryview(value), 0
    while start < len(view):
        if start + 8 > len(view):
            raise ValueError("truncated box")
        size, header = int.from_bytes(view[start:start + 4], "big"), 8
        if size == 1:
            if start + 16 > len(view):
                raise ValueError("truncated extended box")
            size, header = int.from_bytes(view[start + 8:start + 16], "big"), 16
        elif size == 0:
            size = len(view) - start
        if size < header or start + size > len(view):
            raise ValueError("invalid box size")
        yield bytes(view[start + 4:start + 8]), view[start + header:start + size]
        start += size


def _sound_track(movie: memoryview) -> bool:
    sound = False
    for kind, track in _boxes(movie):
        if kind == b"trak":
            for code, media in _boxes(track):
                if code == b"mdia":
                    for name, payload in _boxes(media):
                        if name == b"hdlr" and len(payload) >= 24 and bytes(payload[8:12]) == b"soun":
                            sound = True
    return sound


def _m4a(value: bytes) -> bool:
    """只验证完整顶层 box、声音轨道标记和非空 mdat，不证明其样本可寻址或可解码。"""
    file_type, sound, samples = False, False, False
    for kind, payload in _boxes(value):
        if kind == b"ftyp":
            file_type = len(payload) >= 8 and len(payload) % 4 == 0
        elif kind == b"moov":
            sound = _sound_track(payload) or sound
        elif kind == b"mdat":
            samples = bool(payload) or samples
    return file_type and sound and samples


def _crc_table(bits: int, polynomial: int) -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << (bits - 8)
        for _ in range(8):
            crc = (crc << 1) ^ (polynomial if crc & (1 << (bits - 1)) else 0)
        table.append(crc & ((1 << bits) - 1))
    return tuple(table)


_FLAC_CRC = _crc_table(16, 0x8005)
_OGG_CRC = _crc_table(32, 0x04C11DB7)


def _flac(value: bytes) -> bool:
    if value[:4] != b"fLaC":
        return False
    start, last = 4, False
    while not last:
        if start + 4 > len(value):
            return False
        kind, last = value[start] & 127, bool(value[start] & 128)
        size = int.from_bytes(value[start + 1:start + 4], "big")
        end = start + 4 + size
        if end > len(value) or kind == 127:
            return False
        if start == 4:
            if kind != 0 or size != 34:
                return False
            minimum, maximum = struct.unpack_from(">HH", value, start + 4)
            if not 16 <= minimum <= maximum:
                return False
            packed = int.from_bytes(value[start + 14:start + 22], "big")
            if not packed >> 44 or ((packed >> 36) & 31) < 3:
                return False
        elif kind == 0:
            return False
        start = end
    # 完整 FLAC 帧串接后的 CRC 为零，但逆命题不成立（例如尾部补零）。
    # 这里只排除明显截断/损坏，不证明每个子帧可解码或逐帧边界完整。
    # STREAMINFO 的未知总样本数 0 不能作为空音频证据。
    if len(value) - start < 10 or value[start] != 255 or value[start + 1] & 0xFE != 0xF8:
        return False
    crc = 0
    for byte in memoryview(value)[start:]:
        crc = ((crc << 8) & 0xFFFF) ^ _FLAC_CRC[(crc >> 8) ^ byte]
    return crc == 0


def _ogg_identification(packet: bytearray) -> tuple[str, int]:
    """识别现有音频映射的必要头；未知复用流不作为音频证据。"""
    if packet.startswith(b"OpusHead"):
        if len(packet) < 19 or not 1 <= packet[8] <= 15 or not packet[9]:
            raise ValueError("invalid Opus identification")
        if packet[18] == 0:
            if packet[9] > 2:
                raise ValueError("invalid Opus channels")
        elif len(packet) < 21 + packet[9] or not packet[19] or packet[20] > packet[19]:
            raise ValueError("truncated Opus channel mapping")
        return "opus", 2
    if packet.startswith(b"\x01vorbis"):
        if (len(packet) < 30 or any(packet[7:11]) or not packet[11] or not int.from_bytes(packet[12:16], "little")
                or not 6 <= packet[28] & 15 <= packet[28] >> 4 <= 13 or not packet[29] & 1):
            raise ValueError("invalid Vorbis identification")
        return "vorbis", 3
    if packet.startswith(b"Speex   "):
        if len(packet) < 80 or not int.from_bytes(packet[36:40], "little") or not int.from_bytes(packet[48:52], "little"):
            raise ValueError("invalid Speex identification")
        return "speex", 2 + int.from_bytes(packet[68:72], "little")
    if packet.startswith(b"\x7fFLAC"):
        if len(packet) < 51 or packet[9:13] != b"fLaC" or packet[13] & 127 or packet[14:17] != b"\0\0\x22":
            raise ValueError("invalid Ogg FLAC identification")
        return "flac", 1 + int.from_bytes(packet[7:9], "big")
    return "", 0


def _ogg_comment(packet: bytearray, start: int) -> bool:
    """仅验证 Vorbis 风格注释的长度，不读取或返回注释内容。"""
    if start + 4 > len(packet):
        return False
    start += 4 + int.from_bytes(packet[start:start + 4], "little")
    if start + 4 > len(packet):
        return False
    count = int.from_bytes(packet[start:start + 4], "little")
    start += 4
    if count > (len(packet) - start) // 4:
        return False
    for _ in range(count):
        if start + 4 > len(packet):
            return False
        start += 4 + int.from_bytes(packet[start:start + 4], "little")
    return start <= len(packet)


def _ogg(value: bytes) -> bool:
    start, streams = 0, {}
    while start < len(value):
        if start + 27 > len(value) or value[start:start + 5] != b"OggS\0" or value[start + 5] & 0xF8:
            return False
        flags, count = value[start + 5], value[start + 26]
        body = start + 27 + count
        if body > len(value):
            return False
        lacing = value[start + 27:body]
        end = body + sum(lacing)
        if end > len(value):
            return False
        crc = 0
        for index in range(start, end):
            byte = 0 if start + 22 <= index < start + 26 else value[index]
            crc = ((crc << 8) & 0xFFFFFFFF) ^ _OGG_CRC[(crc >> 24) ^ byte]
        if crc != int.from_bytes(value[start + 22:start + 26], "little"):
            return False
        serial = value[start + 14:start + 18]
        sequence = int.from_bytes(value[start + 18:start + 22], "little")
        stream = streams.get(serial)
        if stream is None:
            if not flags & 2 or flags & 1:
                return False
            stream = {"sequence": sequence, "pending": bytearray(), "packets": 0, "headers": 0, "codec": "", "audio": False, "end": False}
            streams[serial] = stream
        elif stream["end"] or sequence != (stream["sequence"] + 1) & 0xFFFFFFFF or bool(flags & 1) != bool(stream["pending"]):
            return False
        stream["sequence"] = sequence
        for size in lacing:
            stream["pending"].extend(value[body:body + size])
            body += size
            if size < 255:
                packet = stream["pending"]
                if stream["packets"] == 0:
                    stream["codec"], stream["headers"] = _ogg_identification(packet)
                elif stream["packets"] == 1:
                    codec = stream["codec"]
                    if codec == "opus" and (not packet.startswith(b"OpusTags") or not _ogg_comment(packet, 8)):
                        return False
                    if codec == "vorbis" and (not packet.startswith(b"\x03vorbis") or not _ogg_comment(packet, 7)):
                        return False
                    if codec == "speex" and not _ogg_comment(packet, 0):
                        return False
                elif stream["codec"] == "vorbis" and stream["packets"] == 2 and (len(packet) <= 7 or not packet.startswith(b"\x05vorbis")):
                    return False
                if stream["headers"] and stream["packets"] >= stream["headers"] and packet:
                    stream["audio"] = True
                stream["packets"] += 1
                packet.clear()
        stream["end"] = bool(flags & 4)
        if stream["end"] and stream["pending"]:
            return False
        start = end
    return bool(streams) and all(stream["end"] for stream in streams.values()) and any(stream["audio"] for stream in streams.values())


_IMAGES = {".png": _png, ".jpg": _jpeg, ".jpeg": _jpeg, ".gif": _gif, ".webp": _webp}
_AUDIO = {".mp3": _mp3, ".wav": _wav, ".m4a": _m4a, ".flac": _flac, ".ogg": _ogg}
