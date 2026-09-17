"""生成不含人声或个人数据的单声道测试音，不调用外部服务。"""
import math
import struct
import wave
from pathlib import Path


def main():
    """生成一秒 440 Hz、16 kHz、16-bit PCM WAV，只用于传输测试。"""
    target = Path(__file__).resolve().parents[1] / "src/codex_workbench/fixtures/probe.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    samples = b"".join(struct.pack("<h", round(1000 * math.sin(2 * math.pi * 440 * i / 16000))) for i in range(16000))
    with wave.open(str(target), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(samples)


if __name__ == "__main__":
    main()
