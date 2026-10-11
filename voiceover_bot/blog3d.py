"""blog3d: 3D-ролик "как у блогера" по простому текстовому сценарию.

python blog3d.py --card КАРТИНКА --scenario сценарий15.txt --out ролик.mp4 [--engine yandex3 --voice anton]

Сценарий - обычный текст (см. сценарий15.txt): шапка про перекрёсток и вопрос,
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
MOVES = [("налев", "left"), ("направ", "right"), ("прям", "straight")]
HEAD_KEYS = {"тема", "хук", "вопрос", "ответ", "голос ответа", "перекресток", "знак", "главная", "очередь"}


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
    parts = re.split(r"(?m)^\s*-{3,}\s*$", raw)
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

    for k, v in lines(parts[0]):
        if k in HEAD_KEYS:
            head[k] = v
            continue
        words = re.split(r"[,\s]+", v)
        frm = next((parse_dir(w) for w in words if parse_dir(w) and not any(norm(w).startswith(m) for m, _ in MOVES)), None)
        move = next((mv for w in words for stem, mv in MOVES if norm(w).startswith(stem)), "straight")
        if frm:
            actors.append({"id": k, "from": frm, "move": move})
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
                b["tags"].append({"target": norm(p[0]), "sub": p[1] if len(p) > 1 else "", "title": p[2] if len(p) > 2 else p[-1]})
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
    missing = None
    if junction.startswith("т"):
        missing = next((parse_dir(w) for w in re.split(r"[,\s]+", junction)[1:] if parse_dir(w)), "N")
    sign = None
    if head.get("знак"):
        w = re.split(r"[,\s]+", head["знак"])
        kind = "main" if any(norm(x).startswith("главн") for x in w) else "yield"
        sign = {"kind": kind, "from": next((parse_dir(x) for x in w if parse_dir(x)), "S")}
    main = [parse_dir(x) for x in re.split(r"[,\s\-]+", head.get("главная", "")) if parse_dir(x)][:2]
    order = [norm(x) for x in head.get("очередь", "").split(",") if x.strip()]
    sc = {"actors": actors, "sign": sign, "main": main, "missing": missing, "order": order}
    return head, beats, sc


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


async def build_timeline(a, head, beats, sc, work):
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
    TL["reveal"] = [round(q1 + 0.15 + i * 0.45, 3) for i in range(len(TL_card_boxes))]
    t0 = (TL["reveal"][-1] if TL["reveal"] else q1) + 0.45
    ticks = str(work / "ticks.wav"); make_ticks(3, ticks)
    clips.append((t0, ticks)); TL["timer"] = [round(t0, 3), 3]
    t = t0 + 3.1
    TL["cardOut"] = round(t, 3)
    t += 0.5
    go_names = []
    for b in beats:
        b["t0"] = round(t, 3)
        vs = t + 0.15
        d = await say(b["voice"], vs) if b["voice"] else 0
        wt = [(w, vs + tt) for w, tt in word_times(b["voice"], d)] if d else []
        need = 2.0
        if b.get("plate"):
            need = 3.0
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
    TL["beats"] = beats
    # mix voice clips at absolute times
    audio = str(work / "track.m4a")
    inp, fl = [], []
    for i, (st, wav) in enumerate(clips):
        inp += ["-i", wav]
        ms = int(st * 1000)
        fl.append(f"[{i}:a]adelay={ms}|{ms}[a{i}]")
    fl.append("".join(f"[a{i}]" for i in range(len(clips))) + f"amix=inputs={len(clips)}:normalize=0,apad,atrim=0:{TL['end']:.3f}[out]")
    subprocess.run([FFMPEG, "-y", *inp, "-filter_complex", ";".join(fl), "-map", "[out]", "-c:a", "aac", "-b:a", "192k", audio],
                   check=True, capture_output=True)
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
    from PIL import Image
    card = work / "card.png"
    Image.open(a.card).convert("RGB").save(card)
    TL_card_boxes[:] = detect_options(str(card))
    if not TL_card_boxes:
        print("! варианты на карточке не найдены - зелёной рамки не будет")
    TL, audio = await build_timeline(a, head, beats, sc, work)
    try:
        correct = int(re.findall(r"\d+", head.get("ответ", "0"))[0])
    except IndexError:
        correct = 0
    with Image.open(card) as im:
        TL["card"] = {"img": "card.png", "ar": im.height / im.width, "boxes": TL_card_boxes, "correct": correct}
    TL["scene"] = sc
    print(f"Duration: {TL['end']:.1f}s, beats: {len(beats)}, options: {len(TL_card_boxes)}")
    frames = work / "frames"; frames.mkdir(exist_ok=True)
    if a.stills:
        pts = [float(x) for x in a.stills.split(",")] if a.stills != "auto" else \
            [1.0, TL["question"][0] + 1, TL["timer"][0] + 1] + [b["t0"] + (b["t1"] - b["t0"]) * 0.7 for b in beats] + [TL["green"] + 0.5]
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
                    "-vf", "scale=1080:1920:flags=lanczos", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{TL['end']:.2f}",
                    "-movflags", "+faststart", a.out], check=True, capture_output=True)
    if not a.workdir:
        shutil.rmtree(work, ignore_errors=True)
    print(f"Done: {a.out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine", choices=["yandex", "yandex3", "edge"], default="yandex3")
    ap.add_argument("--voice", default="anton")
    ap.add_argument("--role", default="good")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--chromium-path", default=None)
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--stills", default=None, help="auto или список секунд через запятую - только кадры, без видео")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
