"""Чёткая озвучка клоном голоса (XTTS v2): без тарабарщины, лишних слов и "аяяя" на буквах.

Каждая фраза режется на короткие куски, каждый кусок озвучивается несколько раз, результат проверяется
распознаванием речи (лишние слова/хвосты обрезаются), выбирается лучший вариант. Запуск:
    python golos_klon.py lines.json ref.wav outdir [tempo]
lines.json - список фраз по порядку; outdir/001.mp3, 002.mp3 ... - готовые клипы.
"""
import difflib, json, os, re, subprocess, sys, time

os.environ.setdefault("COQUI_TOS_AGREED", "1")
CARRIER = "вот"


def norm(s):
    return re.sub(r"[^а-яё0-9 ]", "", s.lower().replace("ё", "е")).split()


def wer(ref, hyp):
    r, h = norm(ref), norm(hyp)
    d = [[0] * (len(h) + 1) for _ in range(len(r) + 1)]
    for i in range(len(r) + 1):
        d[i][0] = i
    for j in range(len(h) + 1):
        d[0][j] = j
    for i in range(1, len(r) + 1):
        for j in range(1, len(h) + 1):
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + (r[i - 1] != h[j - 1]))
    return d[-1][-1] / max(1, len(r))


def spoken(t):
    t = t.replace("«", "").replace("»", "").replace(" - ", ", ").replace(" — ", ", ")
    t = re.sub(r"^(Первый|Второй|Третий|Четвёртый|Пятый): ", lambda m: m.group(1) + " вариант. ", t)
    t = re.sub(r"(?<![А-Яа-яЁё])Б(?![а-яё])", "бэ", t)
    t = re.sub(r"(?<![А-Яа-яЁё])В(?![а-яё])", "вэ", t)
    t = re.sub(r"(?<![А-Яа-яЁё])А(?![а-яё])", "а", t)
    return t


def chunks(t):
    out = []
    for s in re.split(r"(?<=[.!?])\s+", t):
        s = s.strip()
        if not s:
            continue
        if len(s.split()) > 9 and "," in s:                    # long sentence: cut at a comma near the middle
            parts = [p.strip() for p in re.split(r"(?<=,)\s+", s)]
            acc, cur = [], ""
            for p in parts:
                if cur and len((cur + " " + p).split()) > 9:
                    acc.append(cur)
                    cur = p
                else:
                    cur = (cur + " " + p).strip()
            acc.append(cur)
            out += acc
        else:
            out.append(s)
    return out


def main(lines_json, ref, outdir, tempo=1.12, only=None):
    import torch, torchaudio, soundfile as sf
    from TTS.api import TTS
    from faster_whisper import WhisperModel

    def _load(p, *a, **k):                                   # torchaudio >= 2.9 needs torchcodec: read with soundfile instead
        d, sr = sf.read(p, dtype="float32", always_2d=True)
        return torch.from_numpy(d.T.copy()), sr
    torchaudio.load = _load
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to("cpu")
    asr = WhisperModel("small", device="cpu", compute_type="int8")
    lines = json.load(open(lines_json, encoding="utf-8"))
    os.makedirs(outdir, exist_ok=True)
    tmp = os.path.join(outdir, "_t")
    os.makedirs(tmp, exist_ok=True)
    t0 = time.time()
    for i, line in enumerate(lines, 1):
        dest = os.path.join(outdir, f"{i:03d}.mp3")
        if os.path.exists(dest) or (only and i not in only):
            continue
        files = []
        for k, ch in enumerate(chunks(spoken(line))):
            words = ch.split()
            carrier = len(words[-1].strip(".,!?")) <= 2 and words[-1].strip(".,!?") in ("а", "бэ", "вэ")   # a letter must not end a chunk
            text = ch.rstrip(".,!?") + ", " + CARRIER + "." if carrier else ch
            refw = norm(ch)
            best = None
            for n in range(8):
                fn = os.path.join(tmp, f"c_{i}_{k}_{n}.wav")
                tts.tts_to_file(text=text, speaker_wav=ref, language="ru", file_path=fn, split_sentences=False, speed=1.0,
                                temperature=0.45 + 0.05 * (n % 4), repetition_penalty=5.0, top_k=30, top_p=0.8)
                seg, _ = asr.transcribe(fn, language="ru", word_timestamps=True)
                hw = [(norm(x.word)[0] if norm(x.word) else "", x.start, x.end) for s in seg for x in s.words]
                cut, tend = None, None
                if carrier:
                    for j in range(len(hw) - 1, -1, -1):                 # find the carrier word, cut right before it
                        if hw[j][0] and difflib.SequenceMatcher(None, hw[j][0], CARRIER).ratio() >= 0.7:
                            cut, tend = j - 1, hw[j][1] - 0.03
                            break
                else:
                    for j in range(len(hw) - 1, -1, -1):                 # last word that matches the last expected word
                        if hw[j][0] and difflib.SequenceMatcher(None, hw[j][0], refw[-1]).ratio() >= 0.75:
                            cut, tend = j, hw[j][2] + 0.1
                            break
                if cut is None or cut < 0:
                    w = 1.0
                else:
                    w = wer(" ".join(refw), " ".join(h for h, _, _ in hw[:cut + 1]))
                if best is None or w < best[0]:
                    best = (w, fn, tend)
                if w <= 0.06 and n >= 1:
                    break
            w, fn, tend = best
            outw = fn + ".ok.wav"
            af = "afade=t=out:st=%.2f:d=0.05" % max(0, (tend or 99) - 0.05) if tend else "anull"
            cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", fn] + (["-t", f"{tend:.2f}"] if tend else []) + ["-af", af, outw]
            subprocess.run(cmd, check=True)
            files.append(outw)
            print(i, k, "wer", round(w, 2), "|", ch[:45], flush=True)
        ins = sum([["-i", f] for f in files], [])
        fl = "".join(f"[{n}:a]apad=pad_dur=0.06[a{n}];" for n in range(len(files))) + "".join(f"[a{n}]" for n in range(len(files))) \
            + f"concat=n={len(files)}:v=0:a=1,atempo={tempo},loudnorm=I=-16:TP=-1.5:LRA=9[o]"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *ins, "-filter_complex", fl, "-map", "[o]", "-ar", "44100",
                        "-c:a", "libmp3lame", "-q:a", "2", dest], check=True)
        print("line", i, "done", round(time.time() - t0), flush=True)
    print("ALLDONE", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], sys.argv[3], float(sys.argv[4]) if len(sys.argv) > 4 else 1.12)
