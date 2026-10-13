"""avto_scenariy: ИИ смотрит на картинку вопроса и сам пишет сценарий для blog3d.

python avto_scenariy.py КАРТИНКА [--hint "ответ 1"] [--out scen.txt]

Ключ (один из, задаётся переменными окружения):
  ANTHROPIC_API_KEY              - Claude напрямую (console.anthropic.com)
  OPENROUTER_API_KEY             - через openrouter.ai (Claude/GPT, одна оплата)
  AI_BASE_URL + AI_API_KEY       - любой OpenAI-совместимый сервис
AI_MODEL - имя модели (по умолчанию подходящее для выбранного сервиса).
"""
import argparse, base64, io, json, os, re, sys, time, urllib.error, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

RULES = r"""
Ты - автор коротких вертикальных роликов-разборов билетов ПДД (категория B) в стиле популярного ТикТок-блогера.
Тебе дают картинку вопроса из билета: фото с места водителя, текст вопроса и варианты ответа.
Твоя задача - написать СЦЕНАРИЙ 3D-ролика в строгом формате ниже. Движок сам строит 3D-перекрёсток по сценарию.

КАК ЧИТАТЬ КАРТИНКУ (это важно):
- Фото снято из НАШЕЙ машины. Мы всегда едем с ЮГА на север (вверх). Мы = актёр "Мы", сторона "юг".
- То, что впереди нас (встречные) - "север". Дорога справа - "восток". Дорога слева - "запад".
- Машина справа от перекрёстка, которая смотрит на перекрёсток, едет с востока; слева - с запада.
- Зелёная стрелка на приборной панели / поворотник показывает, куда едем МЫ (налево/направо). Нет стрелки - прямо.
- Поворотник у другой машины: её левый бок для встречной - это наш правый край. Встречная, мигающая "налево", едет с севера и уходит на ВОСТОК.
- Знак на нашей стороне: жёлтый ромб = "главная", красный треугольник углом вниз = "уступи".
  Табличка под знаком (8.13) - толстая линия = главная дорога: верх таблички = север, низ = юг (наш подъезд), право = восток, лево = запад.
- Если таблички нет: при знаке "главная" главная идёт прямо юг-север; при "уступи" главная - поперечная запад-восток.

ЧТО УМЕЕТ ДВИЖОК (другого НЕ используй):
- Перекрёсток: "крест" (4 дороги), "т, нет <сторона>" (например "т, нет запада"), "кольцо" (круговое движение).
- Машины: любое имя одним словом; "Грузовик" (фургон-грузовик), "Самосвал"/"Пикап", "Автобус", "Трамвай" (с рельсами), "Такси", "Полиция", "Скорая",
  "Пожарная", "Фургон", "Газель", "Мусоровоз", "Трактор", "Внедорожник" рисуются своими моделями, остальное - легковой. "Мы" - наша (оранжевая).
  Строка: "Имя: <откуда>, <налево|направо|прямо|разворот>". Стороны: юг, север, восток, запад.
- Знаки (строк "Знак:" может быть несколько, у каждой дороги свой): "Знак: <главная|уступи|стоп|круг|односторонняя направо|односторонняя налево>, <сторона>". Синий знак одностороннего движения вешается над другим знаком той же стороны.
  "круг" = знак кругового движения. Знак ставится на подъезде с этой стороны. Нет знаков - строку "Знак" не пиши.
- "Главная: <сторона>, <сторона>" - две стороны, между которыми идёт главная дорога (если она поворачивает - сама появится табличка 8.13).
  Перекрёсток равнозначный или регулируемый - строку не пиши.
- "Светофор: <цвет>" - цвет для нас (юг) и встречных (север), поперечные получат противоположный. Цвета: зелёный, красный, жёлтый,
  мигает (мигающий жёлтый), выключен. Можно по сторонам: "Светофор: юг зелёный, север зелёный, восток красный, запад красный".
  При светофоре сами рисуются зебры и пешеходные светофоры.
- "Пешеходы: <сторона>, <сколько>" - пешеходы переходят дорогу с этой стороны перекрёстка (по зебре). Добавь "идут", если уже на переходе.
  В "Очередь" они называются "пешеходы".
- "Очередь: имя, имя, ..." - в каком порядке участники проезжают/проходят по правилам (в самом конце ролика). Включи ВСЕХ, в т.ч. "мы".
Разметки, ж/д переездов, регулировщика движок НЕ рисует - для таких вопросов построй ближайшую сцену, остальное объясняй голосом.

ФОРМАТ СЦЕНАРИЯ (строки "Ключ: значение", кадры разделяются строкой ---):
Тема: <3-4 слова КАПСОМ, о чём вопрос>
Хук: <одна цепляющая фраза, до 6 слов, без мата>
Вопрос: <текст вопроса с картинки дословно>
Вариант 1: <дословно с картинки>
Вариант 2: ...
(столько вариантов, сколько на картинке)
Ответ: <номер правильного>
Голос ответа: <"Правильный ответ первый/второй/третий." + короткая итоговая фраза, можно шутку про неправильный вариант>

Перекрёсток: ...
Мы: юг, ...
<Другие машины>
Знак: ...
Главная: ...
Очередь: ...

---
Голос: <1-2 коротких предложения, что говорит диктор>
Камера: <сверху | общий | знак | табличка | светофор | пешеходы | <имя машины>>
Надпись: <короткая плашка КАПСОМ, до 6 слов; *слово* = зелёным, _слово_ = красным>
Метка: <цель> | <маленькая подпись> | <крупная подпись КАПСОМ>
Стрелка: <имя машины>          (рисует её путь на дороге; остаётся до конца)
Пересечение: <имя>, <имя>       (красный "!" где пути пересекаются; нужны обе стрелки)
Главная                         (подсвечивает главную дорогу; только если есть строка "Главная")
Табличка на дорогу              (табличка 8.13 ложится на перекрёсток; только если табличка есть)
Галочка: <имя>                  (зелёная галочка над машиной - едет первой / права)
Крест: <имя>                    (красный крестик - ошибка / не едет)
Поехали                         (машины едут по "Очередь"; ставь ТОЛЬКО в последний кадр)

Цели для "Метка": имя машины (мы, автобус...), пешеходы, светофор, зебра, кольцо, знак, табличка, табличка.толстая, табличка.тонкая, главная, второстепенная.
В одном кадре 0-2 метки. Каждый кадр - одна мысль. Всего 6-9 кадров.

СТИЛЬ (как у блогера):
- Просто, по-разговорному, коротко. Как объясняешь другу. Лёгкий юмор и подколы ("кто на главной - тот и король", "стоять до пенсии"), БЕЗ мата и оскорблений.
- Логика разбора: куда едем мы (стрелка) -> какой знак/кто главный -> где кто стоит -> чьи пути пересекаются -> вывод -> поехали.
- Плашки "Надпись" - короткие выжимки сказанного, ключевые слова цветом. Субтитров нет, поэтому плашки важны.
- Числа и номера пунктов ПДД можно, но без занудства.

Выведи ТОЛЬКО сценарий, без пояснений и без ``` .
"""

EXAMPLE = (HERE / "scenariy15.txt").read_text(encoding="utf-8") if (HERE / "scenariy15.txt").exists() else ""


def _img_b64(path):
    from PIL import Image
    im = Image.open(path).convert("RGB")
    im.thumbnail((1400, 1400))
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def _post(url, headers, body, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ИИ ответил ошибкой {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")


def _chat_stream(url, key, body, timeout=90):
    """OpenAI-compatible chat with stream=True: long answers do not hit gateway timeouts; returns the text."""
    body = dict(body, stream=True)
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    out = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    j = json.loads(data)
                except ValueError:
                    continue
                if j.get("error"):
                    raise RuntimeError(f"ИИ ответил ошибкой {j['error'].get('code', '')}: {str(j['error'])[:300]}")
                for ch in j.get("choices") or []:
                    out.append((ch.get("delta") or {}).get("content") or "")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"ИИ ответил ошибкой {e.code}: {e.read().decode('utf-8', 'replace')[:500]}")
    except (TimeoutError, OSError) as e:
        raise RuntimeError(f"ИИ не ответил вовремя: {e}")
    return "".join(out)


def provider():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GITHUB_TOKEN") or os.environ.get("OPENROUTER_API_KEY") or (os.environ.get("AI_BASE_URL") and os.environ.get("AI_API_KEY")):
        return "openai"
    return None


def ask_ai(image_path, user_text):
    b64 = _img_b64(image_path)
    system = RULES + "\n\nПРИМЕР ГОТОВОГО СЦЕНАРИЯ (для другого вопроса, формат и стиль):\n" + EXAMPLE
    p = provider()
    if p == "anthropic":
        model = os.environ.get("AI_MODEL", "claude-sonnet-5-5")
        r = _post("https://api.anthropic.com/v1/messages",
                  {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
                  {"model": model, "max_tokens": 3000, "system": system,
                   "messages": [{"role": "user", "content": [
                       {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
                       {"type": "text", "text": user_text}]}]})
        return "".join(c.get("text", "") for c in r.get("content", []))
    if p == "openai":
        # every configured endpoint with its models, tried in this order; free ones are often busy -> next
        cands = []
        def add(base, key, models):
            cands.extend((base, key, m.strip()) for m in models.split(",") if m.strip())
        if os.environ.get("GITHUB_TOKEN"):
            add("https://models.github.ai/inference", os.environ["GITHUB_TOKEN"], os.environ.get("GITHUB_MODEL", "openai/gpt-4.1,openai/gpt-4o"))
        if os.environ.get("GEMINI_API_KEY"):
            add("https://generativelanguage.googleapis.com/v1beta/openai", os.environ["GEMINI_API_KEY"], os.environ.get("GEMINI_MODEL", "gemini-2.5-flash,gemini-2.5-flash-lite"))
        if os.environ.get("AI_BASE_URL") and os.environ.get("AI_API_KEY"):
            add(os.environ["AI_BASE_URL"].rstrip("/"), os.environ["AI_API_KEY"], os.environ.get("AI_MODEL", ""))
        if os.environ.get("OPENROUTER_API_KEY"):
            add("https://openrouter.ai/api/v1", os.environ["OPENROUTER_API_KEY"],
                os.environ.get("OPENROUTER_MODEL") or "google/gemma-4-31b-it:free,google/gemma-4-26b-a4b-it:free,nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free")
        err = None
        tries = max(4, 2 * len(cands))
        for attempt in range(tries):
            base, key, mdl = cands[attempt % len(cands)]
            try:
                txt = _chat_stream(base + "/chat/completions", key,
                                   {"model": mdl, "max_tokens": 6000, "messages": [
                                       {"role": "system", "content": system},
                                       {"role": "user", "content": [
                                           {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                                           {"type": "text", "text": user_text}]}]})
                if len(txt.strip()) > 200:
                    return txt
                err = RuntimeError(f"пустой ответ от {mdl}")
            except RuntimeError as e:
                err = RuntimeError(f"{mdl}: {e}")
            print(f"  {mdl} не ответил ({str(err)[:120]}), пробую дальше ({attempt + 1}/{tries})...", flush=True)
            if (attempt + 1) % len(cands) == 0:
                time.sleep(min(30, 8 * (attempt + 1) // len(cands)))
        raise err
    raise RuntimeError("Не задан ключ ИИ (ANTHROPIC_API_KEY или OPENROUTER_API_KEY)")


def check(text, n_options=None):
    """Возвращает список проблем сценария (пусто = ок)."""
    from blog3d import parse_scenario
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)
    head, beats, sc = parse_scenario(f.name)
    os.unlink(f.name)
    bad = []
    if not head.get("вопрос"):
        bad.append("нет строки 'Вопрос:'")
    if not re.search(r"\d", head.get("ответ", "")):
        bad.append("нет номера в 'Ответ:'")
    if not any(a["id"] == "мы" for a in sc["actors"]):
        bad.append("нет строки 'Мы: юг, ...'")
    if len(beats) < 3:
        bad.append("меньше 3 кадров (разделитель ---)")
    names = {a["id"] for a in sc["actors"]}
    for x in sc["order"]:
        if x not in names:
            bad.append(f"в 'Очередь' есть '{x}', но такой машины нет в шапке")
    return bad


def make(image_path, hint=""):
    ask = "Напиши сценарий по этой картинке."
    m = re.search(r"ответ\D{0,3}(\d)", hint or "", re.I)
    if m:
        ask += f" ВАЖНО: правильный ответ - вариант {m.group(1)}, объяснение должно вести к нему."
    rest = re.sub(r"ответ\D{0,3}\d", "", hint or "", flags=re.I).strip()
    if len(rest) > 120:
        ask += ("\nНиже ТЕКСТ АВТОРА - его объяснение этого вопроса. Сохрани его слова и смысл в строках 'Голос:' почти дословно "
                "(можно чуть сократить, убрать мат, разбить на кадры по 1-2 предложения). Хук, плашки, метки, камеры и расстановку "
                "машин придумай сам по картинке и этому тексту. Правильный ответ бери из текста автора.\n\nТЕКСТ АВТОРА:\n" + rest)
    elif rest:
        ask += f" Подсказка автора: {rest}"
    text = ""
    for attempt in range(3):
        text = ask_ai(image_path, ask).strip().strip("`").strip()
        bad = check(text)
        if not bad:
            import otvety                          # the official answer from the 800-question base wins
            q = re.search(r"(?m)^\s*Вопрос\s*:\s*(.+)$", text)
            opts = [mm.group(1).strip() for mm in re.finditer(r"(?m)^\s*Вариант\s*\d+\s*:\s*(.+)$", text)]
            f = otvety.find(q.group(1), opts) if q else None
            got = re.search(r"(?m)^\s*Ответ\s*:\s*(\d+)", text)
            if f and (not got or int(got.group(1)) != f["index"]) and attempt == 0:
                ask += (f"\nОШИБКА: правильный ответ - вариант {f['index']} («{f['correct']}»). Официальное пояснение: {f['hint']}\n"
                        "Перепиши сценарий целиком так, чтобы объяснение вело именно к этому ответу.")
                continue
            return otvety.check_scenario(text)[0]
        ask += "\nПрошлый вариант был с ошибками: " + "; ".join(bad) + ". Исправь и выведи сценарий целиком заново."
    import otvety
    return otvety.check_scenario(text)[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("image")
    ap.add_argument("--hint", default="")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    t = make(a.image, a.hint)
    if a.out:
        Path(a.out).write_text(t, encoding="utf-8")
    print(t)


if __name__ == "__main__":
    main()
