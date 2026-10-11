"""Проверка правильных ответов в билетах.

Проходит каждый билет на pdd-exam.ru, определяет верный ответ ПО ЗЕЛЁНОМУ
самого сайта (ждёт дольше и с повтором — надёжнее, чем при сборке), и
сверяет с тем, что записано в твоих файлах bilet<N>.txt.

Выдаёт:
  - РАСХОЖДЕНИЯ — где в файле ответ не совпал с сайтом (это и есть ошибки,
    их надо поправить и пересобрать билет);
  - НЕ ОПРЕДЕЛИЛ — где сайт не показал зелёный уверенно (проверь глазами).

Ничего не меняет сам. Результат печатает и пишет в proverka_result.txt.

Запуск:
    python proverka.py --from 1 --to 40
    python proverka.py --from 1 --to 40 --dir .          # где лежат bilet<N>.txt
"""
import argparse
import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

import auto_quiz
import site_quiz
from auto_quiz import parse_questions
from make_video import read_text_any
from site_quiz import (BILET_URL, _prepare_page, _wait_question_ready,
                       _read_hint_and_options, _click_answer,
                       _find_green_option, _goto_next, _correct_from_hint)


async def _reveal_green(page, nopts: int) -> int:
    """Возвращает индекс зелёного (верного) варианта. Кликает вариант, ждёт,
    читает зелёный; если не видно — ещё раз ждёт и пробует другой вариант.
    -1, если так и не определился."""
    # первый клик — по варианту 0
    await _click_answer(page, 0)
    for wait in (1.4, 1.2):
        await asyncio.sleep(wait)
        g = await _find_green_option(page)
        if g >= 0:
            return g
    # запасной клик по другому варианту (вдруг первый не зарегистрировался)
    if nopts > 1:
        await _click_answer(page, 1)
        await asyncio.sleep(1.4)
        g = await _find_green_option(page)
        if g >= 0:
            return g
    return -1


async def check_bilet(n: int, local_correct: dict[int, int],
                      executable_path: str | None, width: int, height: int):
    """Проходит билет n на сайте, сверяет с local_correct {номер: индекс}.
    Возвращает (mismatches, uncertain) — списки строк."""
    exe = executable_path or auto_quiz._find_chromium()
    launch_kw = {"args": ["--no-sandbox"]}
    if exe:
        launch_kw["executable_path"] = exe

    mism, uncert = [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**launch_kw)
        ctx = await browser.new_context(viewport={"width": width, "height": height})
        page = await ctx.new_page()
        await _prepare_page(page, BILET_URL.format(n=n))
        for i in range(20):
            await _wait_question_ready(page)
            info = await _read_hint_and_options(page)
            num = info["num"] or (i + 1)
            nopts = len(info["opts"])
            site_green = await _reveal_green(page, nopts)
            hint_guess = _correct_from_hint(info["hint"], info["opts"])

            loc = local_correct.get(num)
            if site_green < 0:
                # сайт не показал зелёный — сверяем с подсказкой, если есть
                if hint_guess >= 0 and loc is not None and hint_guess != loc:
                    uncert.append(f"  Билет {n}, вопрос {num}: в файле №{loc+1}, "
                                  f"по пояснению №{hint_guess+1} (зелёный не определился — ПРОВЕРЬ)")
                else:
                    uncert.append(f"  Билет {n}, вопрос {num}: зелёный не определился — ПРОВЕРЬ глазами")
            elif loc is None:
                uncert.append(f"  Билет {n}, вопрос {num}: в файле нет этого вопроса (сайт: №{site_green+1})")
            elif site_green != loc:
                mism.append(f"  Билет {n}, вопрос {num}: в файле №{loc+1}, "
                            f"на сайте верный №{site_green+1}  <-- ИСПРАВИТЬ")

            if i < 19:
                cur = info["num"]
                await _goto_next(page)
                try:
                    await page.wait_for_function(
                        "(k)=>{const e=document.querySelector('.bilet__qs-num');"
                        "return e && parseInt(e.textContent)!==k;}",
                        arg=cur, timeout=6000)
                except Exception:
                    pass
        await ctx.close()
        await browser.close()
    return mism, uncert


def load_local(path: Path) -> dict[int, int]:
    """{номер вопроса: индекс правильного (0-based)} из bilet<N>.txt."""
    out = {}
    for q in parse_questions(read_text_any(str(path))):
        if 0 <= q.correct < len(q.options):
            out[q.number] = q.correct
    return out


async def main_async(args):
    all_mism, all_uncert, missing = [], [], []
    for n in range(args.from_n, args.to_n + 1):
        f = Path(args.dir) / f"bilet{n}.txt"
        if not f.exists():
            missing.append(n)
            continue
        local = load_local(f)
        print(f"== Проверяю билет {n} ({len(local)} вопросов в файле)…", flush=True)
        try:
            m, u = await check_bilet(n, local, args.chromium_path, args.width, args.height)
        except Exception as e:  # noqa: BLE001
            all_uncert.append(f"  Билет {n}: не смог проверить ({e})")
            continue
        all_mism += m
        all_uncert += u

    lines = []
    lines.append("================ РАСХОЖДЕНИЯ (ошибки, исправить) ================")
    lines += all_mism if all_mism else ["  нет — все совпали"]
    lines.append("")
    lines.append("================ НЕ ОПРЕДЕЛИЛ (проверь глазами) ================")
    lines += all_uncert if all_uncert else ["  нет"]
    if missing:
        lines.append("")
        lines.append("Файлы не найдены для билетов: " + ", ".join(map(str, missing)))

    report = "\n".join(lines)
    print("\n" + report)
    Path("proverka_result.txt").write_text(report, encoding="utf-8")
    print("\n💾 Отчёт сохранён: proverka_result.txt")
    if all_mism:
        print(f"⚠️  Найдено расхождений: {len(all_mism)}. Поправь «Ответ:» в этих "
              "bilet<N>.txt и пересобери только эти билеты.")


def main():
    ap = argparse.ArgumentParser(description="Сверка правильных ответов билетов с сайтом")
    ap.add_argument("--from", dest="from_n", type=int, default=1)
    ap.add_argument("--to", dest="to_n", type=int, default=40)
    ap.add_argument("--dir", default=".", help="папка с bilet<N>.txt (по умолчанию текущая)")
    ap.add_argument("--chromium-path", default=None)
    ap.add_argument("--width", type=int, default=1280)
    ap.add_argument("--height", type=int, default=720)
    args = ap.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
