"""Режет одну длинную озвучку (сделанную на другом сайте) на N фраз по самым длинным паузам."""
import re, subprocess
from pathlib import Path
from auto_quiz import FFMPEG, media_duration


def split_by_pauses(audio, n, out_dir):
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.mp3"):
        f.unlink()
    total = media_duration(str(audio))
    r = subprocess.run([FFMPEG, "-i", str(audio), "-af", "silencedetect=noise=-38dB:d=0.25", "-f", "null", "-"],
                       capture_output=True, text=True, timeout=300)
    starts = [float(x) for x in re.findall(r"silence_start: ([\d.]+)", r.stderr)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", r.stderr)]
    sil = [(s, e) for s, e in zip(starts, ends) if s > 0.3 and e < total - 0.3]
    if len(sil) < n - 1:
        raise ValueError(f"нашёл только {len(sil) + 1} кусков, а фраз {n}. Сделай паузы между абзацами длиннее (пустая строка или «...»).")
    cuts = sorted(sorted(sil, key=lambda x: x[1] - x[0], reverse=True)[:n - 1])
    bounds = [0.0] + [(s + e) / 2 for s, e in cuts] + [total]
    for i in range(n):
        subprocess.run([FFMPEG, "-y", "-i", str(audio), "-ss", f"{bounds[i]:.3f}", "-to", f"{bounds[i + 1]:.3f}",
                        "-ar", "48000", "-ac", "2", "-b:a", "192k", str(out / f"{i + 1:03d}.mp3")], capture_output=True, check=True)
    return [round(bounds[i + 1] - bounds[i], 2) for i in range(n)]
