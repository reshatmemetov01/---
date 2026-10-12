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
import json
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

SETTINGS = HERE / "bot3d_settings.json"
VOICES = ["anton", "ermil", "zahar", "alexander", "kirill", "filipp"]
SAMPLE = "Смотри: грузовик едет по главной, а мы на второстепенной. Кто на главной, тот и король! Так что ждём."

logging.basicConfig(level=logging.INFO)


def settings() -> dict:
    try:
        return json.loads(SETTINGS.read_text(encoding="utf-8"))
    except Exception:
        return {"voice": VOICE, "speed": os.environ.get("YANDEX_SPEED", "1.15")}


def role_for(v: str) -> str:
    return "" if v == "filipp" else "good"
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
        "Можно прислать только картинку - текст придумаю сам.\nОбразец сценария: /obrazec\nГолоса: /golosa\n"
        f"Твой id: {m.from_user.id}")


@dp.message(Command("golosa"))
async def cmd_voices(m: Message) -> None:
    if not allowed(m):
        return
    parts = (m.text or "").split()
    speed = parts[1] if len(parts) > 1 else "1.35"
    await m.answer(f"Делаю образцы голосов на скорости {speed}...")
    from auto_quiz import synth_yandex_v3
    key = os.environ.get("YANDEX_API_KEY", "")
    for v in VOICES:
        def run():
            os.environ["YANDEX_SPEED"] = speed
            return synth_yandex_v3(SAMPLE, v, key, role_for(v), retries=2)
        try:
            data = await asyncio.to_thread(run)
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"✅ Выбрать {v}", callback_data=f"setv:{v}:{speed}")]])
            await m.answer_audio(BufferedInputFile(data, filename=f"{v}_{speed}.mp3"), caption=f"{v}, скорость {speed}", reply_markup=kb)
        except Exception as e:
            await m.answer(f"{v}: не получилось ({str(e)[:100]})")
    os.environ["YANDEX_SPEED"] = settings()["speed"]
    await m.answer("Жми «Выбрать» под понравившимся. Другая скорость: /golosa 1.5")


@dp.message(Command("golos"))
async def cmd_voice(m: Message) -> None:
    if not allowed(m):
        return
    parts = (m.text or "").split()
    cur = settings()
    if len(parts) >= 2 and parts[1] in VOICES:
        cur["voice"] = parts[1]
    if len(parts) >= 3:
        cur["speed"] = parts[2].replace(",", ".")
    SETTINGS.write_text(json.dumps(cur), encoding="utf-8")
    await m.answer(f"Голос для видео: {cur['voice']}, скорость {cur['speed']}.\nОбразцы: /golosa")


@dp.callback_query(F.data.startswith("setv:"))
async def cb_setv(cb: CallbackQuery) -> None:
    if OWNER and str(cb.from_user.id) != OWNER:
        return
    _, v, sp = cb.data.split(":")
    SETTINGS.write_text(json.dumps({"voice": v, "speed": sp}), encoding="utf-8")
    await cb.answer(f"Выбран {v}, {sp}")
    await cb.message.answer(f"Голос для видео: {v}, скорость {sp}.")


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
    cap = plain_text(m) if m.caption else ""
    if is_scenario(cap):
        await run_job(m, bot, cap)
    elif len(cap) > 120 and avto_scenariy.provider():
        await auto_job(m, bot, m.from_user.id, cap)
    else:
        await ask_mode(m, cap)


def is_scenario(t: str) -> bool:
    return bool(re.search(r"(?m)^\s*Голос\s*:", t)) and bool(re.search(r"(?m)^\s*(Вопрос|Мы)\s*:", t))


def plain_text(m: Message) -> str:
    """Text as the user typed it: Telegram eats *stars* and _underscores_ (shows italic/bold) - put them back."""
    text = m.text or m.caption or ""
    ents = m.entities or m.caption_entities or []
    marks = {"italic": "*", "bold": "*", "underline": "_", "strikethrough": "_"}
    u16 = text.encode("utf-16-le")
    ins = []
    for e in ents:
        mk = marks.get(e.type if isinstance(e.type, str) else e.type.value)
        if mk:
            ins.append((e.offset, mk)); ins.append((e.offset + e.length, mk))
    for off, mk in sorted(ins, key=lambda x: -x[0]):
        u16 = u16[:off * 2] + mk.encode("utf-16-le") + u16[off * 2:]
    return u16.decode("utf-16-le")


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
    await auto_job(cb.message, bot, uid, HINTS.get(uid, ""))


async def auto_job(msg: Message, bot: Bot, uid: int, hint: str) -> None:
    await msg.answer("Смотрю картинку" + (" и твой текст" if len(hint) > 120 else "") + ", пишу сценарий (до пары минут)...")
    try:
        text = await asyncio.to_thread(avto_scenariy.make, str(CARDS[uid]), hint)
    except Exception as e:
        await msg.answer(f"ИИ не смог: {str(e)[:300]}\nПопробуй через 5-10 минут или пришли сценарий сам (/obrazec).")
        return
    await msg.answer_document(BufferedInputFile(text.encode("utf-8"), filename="scenariy.txt"),
                              caption="Вот сценарий. Собираю видео. Если что-то не так - поправь файл и пришли мне.")
    await run_job(msg, bot, text, uid=uid)


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
    text = plain_text(m)
    if is_scenario(text):
        await run_job(m, bot, text)
    elif m.from_user.id in CARDS and avto_scenariy.provider() and len(m.text) > 60:
        await auto_job(m, bot, m.from_user.id, m.text)       # свой текст простыми словами -> ИИ раскладывает по кадрам
    elif m.from_user.id not in CARDS:
        await m.answer("Сначала пришли картинку вопроса, потом текст.")
    else:
        await m.answer("Это не похоже на сценарий. Образец: /obrazec")


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
               "--out", str(out), "--engine", ENGINE, "--voice", settings()["voice"], "--role", role_for(settings()["voice"])]
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(HERE), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT,
                                                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "YANDEX_SPEED": settings()["speed"]})
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
