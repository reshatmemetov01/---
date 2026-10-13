"""otvety: база всех 800 вопросов ПДД (pdd-exam.ru) - правильный ответ по тексту вопроса и вариантов.

python otvety.py build ПАПКА_С_bilet*.txt     -> pdd800.json
python otvety.py find "текст вопроса" "вариант 1" "вариант 2" ...
"""
import json, re, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "pdd800.json"
_DB = None


def norm(s):
    s = (s or "").lower().replace("ё", "е")
    return " ".join(re.findall(r"[a-zа-я0-9]+", s))


def words(s):
    return set(w[:6] for w in norm(s).split() if len(w) > 2)


def sim(a, b):
    A, B = words(a), words(b)
    if not A or not B:
        return 1.0 if norm(a) == norm(b) else 0.0
    return len(A & B) / len(A | B)


def build(folder):
    out = []
    for f in sorted(Path(folder).glob("bilet*.txt"), key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)):
        b = int(re.sub(r"\D", "", f.stem))
        parts = re.split(r"(?m)^Вопрос (\d+)\s*$", f.read_text(encoding="utf-8"))
        for i in range(1, len(parts), 2):
            num, body = int(parts[i]), parts[i + 1].strip().splitlines()
            opts = [re.sub(r"^\d+\.\s*", "", l).strip() for l in body if re.match(r"^\d+\.\s", l)]
            q = body[0].strip() if body else ""
            m = re.search(r"(?m)^Ответ:\s*(\d+)", "\n".join(body))
            h = re.search(r"(?ms)^Пояснение:\s*(.+)", "\n".join(body))
            if not (q and opts and m):
                continue
            k = int(m.group(1))
            out.append({"bilet": b, "n": num, "q": q, "options": opts,
                        "correct": opts[k - 1] if 1 <= k <= len(opts) else "",
                        "hint": h.group(1).strip() if h else ""})
    DB_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{len(out)} вопросов -> {DB_PATH}")


def db():
    global _DB
    if _DB is None:
        _DB = json.loads(DB_PATH.read_text(encoding="utf-8")) if DB_PATH.exists() else []
    return _DB


def find(question, options):
    """-> {"index": номер правильного среди ВАШИХ вариантов (с 1), "hint": пояснение, "where": "билет N вопрос M"}
    или None, если вопрос не найден / по тексту не отличить (одинаковые вопросы с разными картинками)."""
    if not db() or not question or not options:
        return None
    scored = []
    for r in db():
        if len(r["options"]) != len(options):
            continue
        qs = sim(question, r["q"])
        if qs < 0.6:
            continue
        # every option must find its pair
        os_ = min(max(sim(o, x) for x in r["options"]) for o in options)
        if os_ < 0.6:
            continue
        scored.append((qs + os_, r))
    if not scored:
        return None
    best = max(s for s, _ in scored)
    top = [r for s, r in scored if s >= best - 0.05]
    idx = set()
    for r in top:
        idx.add(max(range(len(options)), key=lambda i: sim(options[i], r["correct"])))
    if len(idx) != 1:
        return None                       # same wording, different answers - only the picture decides
    r = top[0]
    return {"index": idx.pop() + 1, "hint": r["hint"], "where": f"билет {r['bilet']}, вопрос {r['n']}",
            "correct": r["correct"]}


def check_scenario(text):
    """Сверяет 'Ответ:' сценария с базой. -> (исправленный текст, сообщение или None)."""
    q = re.search(r"(?m)^\s*Вопрос\s*:\s*(.+)$", text)
    opts = [m.group(2).strip() for m in re.finditer(r"(?m)^\s*Вариант\s*(\d+)\s*:\s*(.+)$", text)]
    if not q or not opts:
        return text, None
    f = find(q.group(1), opts)
    if not f:
        return text, None
    m = re.search(r"(?m)^(\s*Ответ\s*:\s*)(\d+)", text)
    if m and int(m.group(2)) == f["index"]:
        return text, None
    if m:
        text = text[:m.start(2)] + str(f["index"]) + text[m.end(2):]
    else:
        last = list(re.finditer(r"(?m)^\s*Вариант\s*\d+\s*:.*$", text))[-1]
        text = text[:last.end()] + f"\nОтвет: {f['index']}" + text[last.end():]
    old = m.group(2) if m else "?"
    return text, (f"⚠️ В сценарии был ответ {old}, а правильный по базе ({f['where']}) — {f['index']}: «{f['correct']}». "
                  f"Номер я исправил, но проверь, чтобы объяснение и «Голос ответа» вели к нему.")


if __name__ == "__main__":
    if sys.argv[1] == "build":
        build(sys.argv[2])
    else:
        print(find(sys.argv[2], sys.argv[3:]))
