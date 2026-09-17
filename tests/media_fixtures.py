"""供容器与摘要回归使用的合成媒体，不访问网络或依赖第三方编码库。

图像由 Pillow 生成后固化并重新打开验证；音频只保证下述合成结构，
不代表完成实际解码、听感验证或供应商能力验证。缺少可靠编码器时不提供 M4A。
"""

from __future__ import annotations

import base64
import io
import wave

# 2 × 2 红色 RGB PNG，包含完整图像数据及 CRC。
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP8zwACTGCSAQANHQEDgslx/wAAAABJ"
    "RU5ErkJggg==",
    validate=True,
)

# 2 × 2 红色 JPEG，包含完整量化表、扫描数据和结束标记。
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwgJC4nICIs"
    "IxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
    "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAACAAIDASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAA"
    "AAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAk"
    "M2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKT"
    "lJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QA"
    "HwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdh"
    "cRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hp"
    "anN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk"
    "5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDi6KKK+ZP3E//Z",
    validate=True,
)

# 2 × 2 红色 GIF，包含颜色表、LZW 图像数据和结束标记。
GIF = base64.b64decode(
    "R0lGODdhAgACAIEAAP8AAAAAAAAAAAAAACwAAAAAAgACAAAIBgABCAQQEAA7",
    validate=True,
)

# 2 × 2 红色 WebP，包含完整 RIFF/WEBP 容器和 VP8 图像块。
WEBP = base64.b64decode(
    "UklGRjwAAABXRUJQVlA4IDAAAADQAQCdASoCAAIAAUAmJaACdLoB+AADsAD+8ut//NgVzXPv9//S4P0uD9Lg/9KQ"
    "AAA=",
    validate=True,
)

# 单声道 16 kHz、16-bit、32 个零采样；STREAMINFO、constant subframe、CRC-8/CRC-16 和 PCM MD5 完整。
# 格式依据：https://www.rfc-editor.org/rfc/rfc9639.html
FLAC = base64.b64decode(
    "ZkxhQ4AAACIAIAAgAAAMAAAMA+gA8AAAACA7XTx9IH433O7t0wHjXi5Y//hgCAAf5gAAAGkR",
    validate=True,
)

# 单声道 Ogg Opus：OpusHead、OpusTags、20 ms 静音包，三页含正确页 CRC、序号和结束标记。
# 容器依据：https://www.rfc-editor.org/rfc/rfc7845.html
OGG = base64.b64decode(
    "T2dnUwACAAAAAAAAAAABAAAAAAAAAIRs2SQBE09wdXNIZWFkAQEAAIC7AAAAAABPZ2dTAAAAAAAAAAAAAAEAAAAB"
    "AAAAh1hgGgEQT3B1c1RhZ3MAAAAAAAAAAE9nZ1MABMADAAAAAAAAAQAAAAIAAAApk7DdAQP4//4=",
    validate=True,
)

# MPEG-1 Layer III，128 kbps、44.1 kHz、单声道、无 CRC、无填充；
# 每帧 417 字节，其中 17 字节 side information 和剩余 payload 全零，连续两帧。
MP3 = (bytes.fromhex("FF FB 90 C0") + bytes(413)) * 2


def wav_bytes(samples: bytes = b"\x00\x00" * 32) -> bytes:
    """把非空的 16-bit 小端 PCM 编码为单声道、16 kHz WAV 容器。

    samples 的每两个字节对应一个采样，可传不同 PCM 生成有效的摘要替换样本。
    返回完整 WAV 字节；空输入或不完整的双字节采样会抛出 ValueError。
    """
    if not samples or len(samples) % 2:
        raise ValueError("samples 必须包含非空且完整的 16-bit PCM 采样")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(samples)
    return output.getvalue()
