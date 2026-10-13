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
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
                           KeyboardButton, Message, ReplyKeyboardMarkup)

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


PROMO = HERE / "promo.jpg"
PROMO_DEFAULT = HERE / "promo_default.jpg"
PROMO_TEXT = ("Кстати! Найди в Телеграме бота «ПДД Эльдархан бот» - он у меня в профиле. "
              "Там я разобрал все 800 вопросов простым и понятным языком.")
AWAIT_PROMO: set[int] = set()


def settings() -> dict:
    cur = {"voice": VOICE, "speed": os.environ.get("YANDEX_SPEED", "1.15"), "promo": True, "promo_text": PROMO_TEXT}
    try:
        cur.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cur


def save_settings(**kw) -> dict:
    cur = settings(); cur.update(kw)
    SETTINGS.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
    return cur


def role_for(v: str) -> str:
    return "" if v == "filipp" else "good"
dp = Dispatcher()
CARDS: dict[int, Path] = {}                              # user -> последняя картинка
HINTS: dict[int, str] = {}                               # user -> подпись к фото ("ответ 1" и т.п.)
LOCK = asyncio.Lock()                                    # один рендер за раз


def allowed(m: Message) -> bool:
    return not OWNER or str(m.from_user.id) == OWNER


BTN_MENU, BTN_NEW, BTN_VOICE, BTN_PROMO, BTN_DUB = "🏠 Меню", "🎬 Новое видео", "🎙 Голос и скорость", "📣 Реклама", "🗣 Озвучить моё видео"
KB = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=BTN_NEW), KeyboardButton(text=BTN_MENU)],
                                   [KeyboardButton(text=BTN_VOICE), KeyboardButton(text=BTN_PROMO)],
                                   [KeyboardButton(text=BTN_DUB)]], resize_keyboard=True)


def menu_text() -> str:
    st = settings()
    return ("🏠 <b>Меню</b>\n\n"
            "<b>Как сделать видео:</b>\n"
            "1. Пришли картинку вопроса.\n"
            "2. Пришли сценарий (текстом или .txt) - или нажми «Придумай сам».\n"
            "3. Жди видео 10-20 минут.\n\n"
            f"🎙 Голос: <b>{st['voice']}</b>, скорость <b>{st['speed']}</b>\n"
            f"📣 Реклама канала: <b>{'включена' if st.get('promo') else 'выключена'}</b>")


def menu_kb() -> InlineKeyboardMarkup:
    st = settings()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎙 Голос и скорость", callback_data="m:voice")],
        [InlineKeyboardButton(text=("📣 Выключить рекламу" if st.get("promo") else "📣 Включить рекламу"), callback_data="m:promo")],
        [InlineKeyboardButton(text="🖼 Новый скрин профиля", callback_data="m:promoimg")],
        [InlineKeyboardButton(text="📄 Образец сценария", callback_data="m:obrazec")]])


def voice_kb() -> InlineKeyboardMarkup:
    st = settings()
    rows = [[InlineKeyboardButton(text=("✅ " if st["voice"] == v else "") + v, callback_data=f"setv:{v}:{st['speed']}") for v in VOICES[i:i + 3]]
            for i in range(0, len(VOICES), 3)]
    rows.append([InlineKeyboardButton(text=("✅ " if st["voice"] == "custom" else "") + "🎤 Свой голос (озвучу на сайте сам)",
                                      callback_data=f"setv:custom:{st['speed']}")])
    rows.append([InlineKeyboardButton(text=("✅ " if st["speed"] == sp else "") + sp, callback_data=f"setv:{st['voice']}:{sp}")
                 for sp in ["1.15", "1.25", "1.35", "1.4", "1.5"]])
    rows.append([InlineKeyboardButton(text="🔊 Послушать все голоса", callback_data="m:samples"),
                 InlineKeyboardButton(text="🏠 Меню", callback_data="m:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def show_menu(m: Message) -> None:
    await m.answer("Кнопки снизу 👇", reply_markup=KB)
    await m.answer(menu_text(), reply_markup=menu_kb(), parse_mode="HTML")


@dp.message(Command("start", "help", "menu"))
async def cmd_start(m: Message) -> None:
    if not allowed(m):
        await m.answer(f"Твой id: {m.from_user.id}")
        return
    await show_menu(m)


@dp.message(F.text.in_({BTN_MENU, BTN_NEW, BTN_VOICE, BTN_PROMO, BTN_DUB}))
async def on_button(m: Message) -> None:
    if not allowed(m):
        return
    if m.text == BTN_MENU:
        DUB.pop(m.from_user.id, None)
        await show_menu(m)
    elif m.text == BTN_DUB:
        await cmd_dub(m)
    elif m.text == BTN_NEW:
        CARDS.pop(m.from_user.id, None)
        await m.answer("Ок, начинаем новое видео. Пришли картинку вопроса 📷")
    elif m.text == BTN_VOICE:
        await m.answer("Выбери голос и скорость:", reply_markup=voice_kb())
    elif m.text == BTN_PROMO:
        st = settings()
        await m.answer(f"📣 Реклама: {'включена' if st.get('promo') else 'выключена'}\nАнтон говорит:\n«{st['promo_text']}»\n\n"
                       "Поменять текст: /reklama текст Твой текст", reply_markup=menu_kb())


@dp.callback_query(F.data.startswith("m:"))
async def cb_menu(cb: CallbackQuery) -> None:
    if OWNER and str(cb.from_user.id) != OWNER:
        return
    act = cb.data[2:]
    await cb.answer()
    if act == "menu":
        await cb.message.answer(menu_text(), reply_markup=menu_kb(), parse_mode="HTML")
    elif act == "voice":
        await cb.message.answer("Выбери голос и скорость:", reply_markup=voice_kb())
    elif act == "promo":
        save_settings(promo=not settings().get("promo"))
        await cb.message.edit_text(menu_text(), reply_markup=menu_kb(), parse_mode="HTML")
    elif act == "promoimg":
        AWAIT_PROMO.add(cb.from_user.id)
        await cb.message.answer("Пришли новый скрин шапки профиля 📷")
    elif act == "obrazec":
        f = HERE / "scenariy15.txt"
        await cb.message.answer_document(FSInputFile(f), caption="Образец сценария")
    elif act == "samples":
        await cb.message.answer("Напиши /golosa 1.4 - пришлю образцы всех голосов на этой скорости.")


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
    save_settings(voice=cur["voice"], speed=cur["speed"])
    await m.answer(f"Голос для видео: {cur['voice']}, скорость {cur['speed']}.\nОбразцы: /golosa")


@dp.callback_query(F.data.startswith("setv:"))
async def cb_setv(cb: CallbackQuery) -> None:
    if OWNER and str(cb.from_user.id) != OWNER:
        return
    _, v, sp = cb.data.split(":")
    save_settings(voice=v, speed=sp)
    await cb.answer(f"Выбран {v}, {sp}")
    if cb.message.text:                       # the voice menu: just tick the new choice
        try:
            await cb.message.edit_reply_markup(reply_markup=voice_kb())
        except Exception:
            pass
    else:                                     # a voice sample
        await cb.message.answer(f"Голос для видео: {v}, скорость {sp}.")


@dp.message(Command("reklama"))
async def cmd_promo(m: Message) -> None:
    if not allowed(m):
        return
    arg = (m.text or "").split(maxsplit=1)[1:] or [""]
    a = arg[0].strip()
    if a.lower() in ("выкл", "off", "нет"):
        save_settings(promo=False)
        await m.answer("Реклама канала выключена. Включить: /reklama вкл")
    elif a.lower() in ("вкл", "on", "да"):
        save_settings(promo=True)
        await m.answer("Реклама канала включена." + ("" if PROMO.exists() else " Но скрина ещё нет - пришли /reklama и потом скрин."))
    elif a.lower().startswith("текст"):
        t = a[5:].strip(" :")
        if t:
            save_settings(promo_text=t)
        await m.answer(f"Текст рекламы:\n{settings()['promo_text']}")
    else:
        AWAIT_PROMO.add(m.from_user.id)
        await m.answer("Пришли скрин шапки профиля (где ссылка на канал). Он будет в середине каждого ролика.\n\n"
                       f"Сейчас Антон говорит:\n«{settings()['promo_text']}»\n\n"
                       "Поменять текст: /reklama текст Твой текст\nВыключить: /reklama выкл")


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
    if m.from_user.id in AWAIT_PROMO:
        AWAIT_PROMO.discard(m.from_user.id)
        await save_file(bot, m.photo[-1].file_id, PROMO)
        save_settings(promo=True)
        await m.answer("Скрин профиля сохранил ✅ Теперь в середине каждого ролика будет реклама канала.")
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


# ---------------------------------------------------------------- dubbing: your video + text -> Anton's voice
DUB: dict[int, dict] = {}            # uid -> {"video": Path | None}


@dp.message(Command("ozvuchka"))
async def cmd_dub(m: Message) -> None:
    if not allowed(m):
        return
    DUB[m.from_user.id] = {"video": None}
    sp = settings()["speed"]
    await m.answer(f"🎙 Озвучка твоего видео голосом {settings()['voice']} (скорость {sp}).\n"
                   "1. Пришли видео.\n2. Потом пришли текст, который надо сказать.\n"
                   "Другая скорость: напиши в конце текста, например «скорость 1.3».")


async def got_dub_video(m: Message, bot: Bot, file_id: str) -> None:
    p = await save_file(bot, file_id, WORK / f"dub_{m.from_user.id}.mp4")
    DUB[m.from_user.id] = {"video": p}
    await m.answer("Видео принял ✅ Теперь пришли текст для озвучки.")


@dp.message(F.video)
async def on_video(m: Message, bot: Bot) -> None:
    if not allowed(m):
        return
    if m.from_user.id not in DUB:
        await m.answer("Чтобы озвучить видео голосом Антона - нажми /ozvuchka, потом пришли видео.")
        return
    await got_dub_video(m, bot, m.video.file_id)


async def do_dub(m: Message, text: str) -> None:
    from auto_quiz import FFMPEG, media_duration, synth_yandex_v3
    st = settings()
    sp = st["speed"]
    ms = re.search(r"скорость\s*([0-9]+[.,][0-9]+)\s*$", text, re.I)
    if ms:
        sp = ms.group(1).replace(",", "."); text = text[:ms.start()].strip()
    video = DUB.pop(m.from_user.id)["video"]
    WORK.mkdir(parents=True, exist_ok=True)
    msg = await m.answer("Озвучиваю...")
    voice = WORK / f"dub_{m.from_user.id}.mp3"
    out = WORK / f"dub_{m.from_user.id}_out.mp4"

    def run():
        os.environ["YANDEX_SPEED"] = sp
        voice.write_bytes(synth_yandex_v3(text, st["voice"], os.environ.get("YANDEX_API_KEY", ""), role_for(st["voice"])))
        os.environ["YANDEX_SPEED"] = settings()["speed"]
        vd, ad = media_duration(str(video)), media_duration(str(voice))
        pad = f"tpad=stop_mode=clone:stop_duration={ad - vd + 0.5:.2f}," if ad + 0.3 > vd else ""
        cmd = [FFMPEG, "-y", "-i", str(video), "-i", str(voice), "-map", "0:v:0", "-map", "1:a:0",
               "-vf", pad + "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=30", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-t", f"{max(vd, ad + 0.5):.2f}", "-movflags", "+faststart", str(out)]
        subprocess.run(cmd, check=True, capture_output=True, timeout=900)
        return vd, ad

    try:
        vd, ad = await asyncio.to_thread(run)
    except Exception as e:
        await msg.edit_text(f"Не получилось: {str(e)[:300]}")
        return
    note = f"Голос {ad:.1f} с, видео {vd:.1f} с" + (" - видео продлил стоп-кадром." if ad + 0.3 > vd else ".")
    await m.answer_video(FSInputFile(out), caption=f"Готово ✅ {note}", supports_streaming=True)
    await m.answer_audio(FSInputFile(voice), caption="Отдельно голос (mp3), если захочешь наложить сам")


async def got_voice_audio(m: Message, bot: Bot, file_id: str, ext: str) -> None:
    job = PENDING_VOICE.pop(m.from_user.id)
    src = await save_file(bot, file_id, WORK / f"myvoice_{m.from_user.id}{ext}")
    import razrezat
    vdir = WORK / f"myvoice_{m.from_user.id}"
    try:
        lens = await asyncio.to_thread(razrezat.split_by_pauses, src, job["n"], vdir)
    except Exception as e:
        PENDING_VOICE[m.from_user.id] = job
        await m.answer(f"Не смог разрезать на {job['n']} фраз: {e}\nПришли аудио ещё раз.")
        return
    await m.answer(f"Разрезал на {len(lens)} фраз ✅ Собираю видео.")
    await run_job(m, bot, "", voice_dir=vdir, cmd_ready=job["cmd"], out_ready=job["out"])


@dp.message(F.audio | F.voice)
async def on_audio(m: Message, bot: Bot) -> None:
    if not allowed(m):
        return
    if m.from_user.id not in PENDING_VOICE:
        await m.answer("Аудио я жду только после сценария, когда выбран «🎤 Свой голос».")
        return
    fid = m.audio.file_id if m.audio else m.voice.file_id
    await got_voice_audio(m, bot, fid, ".mp3" if m.audio else ".ogg")


@dp.message(F.document)
async def on_document(m: Message, bot: Bot) -> None:
    if not allowed(m):
        return
    name = (m.document.file_name or "").lower()
    if m.from_user.id in PENDING_VOICE and name.endswith((".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac")):
        await got_voice_audio(m, bot, m.document.file_id, Path(name).suffix)
        return
    if m.from_user.id in DUB and name.endswith((".mp4", ".mov", ".m4v", ".webm")):
        await got_dub_video(m, bot, m.document.file_id)
        return
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
    if m.from_user.id in DUB:
        if DUB[m.from_user.id]["video"] is None:
            await m.answer("Сначала пришли видео, потом текст.")
        else:
            await do_dub(m, text)
        return
    if is_scenario(text):
        await run_job(m, bot, text)
    elif m.from_user.id in CARDS and avto_scenariy.provider() and len(m.text) > 60:
        await auto_job(m, bot, m.from_user.id, m.text)       # свой текст простыми словами -> ИИ раскладывает по кадрам
    elif m.from_user.id not in CARDS:
        await m.answer("Сначала пришли картинку вопроса, потом текст.")
    else:
        await m.answer("Это не похоже на сценарий. Образец: /obrazec")


PENDING_VOICE: dict[int, dict] = {}       # uid -> job waiting for a voice-over recorded on another site


async def run_job(m: Message, bot: Bot, scenario: str, uid: int | None = None, voice_dir: Path | None = None,
                  cmd_ready: list | None = None, out_ready: Path | None = None) -> None:
    uid = uid or m.from_user.id
    card = CARDS.get(uid)
    if not card:
        await m.answer("Сначала пришли картинку вопроса, потом сценарий.")
        return
    if LOCK.locked():
        await m.answer("Сейчас собираю другое видео, твоё встанет следующим.")
    async with LOCK:
        import otvety
        scenario, warn = otvety.check_scenario(scenario)
        if warn:
            await m.answer(warn)
        if cmd_ready:
            cmd, out = cmd_ready + ["--voice-dir", str(voice_dir)], out_ready
        else:
            stamp = time.strftime("%m%d_%H%M")
            scen = WORK / f"scen_{uid}_{stamp}.txt"
            scen.write_text(scenario, encoding="utf-8")
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            out = OUT_DIR / f"3d_{stamp}.mp4"
            cmd = [sys.executable, "-u", str(HERE / "blog3d.py"), "--card", str(card), "--scenario", str(scen),
                   "--out", str(out), "--engine", ENGINE, "--voice", settings()["voice"], "--role", role_for(settings()["voice"])]
            pimg = PROMO if PROMO.exists() else PROMO_DEFAULT
            if settings().get("promo") and pimg.exists():
                cmd += ["--promo", str(pimg), "--promo-text", settings()["promo_text"]]
            if settings()["voice"] == "custom":         # voice-over from another site: give the text, wait for the audio
                r = await asyncio.to_thread(subprocess.run, cmd + ["--dump-lines"], cwd=str(HERE), capture_output=True, text=True,
                                            env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=600)
                js = next((l[11:] for l in r.stdout.splitlines() if l.startswith("LINES_JSON ")), None)
                if not js:
                    await m.answer("Не смог разобрать сценарий:\n" + (r.stdout + r.stderr)[-1500:])
                    return
                lines = json.loads(js)
                PENDING_VOICE[uid] = {"cmd": cmd, "out": out, "n": len(lines)}
                text = "\n\n".join(lines)
                await m.answer_document(BufferedInputFile(text.encode("utf-8"), filename="ozvuchka.txt"),
                                        caption=(f"🎤 Озвучь этот текст на своём сайте ОДНИМ файлом ({len(lines)} абзацев).\n"
                                                 "Между абзацами должны быть паузы - не склеивай их. Потом пришли мне аудио (mp3/wav)."))
                if len(text) < 3800:
                    await m.answer(text)
                return
        status = await m.answer("Начал. Озвучиваю..." if not voice_dir else "Начал. Режу твою озвучку и рендерю...")
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(HERE), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT,
                                                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "YANDEX_SPEED": settings()["speed"]})
        tail, last_edit, t_start, voiced = [], 0.0, time.time(), 0
        while True:
            try:                                              # nothing printed for 20 min = hung -> stop it
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=1800)
            except asyncio.TimeoutError:
                proc.kill()
                tail.append("Сборка зависла (30 минут без прогресса) и была остановлена. Попробуй ещё раз.")
                break
            if not line:
                break
            s = line.decode("utf-8", "replace").rstrip()
            print("[blog3d]", s, flush=True)                 # visible in journalctl -u bot3d
            tail = (tail + [s])[-15:]
            mins = int((time.time() - t_start) // 60)
            mf = re.search(r"frame (\d+)/(\d+)\s+(\d+)s", s)
            note = None
            if mf:
                done, total, secs = int(mf.group(1)), int(mf.group(2)), int(mf.group(3))
                eta = f", осталось ~{int(secs / done * (total - done) / 60) + 1} мин" if done else ""
                note = f"Рендер: {done * 100 // total}%{eta}"
            elif "озвучиваю" in s.lower() or "кэша" in s.lower():
                voiced += 1
                note = f"Озвучиваю... ({voiced} фраз, {mins} мин)"
            elif s.startswith("Duration"):
                note = "Озвучка готова, рендерю кадры..."
            elif s.startswith("Encoding"):
                note = "Склеиваю видео..."
            elif s.startswith("!"):
                await m.answer("Внимание: " + s[1:].strip())
            elif "⚠" in s or "Error" in s or "ошибк" in s.lower():
                note = f"⚠️ {s.strip()[:150]}"
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
