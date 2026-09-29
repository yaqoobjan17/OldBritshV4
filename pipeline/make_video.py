#!/usr/bin/env python3
"""Old British Cars - one-click pipeline: script -> 1080p MP4.

One scene per SENTENCE. Every sentence gets its own picture that must match it.
Stages (each reads/writes work/state.json):
  plan    split script into sentences; Gemini (if key) writes photo-search words + AI image prompt per sentence
  voice   narration per sentence (ElevenLabs if key, else free Edge-TTS British voice)
  visuals per sentence: verified real photo/clip (Wikimedia, Pexels) or AI image (Pollinations) - never a random one
  render  Ken-Burns animation, vintage grade, SFX, subtitles -> final.mp4 (1920x1080)
"""
import argparse, asyncio, html, json, math, os, re, subprocess, sys, textwrap, time
from urllib.parse import quote
from pathlib import Path
import requests

W, H, FPS = 1920, 1080, 30
LEAD, TAIL = 0.12, 0.30          # silence before / after narration in each sentence-scene
UA = "OldBritishCarsVideoBot/1.0 (personal YouTube documentary project)"
SFX_KINDS = {"engine", "road", "factory", "none"}
def log(m): print(m, flush=True)


def run(cmd, cwd=None):
    p = subprocess.run([str(c) for c in cmd], cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("command failed: " + " ".join(map(str, cmd))[:250] + "\n" + p.stderr[-1800:])
    return p.stdout + p.stderr


def probe_dur(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True).stdout
    return float(out.strip() or 0)


def wc(t): return len(re.findall(r"\S+", t))


# ----------------------------------------------------------------- PLAN
STOPW = set("""a an the and or but if then so of to in on at by for with from as is are was were be been being am do does did
have has had having it its this that these those he she they we you i his her their our your my me him them us who whom
which what when where why how not no nor than too very can could would should will shall may might must just also only
even still yet about into over under after before between through during without within against among per each every both
either neither some any all most many much more less other another such own same there here again once one two three
four five first second new old like made make makes made became become came come went go goes got get thing things
something behind while until since because though although however meanwhile""".split())
GENERIC_Q = {"car", "cars", "vehicle", "vehicles", "vintage", "british", "britain", "classic", "old", "photo", "image",
             "picture", "uk", "english", "history", "historic", "historical"}
CARWORDS = {"car", "cars", "automobile", "vehicle", "saloon", "sedan", "coupe", "motor", "roadster", "estate", "van", "lorry", "truck"}
STOP_CAP = set("""The A An In On At It This That These Those He She They We I You When While By For With But And Or As After
Before Then So If Its His Her Their Our One Some Many Most Today Now From To Of What Why How Where Who There Here Even
Still Yet During Despite Although Because Since Once Every Each Both Was Were Is Are Had Has Have""".split())


def stem(w): return w[:-1] if len(w) > 3 and w.endswith("s") else w


def qtokens(q): return [stem(w) for w in re.findall(r"[a-z0-9]+", q.lower()) if w not in GENERIC_Q and w not in STOPW and len(w) > 1]


def relevant(hay, query):
    """Strict check: does the candidate's title/description really talk about what the sentence needs?"""
    toks = qtokens(query)
    if not toks: return False
    hs = {stem(w) for w in re.findall(r"[a-z0-9]+", hay.lower())}
    if not any(stem(c) in hs for c in CARWORDS): return False     # channel topic = cars: a car word must be in the text
    hits = sum(1 for t in toks if t in hs)
    return hits >= (len(toks) if len(toks) <= 2 else max(2, math.ceil(len(toks) * 0.6)))


def extract_query(text):
    good = []
    for m in re.finditer(r"(?:[A-Z][A-Za-z0-9\-]+|\b\d{2,4}\b)(?:\s+(?:[A-Z][A-Za-z0-9\-]+|\d{2,4}))*", text):
        toks = m.group(0).split()
        if m.start() == 0 and len(toks) == 1: continue       # a lone capitalised first word is just the sentence start
        while toks and toks[0] in STOP_CAP: toks.pop(0)
        if toks and not all(t.isdigit() for t in toks): good.append(" ".join(toks[:4]))
    good.sort(key=lambda g: (-(any(c.isdigit() for c in g) or len(g.split()) > 1), -len(g)))
    return good[0] if good else ""


def keywords(text, n=4):
    words = re.findall(r"[A-Za-z][A-Za-z\-']+", text)
    cand = []
    for k, w in enumerate(words):
        if w.lower() in STOPW or len(w) < 3: continue
        cand.append((k, w, (2 if (w[0].isupper() and k > 0) else 0) + len(w) / 10))
    top = sorted(cand, key=lambda c: -c[2])[:n]
    seen, out = set(), []
    for k, w, _ in sorted(top):
        if w.lower() not in seen: seen.add(w.lower()); out.append(w)
    return " ".join(out)


def guess_topic(script):
    from collections import Counter
    cnt = Counter(w.lower() for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", script) if w.lower() not in STOPW and w.lower() not in GENERIC_Q)
    top = [w for w, c in cnt.most_common(3) if c >= 2]
    return ("British " + " ".join(top[:2]) + " vintage cars") if top else "vintage British cars"


def guess_sfx(text, i):
    t = text.lower()
    if re.search(r"\b(engines?|horsepower|rac(?:e|es|ing)|speed|motors?|v8|v12|cylinders?|revs?|chase|siren|sirens)\b", t): return "engine"
    if re.search(r"\b(factory|plant|assembly|production|workers|workshop)\b", t): return "factory"
    if re.search(r"\b(drive|drives|driving|road|roads|journey|highway|motorway|motorways|travel|street|streets)\b", t): return "road"
    return "none"


def split_long(s, maxw=24):
    if wc(s) <= maxw: return [s]
    out, cur = [], ""
    for p in re.split(r"(?<=[,;:\u2014\u2013])\s+", s):
        if cur and wc(cur) + wc(p) > maxw: out.append(cur); cur = p
        else: cur = (cur + " " + p).strip()
    if cur: out.append(cur)
    if len(out) > 1 and wc(out[-1]) < 5: out[-2] += " " + out.pop()
    return out


def split_units(script):
    """One unit per sentence (very long ones are split at commas, tiny ones merged into the next)."""
    units = []
    for line in re.split(r"\n+", script):
        line = line.strip()
        if not line: continue
        t = re.sub(r"\b(Mr|Mrs|Ms|Dr|St|Jr|Sr|vs|No|Ltd|Co|Inc|Mt|Prof)\.", r"\1<D>", line)
        t = re.sub(r"(\d)\.(\d)", r"\1<D>\2", t)
        for sent in re.split(r'(?<=[.!?\u2026]["\u201d\u2019)])\s+|(?<=[.!?\u2026])\s+', t):
            sent = sent.replace("<D>", ".").strip()
            if sent: units += split_long(sent)
    merged, i = [], 0
    while i < len(units):
        if wc(units[i]) < 5 and i + 1 < len(units):
            units[i + 1] = units[i] + " " + units[i + 1]; i += 1; continue
        merged.append(units[i]); i += 1
    if len(merged) > 1 and wc(merged[-1]) < 4: merged[-2] += " " + merged.pop()
    return merged


def heuristic_scene(u, i, topic):
    return dict(narration=u, search_query=extract_query(u) or keywords(u, 3),
                image_prompt=f"{topic}: a vintage British car scene illustrating - {u}", sfx=guess_sfx(u, i))


GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-flash-latest", "gemini-2.5-flash"]


def gemini_call(body, key):
    errors = []
    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=240)
        except requests.RequestException as e:
            errors.append(f"{model}: network error {type(e).__name__}"); continue
        if r.status_code == 200:
            try:
                txt = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                json.loads(txt)
                log(f"Gemini model used: {model}")
                return txt
            except Exception as e:
                errors.append(f"{model}: unreadable answer ({type(e).__name__})"); continue
        try: msg = r.json()["error"]["message"]
        except Exception: msg = r.text[:150]
        errors.append(f"{model}: HTTP {r.status_code} - {re.sub(r'AIza[\w-]+|AQ\.[\w.-]+', '<key>', msg)[:160]}")
        if r.status_code in (400, 401) and "API key" in msg: break     # a bad key fails on every model
    raise RuntimeError(" || ".join(errors))


def gemini_annotate(units, key):
    numbered = "\n".join(f"{i + 1}. {u}" for i, u in enumerate(units))
    prompt = (
        "You are the art director of a cinematic YouTube documentary about old British cars. The narration is split into "
        "numbered sentences. For EVERY sentence design ONE striking image.\n"
        'Return JSON only: {"topic":"<4-8 words: overall subject and era>",'
        '"style":"<ONE reusable visual style line for ALL images: film stock, colour palette, light, era, mood>",'
        '"scenes":[{"i":1,"search_query":"<2-5 English words for a photo search: the specific car model, marque or place in THIS '
        'sentence; include the word car when a car is the subject>",'
        '"image_prompt":"<one vivid sentence, 25-45 words, describing exactly what the camera sees: the specific vehicle (marque, '
        'model, colour, era), the setting, camera angle/shot type and light. No text, no readable number plates, no logos, no '
        'recognisable real people.>","sfx":"engine|road|factory|none"}]}\n'
        "Rules:\n"
        "- exactly one entry per sentence, same numbering; be historically accurate; never invent facts.\n"
        "- EVERY image must feature a vintage British car or a period automotive scene (street, garage, factory, showroom, "
        "police station, motorway). For abstract or figurative sentences, show a concrete car scene that fits the mood - never a "
        "literal object such as fruit, insects, animals or drawings.\n"
        "- vary the shots: wide establishing, low-angle hero shot, close-up of grille/badge/headlamp/dashboard, interior, "
        "motion on the road, rear three-quarter, night with headlights.\n\nSENTENCES:\n" + numbered)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.5}}
    data = json.loads(gemini_call(body, key))
    items = data.get("scenes", []) if isinstance(data, dict) else data
    by_i = {}
    for x in items:
        try: by_i[int(x["i"])] = x
        except Exception: pass
    topic = str(data.get("topic", "")).strip() if isinstance(data, dict) else ""
    style = str(data.get("style", "")).strip() if isinstance(data, dict) else ""
    return topic, style, [by_i.get(i + 1) for i in range(len(units))]


def stage_plan(a, st):
    script = re.sub(r"[ \t]+", " ", Path(a.script).read_text(encoding="utf-8")).strip()
    if not script: raise SystemExit("Script is empty")
    units = split_units(script)
    topic, style, notes, key = guess_topic(script), "", [None] * len(units), os.getenv("GEMINI_API_KEY", "").strip()
    if not key: st["warnings"].append("No GEMINI_API_KEY secret found - using basic keyword logic (pictures will match worse)")
    if key and not a.offline:
        try:
            t, sty, notes = gemini_annotate(units, key); topic, style = t or topic, sty
            log(f"Gemini directed {sum(1 for n in notes if n)}/{len(units)} sentences")
        except Exception as e:
            log("GEMINI FAILED:\n" + "\n".join(textwrap.wrap(str(e).replace(" || ", "\n"), 60, replace_whitespace=False)))
            st["warnings"].append("Gemini failed, used built-in keyword logic (see 'GEMINI FAILED' above)")
    scenes = []
    for i, u in enumerate(units):
        h, n = heuristic_scene(u, i, topic), notes[i] or {}
        sfx = n.get("sfx", h["sfx"])
        scenes.append(dict(i=i, narration=u, search_query=str(n.get("search_query") or h["search_query"]).strip(),
                           image_prompt=str(n.get("image_prompt") or h["image_prompt"]).strip(), sfx=sfx if sfx in SFX_KINDS else "none"))
    for i, sc in enumerate(scenes):       # smooth ambient sound so it does not flip every sentence
        w = [scenes[j]["sfx"] for j in (i - 1, i, i + 1) if 0 <= j < len(scenes)]
        sc["sfx_final"] = max(set(w), key=lambda k: (w.count(k), k == sc["sfx"]))
    if a.max_scenes: scenes = scenes[:a.max_scenes]
    st["scenes"], st["topic"], st["style"] = scenes, topic, style
    log(f"{len(scenes)} sentence-scenes. Topic: {topic}")


# ----------------------------------------------------------------- VOICE
def eleven_tts(text, out, prev="", nxt=""):
    key, voice = os.environ["ELEVENLABS_API_KEY"].strip(), os.getenv("ELEVENLABS_VOICE_ID", "").strip() or "JBFqnCBsd6RMkjVDRZzb"
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
                      headers={"xi-api-key": key}, timeout=180,
                      json={"text": text, "model_id": "eleven_multilingual_v2", "previous_text": prev, "next_text": nxt})
    if r.status_code >= 400: raise RuntimeError(f"ElevenLabs HTTP {r.status_code}: {r.text[:150]}")
    Path(out).write_bytes(r.content)


def edge_tts_save(text, out, voice):
    import edge_tts
    asyncio.run(edge_tts.Communicate(text, voice, rate="-4%").save(str(out)))


def silent_voice(text, out):
    secs = max(2.0, wc(text) / 2.5)
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", f"{secs:.2f}", out])


def stage_voice(a, st):
    work = Path(a.work)
    def synth_all(engine):
        for s in st["scenes"]:
            f = work / f"voice_{s['i']:03d}.{'mp3' if engine != 'silent' else 'wav'}"
            if engine == "eleven":
                k = s["i"]; sc = st["scenes"]
                eleven_tts(s["narration"], f, sc[k - 1]["narration"] if k else "", sc[k + 1]["narration"] if k + 1 < len(sc) else "")
            elif engine == "edge": edge_tts_save(s["narration"], f, a.voice)
            else: silent_voice(s["narration"], f)
            s["voice_file"], s["voice_dur"] = f.name, probe_dur(f)
            log(f"  voice {s['i'] + 1}/{len(st['scenes'])}: {s['voice_dur']:.1f}s")
        st["voice_engine_used"] = engine
    order = []
    if not a.offline:
        if a.voice_engine in ("auto", "eleven") and os.getenv("ELEVENLABS_API_KEY", "").strip(): order.append("eleven")
        order.append("edge")
    if a.offline: order.append("silent")
    for eng in order:
        try:
            synth_all(eng); log(f"Voice engine: {eng}")
            if eng == "silent": st["warnings"].append("No voice engine worked - video has silent narration")
            return
        except Exception as e:
            st["warnings"].append(f"Voice engine '{eng}' failed: {str(e)[:150]}"); log(f"  {eng} failed, trying next")
    raise SystemExit("VOICE FAILED - no narration engine worked:\n - " + "\n - ".join(st["warnings"]) +
                     "\nAdd an ELEVENLABS_API_KEY secret or re-run later.")


# ----------------------------------------------------------------- VISUALS
def download(url, path, headers=None):
    for attempt in range(3):
        r = requests.get(url, headers=headers or {"User-Agent": UA}, timeout=120, stream=True)
        if r.status_code == 429: time.sleep(3 * (attempt + 1)); continue
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 16): f.write(chunk)
        return
    raise RuntimeError("download rate-limited")


def strip_html(s): return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def wikimedia(query, path_base, used, credits):
    """Only returns a file whose title/description/categories really match the query."""
    r = requests.get("https://commons.wikimedia.org/w/api.php", headers={"User-Agent": UA}, timeout=30, params=dict(
        action="query", format="json", generator="search", gsrsearch=f"{query} filetype:bitmap", gsrnamespace=6,
        gsrlimit=30, prop="imageinfo", iiprop="url|size|mime|extmetadata", iiurlwidth=1920))
    pages = sorted(((r.json().get("query") or {}).get("pages") or {}).values(), key=lambda p: p.get("index", 99))
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime") not in ("image/jpeg", "image/png"): continue
        if ii.get("width", 0) < 1200 or ii.get("width", 0) < ii.get("height", 1) * 1.2: continue
        url = ii.get("thumburl") or ii.get("url")
        md = ii.get("extmetadata", {})
        lic = (md.get("LicenseShortName") or {}).get("value", "")
        if not url or url in used or not lic or "fair use" in lic.lower(): continue
        hay = " ".join([p.get("title", ""), strip_html((md.get("ImageDescription") or {}).get("value")),
                        strip_html((md.get("ObjectName") or {}).get("value")), strip_html((md.get("Categories") or {}).get("value")).replace("|", " ")])
        if not relevant(hay, query): continue
        path = Path(str(path_base) + (".png" if ii["mime"] == "image/png" else ".jpg"))
        download(url, path); used.add(url)
        credits.append(f"{p.get('title', '').replace('File:', '')} - {strip_html((md.get('Artist') or {}).get('value'))} "
                       f"- {lic} - {ii.get('descriptionurl', '')}")
        return path, "image"
    return None


def pexels(query, path_base, used, credits, want_video):
    key = os.getenv("PEXELS_API_KEY", "").strip()
    if not key: return None
    h = {"Authorization": key}
    if want_video:
        r = requests.get("https://api.pexels.com/videos/search", headers=h, timeout=30,
                         params={"query": query, "per_page": 15, "orientation": "landscape"})
        for v in r.json().get("videos", []):
            files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and (f.get("width") or 0) >= 1280]
            if v.get("duration", 0) < 3 or not files or v["id"] in used: continue
            if not relevant(v.get("url", "").split("/video/")[-1].replace("-", " "), query): continue
            f = min(files, key=lambda f: abs(f["width"] - 1920))
            path = Path(str(path_base) + ".mp4"); download(f["link"], path); used.add(v["id"])
            credits.append(f"Pexels video by {v.get('user', {}).get('name', '')} - {v.get('url', '')}")
            return path, "video"
        return None
    r = requests.get("https://api.pexels.com/v1/search", headers=h, timeout=30,
                     params={"query": query, "per_page": 15, "orientation": "landscape"})
    for p in r.json().get("photos", []):
        if p["id"] in used: continue
        if not relevant(p.get("alt", "") + " " + p.get("url", "").split("/photo/")[-1].replace("-", " "), query): continue
        path = Path(str(path_base) + ".jpg"); download(p["src"]["large2x"], path); used.add(p["id"])
        credits.append(f"Pexels photo by {p.get('photographer', '')} - {p.get('url', '')}")
        return path, "image"
    return None


AI_STYLE = ("cinematic 35mm film photograph, vintage British cars, muted natural colours, soft overcast light, shallow depth of field, "
            "highly detailed, professional automotive photography, no text, no captions, no logos, no watermark")
_ai = {"last": 0.0, "fails": 0}


def ai_image(prompt, seed, path_base, style=""):
    """AI picture for exactly this sentence (Pollinations; free without a key, faster with POLLINATIONS_API_KEY)."""
    key = os.getenv("POLLINATIONS_API_KEY", "").strip()
    wait = _ai["last"] + (5 if key else 16) - time.time()
    if wait > 0: time.sleep(wait)
    q = quote(f"{prompt}. {style or AI_STYLE}"[:900])
    if key: url, hdr = f"https://gen.pollinations.ai/image/{q}?width=1920&height=1080&model=flux&seed={seed}&nologo=true", {"Authorization": "Bearer " + key}
    else: url, hdr = f"https://image.pollinations.ai/prompt/{q}?width=1920&height=1080&model=flux&seed={seed}&nologo=true", {}
    hdr["User-Agent"] = UA
    for attempt in range(4):
        _ai["last"] = time.time()
        try:
            r = requests.get(url, headers=hdr, timeout=200)
        except requests.RequestException:
            time.sleep(10); continue
        if r.status_code == 429 or r.status_code >= 500: time.sleep(16); continue
        c = r.content
        ext = ".png" if c[:4] == b"\x89PNG" else ".jpg" if c[:2] == b"\xff\xd8" else None
        if r.status_code != 200 or not ext or len(c) < 20000: return None
        path = Path(str(path_base) + ext); path.write_bytes(c)
        return path, "image"
    return None


FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]


def make_card(path, i):
    cols = ["0x16301c", "0x2b1d12", "0x1a2233", "0x2a1a1a"]
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={cols[i % 4]}:s={W}x{H}", "-vf", "vignette=PI/3", "-frames:v", "1", path.name], cwd=path.parent)
    return path


def stage_visuals(a, st):
    used, credits = set(), []
    st["credits"] = credits
    have_pexels, mode, topic = bool(os.getenv("PEXELS_API_KEY", "").strip()), a.visuals, st.get("topic", "")
    order = {"auto": ["ai", "wiki", "pvideo", "pphoto"], "ai": ["ai"], "real": ["wiki", "pvideo", "pphoto"]}[mode]
    src_count = {}
    for s in st["scenes"]:
        i, base, got, src = s["i"], Path(a.work) / f"vis_{s['i']:03d}", None, ""
        queries = [q for q in dict.fromkeys([s["search_query"], keywords(s["narration"], 3)]) if q]
        for step in ([] if a.offline else order):
            try:
                if step == "wiki":
                    for q in queries:
                        got = wikimedia(q, base, used, credits)
                        if got: break
                        time.sleep(0.3)
                elif step in ("pvideo", "pphoto") and have_pexels:
                    for q in queries:
                        got = pexels(q, base, used, credits, step == "pvideo")
                        if got: break
                elif step == "ai" and _ai["fails"] < 3:
                    got = ai_image(s["image_prompt"], 1000 + i, base, st.get("style", ""))
                    _ai["fails"] = 0 if got else _ai["fails"] + 1
                    if _ai["fails"] == 3: st["warnings"].append("AI image service failed 3 times in a row - switched it off for the rest of this video")
            except Exception as e:
                log(f"  visual lookup error ({step}): {str(e)[:100]}"); got = None
            if got: src = step; break
        if got: s["visual_file"], s["visual_kind"], s["source"], s["wm"] = got[0].name, got[1], src, (src == "ai")
        else: s["visual_file"] = ""
        src_count[src or "-"] = src_count.get(src or "-", 0) + 1
        log(f"  {i + 1}/{len(st['scenes'])} [{src or 'none yet'}] {s['narration'][:60]}")
    # sentences with no picture reuse the nearest good one (different camera move) - never a random unrelated photo
    good = [s for s in st["scenes"] if s["visual_file"]]
    for s in st["scenes"]:
        if s["visual_file"]: continue
        if good:
            ref = min(good, key=lambda g: (abs(g["i"] - s["i"]), g["i"] > s["i"]))
            s["visual_file"], s["visual_kind"], s["source"], s["wm"] = ref["visual_file"], ref["visual_kind"], "reused", ref.get("wm", False)
        else:
            s["visual_file"], s["visual_kind"], s["source"] = make_card(Path(a.work) / f"vis_{s['i']:03d}.png", s["i"]).name, "image", "card"
        st["warnings"].append(f"Sentence {s['i'] + 1}: no matching picture found, reused a neighbouring one")
    if any(s.get("source") == "ai" for s in st["scenes"]):
        credits.append("Some images are AI-generated illustrations (Pollinations.ai). Tick 'altered or synthetic content' in YouTube Studio.")
    log("Picture sources: " + ", ".join(f"{k}={v}" for k, v in src_count.items()))


# ----------------------------------------------------------------- RENDER
def ts_ass(t):
    cs = int(round(t * 100)); return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ts_srt(t):
    ms = int(round(t * 1000)); return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def chunks_for(scene):
    words, chunks, cur = scene["narration"].split(), [], []
    for w in words:
        cur.append(w)
        if len(cur) >= 7 or (len(cur) >= 3 and re.search(r"[.,;:!?]$", w)):
            chunks.append(" ".join(cur)); cur = []
    if cur:
        if chunks and len(cur) < 3: chunks[-1] += " " + " ".join(cur)
        else: chunks.append(" ".join(cur))
    total, t, out = sum(len(c) for c in chunks) or 1, LEAD, []
    for c in chunks:
        d = scene["voice_dur"] * len(c) / total
        out.append((t, t + d, c)); t += d
    return out


ASS_HEAD = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,56,&H00FFFFFF,&H000000FF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,4,2,2,140,140,85,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def write_ass(scene, path):
    lines = [ASS_HEAD]
    for a, b, c in chunks_for(scene):
        c = c.replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{ts_ass(a)},{ts_ass(b)},Default,,0,0,0,,{{\\fad(120,120)}}{c}\n")
    Path(path).write_text("".join(lines), encoding="utf-8")


def sfx_source(kind, D):
    if kind == "engine": return f"anoisesrc=color=brown:amplitude=1.0:duration={D:.2f}:sample_rate=44100", "lowpass=f=170,tremolo=f=9:d=0.55", 0.30
    if kind == "road": return f"anoisesrc=color=pink:amplitude=1.0:duration={D:.2f}:sample_rate=44100", "bandpass=f=700:width_type=h:w=900,tremolo=f=3:d=0.3", 0.16
    if kind == "factory": return f"anoisesrc=color=pink:amplitude=1.0:duration={D:.2f}:sample_rate=44100", "lowpass=f=900,tremolo=f=4.5:d=0.9", 0.14
    return None


def motion(i, N):
    return [("1+0.12*on/%d" % N, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
            ("1.12-0.12*on/%d" % N, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
            ("1.10", "(iw-iw/zoom)*on/%d" % N, "(ih-ih/zoom)/2"),
            ("1.10", "(iw-iw/zoom)*(1-on/%d)" % N, "(ih-ih/zoom)/2")][i % 4]


def render_segment(s, work, D, style, subs):
    i, kind = s["i"], s["visual_kind"]
    seg = f"seg_{i:03d}.mkv"
    cmd = ["ffmpeg", "-y"]
    cmd += ["-i", s["visual_file"]] if kind == "image" else ["-stream_loop", "-1", "-i", s["visual_file"]]
    cmd += ["-i", s["voice_file"]]
    fc, mix = [], ["[va]"]
    if kind == "image":
        N = int(D * FPS) + 2; z, x, y = motion(i, N)
        pre = "crop=iw:ih*0.93:0:0," if (s.get("wm") and not os.getenv("POLLINATIONS_API_KEY", "").strip()) else ""
        fc.append(f"[0:v]{pre}scale=2400:1350:force_original_aspect_ratio=increase,crop=2400:1350,setsar=1,"
                  f"zoompan=z='{z}':x='{x}':y='{y}':d={N}:s={W}x{H}:fps={FPS}[v0]")
    else:
        fc.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS}[v0]")
    if style == "vintage":
        fc.append("[v0]eq=contrast=1.06:saturation=0.88:brightness=-0.02,curves=r='0/0.03 1/0.98':b='0/0.05 1/0.88',"
                  "vignette=angle=PI/5[v1]")
    else:
        fc.append("[v0]null[v1]")
    fc.append(f"[v1]fade=t=in:st=0:d=0.12,fade=t=out:st={D - 0.12:.2f}:d=0.12[v2]")
    fc.append(f"[v2]ass=s_{i:03d}.ass[vout]" if subs else "[v2]null[vout]")
    fc.append(f"[1:a]aresample=44100,aformat=channel_layouts=stereo,adelay={int(LEAD * 1000)}:all=1,apad=whole_dur={D:.2f}[va]")
    k = 2
    src = sfx_source(s.get("sfx_final", s["sfx"]), D)
    if src:
        cmd += ["-f", "lavfi", "-i", src[0]]
        fc.append(f"[{k}:a]{src[1]},aformat=channel_layouts=stereo,volume={src[2]},afade=t=in:d=0.08,afade=t=out:st={D - 0.08:.2f}:d=0.08[sa]")
        mix.append("[sa]"); k += 1
    if i > 0 and i % 4 == 0:  # occasional whoosh accent
        cmd += ["-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=1.0:duration=0.9:sample_rate=44100"]
        fc.append(f"[{k}:a]highpass=f=400,lowpass=f=5000,aformat=channel_layouts=stereo,afade=t=in:d=0.35,"
                  f"afade=t=out:st=0.35:d=0.5,volume=0.12,apad=whole_dur={D:.2f}[wh]")
        mix.append("[wh]"); k += 1
    fc.append("".join(mix) + f"amix=inputs={len(mix)}:duration=first:normalize=0[aout]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]", "-t", f"{D:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-maxrate", "12M", "-bufsize", "24M",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "pcm_s16le", seg]
    run(cmd, cwd=work)
    return seg


def stage_render(a, st):
    work, out = Path(a.work), Path(a.out); out.mkdir(parents=True, exist_ok=True)
    from concurrent.futures import ThreadPoolExecutor
    total = len(st["scenes"])
    for s in st["scenes"]:
        s["dur"] = max(2.0, s["voice_dur"] + LEAD + TAIL)
        if a.subtitles: write_ass(s, work / f"s_{s['i']:03d}.ass")
    workers = max(1, min(3, (os.cpu_count() or 1) // 2 or 1))
    log(f"  rendering {total} scenes with {workers} parallel worker(s)")
    def job(s):
        seg = render_segment(s, work, s["dur"], a.style, a.subtitles); log(f"  rendered scene {s['i'] + 1}/{total}"); return seg
    with ThreadPoolExecutor(workers) as ex: segs = list(ex.map(job, st["scenes"]))
    offset, srt, n = 0.0, [], 1
    for s in st["scenes"]:
        for c0, c1, c in chunks_for(s):
            srt.append(f"{n}\n{ts_srt(offset + c0)} --> {ts_srt(offset + c1)}\n{c}\n"); n += 1
        offset += s["dur"]
    (work / "list.txt").write_text("".join(f"file '{x}'\n" for x in segs))
    music = next((p for p in (os.getenv("MUSIC_FILE", ""), "assets/music.mp3") if p and os.path.exists(p)), None)
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt"]
    if music:
        cmd += ["-stream_loop", "-1", "-i", os.path.abspath(music)]
        af = (f"[1:a]atrim=0:{offset:.2f},volume=0.22,aformat=channel_layouts=stereo[m];[0:a]asplit=2[va][vs];"
              "[m][vs]sidechaincompress=threshold=0.02:ratio=10:attack=30:release=500[md];"
              "[va][md]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=44100[aout]")
    else:
        af = "[0:a]loudnorm=I=-16:TP=-1.5:LRA=11,aresample=44100[aout]"
    final = (out / "final.mp4").resolve()
    cmd += ["-filter_complex", af, "-map", "0:v", "-map", "[aout]", "-t", f"{offset:.2f}", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", final]
    run(cmd, cwd=work)
    (out / "subtitles.srt").write_text("\n".join(srt), encoding="utf-8")
    credits = ["IMAGE / VIDEO CREDITS (paste into YouTube description)", ""] + sorted(set(st.get("credits", [])))
    if not st.get("credits"): credits.append("(no third-party media used)")
    (out / "credits.txt").write_text("\n".join(credits), encoding="utf-8")
    (out / "scene_plan.json").write_text(json.dumps(st["scenes"], indent=1, ensure_ascii=False), encoding="utf-8")
    info = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                           "-of", "csv=p=0", str(final)], capture_output=True, text=True).stdout.strip()
    log(f"DONE: {final}  {info}  {probe_dur(final):.1f}s  {final.stat().st_size / 1e6:.1f} MB")


# ----------------------------------------------------------------- MAIN
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["all", "plan", "voice", "visuals", "render"])
    ap.add_argument("--script", default="work/script.txt"); ap.add_argument("--work", default="work"); ap.add_argument("--out", default="out")
    ap.add_argument("--voice", default="en-GB-RyanNeural"); ap.add_argument("--voice-engine", default="auto", choices=["auto", "edge", "eleven"])
    ap.add_argument("--style", default="vintage", choices=["vintage", "clean"])
    ap.add_argument("--visuals", default="auto", choices=["auto", "ai", "real"], help="auto: verified real photo else AI; ai: AI image for every sentence; real: photos only")
    ap.add_argument("--subtitles", default="true"); ap.add_argument("--max-scenes", type=int, default=0)
    ap.add_argument("--offline", action="store_true", help="no network: title cards + silent voice (for testing)")
    a = ap.parse_args(); a.subtitles = str(a.subtitles).lower() in ("1", "true", "yes", "on")
    Path(a.work).mkdir(parents=True, exist_ok=True)
    sp = Path(a.work) / "state.json"
    st = json.loads(sp.read_text()) if sp.exists() else {"scenes": [], "warnings": [], "credits": []}
    stages = {"plan": stage_plan, "voice": stage_voice, "visuals": stage_visuals, "render": stage_render}
    for name in (stages if a.stage == "all" else [a.stage]):
        log(f"== {name} =="); t = time.time()
        if name != "plan" and not st["scenes"]: raise SystemExit("Run the plan stage first")
        stages[name](a, st); sp.write_text(json.dumps(st, ensure_ascii=False, indent=1)); log(f"   ({time.time() - t:.0f}s)")
    if st["warnings"]: log("WARNINGS:\n - " + "\n - ".join(st["warnings"]))


if __name__ == "__main__":
    main()
