import asyncio, re, sys
from pathlib import Path
from playwright.async_api import async_playwright
import auto_quiz
from auto_quiz import parse_questions
from make_video import read_text_any
from site_quiz import (BILET_URL, _prepare_page, _wait_question_ready,
    _read_hint_and_options, _click_answer, _find_green_option, _goto_next)

def norm(t):
    t = t.lower().replace("\u0451", "\u0435")
    return re.sub(r"[^0-9a-z\u0430-\u044f]+", "", t)


async def green(page, n):
    await _click_answer(page, 0)
    for w in (1.4, 1.2):
        await asyncio.sleep(w)
        g = await _find_green_option(page)
        if g >= 0:
            return g
    if n > 1:
        await _click_answer(page, 1)
        await asyncio.sleep(1.4)
        return await _find_green_option(page)
    return -1

async def check(b, local, out):
    exe = auto_quiz._find_chromium()
    kw = {"args": ["--no-sandbox"]}
    if exe:
        kw["executable_path"] = exe
    async with async_playwright() as pw:
        br = await pw.chromium.launch(**kw)
        page = await br.new_page(viewport={"width": 1280, "height": 720})
        await _prepare_page(page, BILET_URL.format(n=b))
        for i in range(20):
            await _wait_question_ready(page)
            info = await _read_hint_and_options(page)
            num = info["num"] or i + 1
            g = await green(page, len(info["opts"]))
            loc = local.get(num)
            if g < 0:
                out.append(f"bilet {b} q{num}: site green not found - CHECK BY EYE")
            elif loc is None:
                out.append(f"bilet {b} q{num}: no answer in file  <-- FIX")
            elif norm(info["opts"][g]) != norm(loc[0]):
                right = next((k + 1 for k, o in enumerate(loc[1]) if norm(o) == norm(info["opts"][g])), "?")
                out.append(f"bilet {b} q{num}: file={loc[2]+1} correct={right}  <-- FIX")
            if i < 19:
                cur = info["num"]
                await _goto_next(page)
                try:
                    await page.wait_for_function("(k)=>{const e=document.querySelector('.bilet__qs-num');return e&&parseInt(e.textContent)!==k;}", arg=cur, timeout=6000)
                except Exception:
                    pass
        await br.close()

async def main(a, z):
    out = []
    for b in range(a, z + 1):
        f = Path(f"bilet{b}.txt")
        if not f.exists():
            out.append(f"bilet {b}: no file")
            continue
        local = {q.number: (q.options[q.correct], q.options, q.correct)
                 for q in parse_questions(read_text_any(str(f))) if 0 <= q.correct < len(q.options)}
        print(f"checking bilet {b}...", flush=True)
        try:
            await check(b, local, out)
        except Exception as e:
            out.append(f"bilet {b}: error {e}")
    rep = "\n".join(out) or "ALL OK - no mismatches"
    print("\n" + rep)
    Path("proverka_result.txt").write_text(rep, encoding="utf-8")

asyncio.run(main(int(sys.argv[1]), int(sys.argv[2])))
