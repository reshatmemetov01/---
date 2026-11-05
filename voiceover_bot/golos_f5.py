"""Озвучка голосом автора через F5-TTS (русская модель) с ручной расстановкой ударений (RUAccent).

Запуск:  python golos_f5.py lines.json ref.wav "текст референса" outdir [speed] [nfe]
- ударения ставит RUAccent ("автомоб+иля"), буквы А/Б/В читаются как "а/бэ/вэ" и не тянутся ("Аааэ");
- каждая фраза режется на короткие куски, результат проверяется распознаванием речи, лишнее обрезается.
lines.json - фразы по порядку; outdir/001.mp3 ... - готовые клипы. Готовые клипы пропускаются (можно продолжать после обрыва).
Нужны: pip install f5-tts ruaccent faster-whisper soundfile (+ torch). Веса: Misha24-10/F5-TTS_RUSSIAN (лицензия cc-by-nc-4.0).
"""
import difflib, json, os, re, subprocess, sys, time

CARRIER = "вот"
HERE = os.path.dirname(os.path.abspath(__file__))
CKPT = os.environ.get("F5_CKPT", os.path.join(HERE, "f5w", "F5TTS_v1_Base_v2", "model_last_inference.safetensors"))
VOCAB = os.environ.get("F5_VOCAB", os.path.join(HERE, "f5w", "F5TTS_v1_Base", "vocab.txt"))


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


def est_duration(text, speed):
    """Speech length in seconds for a chunk: by syllables, with extra time for single letters and for punctuation."""
    syl = len(re.findall(r"[аеёиоуыэюя]", text.lower()))
    letters = len(re.findall(r"(?<![а-яё])(?:а|бэ|вэ)(?![а-яё])", text.lower()))
    pauses = len(re.findall(r"[,:;]", text)) * 0.12 + len(re.findall(r"[.!?]", text)) * 0.18
    return (syl * 0.15 + letters * 0.16 + len(text.split()) * 0.03 + pauses) / speed


def chunks(t):
    out = []
    for s in re.split(r"(?<=[.!?])\s+", t):
        s = s.strip()
        if not s:
            continue
        if len(s.split()) > 11 and "," in s:
            acc, cur = [], ""
            for p in [p.strip() for p in re.split(r"(?<=,)\s+", s)]:
                if cur and len((cur + " " + p).split()) > 11:
                    acc.append(cur)
                    cur = p
                else:
                    cur = (cur + " " + p).strip()
            acc.append(cur)
            out += acc
        else:
            out.append(s)
    return out


def main(lines_json, ref, ref_text, outdir, speed=1.08, nfe=18, tempo=1.0):
    from ruaccent import RUAccent
    from faster_whisper import WhisperModel
    from f5_tts.api import F5TTS
    acc = RUAccent()
    acc.load(omograph_model_size="turbo3.1", use_dictionary=True, tiny_mode=False)
    f5 = F5TTS(model="F5TTS_v1_Base", ckpt_file=CKPT, vocab_file=VOCAB, device="cpu")
    asr = WhisperModel("small", device="cpu", compute_type="int8")
    lines = json.load(open(lines_json, encoding="utf-8"))
    os.makedirs(outdir, exist_ok=True)
    tmp = os.path.join(outdir, "_t")
    os.makedirs(tmp, exist_ok=True)

    def accent(txt):
        a = acc.process_all(txt)
        return a.replace("б+э", "бэ").replace("в+э", "вэ").replace("+а ", "а ")
    import soundfile as _sf
    _i = _sf.info(ref)
    ref_dur = _i.frames / _i.samplerate
    t0 = time.time()
    for i, line in enumerate(lines, 1):
        dest = os.path.join(outdir, f"{i:03d}.mp3")
        if os.path.exists(dest):
            continue
        files = []
        for k, ch in enumerate(chunks(spoken(line))):
            words = ch.split()
            last = words[-1].strip(".,!?")
            letter_end = last in ("а", "бэ", "вэ")                  # a letter must not end a chunk: F5 stretches it ("Аааэ") - keep a short letter and cut
            carrier = False
            text = ch
            refw = norm(ch)
            trail = 0                                                # trailing short tokens ("а", "и", "вэ") that the recogniser often misses
            while letter_end and trail < len(refw) - 1 and len(refw[-1 - trail]) <= 2:
                trail += 1
            core = refw[:len(refw) - trail] if letter_end else refw
            best = None
            for n in range(3):
                fn = os.path.join(tmp, f"c_{i}_{k}_{n}.wav")
                f5.infer(ref_file=ref, ref_text=ref_text, gen_text=accent(text), nfe_step=nfe, speed=speed, file_wave=fn,
                         remove_silence=False, seed=1000 * i + 10 * k + n, fix_duration=ref_dur + est_duration(text, speed) * (1.0 + 0.08 * n))
                seg, _ = asr.transcribe(fn, language="ru", word_timestamps=True)
                hw = [(norm(x.word)[0] if norm(x.word) else "", x.start, x.end) for s in seg for x in s.words]
                cut = tend = None
                target = CARRIER if carrier else core[-1]
                for j in range(len(hw) - 1, -1, -1):
                    if hw[j][0] and difflib.SequenceMatcher(None, hw[j][0], target).ratio() >= (0.7 if carrier else 0.75):
                        if carrier:
                            cut, tend = j - 1, hw[j][1] - 0.03
                        elif letter_end:
                            cut, tend = j, hw[j][2] + 0.38 * trail + 0.06   # the short tokens after the last real word, then stop
                        else:
                            cut, tend = j, hw[j][2] + 0.1
                        break
                w = 1.0 if cut is None or cut < 0 else wer(" ".join(core), " ".join(h for h, _, _ in hw[:cut + 1]))
                if best is None or w < best[0]:
                    best = (w, fn, tend)
                if w <= 0.1:
                    break
            w, fn, tend = best
            outw = fn + ".ok.wav"
            cmd = ["ffmpeg", "-loglevel", "error", "-y", "-i", fn] + (["-t", f"{tend:.2f}", "-af", "afade=t=out:st=%.2f:d=0.05" % max(0, tend - 0.05)] if tend else ["-af", "anull"]) + [outw]
            subprocess.run(cmd, check=True)
            files.append(outw)
            print(i, k, "wer", round(w, 2), "|", ch[:45], flush=True)
        ins = sum([["-i", f] for f in files], [])
        fl = "".join(f"[{n}:a]apad=pad_dur=0.05[a{n}];" for n in range(len(files))) + "".join(f"[a{n}]" for n in range(len(files))) \
            + f"concat=n={len(files)}:v=0:a=1,atempo={tempo},loudnorm=I=-16:TP=-1.5:LRA=9[o]"
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *ins, "-filter_complex", fl, "-map", "[o]", "-ar", "44100",
                        "-c:a", "libmp3lame", "-q:a", "2", dest], check=True)
        print("line", i, "done", round(time.time() - t0), flush=True)
    print("ALLDONE", flush=True)


if __name__ == "__main__":
    a = sys.argv
    main(a[1], a[2], a[3], a[4], float(a[5]) if len(a) > 5 else 1.08, int(a[6]) if len(a) > 6 else 18)
