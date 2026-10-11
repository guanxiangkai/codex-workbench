"""用 macOS 内置语音生成公开测试语句；不录音、不调用外部服务。"""
from pathlib import Path
import subprocess
import tempfile
import wave


def main():
    """生成包含有效合成语音的 16 kHz 单声道 PCM WAV，供 ASR 验证。"""
    target = Path(__file__).resolve().parents[1] / "src/codex_workbench/fixtures/probe.wav"
    with tempfile.TemporaryDirectory() as directory:
        intermediate = Path(directory) / "speech.aiff"
        converted = Path(directory) / "speech.wav"
        subprocess.run(["/usr/bin/say", "-v", "Tingting", "-o", str(intermediate),
                        "这是一个语音识别测试。今天阳光很好。"], check=True)
        subprocess.run(["/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                        str(intermediate), str(converted)], check=True)
        with wave.open(str(converted), 'rb') as source, wave.open(str(target), 'wb') as output:
            output.setparams(source.getparams())
            output.writeframes(source.readframes(source.getnframes()))


if __name__ == "__main__":
    main()
