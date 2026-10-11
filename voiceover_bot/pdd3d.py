"""pdd3d: card (question) -> 3D explanation -> card (green answer) video.

python pdd3d.py --card IMG --speak TEXT.txt --scene sceneNN.json --marks metkiNN.json --correct 3 --out OUT.mp4
                [--engine yandex3 --voice anton] [--fps 30] [--chromium-path PATH]
"""
import argparse, asyncio, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from auto_quiz import FFMPEG, make_cached_synth, media_duration, concat_audio, _trim_tail_silence, _find_chromium
from make_video import read_text_any
from tiktok import _speakable, make_ticks, parse_narration, detect_boxes
from tiktok_pro import split_sentences, word_times, group_words, HL_STEMS, make_synth

THREE_CDN = "https://unpkg.com/three@0.160.0/build/three.module.min.js"


def words_hl(group):
    return [[re.sub(r"[\"<>]", "", w).upper(), bool(any(s in w.lower() for s in HL_STEMS) or re.search(r"\d", w))]
            for w, _ in group]


async def build_timeline(a, sc, tmp):
    raw = read_text_any(a.speak)
    hook = ""
    mh = re.search(r"(?mi)^\s*\u0425\u0443\u043a\s*:\s*(.+)$", raw)
    if mh:
        hook = mh.group(1).strip()
        raw = raw[:mh.start()] + raw[mh.end():]
    question, expl, _sp, answer, _g, _m = parse_narration(raw)
    base, vn, vr = make_synth(a.engine, a.voice, a.role)
    synth = make_cached_synth(base, a.engine, vn, vr, "+0Hz", 30)
    clips, gaps, caps = [], [], []
    st = {"t": 0.0, "n": 0}

    async def say(text):
        st["n"] += 1
        mp3 = tmp / f"s{st['n']:03d}.mp3"
        mp3.write_bytes(await synth(_speakable(text)))
        _trim_tail_silence(mp3)
        wav = tmp / f"s{st['n']:03d}.wav"
        subprocess.run([FFMPEG, "-y", "-i", str(mp3), "-ar", "48000", "-ac", "2", str(wav)], capture_output=True, check=True)
        return str(wav), media_duration(str(wav))

    async def block(text, gap=0.14):
        allw = []
        for s in split_sentences(text):
            if not _speakable(s).strip():
                continue
            p, d = await say(s)
            clips.append((p, d)); gaps.append(gap)
            wt = [(w, st["t"] + tt) for w, tt in word_times(s, d)]
            for g in group_words(wt):
                caps.append([round(g[0][1], 3), words_hl(g)])
            allw += wt
            st["t"] += d + gap
        return allw

    TL = {}
    print("Voicing...")
    h0 = st["t"]
    if hook:
        await block(hook); st["t"] += 0.3
    TL["hook"] = [h0, st["t"]]
    q0 = st["t"]; await block(question); TL["question"] = [q0, st["t"]]
    ticks = str(tmp / "ticks.wav"); make_ticks(3, ticks); td = media_duration(ticks)
    TL["capOff"] = [round(st["t"], 3)]; TL["timer"] = [round(st["t"], 3), 3]
    clips.append((ticks, td)); gaps.append(0.2); st["t"] += td + 0.2
    e0 = st["t"]; ew = await block(expl); TL["expl"] = [e0, st["t"]]
    focus = []
    for i, f in enumerate(sc.get("focus", [])):
        hit = next((tw for w, tw in ew if any(k in w.lower() for k in f.get("keys", []))), None)
        if hit is None:
            hit = e0 + (st["t"] - e0) * (0.1 + 0.6 * (i + 1) / (len(sc["focus"]) + 1))
        focus.append([round(max(e0 + 0.3, hit - 0.15), 3), i])
    focus.sort()
    TL["focus"] = focus
    need = max([off for _, off in sc.get("order", [])] + [0]) + 3.8
    resolve = max(e0 + 1.0, (focus[-1][0] + 2.2) if focus else e0 + 1.0)
    if resolve + need > st["t"]:                     # silent room for the animation before the answer
        pad = resolve + need - st["t"]
        sil = tmp / "pad.wav"
        subprocess.run([FFMPEG, "-y", "-f", "lavfi", "-t", f"{pad:.3f}", "-i", "anullsrc=r=48000:cl=stereo", str(sil)], capture_output=True, check=True)
        clips.append((str(sil), pad)); gaps.append(0.0); st["t"] += pad
    TL["resolve"] = round(resolve, 3)
    a0 = st["t"]; await block("\u041e\u0442\u0432\u0435\u0442. " + answer, gap=0.0); TL["answer"] = [a0, st["t"]]
    TL["end"] = st["t"] + 2.6
    TL["caps"] = caps
    audio = str(tmp / "track.m4a")
    concat_audio(clips, gaps, audio)
    return TL, audio, answer


async def render(TL, work, out_frames, fps, chromium):
    from playwright.async_api import async_playwright
    three = "./three.module.min.js" if (work / "three.module.min.js").exists() else THREE_CDN
    html = (HERE / "scene3d.html").read_text(encoding="utf-8").replace("__THREE__", three).replace("__TL__", json.dumps(TL, ensure_ascii=True))
    (work / "scene_run.html").write_text(html, encoding="utf-8")
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
            await page.goto(f"http://127.0.0.1:{port}/scene_run.html")
            await page.wait_for_function("window.__ready === true", timeout=120000)
            n = int(TL["end"] * fps)
            times = [i / fps for i in range(n)]
            if os.environ.get("PDD3D_STILLS"):
                times = [0.4, TL["hook"][1] - 0.3, TL["question"][0] + 1.5, TL["timer"][0] + 1, TL["expl"][0] + 0.4] + \
                        [f[0] + 1.3 for f in TL["focus"]] + [TL["resolve"] + 1.0, TL["resolve"] + 2.5, TL["resolve"] + 4.5, TL["answer"][1] + 0.8]
                n = len(times)
            t0 = time.time()
            for i, tt in enumerate(times):
                await page.evaluate(f"window.renderAt({tt:.4f})")
                await page.screenshot(path=str(out_frames / f"f{i:05d}.jpg"), type="jpeg", quality=90)
                if i % 100 == 0:
                    print(f"frame {i}/{n}  {time.time() - t0:.0f}s", flush=True)
            await br.close()
    finally:
        srv.terminate()


async def main_async(a):
    sc = json.loads(Path(a.scene).read_text(encoding="utf-8-sig"))
    marks = json.loads(Path(a.marks).read_text(encoding="utf-8-sig")) if a.marks else {"objects": [], "underline": []}
    work = Path(tempfile.mkdtemp(prefix="pdd3d_"))
    for f in ["three.module.min.js", "mont_900_cyrillic.woff2", "mont_900_latin.woff2"]:
        if (HERE / f).exists():
            shutil.copy(HERE / f, work / f)
    card = work / ("card" + Path(a.card).suffix.lower())
    shutil.copy(a.card, card)
    TL, audio, answer = await build_timeline(a, sc, work)
    green = None
    boxes = detect_boxes(str(card))
    if a.correct and boxes and 1 <= a.correct <= len(boxes):
        top, bot = boxes[a.correct - 1]
        green = [0.03, top - 0.004, 0.94, bot - top + 0.008]
    else:
        print(f"! option boxes not found or --correct out of range ({len(boxes or [])} boxes)")
    ph = marks.get("photo")
    TL["card"] = {"img": card.name, "objects": marks.get("objects", []), "underline": marks.get("underline", []),
                  "green": green, "ans": f"{a.correct}. {answer.rstrip('.')}",
                  "photoY": ((ph[1] + ph[3]) / 2) if ph else 0.3}
    TL["stamp"] = a.stamp
    TL["ansTitle"] = "\u041f\u0420\u0410\u0412\u0418\u041b\u042c\u041d\u042b\u0419 \u041e\u0422\u0412\u0415\u0422"
    TL["scene"] = sc
    print(f"Duration: {TL['end']:.1f}s. Rendering frames...")
    frames = work / "frames"; frames.mkdir()
    await render(TL, work, frames, a.fps, a.chromium_path)
    if os.environ.get("PDD3D_STILLS"):
        subprocess.run([FFMPEG, "-y", "-pattern_type", "glob", "-i", str(frames / "*.jpg"), "-vf", "scale=240:-1,tile=8x2", "-frames:v", "1", a.out], check=True, capture_output=True)
        print("stills:", a.out); return
    print("Encoding...")
    subprocess.run([FFMPEG, "-y", "-framerate", str(a.fps), "-i", str(frames / "f%05d.jpg"), "-i", audio,
                    "-vf", "scale=1080:1920:flags=lanczos", "-c:v", "libx264", "-preset", "medium", "-crf", "19",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{TL['end']:.2f}",
                    "-movflags", "+faststart", a.out], check=True, capture_output=True)
    print(f"Done: {a.out}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", required=True)
    ap.add_argument("--speak", required=True)
    ap.add_argument("--scene", required=True)
    ap.add_argument("--marks", default=None)
    ap.add_argument("--correct", type=int, default=0)
    ap.add_argument("--out", required=True)
    ap.add_argument("--engine", choices=["yandex", "yandex3", "edge"], default="yandex3")
    ap.add_argument("--voice", default="anton")
    ap.add_argument("--role", default="good")
    ap.add_argument("--stamp", default="\u0416\u0415\u0421\u0422\u042c")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--chromium-path", default=None)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
