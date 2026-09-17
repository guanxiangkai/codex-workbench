"""最小媒体结构及两个入口的一致性回归，不访问模型或运行解码器。"""

import struct
import unittest
import zlib
from pathlib import Path

from codex_workbench.capability_runtime import _media_descriptor
from codex_workbench.media_validation import valid_media
from codex_workbench.model_probe import _audio_bytes
from media_fixtures import PNG, JPEG, GIF, WEBP, MP3, FLAC, OGG, wav_bytes


class MediaValidationTests(unittest.TestCase):
    def test_real_format_synthetic_media_are_accepted_and_truncation_is_rejected(self):
        for suffix, media, raw in (
            (".png", "image", PNG), (".jpg", "image", JPEG), (".jpeg", "image", JPEG),
            (".gif", "image", GIF), (".webp", "image", WEBP), (".mp3", "audio", MP3),
            (".wav", "audio", wav_bytes()), (".flac", "audio", FLAC), (".ogg", "audio", OGG),
        ):
            with self.subTest(suffix=suffix):
                self.assertTrue(valid_media(raw, suffix, media))
                self.assertIsNotNone(_media_descriptor(Path("sample" + suffix.upper()), raw, media))
                for truncated in (b"", raw[:3], raw[:12], raw[:-1]):
                    self.assertFalse(valid_media(truncated, suffix, media))
                    self.assertIsNone(_media_descriptor(Path("sample" + suffix), truncated, media))

    def test_magic_only_and_extension_or_kind_mismatches_are_rejected(self):
        for suffix, raw, kind in (
            (".png", b"\x89PNG\r\n\x1a\n", "image"), (".jpg", b"\xff\xd8\xff\xd9", "image"),
            (".gif", b"GIF89a", "image"), (".webp", b"RIFF\4\0\0\0WEBP", "image"),
            (".mp3", b"ID3", "audio"), (".wav", b"RIFF\4\0\0\0WAVE", "audio"),
            (".flac", b"fLaC", "audio"), (".ogg", b"OggS", "audio"),
            (".m4a", b"\0\0\0\x10ftypM4A \0\0\0\0", "audio"),
            (".png", JPEG, "image"), (".mp3", wav_bytes(), "audio"),
            (".png", PNG, "audio"), (".wav", wav_bytes(), "image"), (".png", PNG, "unknown"),
        ):
            with self.subTest(suffix=suffix, kind=kind):
                self.assertIsNone(_media_descriptor(Path("sample" + suffix), raw, kind))

    def test_mp3_probe_and_runtime_share_exact_tts_contract(self):
        for raw in (MP3, MP3[:417], MP3[:-1], MP3[:4], b"ID3", b"ID3audio", wav_bytes(), OGG, PNG):
            with self.subTest(size=len(raw), prefix=raw[:4]):
                self.assertEqual(_audio_bytes(raw), _media_descriptor(Path("speech.mp3"), raw, "audio") is not None)
        self.assertTrue(_audio_bytes(MP3[:417]))  # 单个完整帧也可以是很短的音频。

    def test_mp3_id3_tags_need_complete_boundaries_and_audio_after_metadata(self):
        for version in (2, 3, 4):
            tag = b"ID3" + bytes((version, 0, 0, 0, 0, 0, 4)) + bytes(4)
            self.assertTrue(_audio_bytes(tag + MP3))
            self.assertFalse(_audio_bytes(tag))
        header = b"ID3\4\0\x10\0\0\0\0"
        tagged = header + b"3DI" + header[3:]
        self.assertTrue(_audio_bytes(tagged + MP3))
        self.assertFalse(_audio_bytes(header + MP3))
        for tag in (b"ID3\4\0\0\0\0\x7f\x7f", b"ID3\4\0\0\x80\0\0\0", b"ID3\4\0\1\0\0\0\0"):
            self.assertFalse(_audio_bytes(tag + MP3))
        self.assertTrue(_audio_bytes(MP3 + b"TAG" + bytes(125)))

    def test_mp3_version_bitrate_padding_crc_and_free_format_headers(self):
        # 帧长度取固定格式事实，覆盖此前只允许 F2/F3/FB 所漏掉的合法保护位和 MPEG-2.5。
        frames = (
            bytes.fromhex("FF FA 90 C0") + bytes(413),  # MPEG-1 + CRC，417 字节。
            bytes.fromhex("FF FB A0 C0") + bytes(518),  # MPEG-1 160 kbps，522 字节。
            bytes.fromhex("FF FB 92 C0") + bytes(414),  # MPEG-1 padding，418 字节。
            bytes.fromhex("FF F3 80 C0") + bytes(204),  # MPEG-2 64 kbps，208 字节。
            bytes.fromhex("FF E3 80 C0") + bytes(413),  # MPEG-2.5 64 kbps，417 字节。
        )
        for frame in frames:
            with self.subTest(header=frame[:4].hex()):
                self.assertTrue(_audio_bytes(frame))
                self.assertFalse(_audio_bytes(frame[:-1]))
        self.assertTrue(_audio_bytes(MP3[:417] + frames[1]))  # VBR 可以改变码率。
        free = bytes.fromhex("FF FB 00 C0") + bytes(413)
        self.assertTrue(_audio_bytes(free * 2))
        self.assertFalse(_audio_bytes((free * 2)[:-1]))
        embedded_sync = bytearray(free * 2)
        embedded_sync[100:104] = free[:4]
        self.assertTrue(_audio_bytes(bytes(embedded_sync)))
        for header in ("FF EB 90 C0", "FF FB F0 C0", "FF FB 9C C0", "FF FD 90 C0"):
            self.assertFalse(_audio_bytes(bytes.fromhex(header) + bytes(413)))

    def test_mp3_index_frame_is_not_audio(self):
        for marker in (b"Xing", b"Info"):
            frame = bytearray(MP3[:417])
            frame[21:25] = marker
            self.assertFalse(_audio_bytes(bytes(frame)))
            self.assertTrue(_audio_bytes(bytes(frame) + MP3))

    def test_wav_empty_data_bad_lengths_and_nonempty_silence(self):
        raw = wav_bytes()
        self.assertTrue(valid_media(raw, ".wav", "audio"))
        empty = raw[:40] + bytes(4)
        empty = empty[:4] + struct.pack("<I", len(empty) - 8) + empty[8:]
        self.assertFalse(valid_media(empty, ".wav", "audio"))
        self.assertFalse(valid_media(raw[:4] + bytes(4) + raw[8:], ".wav", "audio"))
        self.assertFalse(valid_media(raw[:40] + struct.pack("<I", 1000) + raw[44:], ".wav", "audio"))
        # 奇数长度的无关元数据带 RIFF padding，合法文件不能被固定头长假设拒绝。
        extra = b"JUNK\1\0\0\0x\0"
        extended = raw[:4] + struct.pack("<I", len(raw) + len(extra) - 8) + raw[8:12] + extra + raw[12:]
        self.assertTrue(valid_media(extended, ".wav", "audio"))

    def test_png_requires_nonempty_data_dimensions_and_intact_crc(self):
        def chunk(kind, payload):
            return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))

        signature, header, end = PNG[:8], PNG[8:33], chunk(b"IEND", b"")
        self.assertFalse(valid_media(signature + header + end, ".png", "image"))
        self.assertFalse(valid_media(signature + header + chunk(b"IDAT", b"") + end, ".png", "image"))
        zero_width = chunk(b"IHDR", struct.pack(">IIBBBBB", 0, 2, 8, 2, 0, 0, 0))
        self.assertFalse(valid_media(signature + zero_width + PNG[33:], ".png", "image"))
        corrupt = bytearray(PNG)
        corrupt[45] ^= 1
        self.assertFalse(valid_media(bytes(corrupt), ".png", "image"))

    def test_flac_metadata_only_and_ogg_headers_only_are_not_audio(self):
        self.assertFalse(valid_media(FLAC[:42], ".flac", "audio"))
        # 总样本数为 0 在 FLAC 表示未知；有完整音频帧时不能误当成空音频。
        unknown = bytearray(FLAC)
        unknown[21] &= 0xF0
        unknown[22:26] = bytes(4)
        self.assertTrue(valid_media(bytes(unknown), ".flac", "audio"))
        corrupt = bytearray(FLAC)
        corrupt[-1] ^= 1
        self.assertFalse(valid_media(bytes(corrupt), ".flac", "audio"))
        bad_blocks = bytearray(FLAC)
        bad_blocks[8:12] = bytes(4)
        self.assertFalse(valid_media(bytes(bad_blocks), ".flac", "audio"))
        self.assertFalse(valid_media(OGG[:47], ".ogg", "audio"))
        self.assertFalse(valid_media(OGG[:-31], ".ogg", "audio"))

    def test_webp_rejects_truncated_partition_even_with_matching_riff_length(self):
        payload = WEBP[20:31]
        chunk = b"VP8 " + struct.pack("<I", len(payload)) + payload + b"\0"
        truncated = b"RIFF" + struct.pack("<I", 4 + len(chunk)) + b"WEBP" + chunk
        self.assertFalse(valid_media(truncated, ".webp", "image"))

    def test_ogg_mandatory_headers_are_checked_even_with_valid_page_crc(self):
        def update_crc(raw, start, end):
            raw[start + 22:start + 26] = bytes(4)
            crc = 0
            for byte in raw[start:end]:
                crc ^= byte << 24
                for _ in range(8):
                    crc = ((crc << 1) ^ (0x04C11DB7 if crc & 0x80000000 else 0)) & 0xFFFFFFFF
            raw[start + 22:start + 26] = struct.pack("<I", crc)

        bad_channels = bytearray(OGG)
        bad_channels[37] = 0
        update_crc(bad_channels, 0, 47)
        self.assertFalse(valid_media(bytes(bad_channels), ".ogg", "audio"))
        bad_tags = bytearray(OGG)
        bad_tags[75:83] = bytes(8)
        update_crc(bad_tags, 47, 91)
        self.assertFalse(valid_media(bytes(bad_tags), ".ogg", "audio"))


if __name__ == "__main__":
    unittest.main()
