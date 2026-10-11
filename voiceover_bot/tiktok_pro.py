"""tiktok_pro: blogger-style vertical PDD video from ONE card image + text.

Style: blurred background, card flies in tilted, word-by-word captions,
hook with "?" stickers and a slammed stamp, red underline under the
question, 3-second timer, then zoom into the photo with marks (yellow
ellipse + label plate) that appear when the voice says the keyword,
zoom out, green correct option + "correct answer" plate.

Run (Yandex keys already set via setx):
    python tiktok_pro.py card.png out.mp4 --speak text.txt --marks marks.json --correct 2

Text file format (same as tiktok.py):
    Hook: ...            (line starting with the Russian word for Hook)
    question text
    Explanation: ...     (Russian word for Explanation)
    Answer: ...          (Russian word for Answer)

marks.json: positions as fractions of the card image (0..1):
    {"objects": [{"x":..,"y":..,"rx":..,"ry":..,"keys":["stem"],
                  "label":"TITLE","sub":"small line","px":..,"py":..}],
     "underline": [[x1,x2,y], ...]}
"""

import argparse
import asyncio
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path

from auto_quiz import (FFMPEG, make_cached_synth, media_duration, concat_audio,
                       _find_chromium, _image_data_uri, synth_yandex,
                       synth_yandex_v3, synth_edge, _trim_tail_silence)
from make_video import read_text_any
from tiktok import parse_narration, _speakable, detect_boxes, make_ticks

W, H = 1080, 1920
CARD_W, CARD_LEFT, CARD_TOP = 960, 60, 520
FOCUS_X, FOCUS_Y = 540, 960          # screen point a focused mark is moved to

VOWELS = set("\u0430\u0435\u0451\u0438\u043e\u0443\u044b\u044d\u044e\u044faeiouy")
HL_STEMS = ["\u0433\u043b\u0430\u0432\u043d", "\u0443\u0441\u0442\u0443\u043f", "\u043d\u0430\u043b\u0435\u0432", "\u043d\u0430\u043f\u0440\u0430\u0432", "\u043f\u0440\u0430\u0432\u0438\u043b\u044c\u043d", "\u043e\u0442\u0432\u0435\u0442", "\u043f\u0435\u0440\u0432",
            "\u0432\u0442\u043e\u0440", "\u0433\u0440\u0443\u0437", "\u043b\u0435\u0433\u043a\u043e\u0432", "\u0437\u043d\u0430\u043a", "\u043d\u0435\u043b\u044c\u0437\u044f", "\u043c\u043e\u0436\u043d\u043e", "\u043e\u0431\u044f\u0437\u0430\u043d",
            "\u043f\u0440\u043e\u043f\u0443\u0441\u043a", "\u0432\u0441\u0442\u0440\u0435\u0447\u043d", "\u0442\u043e\u043b\u044c\u043a\u043e", "\u043f\u0435\u0440\u0432\u044b\u043c"]
SHORT = {"\u0438", "\u0432", "\u043d\u0430", "\u043d\u0435", "\u0430", "\u043f\u043e", "\u0441", "\u043a", "\u0443", "\u043e", "\u0447\u0442\u043e", "\u044d\u0442\u043e",
         "\u043c\u044b", "\u0432\u044b", "\u0436\u0435", "\u043b\u0438", "\u0434\u043e", "\u0437\u0430", "\u0438\u0437", "\u043e\u0442", "\u043f\u0440\u0438", "\u043f\u043e\u0434", "\u043d\u0430\u0441"}


# --------------------------------------------------------------------------- #
#  Text helpers
# --------------------------------------------------------------------------- #

def split_sentences(text):
    parts = [p.strip() for p in re.split(r"(?<=[.!?\u2026])\s+", text.strip()) if p.strip()]
    out = []
    for p in parts:
        if out and len(out[-1].split()) < 3:
            out[-1] = out[-1] + " " + p
        else:
            out.append(p)
    return out


def word_times(text, dur, lead=0.06):
    """Estimate start time of every word inside one voiced clip (TTS speaks at
    a steady pace, so vowels ~ syllables ~ time)."""
    words = text.split()
    weights, pauses = [], []
    for w in words:
        v = sum(1 for c in w.lower() if c in VOWELS)
        weights.append(max(1, v) + 0.6)
        pauses.append(1.4 if w.endswith((",", ";", ":", "\u2014")) else
                      (2.2 if w.endswith((".", "!", "?")) else 0.0))
    total = sum(weights) + sum(pauses[:-1]) if words else 1.0
    usable = max(0.2, dur - lead - 0.05)
    t, acc, res = lead, 0.0, []
    for i, w in enumerate(words):
        res.append((w, round(t + usable * acc / total, 3)))
        acc += weights[i] + (pauses[i] if i < len(words) - 1 else 0)
    return res


def group_words(wt):
    """1-2 words per caption, short function words glued to the next one."""
    groups, cur = [], []
    for w, t in wt:
        cur.append((w, t))
        bare = re.sub(r"[^\w%]", "", w.lower())
        chars = sum(len(x) for x, _ in cur)
        if bare in SHORT and len(cur) < 3:
            continue
        if len(cur) >= 2 or chars >= 11:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def caption_html(group):
    parts = []
    for w, _ in group:
        clean = re.sub(r"[\"<>]", "", w).upper()
        low = w.lower()
        hl = any(s in low for s in HL_STEMS) or re.search(r"\d", w)
        parts.append(f'<span class="hl">{clean}</span>' if hl else clean)
    return " ".join(parts)


# --------------------------------------------------------------------------- #
#  Page
# --------------------------------------------------------------------------- #

_PAGE = r"""<!doctype html><html><head><meta charset="utf-8">
<link id="gf" href="https://fonts.googleapis.com/css2?family=Montserrat:wght@700;800;900&display=block" rel="stylesheet">
<style>
html,body{margin:0;width:1080px;height:1920px;overflow:hidden;background:#111;}
*{box-sizing:border-box;font-family:Montserrat,'Arial Black',Arial,sans-serif;}
#bg{position:absolute;inset:-80px;background-size:cover;background-position:center 20%;
    filter:blur(34px) brightness(.72) saturate(1.15);transform:scale(1.15);}
#stage{position:absolute;inset:0;}
#stage.shake{animation:shake .38s linear;}
@keyframes shake{0%,100%{transform:translate(0,0)}15%{transform:translate(-14px,9px)}
 30%{transform:translate(12px,-10px)}45%{transform:translate(-9px,-6px)}
 60%{transform:translate(8px,8px)}80%{transform:translate(-4px,3px)}}
#tilt{position:absolute;left:__CL__px;top:__CT__px;width:__CW__px;
      transform-origin:50% 40%;opacity:0;}
#tilt.enter{animation:enter .9s cubic-bezier(.2,.9,.25,1.12) both;}
@keyframes enter{0%{transform:perspective(1400px) rotateY(-38deg) rotateX(18deg) scale(.6) translateY(120px);opacity:0}
 20%{opacity:1}100%{transform:perspective(1400px) rotateY(0) rotateX(0) scale(1) translateY(0);opacity:1}}
#zoom{position:relative;width:__CW__px;transform-origin:0 0;
      transition:transform .9s cubic-bezier(.45,.05,.25,1);}
#card{display:block;width:100%;border-radius:26px;box-shadow:0 30px 80px rgba(0,0,0,.55);
      clip-path:inset(0% 0% 0% 0% round 26px);transition:clip-path .7s ease;}
#zoom.photo #card{clip-path:var(--photo);}
#ov{position:absolute;inset:0;width:100%;height:100%;overflow:visible;pointer-events:none;}
.ell{fill:none;stroke:#FFD400;stroke-width:7;stroke-linecap:round;
     filter:drop-shadow(0 0 6px rgba(0,0,0,.6));}
.ul{fill:none;stroke:#E53935;stroke-width:6;stroke-linecap:round;}
.green{fill:rgba(46,204,113,.18);stroke:#2ECC71;stroke-width:7;}
.q{position:absolute;width:64px;height:64px;margin:-32px 0 0 -32px;border-radius:50%;
   background:#E53935;color:#fff;font-weight:900;font-size:44px;line-height:64px;text-align:center;
   border:4px solid #fff;box-shadow:0 6px 18px rgba(0,0,0,.45);animation:pop .35s cubic-bezier(.2,1.6,.4,1) both;}
@keyframes pop{0%{transform:scale(0)}100%{transform:scale(1)}}
.plate{position:absolute;transform:translate(-50%,-50%);background:rgba(12,14,18,.92);
   border-radius:14px;padding:10px 18px 9px;text-align:center;white-space:nowrap;
   box-shadow:0 8px 22px rgba(0,0,0,.5);animation:popc .3s cubic-bezier(.2,1.5,.4,1) both;
   transition:opacity .3s;}
@keyframes popc{0%{transform:translate(-50%,-50%) scale(0)}100%{transform:translate(-50%,-50%) scale(1)}}
.plate.big{white-space:normal;width:88%;padding:16px 24px 14px;}
.plate b{display:block;color:#fff;font-weight:900;font-size:24px;letter-spacing:.5px;}
.plate i{display:block;color:#7CFF4F;font-style:normal;font-weight:800;font-size:15px;margin-top:2px;text-transform:uppercase;}
.plate.big b{font-size:34px}.plate.big i{font-size:26px;}
#stamp{position:absolute;left:50%;top:63%;transform:translate(-50%,-50%) rotate(-12deg);
   border:10px solid #E53935;border-radius:18px;padding:8px 34px;color:#E53935;
   background:rgba(255,255,255,.88);font-weight:900;font-size:96px;letter-spacing:2px;display:none;}
#stamp.on{display:block;animation:slam .32s cubic-bezier(.3,1.4,.5,1) both;}
@keyframes slam{0%{transform:translate(-50%,-50%) rotate(-12deg) scale(3.2);opacity:0}
 100%{transform:translate(-50%,-50%) rotate(-12deg) scale(1);opacity:1}}
#cap{position:absolute;left:60px;right:60px;top:250px;display:flex;justify-content:center;}
#cap .pill{background:rgba(18,22,30,.78);border-radius:22px;padding:16px 30px 14px;
   color:#fff;font-weight:900;font-size:66px;line-height:1.08;text-align:center;
   text-shadow:0 3px 0 rgba(0,0,0,.35);box-shadow:0 10px 30px rgba(0,0,0,.35);
   animation:cp .14s ease-out both;max-width:960px;}
#cap .hl{color:#B9F64A;}
@keyframes cp{0%{transform:scale(.86);opacity:.4}100%{transform:scale(1);opacity:1}}
#timer{position:absolute;left:50%;top:800px;width:220px;height:220px;margin-left:-110px;display:none;}
#timer.on{display:block;}
#timer svg{width:220px;height:220px;transform:rotate(-90deg);}
#timer .n{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
   color:#fff;font-weight:900;font-size:110px;text-shadow:0 4px 14px rgba(0,0,0,.6);}
.fade{transition:opacity .35s;}
</style></head><body>
<div id="bg"></div>
<div id="stage">
  <div id="tilt"><div id="zoom">
    <img id="card">
    <svg id="ov"></svg>
    <div id="dom"></div>
    <div id="stamp"></div>
  </div></div>
  <div id="cap"></div>
  <div id="timer"><svg viewBox="0 0 220 220"><circle cx="110" cy="110" r="96" fill="rgba(0,0,0,.45)"
     stroke="rgba(255,255,255,.25)" stroke-width="14"/><circle id="arc" cx="110" cy="110" r="96" fill="none"
     stroke="#FFD400" stroke-width="14" stroke-linecap="round" stroke-dasharray="603" stroke-dashoffset="0"/></svg>
     <div class="n" id="tn"></div></div>
</div>
<script>
const D = __DATA__;
const NS = "http://www.w3.org/2000/svg";
const img = document.getElementById("card"), ov = document.getElementById("ov"),
      dom = document.getElementById("dom"), zoom = document.getElementById("zoom");
let IW = 1, IH = 1;
function px(x){return x*IW;} function py(y){return y*IH;}
function el(tag, attrs, parent){const e=document.createElementNS(NS,tag);
  for(const k in attrs) e.setAttribute(k, attrs[k]); (parent||ov).appendChild(e); return e;}
function draw(e, dur){const L=e.getTotalLength(); e.style.strokeDasharray=L; e.style.strokeDashoffset=L;
  e.getBoundingClientRect(); e.style.transition="stroke-dashoffset "+dur+"s ease-out"; e.style.strokeDashoffset=0;}
function ellPath(o){const cx=px(o.x), cy=py(o.y), rx=px(o.rx), ry=py(o.ry);
  return `M ${cx+rx} ${cy} A ${rx} ${ry} 0 1 1 ${cx-rx} ${cy+0.5} A ${rx} ${ry} 0 1 1 ${cx+rx*0.92} ${cy-ry*0.25}`;}
function plate(x, y, title, sub, big){const d=document.createElement("div");
  d.className="plate"+(big?" big":""); d.style.left=(x*100)+"%"; d.style.top=(y*100)+"%";
  d.innerHTML="<b>"+title+"</b>"+(sub?"<i>"+sub+"</i>":""); dom.appendChild(d); return d;}
const CW = __CW__, CH_ = () => CW*IH/IW;
window.__tt = {
  ready:false,
  enter(){document.getElementById("tilt").classList.add("enter");},
  cap(html){const c=document.getElementById("cap"); c.innerHTML='<div class="pill">'+html+'</div>';},
  capOff(){document.getElementById("cap").innerHTML="";},
  q(x,y){const d=document.createElement("div"); d.className="q"; d.textContent="?";
    d.style.left=(x*100)+"%"; d.style.top=(y*100)+"%"; d.dataset.k="hook"; dom.appendChild(d);},
  stamp(t){const s=document.getElementById("stamp"); s.textContent=t; s.classList.add("on");
    const st=document.getElementById("stage"); st.classList.remove("shake"); void st.offsetWidth; st.classList.add("shake");},
  hookOff(){document.getElementById("stamp").classList.remove("on");
    dom.querySelectorAll('[data-k="hook"]').forEach(e=>e.remove());},
  underline(dur){(D.underline||[]).forEach((u,i)=>{const p=el("path",{d:`M ${px(u[0])} ${py(u[2])} L ${px(u[1])} ${py(u[2])}`,"class":"ul"});
    const share=(u[1]-u[0]); setTimeout(()=>draw(p, dur*share/ (D.ulTotal||1)), 1000*dur*(D.ulStart[i]||0));});},
  ulOff(){ov.querySelectorAll(".ul").forEach(e=>e.remove());},
  timer(n){const t=document.getElementById("timer"), a=document.getElementById("arc"), tn=document.getElementById("tn");
    t.classList.add("on"); let k=n; tn.textContent=k; a.style.transition="none"; a.style.strokeDashoffset=0;
    a.getBoundingClientRect(); a.style.transition="stroke-dashoffset "+n+"s linear"; a.style.strokeDashoffset=603;
    const iv=setInterval(()=>{k--; if(k<=0){clearInterval(iv); t.classList.remove("on");} else tn.textContent=k;},1000);},
  focus(fx, fy, s){zoom.classList.add("photo"); const ch=CH_(); let tx=__FX__-__CL__-s*fx*CW; const ty=__FY__-__CT__-s*fy*ch;
    const L=__CL__+tx+s*D.photo[0]*CW, R=__CL__+tx+s*D.photo[2]*CW;
    if (R-L >= 1080) { if (L > 0) tx -= L; else if (R < 1080) tx += 1080-R; }
    zoom.style.transform=`translate(${tx}px,${ty}px) scale(${s})`;},
  reset(){zoom.classList.remove("photo"); zoom.style.transform="none";},
  mark(i){const o=D.objects[i]; dom.querySelectorAll(".plate:not(.keep)").forEach(e=>{e.style.opacity=0; setTimeout(()=>e.remove(),300);});
    const p=el("path",{d:ellPath(o),"class":"ell"}); draw(p,.55);
    setTimeout(()=>plate(o.px, o.py, o.label, o.sub, false), 350);},
  marksOff(){ov.querySelectorAll(".ell").forEach(e=>e.remove()); dom.querySelectorAll(".plate").forEach(e=>e.remove());},
  green(x,y,w,h,title,sub){const r=el("rect",{x:px(x),y:py(y),width:px(w),height:py(h),rx:22,"class":"green"});
    draw(r,.45); const p=plate(0.5, 1.10, title, sub, true); p.classList.add("keep");}
};
img.onload = () => { IW = img.naturalWidth; IH = img.naturalHeight;
  ov.setAttribute("viewBox", `0 0 ${IW} ${IH}`); const P=D.photo; zoom.style.setProperty("--photo", `inset(${P[1]*100}% ${(1-P[2])*100}% ${(1-P[3])*100}% ${P[0]*100}% round 22px)`);
  document.getElementById("bg").style.backgroundImage = `url(${D.image})`;
  const go = () => { window.__tt.ready = true; };
  const fontsOk = () => document.fonts.load("900 66px Montserrat").then(()=>document.fonts.load("800 24px Montserrat")).then(go, go);
  const gf = document.getElementById("gf");
  if (gf.sheet) fontsOk(); else { gf.onload = fontsOk; gf.onerror = go; }
  setTimeout(go, 8000); };
img.src = D.image;
</script></body></html>"""


def build_page(image_uri, marks):
    ul = marks.get("underline", [])
    lens = [u[1] - u[0] for u in ul]
    tot = sum(lens) or 1.0
    starts, acc = [], 0.0
    for L in lens:
        starts.append(acc / tot)
        acc += L
    data = {"image": image_uri, "objects": marks.get("objects", []),
            "underline": ul, "ulTotal": tot, "ulStart": starts, "photo": marks.get("photo", [0.024, 0.108, 0.976, 0.488])}
    payload = json.dumps(data, ensure_ascii=True).replace("</", "<\\/")
    page = _PAGE.replace("__DATA__", payload)
    for k, v in {"__CW__": CARD_W, "__CL__": CARD_LEFT, "__CT__": CARD_TOP,
                 "__FX__": FOCUS_X, "__FY__": FOCUS_Y}.items():
        page = page.replace(k, str(v))
    return page


# --------------------------------------------------------------------------- #
#  Recording (with the page-load lead trimmed off, so captions stay in sync)
# --------------------------------------------------------------------------- #

async def record(page_html, events, total, out_dir, executable_path=None):
    from playwright.async_api import async_playwright
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(page_html)
        uri = Path(f.name).as_uri()
    exe = executable_path or _find_chromium()
    kw = {"args": ["--no-sandbox", "--force-color-profile=srgb"]}
    if exe:
        kw["executable_path"] = exe
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(**kw)
        ctx = await browser.new_context(viewport={"width": W, "height": H},
                                        record_video_dir=out_dir,
                                        record_video_size={"width": W, "height": H})
        page = await ctx.new_page()
        t_page = time.monotonic()
        await page.goto(uri)
        await page.wait_for_function("window.__tt && window.__tt.ready", timeout=60000)
        start = time.monotonic()
        lead = start - t_page
        for t, js in events:
            delay = start + t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            await page.evaluate(js)
        rest = start + total + 0.4 - time.monotonic()
        if rest > 0:
            await asyncio.sleep(rest)
        video = page.video
        await ctx.close()
        await browser.close()
        return await video.path(), lead


def mux(webm, audio, out, total, lead):
    subprocess.run([FFMPEG, "-hide_banner", "-y", "-ss", f"{lead:.3f}", "-i", webm, "-i", audio,
                    "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "192k",
                    "-t", f"{total:.3f}", "-movflags", "+faststart", out],
                   capture_output=True, check=True)


# --------------------------------------------------------------------------- #
#  Build
# --------------------------------------------------------------------------- #

def make_synth(engine, voice, role):
    import os
    if engine == "yandex3":
        key = os.environ.get("YANDEX_API_KEY", "").strip()
        if not key:
            raise SystemExit("YANDEX_API_KEY is not set")
        v = voice or "anton"

        async def base(t):
            return await asyncio.to_thread(synth_yandex_v3, t, v, key, role)
        return base, v, role + "@" + (os.environ.get("YANDEX_SPEED") or "1.0").strip()
    if engine == "yandex":
        key = os.environ.get("YANDEX_API_KEY", "").strip()
        folder = os.environ.get("YANDEX_FOLDER_ID", "").strip()
        if not key or not folder:
            raise SystemExit("YANDEX_API_KEY / YANDEX_FOLDER_ID are not set")
        v = voice or "filipp"

        async def base(t):
            return await asyncio.to_thread(synth_yandex, t, v, key, folder)
        return base, v, "v1"
    v = voice or "ru-RU-DmitryNeural"

    async def base(t):
        return await synth_edge(t, v, "+0%", "+0Hz")
    return base, v, "+0%"


async def build(a):
    raw = read_text_any(a.speak)
    hook = ""
    mh = re.search(r"(?mi)^\s*\u0425\u0443\u043a\s*:\s*(.+)$", raw)
    if mh:
        hook = mh.group(1).strip()
        raw = raw[:mh.start()] + raw[mh.end():]
    question, expl, _sp, answer, _g, _m = parse_narration(raw)
    marks = json.loads(Path(a.marks).read_text(encoding="utf-8-sig")) if a.marks else {}
    objects = marks.get("objects", [])

    base, vname, vrole = make_synth(a.engine, a.voice, a.role)
    synth = make_cached_synth(base, a.engine, vname, vrole, "+0Hz", len(split_sentences(hook)) + len(split_sentences(question)) + len(split_sentences(expl)) + 1)
    tmp = Path(tempfile.mkdtemp(prefix="ttpro_"))
    n = [0]

    async def say(text):
        n[0] += 1
        mp3 = tmp / f"s{n[0]:03d}.mp3"
        mp3.write_bytes(await synth(_speakable(text)))
        _trim_tail_silence(mp3)
        wav = tmp / f"s{n[0]:03d}.wav"
        subprocess.run([FFMPEG, "-hide_banner", "-y", "-i", str(mp3), "-ar", "48000", "-ac", "2",
                        str(wav)], capture_output=True, check=True)
        return str(wav), media_duration(str(wav))

    clips, gaps, events = [], [], []
    t = 0.0
    events.append((0.0, "window.__tt.enter()"))

    async def voice_block(text, gap_after=0.14, prefix_words=None):
        """Voice text sentence by sentence; returns list of (word, abs_time)."""
        nonlocal t
        allw = []
        for s in split_sentences(text):
            if not _speakable(s).strip(): continue
            p, d = await say(s)
            clips.append((p, d)); gaps.append(gap_after)
            wt = [(w, t + tt) for w, tt in word_times(s, d)]
            for g in group_words(wt):
                events.append((round(g[0][1], 3), f"window.__tt.cap({json.dumps(caption_html(g))})"))
            allw += wt
            t += d + gap_after
        return allw

    print("Voicing...")
    # 1) hook
    if hook and not a.no_hook:
        h0 = t
        await voice_block(hook)
        hd = t - h0
        for i, o in enumerate(objects[:3]):
            events.append((round(h0 + 0.25 + i * 0.3, 3),
                           f"window.__tt.q({o['x']},{max(0.12, o['y'] - o['ry'] - 0.035)})"))
        events.append((round(max(h0 + 1.0, h0 + hd - 0.55), 3), f"window.__tt.stamp({json.dumps(a.stamp)})"))
        t += 0.25
        hook_end = t
    else:
        hook_end = 0.0

    # 2) question with underline
    q0 = t
    events.append((round(q0 + (0.9 if hook_end else 0), 3), "window.__tt.hookOff()"))
    await voice_block(question)
    qd = t - q0
    events.append((round(q0 + 0.1, 3), f"window.__tt.underline({max(0.8, qd - 0.4):.2f})"))

    # 3) think timer
    tp = max(1, int(a.think))
    ticks = str(tmp / "ticks.wav")
    make_ticks(tp, ticks)
    td = media_duration(ticks)
    events.append((round(t, 3), "window.__tt.capOff()"))
    events.append((round(t, 3), f"window.__tt.timer({tp})"))
    clips.append((ticks, td)); gaps.append(0.15)
    t += td + 0.15

    # 4) explanation with zoom + marks
    e0 = t
    events.append((round(e0, 3), "window.__tt.ulOff(); window.__tt.focus(%.3f,%.3f,%.3f)" % (lambda p: ((p[0]+p[2])/2, (p[1]+p[3])/2, min(1.6, 1080/((p[2]-p[0])*CARD_W))))(marks.get("photo", [0.024, 0.108, 0.976, 0.488]))))
    ew = await voice_block(expl)
    e1 = t
    trig = []
    for i, o in enumerate(objects):
        hit = None
        for w, tw in ew:
            if any(k.lower() in w.lower() for k in o.get("keys", [])):
                hit = tw
                break
        trig.append(hit)
    free = [i for i, h in enumerate(trig) if h is None]
    for j, i in enumerate(free):
        trig[i] = e0 + (e1 - e0) * (0.15 + 0.7 * (j + 1) / (len(free) + 1))
    for i in sorted(range(len(objects)), key=lambda k: trig[k]):
        o = objects[i]
        ts = max(e0 + 0.6, trig[i] - 0.15)
        events.append((round(ts, 3), f"window.__tt.focus({o['x']},{o['y']},{a.zoom})"))
        events.append((round(ts + 0.55, 3), f"window.__tt.mark({i})"))

    # 5) answer
    a0 = t
    events.append((round(a0, 3), "window.__tt.marksOff(); window.__tt.reset()"))
    await voice_block("\u041e\u0442\u0432\u0435\u0442. " + answer, gap_after=0.0)
    if a.correct:
        boxes = detect_boxes(a.image)
        if boxes and 1 <= a.correct <= len(boxes):
            top, bot = boxes[a.correct - 1]
            title = "\u041f\u0420\u0410\u0412\u0418\u041b\u042c\u041d\u042b\u0419 \u041e\u0422\u0412\u0415\u0422"
            sub = f"{a.correct}. {answer.rstrip('.')}"
            events.append((round(t, 3),
                           f"window.__tt.green(0.03,{top - 0.004},0.94,{bot - top + 0.008},"
                           f"{json.dumps(title)},{json.dumps(sub)})"))
        else:
            print(f"! option boxes not found or --correct out of range ({len(boxes or [])} boxes)")
    total = t + a.hold
    events.sort(key=lambda e: e[0])
    print(f"Duration: {total:.1f}s")

    print("Recording...")
    page = build_page(_image_data_uri(Path(a.image)), marks)
    vdir = tmp / "vid"
    vdir.mkdir()
    webm, lead = await record(page, events, total, str(vdir), a.chromium_path)
    print("Mixing audio...")
    audio = str(tmp / "track.m4a")
    concat_audio(clips, gaps, audio)
    mux(webm, audio, a.output, total, lead)
    print(f"Done: {a.output}")


def main():
    ap = argparse.ArgumentParser(description="Blogger-style PDD TikTok")
    ap.add_argument("image")
    ap.add_argument("output")
    ap.add_argument("--speak", required=True)
    ap.add_argument("--marks", default=None)
    ap.add_argument("--correct", type=int, default=0)
    ap.add_argument("--engine", choices=["yandex", "yandex3", "edge"], default="yandex")
    ap.add_argument("--voice", default="")
    ap.add_argument("--role", default="good")
    ap.add_argument("--stamp", default="\u0416\u0415\u0421\u0422\u042c")
    ap.add_argument("--think", type=int, default=3)
    ap.add_argument("--zoom", type=float, default=1.8)
    ap.add_argument("--hold", type=float, default=2.2)
    ap.add_argument("--no-hook", action="store_true")
    ap.add_argument("--chromium-path", default=None)
    a = ap.parse_args()
    asyncio.run(build(a))


if __name__ == "__main__":
    main()
