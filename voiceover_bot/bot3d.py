"""
Телеграм-бот для 3D-роликов: шлёшь картинку вопроса и сценарий - получаешь видео.

Запуск (PowerShell, ноут должен быть включён, пока бот работает):
    $env:BOT_TOKEN = "токен от @BotFather"
    $env:YANDEX_SPEED = "1.15"
    python bot3d.py

Как пользоваться:
    1) отправь боту картинку вопроса (как фото или как файл);
    2) отправь сценарий - текстом или .txt файлом (образец: /obrazec);
    3) жди: бот пишет прогресс и присылает готовое видео.
Картинку со сценарием можно отправить и одним сообщением: сценарий в подписи к фото.
"""
import asyncio
import logging
import os
import re
import sys
import tempfile
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

import avto_scenariy

HERE = Path(__file__).resolve().parent
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OWNER = os.environ.get("BOT_OWNER", "").strip()           # id пользователя; пусто - бот отвечает всем
ENGINE = os.environ.get("BOT3D_ENGINE", "yandex3")
VOICE = os.environ.get("BOT3D_VOICE", "anton")
OUT_DIR = Path(os.environ.get("BOT3D_OUT", str(Path.home() / "Desktop" / "пдд тик ток")))
WORK = Path(tempfile.gettempdir()) / "bot3d"

logging.basicConfig(level=logging.INFO)
dp = Dispatcher()
CARDS: dict[int, Path] = {}                              # user -> последняя картинка
HINTS: dict[int, str] = {}                               # user -> подпись к фото ("ответ 1" и т.п.)
LOCK = asyncio.Lock()                                    # один рендер за раз


def allowed(m: Message) -> bool:
    return not OWNER or str(m.from_user.id) == OWNER


@dp.message(Command("start", "help"))
async def cmd_start(m: Message) -> None:
    await m.answer(
        "Привет! Делаю 3D-ролики по вопросам ПДД.\n\n"
        "1. Пришли картинку вопроса.\n"
        "2. Пришли сценарий - текстом или .txt файлом.\n"
        "3. Жди видео (10-20 минут).\n\n"
        "Можно прислать только картинку - текст придумаю сам.\nОбразец сценария: /obrazec\n"
        f"Твой id: {m.from_user.id}")


@dp.message(Command("obrazec"))
async def cmd_sample(m: Message) -> None:
    f = HERE / "scenariy15.txt"
    if f.exists():
        await m.answer_document(FSInputFile(f), caption="Образец: скопируй, поменяй под свой вопрос и пришли обратно.")
    else:
        await m.answer("Файл scenariy15.txt не найден рядом с ботом.")


async def save_file(bot: Bot, file_id: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    await bot.download(file_id, destination=dest)
    return dest


@dp.message(F.photo)
async def on_photo(m: Message, bot: Bot) -> None:
    if not allowed(m):
        return
    p = await save_file(bot, m.photo[-1].file_id, WORK / f"card_{m.from_user.id}.jpg")
    CARDS[m.from_user.id] = p
    if m.caption and len(m.caption) > 40:
        await run_job(m, bot, m.caption)
    else:
        await ask_mode(m, m.caption or "")


async def ask_mode(m: Message, hint: str) -> None:
    HINTS[m.from_user.id] = hint
    if not avto_scenariy.provider():
        await m.answer("Картинку принял. Теперь пришли сценарий (текстом или .txt).")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🤖 Придумай сам", callback_data="auto"),
        InlineKeyboardButton(text="✍️ Пришлю свой текст", callback_data="own")]])
    await m.answer("Картинку принял. Текст придумать самому или пришлёшь свой?\n"
                   "(подсказка: в подписи к фото можно написать «ответ 1», чтобы я точно не ошибся)", reply_markup=kb)


@dp.callback_query(F.data == "own")
async def cb_own(cb: CallbackQuery) -> None:
    await cb.answer()
    await cb.message.answer("Жду сценарий - текстом или .txt файлом. Образец: /obrazec")


@dp.callback_query(F.data == "auto")
async def cb_auto(cb: CallbackQuery, bot: Bot) -> None:
    await cb.answer()
    uid = cb.from_user.id
    if OWNER and str(uid) != OWNER:
        return
    if uid not in CARDS:
        await cb.message.answer("Сначала пришли картинку.")
        return
    await cb.message.edit_reply_markup(reply_markup=None)
    await cb.message.answer("Смотрю картинку и пишу сценарий (до минуты)...")
    try:
        text = await asyncio.to_thread(avto_scenariy.make, str(CARDS[uid]), HINTS.get(uid, ""))
    except Exception as e:
        await cb.message.answer(f"ИИ не смог: {e}\nПришли сценарий сам (/obrazec).")
        return
    await cb.message.answer_document(BufferedInputFile(text.encode("utf-8"), filename="scenariy.txt"),
                                     caption="Вот сценарий. Собираю видео. Если что-то не так - поправь файл и пришли мне.")
    await run_job(cb.message, bot, text, uid=uid)


@dp.message(F.document)
async def on_document(m: Message, bot: Bot) -> None:
    if not allowed(m):
        return
    name = (m.document.file_name or "").lower()
    if name.endswith((".png", ".jpg", ".jpeg", ".webp")):
        p = await save_file(bot, m.document.file_id, WORK / f"card_{m.from_user.id}{Path(name).suffix}")
        CARDS[m.from_user.id] = p
        await ask_mode(m, m.caption or "")
    elif name.endswith(".txt"):
        p = await save_file(bot, m.document.file_id, WORK / f"scen_{m.from_user.id}.txt")
        raw = p.read_bytes()
        for enc in ("utf-8-sig", "cp1251"):
            try:
                text = raw.decode(enc); break
            except UnicodeDecodeError:
                continue
        await run_job(m, bot, text)
    else:
        await m.answer("Жду картинку (png/jpg/webp) или сценарий (.txt).")


@dp.message(F.text)
async def on_text(m: Message, bot: Bot) -> None:
    if not allowed(m) or m.text.startswith("/"):
        return
    if "---" not in m.text and "Вопрос:" not in m.text:
        await m.answer("Это не похоже на сценарий. Образец: /obrazec")
        return
    await run_job(m, bot, m.text)


async def run_job(m: Message, bot: Bot, scenario: str, uid: int | None = None) -> None:
    uid = uid or m.from_user.id
    card = CARDS.get(uid)
    if not card:
        await m.answer("Сначала пришли картинку вопроса, потом сценарий.")
        return
    if LOCK.locked():
        await m.answer("Сейчас собираю другое видео, твоё встанет следующим.")
    async with LOCK:
        stamp = time.strftime("%m%d_%H%M")
        scen = WORK / f"scen_{uid}_{stamp}.txt"
        scen.write_text(scenario, encoding="utf-8")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out = OUT_DIR / f"3d_{stamp}.mp4"
        status = await m.answer("Начал. Озвучиваю...")
        cmd = [sys.executable, "-u", str(HERE / "blog3d.py"), "--card", str(card), "--scenario", str(scen),
               "--out", str(out), "--engine", ENGINE, "--voice", VOICE]
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(HERE), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT,
                                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        tail, last_edit = [], 0.0
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            s = line.decode("utf-8", "replace").rstrip()
            tail = (tail + [s])[-15:]
            mf = re.search(r"frame (\d+)/(\d+)", s)
            note = None
            if mf:
                note = f"Рендер: {int(mf.group(1)) * 100 // int(mf.group(2))}%"
            elif s.startswith("Duration"):
                note = "Озвучка готова, рендерю кадры..."
            elif s.startswith("Encoding"):
                note = "Склеиваю видео..."
            elif s.startswith("!"):
                await m.answer("Внимание: " + s[1:].strip())
            if note and time.time() - last_edit > 8:
                last_edit = time.time()
                try:
                    await status.edit_text(note)
                except Exception:
                    pass
        await proc.wait()
        if proc.returncode != 0 or not out.exists():
            err = "\n".join(tail)[-3000:]
            await m.answer(f"Не получилось :(\n\n{err}")
            return
        size = out.stat().st_size
        if size > 48 * 1024 * 1024:
            await m.answer(f"Видео готово, но оно больше 50 МБ - Телеграм не даст отправить.\nЛежит тут: {out}")
            return
        await status.edit_text("Готово, отправляю...")
        await m.answer_video(FSInputFile(out), caption=f"Готово! Копия на ноуте: {out.name}", supports_streaming=True)


async def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit('Нет токена. Сначала: $env:BOT_TOKEN = "токен от @BotFather"')
    WORK.mkdir(parents=True, exist_ok=True)
    bot = Bot(BOT_TOKEN)
    print("Бот запущен. Не закрывай это окно.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
