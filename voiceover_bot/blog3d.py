"""blog3d: 3D-ролик "как у блогера" по простому текстовому сценарию.

python blog3d.py --card КАРТИНКА --scenario scenariy15.txt --out ролик.mp4 [--engine yandex3 --voice anton]

Сценарий - обычный текст (см. scenariy15.txt): шапка про перекрёсток и вопрос,
дальше кадры через ---. Субтитров нет: текст только на плашках и бирках.
"""
import argparse, asyncio, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from auto_quiz import FFMPEG, make_cached_synth, media_duration, _trim_tail_silence, _find_chromium
from make_video import read_text_any
from tiktok import _speakable, make_ticks
from tiktok_pro import word_times, make_synth

THREE_CDN = "https://unpkg.com/three@0.160.0/build/three.module.min.js"

DIRS = [("юг", "S"), ("сниз", "S"), ("низ", "S"), ("север", "N"), ("сверх", "N"), ("верх", "N"),
        ("спере", "N"), ("впере", "N"), ("восток", "E"), ("справ", "E"), ("прав", "E"),
        ("запад", "W"), ("слев", "W"), ("лев", "W")]
MOVES = [("налев", "left"), ("направ", "right"), ("прям", "straight"), ("развор", "uturn")]
HEAD_KEYS = {"тема", "хук", "вопрос", "ответ", "голос ответа", "перекресток", "главная", "очередь"}
COLORS = [("зел", "green"), ("крас", "red"), ("желт", "yellow"), ("миг", "blink"), ("выкл", "off"), ("не раб", "off")]


def parse_color(text):
    t = norm(text)
    return next((c for stem, c in COLORS if stem in t), None)


CAR_COLORS = [("красн", "#c8312b"), ("син", "#2f63c8"), ("голуб", "#6fa6dc"), ("бел", "#eeeeea"), ("черн", "#22252a"),
              ("сер", "#8a9099"), ("желт", "#f0c419"), ("зелен", "#3b9a4c"), ("оранж", "#ef7a22"), ("коричн", "#7a4a2a"),
              ("бордов", "#7a1f2b"), ("фиолет", "#6b4bb0"), ("серебр", "#c3c8cf")]


def norm(s):
    return s.strip().lower().replace("ё", "е")


def parse_dir(word):
    w = norm(word)
    for stem, d in DIRS:
        if w.startswith(stem):
            return d
    return None


def parse_scenario(path):
    raw = read_text_any(path)
    # Telegram/ChatGPT turn "---" into long dashes or a line: accept any run of dashes/underscores as a separator
    parts = re.split(r"(?m)^\s*[-\u2010-\u2015\u2212_=*]{2,}\s*$", raw)
    head, beats = {}, []
    actors = []

    def lines(block):
        for ln in block.splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if ":" in ln:
                k, v = ln.split(":", 1)
                yield norm(k), v.strip()
            else:
                yield norm(ln), ""

    signs, lights, peds, zebras = [], {}, [], []
    extra = {}
    for k, v in lines(parts[0]):
        if k in HEAD_KEYS or re.match(r"вариант\s*\d", k):
            head[k] = v
            continue
        if k == "знак":
            for part in v.split(";"):
                w = re.split(r"[,\s]+", part)
                if any(norm(x).startswith("односторон") for x in w):
                    kind = "oneway_left" if any(norm(x).startswith("налев") for x in w) else "oneway_right"
                elif any(norm(x).startswith("круг") for x in w):
                    kind = "round"
                elif any(norm(x).startswith(("пешеход", "переход")) for x in w):
                    kind = "crosswalk"
                elif any(norm(x).startswith("остановк") for x in w):
                    kind = "nostop"
                elif any(norm(x).startswith("стоянк") for x in w):
                    kind = "nopark"
                else:
                    kind = "main" if any(norm(x).startswith("главн") for x in w) else "stop" if any(norm(x).startswith("стоп") for x in w) else "yield"
                signs.append({"kind": kind, "from": next((parse_dir(x) for x in w if parse_dir(x)), "S")})
            continue
        if k in ("переход", "зебра", "переходы"):
            zebras += [parse_dir(x) for x in re.split(r"[,\s]+", v) if parse_dir(x)]
            continue
        if k == "светофор":
            for part in v.split(","):
                c = parse_color(part)
                d = next((parse_dir(x) for x in part.split() if parse_dir(x)), None)
                if c and d:
                    lights[d] = c
                elif c:                      # one color without a side: ours (S/N) gets it, crossing road the opposite
                    lights.update({"S": c, "N": c})
                    if c in ("green", "red"):
                        lights.update({"E": "red" if c == "green" else "green", "W": "red" if c == "green" else "green"})
                    else:
                        lights.update({"E": c, "W": c})
            continue
        if k == "разделитель":                       # a median strip with bushes along an arm: two carriageways
            extra.setdefault("median", []).append(next((parse_dir(x) for x in re.split(r"[,\s]+", v) if parse_dir(x)), "E"))
            continue
        if k == "доезжает":                          # where a driving car ("едет") stops before the junction / crosswalk, metres from the centre
            m_ = re.search(r"\d+([.,]\d+)?", v)
            if m_:
                extra["cruiseStop"] = float(m_.group(0).replace(",", "."))
            continue
        if k == "разметка":
            extra["doubleline"] = "двойн" in norm(v)
            continue
        if k == "прохожая":                          # a woman walking along the pavement of an arm
            extra["walker"] = next((parse_dir(x) for x in re.split(r"[,\s]+", v) if parse_dir(x)), "E")
            continue
        if k.startswith("пешеход"):
            w = re.split(r"[,\s]+", v)
            n = next((int(x) for x in w if x.isdigit()), 3)
            peds.append({"id": "пешеходы" if not peds else f"пешеходы{len(peds) + 1}",
                         "cross": next((parse_dir(x) for x in w if parse_dir(x)), "W"), "n": n,
                         "walking": any(norm(x).startswith(("ид", "перех")) for x in w)})
            continue
        words = re.split(r"[,\s]+", v)
        frm = next((parse_dir(w) for w in words if parse_dir(w) and not any(norm(w).startswith(m) for m, _ in MOVES)), None)
        move = next((mv for w in words for stem, mv in MOVES if norm(w).startswith(stem)), "straight")
        if frm:
            col = next((hexc for w in words for stem, hexc in CAR_COLORS if norm(w).startswith(stem)), None)
            letter = next((w for w in words if re.fullmatch(r"[А-ЯA-Z]", w)), None)
            at = next((float(w.replace(",", ".")) for w in words if re.fullmatch(r"\d+([.,]\d+)?", w)), None)
            if any(norm(w).startswith(("стоит", "стоян", "припарк", "стоим")) for w in words):
                move = "park"
            elif any(norm(w).startswith(("едет", "едем", "катит", "подъезж")) for w in words):
                move = "cruise"                   # drives up to the junction on its own (start distance = the number)
            side = 2 if any(norm(w).startswith("соседн") for w in words) else -1 if any(norm(w).startswith("слева") for w in words) else 0 if any(norm(w).startswith("полос") for w in words) else 1
            actors.append({"id": k, "from": frm, "move": move, "color": col, "letter": letter, "at": at, "side": side})
        else:
            print(f"! не понял строку шапки: {k}: {v}")
    for block in parts[1:]:
        b = {"voice": "", "tags": [], "arrows": [], "checks": []}
        for k, v in lines(block):
            if k == "голос":
                b["voice"] = v
            elif k == "камера":
                b["cam"] = norm(v)
            elif k == "надпись":
                b["cap"] = v
            elif k == "метка":
                p = [x.strip() for x in v.split("|")]
                if len(p) == 2:
                    p = [p[0], "", p[1]]
                b["tags"].append({"target": norm(p[0]), "sub": p[1] if len(p) > 1 else "", "title": p[2] if len(p) > 2 else p[-1],
                                 "color": ("red" if len(p) > 3 and norm(p[3]).startswith("красн") else "green" if len(p) > 3 and norm(p[3]).startswith("зел") else None)})
            elif k == "зона":
                p = [x.strip() for x in v.split("|")]
                red = len(p) < 2 or norm(p[1]).startswith(("нарушает", "красн", "нельзя", "плох", "нет"))
                b.setdefault("zones", []).append([norm(p[0]), "red" if red else "green"])
            elif k == "линейка":
                p = [x.strip() for x in v.split("|")]
                num = re.search(r"\d+([.,]\d+)?", p[1] if len(p) > 1 else "")
                b.setdefault("rulers", []).append([norm(p[0]), p[1] if len(p) > 1 else "", float(num.group(0).replace(",", ".")) if num else 5.0])
            elif k == "часть":
                b.setdefault("parts", []).extend(int(x) for x in re.findall(r"\d", v))
            elif k == "перестроить":
                p = [x.strip() for x in v.split("|")]
                b.setdefault("shifts", []).append([norm(p[0]), -1 if len(p) > 1 and norm(p[1]).startswith(("лев", "влев")) else 1])
            elif k == "стрелка":
                b["arrows"] += [norm(x) for x in v.split(",") if x.strip()]
            elif k == "пересечение":
                b["cross"] = [norm(x) for x in v.split(",")][:2]
            elif k == "галочка":
                b["checks"] += [[norm(x), "ok"] for x in v.split(",") if x.strip()]
            elif k in ("крест", "крестик"):
                b["checks"] += [[norm(x), "x"] for x in v.split(",") if x.strip()]
            elif k == "поехали":
                b["go"] = True
            elif k.startswith("табличка на"):
                b["plate"] = True
            elif k == "главная":
                b["main"] = True
            else:
                print(f"! не понял строку кадра: {k}: {v}")
        if b["voice"] or b.get("cap"):
            beats.append(b)
    # scene
    junction = norm(head.get("перекресток", "крест"))
    missing = []
    street = junction.startswith(("улиц", "дорог", "прям"))
    if street:
        missing = ["E", "W"]
    elif junction.startswith("т") and not junction.startswith("тр"):
        missing = [next((parse_dir(w) for w in re.split(r"[,\s]+", junction)[1:] if parse_dir(w)), "N")]
    main = [parse_dir(x) for x in re.split(r"[,\s\-]+", head.get("главная", "")) if parse_dir(x)][:2]
    order = [norm(x) for x in head.get("очередь", "").split(",") if x.strip()]
    sc = {"actors": actors, "signs": signs, "main": main, "missing": missing, "street": street, "zebras": zebras, "order": order,
          "lights": lights, "peds": peds, "round": junction.startswith("кольц") or junction.startswith("круг"), **extra}
    return head, beats, sc


def last_text_line(path):
    """Bounds of the lowest line of dark text on a screenshot (the link in the bio) -> [x0, x1, y_bottom] in fractions."""
    from PIL import Image
    im = Image.open(path).convert("L"); W, H = im.size; px = im.load()
    rows = [sum(1 for x in range(0, W, 2) if px[x, y] < 110) for y in range(H)]
    y = H - 1
    while y > H * 0.3 and rows[y] < 3:
        y -= 1
    bot = y
    while y > 0 and rows[y] >= 1:
        y -= 1
    top = y + 1
    xs = [x for yy in range(top, bot + 1) for x in range(0, W, 2) if px[x, yy] < 110]
    if not xs or bot - top < 4:
        return [0.1, 0.7, 0.85]
    return [round(min(xs) / W, 3), round(max(xs) / W, 3), round(min(H, bot + 6) / H, 3)]


def detect_options(path):
    """Рамки вариантов на карточке, снизу вверх: низ рамки -> ближайший верх выше 5% высоты."""
    from PIL import Image
    im = Image.open(path).convert("RGB"); W, H = im.size; px = im.load()
    x0, x1 = int(W * 0.06), int(W * 0.94); n = max(1, (x1 - x0) // 3)
    b = []
    for y in range(int(H * 0.4), H):
        c = sum(1 for x in range(x0, x1, 3) if 195 < sum(px[x, y]) // 3 < 249)
        if c / n > 0.5:
            if b and y - b[-1] <= 6:
                b[-1] = y
            else:
                b.append(y)
    b = [v / H for v in b]
    boxes, i = [], len(b) - 1
    while i > 0:
        bot = b[i]; j = i - 1
        while j >= 0 and bot - b[j] < 0.05:
            j -= 1
        if j < 0 or bot - b[j] > 0.16:
            break
        boxes.append((round(b[j], 3), round(bot, 3)))
        i = j - 1
        if i < 0 or b[j] - b[i] > 0.03:      # next box must sit right above
            break
    return boxes[::-1]


LINES = []                                      # every phrase in the order it is voiced (for --dump-lines)


async def build_timeline(a, head, beats, sc, work):
    if getattr(a, "dump_lines", False) or getattr(a, "voice_dir", None):
        files = sorted(Path(a.voice_dir).glob("*.mp3")) if a.voice_dir else []

        async def synth(text):                 # record the order / take ready-made audio from another TTS site
            LINES.append(text)
            if files:
                if len(LINES) > len(files):
                    raise SystemExit(f"! В твоём аудио {len(files)} кусков, а фраз больше. Проверь паузы между абзацами.")
                return files[len(LINES) - 1].read_bytes()
            return subprocess.run([FFMPEG, "-f", "lavfi", "-t", "0.6", "-i", "sine=frequency=300:sample_rate=24000", "-f", "mp3", "-"],
                                  capture_output=True).stdout
    elif a.engine == "xtts":                   # own cloned voice, locally (XTTS v2): --voice = path to the sample
        import cloning, tempfile as _tf
        sp = float(os.environ.get("YANDEX_SPEED", "1.0") or 1.0)

        def _x(t):
            with _tf.TemporaryDirectory() as td:
                wav = os.path.join(td, "x.wav")
                cloning._synth(t, a.voice, "ru", wav)
                tempo = max(0.5, min(2.0, sp))
                r = subprocess.run([FFMPEG, "-y", "-i", wav, "-af", f"atempo={tempo:.2f}", "-f", "mp3", "-"], capture_output=True)
                return r.stdout

        async def base(t):
            return await asyncio.to_thread(_x, t)
        synth = make_cached_synth(base, "xtts", Path(a.voice).stem + str(Path(a.voice).stat().st_mtime_ns), "s" + str(sp), "+0Hz", 30)
    elif a.engine == "eleven":                 # own cloned voice (ElevenLabs)
        import eleven
        sp = os.environ.get("YANDEX_SPEED", "1.0")

        async def base(t):
            return await asyncio.to_thread(eleven.synth, t, a.voice, sp)
        synth = make_cached_synth(base, "eleven", a.voice, "s" + sp, "+0Hz", 30)
    else:
        base, vn, vr = make_synth(a.engine, a.voice, a.role)
        synth = make_cached_synth(base, a.engine, vn, vr, "+0Hz", 30)
    clips = []                                  # (start, wav)
    n = [0]

    async def say(text, t0):
        n[0] += 1
        mp3 = work / f"v{n[0]:03d}.mp3"
        mp3.write_bytes(await synth(_speakable(text)))
        _trim_tail_silence(mp3)
        wav = work / f"v{n[0]:03d}.wav"
        subprocess.run([FFMPEG, "-y", "-i", str(mp3), "-ar", "48000", "-ac", "2", str(wav)], capture_output=True, check=True)
        d = media_duration(str(wav))
        clips.append((t0, str(wav)))
        return d

    TL = {"topic": head.get("тема", ""), "hookText": head.get("хук", "")}
    print("Voicing...")
    t = 0.25
    if TL["hookText"]:
        t += await say(TL["hookText"], t)
    TL["hook"] = [0, t]
    TL["cardIn"] = round(max(2.2, t) - 0.1, 3)
    q0 = TL["cardIn"] + 0.55
    q1 = q0 + await say(head.get("вопрос", ""), q0)
    TL["question"] = [q0, q1]
    # options are read aloud one by one, each appears on the card when it is read
    ORD = ["Первый", "Второй", "Третий", "Четвёртый", "Пятый"]
    opts = [head.get(f"вариант {i + 1}") or head.get(f"вариант{i + 1}") for i in range(len(TL_card_boxes))]
    TL["reveal"], t = [], q1 + 0.35
    for i, o in enumerate(opts):
        TL["reveal"].append(round(t, 3))
        if o:
            t += await say(f"{ORD[i] if i < len(ORD) else i + 1}: {o}", t) + 0.3
        else:
            t += 0.5
    TL["timer"] = [-10, 0]
    t += 0.5
    TL["cardOut"] = round(t, 3)
    t += 0.5
    go_names = []
    if getattr(a, "phone", False):
        TL["phone"] = True
        if a.phone_video and Path(a.phone_video).exists():
            pv = work / "pv"; shutil.rmtree(pv, ignore_errors=True); pv.mkdir()
            subprocess.run([FFMPEG, "-y", "-i", a.phone_video, "-t", "14", "-vf", "fps=10,scale=360:-2", "-q:v", "5", str(pv / "p%04d.jpg")],
                           capture_output=True)
            nfr = len(list(pv.glob("p*.jpg")))
            if nfr:
                TL["phoneFrames"] = nfr; TL["phoneFps"] = 10; TL["phoneVideoDur"] = round(nfr / 10, 2)
        if not TL.get("phoneFrames") and not getattr(a, "promo", None):
            TL["phone"] = False
    promo_on = bool(getattr(a, "promo", None) or TL.get("phoneFrames"))
    ppos = getattr(a, "promo_pos", "mid")
    if promo_on and beats and ppos in ("mid", "both"):            # advert of the channel in the middle of the video
        mid = max(1, len(beats) // 2)
        beats.insert(mid, {"voice": a.promo_text, "promo": True, "tags": [], "arrows": [], "checks": [],
                           "cam": beats[mid - 1].get("cam")})
    for b in beats:
        b["t0"] = round(t, 3)
        vs = t + 0.15
        d = await say(b["voice"], vs) if b["voice"] else 0
        wt = [(w, vs + tt) for w, tt in word_times(b["voice"], d)] if d else []
        need = 2.0
        if b.get("plate"):
            need = 3.0
        if b.get("promo"):
            need = 4.0
            if TL.get("phone"):
                need = 0.7 + TL.get("phoneVideoDur", 0) + (3.8 if getattr(a, "promo", None) else 0.0)
        if b.get("go"):
            need = 3.6 + 3.2 * max(0, len(sc["order"]) - 1) + 1.0
            b["goAt"] = {nm: round(b["t0"] + 0.4 + i * 3.2, 3) for i, nm in enumerate(sc["order"])}
            go_names.append(b["goAt"])
        for i, tg in enumerate(b["tags"]):
            keys = [norm(x)[:5] for x in (tg["target"].split(".")[0], re.sub(r"[*_]", "", tg["title"]).split()[0] if tg["title"].strip() else "") if len(norm(x)) >= 3]
            hit = next((tw for w, tw in wt if any(norm(w).startswith(k) for k in keys)), None)
            tg["t"] = round(hit - 0.1 if hit else b["t0"] + 0.5 + 0.7 * i, 3)
        b["t1"] = round(max(vs + d + 0.4, b["t0"] + need), 3)
        t = b["t1"]
    GO = {}
    for g in go_names:
        GO.update(g)
    TL["go"] = GO
    a0 = t
    TL["answer"] = [round(a0, 3)]
    av = a0 + 0.7
    atext = head.get("голос ответа") or f"Правильный ответ {head.get('ответ', '')}."
    d = await say(atext, av)
    wt = word_times(atext, d)
    hit = next((tw for w, tw in wt if re.match(r"(перв|втор|трет|четв|\d)", norm(w))), 0.4)
    TL["green"] = round(av + hit, 3)
    TL["end"] = round(av + d + 1.6, 3)
    if promo_on and beats and ppos in ("end", "both"):         # advert after the answer
        e0 = round(av + d + 0.5, 3)
        eb = {"voice": a.promo_text, "promo": True, "tags": [], "arrows": [], "checks": [], "cam": beats[-1].get("cam"), "t0": e0}
        evs = e0 + 0.15
        ed = await say(eb["voice"], evs) if eb["voice"] else 0
        need = 4.0
        if TL.get("phone"):
            need = 0.7 + TL.get("phoneVideoDur", 0) + (3.8 if getattr(a, "promo", None) else 0.0)
        eb["t1"] = round(max(evs + ed + 0.4, e0 + need), 3)
        beats.append(eb)
        TL["endPromo"] = e0
        TL["end"] = round(eb["t1"] + 0.4, 3)
    TL["beats"] = beats
    # mix voice clips at absolute times
    audio = str(work / "track.m4a")
    # silent base of exact length first + amix duration=first: the mix always ends (apad could run forever)
    end = f"{TL['end']:.3f}"
    inp, fl = ["-f", "lavfi", "-t", end, "-i", "anullsrc=r=48000:cl=stereo"], []
    for i, (st, wav) in enumerate(clips, start=1):
        inp += ["-i", wav]
        ms = int(st * 1000)
        fl.append(f"[{i}:a]aresample=48000,adelay={ms}|{ms}[a{i}]")
    fl.append("[0:a]" + "".join(f"[a{i}]" for i in range(1, len(clips) + 1)) +
              f"amix=inputs={len(clips) + 1}:duration=first:normalize=0[out]")
    subprocess.run([FFMPEG, "-y", *inp, "-filter_complex", ";".join(fl), "-map", "[out]", "-t", end,
                    "-c:a", "aac", "-b:a", "192k", audio], check=True, capture_output=True, timeout=600)
    if getattr(a, "music", None) and Path(a.music).exists():
        mixed = str(work / "track_music.m4a")
        fl = (f"[1:a]volume={a.music_vol},afade=t=in:d=1.2,afade=t=out:st={max(0, TL['end'] - 2.0):.2f}:d=2.0[m];"
              "[m][0:a]sidechaincompress=threshold=0.03:ratio=10:attack=15:release=350[mc];"
              "[0:a][mc]amix=inputs=2:duration=first:normalize=0[out]")
        r = subprocess.run([FFMPEG, "-y", "-i", audio, "-stream_loop", "-1", "-i", str(a.music), "-filter_complex", fl, "-map", "[out]",
                            "-t", end, "-c:a", "aac", "-b:a", "192k", mixed], capture_output=True, timeout=600)
        if r.returncode == 0:
            audio = mixed
        else:
            print("музыка не добавилась:", r.stderr.decode("utf-8", "ignore")[-300:])
    return TL, audio


TL_card_boxes = []


async def render(TL, work, frames, fps, chromium, only=None):
    from playwright.async_api import async_playwright
    three = "./three.module.min.js" if (work / "three.module.min.js").exists() else THREE_CDN
    html = (HERE / "blog3d.html").read_text(encoding="utf-8").replace("__THREE__", three).replace("__TL__", json.dumps(TL, ensure_ascii=True))
    (work / "run.html").write_text(html, encoding="utf-8")
    port = 8765 + os.getpid() % 500
    srv = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1", "--directory", str(work)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.0)
    try:
        args = ["--no-sandbox", "--ignore-gpu-blocklist"]
        if os.environ.get("PDD3D_SWIFTSHADER"):
            args += ["--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
        kw = {"args": args}
        exe = chromium or _find_chromium()
        if exe:
            kw["executable_path"] = exe
        async with async_playwright() as pw:
            br = await pw.chromium.launch(**kw)
            page = await br.new_page(viewport={"width": 720, "height": 1280})
            page.on("pageerror", lambda e: print("pageerror:", e))
            page.on("console", lambda m: print("console:", m.text) if m.type == "error" else None)
            await page.goto(f"http://127.0.0.1:{port}/run.html")
            await page.wait_for_function("window.__ready === true", timeout=120000)
            times = only if only is not None else [i / fps for i in range(int(TL["end"] * fps))]
            t0 = time.time()
            start = int(os.environ.get("RESUME", "0"))
            for i, tt in enumerate(times):
                if i < start:
                    continue
                await page.evaluate(f"window.renderAt({tt:.4f})")
                await page.screenshot(path=str(frames / f"f{i:05d}.jpg"), type="jpeg", quality=90)
                if i % 100 == 0:
                    print(f"frame {i}/{len(times)}  {time.time() - t0:.0f}s", flush=True)
            await br.close()
    finally:
        srv.terminate()


async def main_async(a):
    head, beats, sc = parse_scenario(a.scenario)
    work = Path(a.workdir) if a.workdir else Path(tempfile.mkdtemp(prefix="blog3d_"))
    work.mkdir(parents=True, exist_ok=True)
    for f in ["three.module.min.js", "mont_900_cyrillic.woff2", "mont_900_latin.woff2"]:
        if (HERE / f).exists():
            shutil.copy(HERE / f, work / f)
    for d in ["addons", "models"]:
        if (HERE / d).exists():
            shutil.copytree(HERE / d, work / d, dirs_exist_ok=True)
    from PIL import Image
    card = work / "card.png"
    Image.open(a.card).convert("RGB").save(card)
    TL_card_boxes[:] = detect_options(str(card))
    if not TL_card_boxes:
        print("! варианты на карточке не найдены - зелёной рамки не будет")
    TL, audio = await build_timeline(a, head, beats, sc, work)
    if a.dump_lines:
        print("LINES_JSON " + json.dumps(LINES, ensure_ascii=False))
        return
    try:
        correct = int(re.findall(r"\d+", head.get("ответ", "0"))[0])
    except IndexError:
        correct = 0
    with Image.open(card) as im:
        TL["card"] = {"img": "card.png", "ar": im.height / im.width, "boxes": TL_card_boxes, "correct": correct}
    TL["scene"] = sc
    TL["fast"] = bool(os.environ.get("BLOG3D_FAST")) or (os.cpu_count() or 4) <= 2
    if TL["fast"] and a.fps == 30 and not getattr(a, "keep_fps", False):
        a.fps = 15                       # weak server: half the frames (doubled at encode), 3D at lower resolution; overlay text stays sharp
        print("Слабый сервер: лёгкий режим (15 кадров/с, облегчённое 3D)")
    if a.promo:
        Image.open(a.promo).convert("RGB").save(work / "promo.jpg", quality=92)
        TL["promo"] = "promo.jpg"
        TL["promoMark"] = last_text_line(work / "promo.jpg")
        TL["promoCap"] = a.promo_cap
    print(f"Duration: {TL['end']:.1f}s, beats: {len(beats)}, options: {len(TL_card_boxes)}")
    frames = work / "frames"; frames.mkdir(exist_ok=True)
    if a.stills:
        pts = [float(x) for x in a.stills.split(",")] if a.stills != "auto" else \
            [1.0, TL["question"][0] + 1, TL["reveal"][-1] + 1 if TL["reveal"] else 5] + [b["t0"] + (b["t1"] - b["t0"]) * 0.7 for b in beats] + [TL["green"] + 0.5]
        for f in frames.glob("*.jpg"):
            f.unlink()
        await render(TL, work, frames, a.fps, a.chromium_path, only=pts)
        print("stills in", frames)
        return
    if not os.environ.get("RESUME"):
        for f in frames.glob("*.jpg"):
            f.unlink()
    print("Rendering frames...")
    await render(TL, work, frames, a.fps, a.chromium_path)
    print("Encoding...")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([FFMPEG, "-y", "-framerate", str(a.fps), "-i", str(frames / "f%05d.jpg"), "-i", audio,
                    "-vf", ("fps=30," if a.fps < 30 else "") + "scale=1080:1920:flags=lanczos", "-c:v", "libx264", "-preset", "veryfast" if TL.get("fast") else "medium", "-crf", "20" if TL.get("fast") else "19",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{TL['end']:.2f}",
                    "-movflags", "+faststart", a.out], check=True, capture_output=True)
    shutil.rmtree(frames, ignore_errors=True)          # frames are the biggest disk hog: drop them right away
    if not a.workdir:
        shutil.rmtree(work, ignore_errors=True)
    print(f"Done: {a.out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine", choices=["yandex", "yandex3", "edge", "eleven", "xtts"], default="yandex3")
    ap.add_argument("--voice", default="anton")
    ap.add_argument("--role", default="good")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--chromium-path", default=None)
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--promo", default=None, help="скрин профиля - реклама канала в середине ролика")
    ap.add_argument("--promo-text", default="Кстати! Переходи по ссылке в моём профиле. Там закрытый канал, где я разобрал все 800 вопросов простым и понятным языком.")
    ap.add_argument("--promo-cap", default="ВСЕ *800 ВОПРОСОВ* - В _TG-БОТЕ_")
    ap.add_argument("--promo-pos", choices=["mid", "end", "both"], default="mid", help="когда показывать рекламу: в середине, в конце или и там и там")
    ap.add_argument("--music", default=None, help="фоновая музыка (mp3): тихо под голосом, во время речи приглушается")
    ap.add_argument("--music-vol", type=float, default=0.5, help="громкость музыки относительно файла (0.5 = тихо)")
    ap.add_argument("--keep-fps", action="store_true", help="на слабом сервере не снижать кадры/с (плавнее, но дольше)")
    ap.add_argument("--phone", action="store_true", help="реклама в 3D-телефоне в руке (скрин из --promo и/или видео)")
    ap.add_argument("--phone-video", default=None, help="видео, которое играет на экране телефона")
    ap.add_argument("--dump-lines", action="store_true", help="только вывести все фразы по порядку (для озвучки на другом сайте)")
    ap.add_argument("--voice-dir", default=None, help="папка 001.mp3, 002.mp3... - готовая озвучка фраз по порядку")
    ap.add_argument("--stills", default=None, help="auto или список секунд через запятую - только кадры, без видео")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
