"""生成插件内置提示音到 extension/sounds/：uv run python scripts/make_sounds.py

三组各三段：
- 合约（CA）：voice / chime / alarm
- 普通消息（仿微信短促提示）：wx-ding / wx-pop / wx-tick
- 星标用户（更醒目）：star-voice / star-bell / star-harp
带 voice 的两段用 Windows 自带的中文语音（Microsoft Huihui）合成，其余是合成音。
"""

import math
import struct
import subprocess
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "extension" / "sounds"
RATE = 22050


def write_wav(path: Path, samples: list[float]) -> None:
    peak = max(1e-9, max(abs(item) for item in samples))
    frames = b"".join(struct.pack("<h", int(item / peak * 0.85 * 32767)) for item in samples)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(frames)


def tone(freq: float, seconds: float, decay: float = 6.0, overtone: float = 0.3) -> list[float]:
    count = int(RATE * seconds)
    attack = int(RATE * 0.005)
    out = []
    for index in range(count):
        t = index / RATE
        envelope = math.exp(-decay * t) * min(1.0, index / attack if attack else 1.0)
        wave_value = math.sin(2 * math.pi * freq * t) + overtone * math.sin(4 * math.pi * freq * t)
        out.append(envelope * wave_value)
    return out


def marimba(freq: float, seconds: float, decay: float = 14.0) -> list[float]:
    """木琴质感：基音加 4 倍频泛音，泛音衰减更快。"""
    count = int(RATE * seconds)
    out = []
    for index in range(count):
        t = index / RATE
        attack = min(1.0, index / (RATE * 0.002))
        body = math.exp(-decay * t) * math.sin(2 * math.pi * freq * t)
        ring = 0.35 * math.exp(-decay * 3 * t) * math.sin(2 * math.pi * freq * 4 * t)
        out.append(attack * (body + ring))
    return out


def bell(freq: float, seconds: float, decay: float = 3.5) -> list[float]:
    """钟声：不成整数倍的泛音让声音更亮。"""
    partials = ((1.0, 1.0), (2.76, 0.45), (5.4, 0.25), (8.93, 0.12))
    count = int(RATE * seconds)
    out = []
    for index in range(count):
        t = index / RATE
        attack = min(1.0, index / (RATE * 0.003))
        value = sum(
            gain * math.exp(-decay * ratio**0.5 * t) * math.sin(2 * math.pi * freq * ratio * t)
            for ratio, gain in partials
        )
        out.append(attack * value)
    return out


def mix(*tracks: tuple[float, list[float]]) -> list[float]:
    """按起始秒数叠加多段。"""
    length = max(int(start * RATE) + len(samples) for start, samples in tracks)
    out = [0.0] * length
    for start, samples in tracks:
        offset = int(start * RATE)
        for index, value in enumerate(samples):
            out[offset + index] += value
    return out


def silence(seconds: float) -> list[float]:
    return [0.0] * int(RATE * seconds)


def chime() -> list[float]:
    return tone(1318.5, 0.35, 5.0) + tone(1046.5, 0.7, 4.0)


def alarm() -> list[float]:
    out: list[float] = []
    for freq in (880.0, 1108.7, 1318.5):
        out += tone(freq, 0.12, 9.0) + silence(0.05)
    return out + tone(1760.0, 0.3, 7.0)


def wx_ding() -> list[float]:
    return mix((0.0, marimba(1567.98, 0.35)), (0.09, marimba(2093.0, 0.45, 11.0)))


def wx_pop() -> list[float]:
    count = int(RATE * 0.16)
    out = []
    phase = 0.0
    for index in range(count):
        t = index / RATE
        freq = 520 + 1500 * (1 - math.exp(-t / 0.03))
        phase += 2 * math.pi * freq / RATE
        out.append(math.exp(-26 * t) * min(1.0, index / (RATE * 0.002)) * math.sin(phase))
    return out + silence(0.05)


def wx_tick() -> list[float]:
    return marimba(1318.5, 0.25, 22.0)


def star_bell() -> list[float]:
    notes = (1046.5, 1318.5, 1568.0, 2093.0)
    return mix(*((index * 0.11, bell(freq, 1.1)) for index, freq in enumerate(notes)))


def star_harp() -> list[float]:
    notes = (783.99, 987.77, 1174.66, 1567.98, 1975.53, 2349.32)
    return mix(*((index * 0.055, tone(freq, 0.9, 4.5, 0.15)) for index, freq in enumerate(notes)))


def speak(path: Path, text: str) -> None:
    script = f"""
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$zh = $s.GetInstalledVoices() |
    Where-Object {{ $_.VoiceInfo.Culture.Name -eq 'zh-CN' }} |
    Select-Object -First 1
if (-not $zh) {{ throw 'no zh-CN voice' }}
$s.SelectVoice($zh.VoiceInfo.Name)
$s.Rate = 1
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo({RATE}, 16, 1)
$s.SetOutputToWaveFile('{path}', $fmt)
$s.Speak('{text}')
$s.Dispose()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    speak(OUT / "voice.wav", "注意，群里有新合约")
    speak(OUT / "star-voice.wav", "星标用户发言了")
    synth = {
        "chime": chime,
        "alarm": alarm,
        "wx-ding": wx_ding,
        "wx-pop": wx_pop,
        "wx-tick": wx_tick,
        "star-bell": star_bell,
        "star-harp": star_harp,
    }
    for name, build in synth.items():
        write_wav(OUT / f"{name}.wav", build())
    for item in sorted(OUT.glob("*.wav")):
        print(item.name, item.stat().st_size)


if __name__ == "__main__":
    main()
