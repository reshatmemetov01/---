"""Озвучить своё видео голосом Яндекса (Антон) на ноуте.

python ozvuchit.py ТЕКСТ.txt [ВИДЕО.mp4] [--speed 1.3] [--voice anton]
Результат кладёт рядом с видео (или с текстом): *_ozvuchka.mp4 и *_ozvuchka.mp3
"""
import argparse, os, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from auto_quiz import FFMPEG, media_duration, synth_yandex_v3
from make_video import read_text_any


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("video", nargs="?")
    ap.add_argument("--speed", default="1.3")
    ap.add_argument("--voice", default="anton")
    a = ap.parse_args()
    key = os.environ.get("YANDEX_API_KEY", "").strip()
    if not key:
        raise SystemExit("Нет YANDEX_API_KEY")
    text = read_text_any(a.text).strip()
    os.environ["YANDEX_SPEED"] = a.speed
    base = Path(a.video or a.text)
    mp3 = base.with_name(base.stem + "_ozvuchka.mp3")
    print("Озвучиваю...")
    mp3.write_bytes(synth_yandex_v3(text, a.voice, key, "" if a.voice == "filipp" else "good"))
    print("Голос:", mp3)
    if not a.video:
        return
    out = base.with_name(base.stem + "_ozvuchka.mp4")
    vd, ad = media_duration(a.video), media_duration(str(mp3))
    # always re-encode to H.264: screen recordings are often .webm (VP8/VP9), which mp4/TikTok do not take as is
    pad = f"tpad=stop_mode=clone:stop_duration={ad - vd + 0.5:.2f}," if ad + 0.3 > vd else ""
    cmd = [FFMPEG, "-y", "-i", a.video, "-i", str(mp3), "-map", "0:v:0", "-map", "1:a:0",
           "-vf", pad + "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{max(vd, ad + 0.5):.2f}", "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True)
    if r.returncode != 0:
        raise SystemExit("ffmpeg: " + r.stderr.decode("utf-8", "replace")[-800:])
    print(f"Готово: {out}  (голос {ad:.1f} с, видео {vd:.1f} с)")


if __name__ == "__main__":
    main()
