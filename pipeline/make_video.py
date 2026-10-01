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
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote
from pathlib import Path
import requests

W, H, FPS = 1920, 1080, 30
LEAD, TAIL = 0.12, 0.30          # silence before / after narration in each sentence-scene
UA = "OldBritishCarsVideoBot/1.0 (personal YouTube documentary project)"
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
    """Strict check: does the candidate's title/description really talk about what the sentence needs? (niche-agnostic)"""
    toks = qtokens(query)
    if not toks: return False
    hs = {stem(w) for w in re.findall(r"[a-z0-9]+", hay.lower())}
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
    return " ".join(top[:3]) if top else ""


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
                image_prompt=f"A scene that illustrates: {u}" + (f" (context: {topic})" if topic else ""))


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
        "You are the art director of a cinematic YouTube documentary/explainer video. The narration is split into "
        "numbered sentences, on ANY topic. For EVERY sentence design ONE striking, on-topic image.\n"
        'Return JSON only: {"topic":"<4-8 words: what this video is actually about>",'
        '"style":"<ONE reusable visual style line for ALL images: film stock, colour palette, light, era, mood - fitting '
        'this specific topic>",'
        '"scenes":[{"i":1,"search_query":"<2-5 English words for a real photo search: the specific subject, person, place '
        'or object named or implied by THIS sentence>",'
        '"image_prompt":"<one vivid sentence, 25-45 words, describing exactly what the camera sees for THIS sentence: '
        'subject, setting, camera angle/shot type and light. No text, no readable signage, no logos, no recognisable real '
        'people.>","sfx":"none"}]}\n'
        "Rules:\n"
        "- exactly one entry per sentence, same numbering; be accurate to the sentence; never invent facts.\n"
        "- every image must be concretely on-topic for what that sentence literally says - never a generic stock filler "
        "unrelated to the sentence's subject.\n"
        "- vary the shots across the video: wide establishing, close-up/detail, low or high angle, action/motion, interior, "
        "night/low light - whatever fits each sentence.\n\nSENTENCES:\n" + numbered)
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
        q = str(n.get("search_query") or h["search_query"]).strip()
        p = str(n.get("image_prompt") or h["image_prompt"]).strip()
        scenes.append(dict(i=i, narration=u, prompts=[dict(query=q, prompt=p)]))
    if a.max_scenes: scenes = scenes[:a.max_scenes]
    prompts_file = Path(a.work) / "image_prompts.txt"
    if prompts_file.exists():
        raw_lines = [ln.strip() for ln in prompts_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if raw_lines:
            n = min(len(raw_lines), len(scenes))
            total_imgs = 0
            for i in range(n):
                line = re.sub(r"^[^:]{0,60}:\s*", "", raw_lines[i])   # drop a leading label like "Sentence 3 image keywords:"
                parts = [x.strip() for x in line.split(",") if x.strip()] or [line]
                scenes[i]["prompts"] = [dict(query=x[:80], prompt=x) for x in parts]
                scenes[i]["user_prompt"] = True
                total_imgs += len(parts)
            log(f"Using {n} user-supplied sentence(s), {total_imgs} image keyword(s) total" +
                (f" ({len(raw_lines)} lines given, {len(scenes)} sentences - "
                 f"{'extra ignored' if len(raw_lines) > len(scenes) else 'rest auto-generated'})" if len(raw_lines) != len(scenes) else ""))
            if len(raw_lines) != len(scenes):
                st["warnings"].append(f"You pasted {len(raw_lines)} image-prompt lines but the script has {len(scenes)} sentences - "
                                      f"{'the extra lines were ignored' if len(raw_lines) > len(scenes) else 'the remaining sentences used automatic prompts'}. "
                                      f"For exact control, match the line count to the sentence count (commas inside one line = multiple images for that sentence).")
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


GEMINI_TTS_MODELS = ["gemini-3.8-flash-tts", "gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts"]


def gemini_tts(text, out, voice="Charon"):
    """Google AI Studio's own narrator voice (Gemini TTS) - a clear, measured option when ElevenLabs
    isn't configured. 'Charon' is a calm, deeper default that reads well for an older documentary
    audience; set GEMINI_TTS_VOICE to try another name from Google AI Studio's voice picker."""
    key = os.environ["GEMINI_API_KEY"].strip()
    body = {"contents": [{"parts": [{"text": text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}
    errors = []
    for model in GEMINI_TTS_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=120)
        except requests.RequestException as e:
            errors.append(f"{model}: {type(e).__name__}"); continue
        if r.status_code != 200:
            try: msg = r.json()["error"]["message"]
            except Exception: msg = r.text[:120]
            errors.append(f"{model}: HTTP {r.status_code} - {re.sub(r'AIza[\w-]+|AQ\.[\w.-]+', '<key>', msg)[:140]}")
            continue
        for part in r.json().get("candidates", [{}])[0].get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                import base64
                rate_m = re.search(r"rate=(\d+)", inline.get("mimeType", inline.get("mime_type", "")) or "")
                rate = rate_m.group(1) if rate_m else "24000"
                pcm = Path(str(out) + ".pcm"); pcm.write_bytes(base64.b64decode(inline["data"]))
                run(["ffmpeg", "-y", "-f", "s16le", "-ar", rate, "-ac", "1", "-i", pcm.name,
                     "-c:a", "libmp3lame", Path(out).name], cwd=Path(out).parent)
                pcm.unlink(missing_ok=True)
                return
        errors.append(f"{model}: no audio in response")
    raise RuntimeError(" || ".join(errors))


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
            elif engine == "gemini": gemini_tts(s["narration"], f, os.getenv("GEMINI_TTS_VOICE", "Charon").strip() or "Charon")
            elif engine == "edge": edge_tts_save(s["narration"], f, a.voice)
            else: silent_voice(s["narration"], f)
            s["voice_file"], s["voice_dur"] = f.name, probe_dur(f)
            log(f"  voice {s['i'] + 1}/{len(st['scenes'])}: {s['voice_dur']:.1f}s")
        st["voice_engine_used"] = engine
    order = []
    if not a.offline:
        if a.voice_engine in ("auto", "eleven") and os.getenv("ELEVENLABS_API_KEY", "").strip(): order.append("eleven")
        if a.voice_engine in ("auto", "gemini") and os.getenv("GEMINI_API_KEY", "").strip(): order.append("gemini")
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
            return path, "image"
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


def pixabay(query, path_base, used, credits, want_video):
    """Free real-photo/video search (pixabay.com/api/docs) - no billing, needs a free PIXABAY_API_KEY."""
    key = os.getenv("PIXABAY_API_KEY", "").strip()
    if not key: return None
    if want_video:
        r = requests.get("https://pixabay.com/api/videos/", timeout=30,
                         params={"key": key, "q": query, "per_page": 15, "safesearch": "true"})
        for v in r.json().get("hits", []):
            vf = v.get("videos", {}).get("large") or v.get("videos", {}).get("medium")
            if not vf or v["id"] in used or vf.get("width", 0) < 1280: continue
            if not relevant(v.get("tags", ""), query): continue
            path = Path(str(path_base) + ".mp4"); download(vf["url"], path); used.add(v["id"])
            credits.append(f"Pixabay video by {v.get('user', '')} - https://pixabay.com/videos/id-{v['id']}/")
            return path, "video"
        return None
    r = requests.get("https://pixabay.com/api/", timeout=30,
                     params={"key": key, "q": query, "per_page": 15, "image_type": "photo",
                             "orientation": "horizontal", "safesearch": "true", "min_width": 1200})
    for p in r.json().get("hits", []):
        if p["id"] in used: continue
        if not relevant(p.get("tags", ""), query): continue
        path = Path(str(path_base) + ".jpg"); download(p["largeImageURL"], path); used.add(p["id"])
        credits.append(f"Pixabay photo by {p.get('user', '')} - https://pixabay.com/photos/id-{p['id']}/")
        return path, "image"
    return None


def google_cse(query, path_base, used, credits):
    """Optional real-photo search via Google's official Custom Search API - needs GOOGLE_CSE_KEY + GOOGLE_CSE_ID.
    Google's free tier is 100 queries/day, then billed - only used when these secrets are actually set."""
    key, cx = os.getenv("GOOGLE_CSE_KEY", "").strip(), os.getenv("GOOGLE_CSE_ID", "").strip()
    if not key or not cx: return None
    r = requests.get("https://www.googleapis.com/customsearch/v1", timeout=30,
                     params={"key": key, "cx": cx, "q": query, "searchType": "image", "num": 10,
                             "safe": "active", "imgSize": "large"})
    data = r.json()
    if "error" in data: raise RuntimeError(data["error"].get("message", "Google CSE error")[:100])
    for item in data.get("items", []):
        link = item.get("link", "")
        if link in used: continue
        if not relevant(item.get("title", "") + " " + item.get("snippet", ""), query): continue
        ext = ".png" if link.lower().endswith(".png") else ".jpg"
        try:
            path = Path(str(path_base) + ext); download(link, path); used.add(link)
        except Exception:
            continue
        credits.append(f"Image via Google Search - {item.get('image', {}).get('contextLink', link)}")
        return path, "image"
    return None


AI_STYLE = ("cinematic 35mm film photograph, natural colours, soft realistic light, shallow depth of field, "
            "highly detailed, professional photography, no text, no captions, no logos, no watermark")
_ai = {"last": 0.0, "fails": 0}
GEMINI_IMAGE_MODELS = ["gemini-3.1-flash-image-preview", "gemini-3-pro-image-preview", "gemini-2.5-flash-image"]


def gemini_image(prompt, path_base, style=""):
    """Best-quality AI photo: Gemini's own native image model, correctly generated in 16:9 (no crop/stretch
    distortion). PAID per image (roughly $0.04-$0.13 depending on model) - billed to the Google Cloud project
    behind GEMINI_API_KEY, separate from that key's free text quota. Used only when no real photo was found."""
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key or os.getenv("DISABLE_GEMINI_IMAGE", "").strip(): return None
    full_prompt = f"{prompt}. {style or AI_STYLE}"[:900]
    body = {"contents": [{"parts": [{"text": full_prompt}]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "16:9"}}}
    for model in GEMINI_IMAGE_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=120)
        except requests.RequestException:
            continue
        if r.status_code != 200:
            if r.status_code in (400, 403) and "billing" in r.text.lower(): return None   # no billing enabled: stop trying, fall through to free AI
            continue
        for part in r.json().get("candidates", [{}])[0].get("content", {}).get("parts", []):
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                import base64
                data = base64.b64decode(inline["data"])
                ext = ".png" if "png" in inline.get("mimeType", inline.get("mime_type", "")) else ".jpg"
                path = Path(str(path_base) + ext); path.write_bytes(data)
                return path, "image"
    return None


def ai_image(prompt, seed, path_base, style=""):
    """AI picture for exactly this sentence: tries Gemini's native image model first (realistic, correct
    16:9, but paid), then falls back to Pollinations (free, without a key ~15s/image, can look softer/wrong
    aspect if it ignores the requested size)."""
    got = gemini_image(prompt, path_base, style)
    if got: return got
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
        path = Path(str(path_base) + ext)
        path.write_bytes(c)
        try:
            w, h = (int(x) for x in subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                    "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
                    capture_output=True, text=True).stdout.strip().split("x"))
            if w < 900 or abs(w / h - 16 / 9) > 0.35: return None   # too small or wrong aspect - Pollinations ignored our size hint
        except Exception:
            pass
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
    mode, topic = a.visuals, st.get("topic", "")
    have = lambda k: bool(os.getenv(k, "").strip())
    order = {"auto": ["pixabay", "gcse", "pexels_photo", "pexels_video", "ai"],
             "ai": ["ai"],
             "real": ["pixabay", "gcse", "pexels_photo", "pexels_video"]}[mode]
    src_count, total_imgs = {}, sum(len(s["prompts"]) for s in st["scenes"])
    done = 0
    for s in st["scenes"]:
        i, visuals = s["i"], []
        for j, pr in enumerate(s["prompts"]):
            base = Path(a.work) / f"vis_{i:03d}_{j}"
            queries = [q for q in dict.fromkeys([pr["query"], keywords(pr["prompt"], 3)]) if q]
            got, src = None, ""
            for step in ([] if a.offline else order):
                try:
                    if step == "pixabay":
                        for q in queries:
                            got = pixabay(q, base, used, credits, False) or pixabay(q, base, used, credits, True)
                            if got: break
                    elif step == "gcse" and have("GOOGLE_CSE_KEY"):
                        for q in queries:
                            got = google_cse(q, base, used, credits)
                            if got: break
                    elif step == "pexels_photo" and have("PEXELS_API_KEY"):
                        for q in queries:
                            got = pexels(q, base, used, credits, False)
                            if got: break
                    elif step == "pexels_video" and have("PEXELS_API_KEY"):
                        for q in queries:
                            got = pexels(q, base, used, credits, True)
                            if got: break
                    elif step == "ai" and _ai["fails"] < 3:
                        got = ai_image(pr["prompt"], 1000 + i * 7 + j, base, st.get("style", ""))
                        _ai["fails"] = 0 if got else _ai["fails"] + 1
                        if _ai["fails"] == 3: st["warnings"].append("AI image generation failed 3 times in a row - switched it off for the rest of this video")
                except Exception as e:
                    log(f"  visual lookup error ({step}): {str(e)[:100]}"); got = None
                if got: src = step; break
            if got: visuals.append(dict(file=got[0].name, kind=got[1], source=src, wm=(src == "ai")))
            src_count[src or "-"] = src_count.get(src or "-", 0) + 1
            done += 1
        s["visuals"] = visuals
        log(f"  {done}/{total_imgs} [{'+'.join(v['source'] for v in visuals) or 'none yet'}] {s['narration'][:55]}")
    # sentences with no picture reuse the nearest good one (different camera move) - never a random unrelated photo
    good = [v for s in st["scenes"] for v in s["visuals"]]
    for s in st["scenes"]:
        if s["visuals"]: continue
        if good:
            ref = good[min(range(len(good)), key=lambda k: abs(st["scenes"].index(s) - k))]
            s["visuals"] = [dict(ref, source="reused")]
        else:
            s["visuals"] = [dict(file=make_card(Path(a.work) / f"vis_{s['i']:03d}.png", s["i"]).name, kind="image", source="card", wm=False)]
        st["warnings"].append(f"Sentence {s['i'] + 1}: no matching picture found, reused a neighbouring one")
    if any(v["source"] == "ai" for s in st["scenes"] for v in s["visuals"]):
        credits.append("Some images are AI-generated illustrations. Tick 'altered or synthetic content' in YouTube Studio.")
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


ACCENT = "&H2FC7FF&"   # ASS colour (BGR): a warm gold/amber accent for highlighted words and stat cards


def ass_head():
    """Built fresh at render time (not a module-level constant) so it always matches the chosen
    --resolution; baking W/H in at import time was a bug that mis-scaled subtitles on 720p renders."""
    fsize, statsize = (56, 96) if H >= 1000 else (40, 68)
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{fsize},&H00FFFFFF,&H000000FF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,4,2,2,140,140,85,1
Style: Stat,DejaVu Sans,{statsize},&H00FFFFFF,&H000000FF,&H00302000,&HD0201004,-1,0,0,0,100,100,0,0,3,0,10,5,80,80,60,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

# ordered: currency, then number+unit, then a bare year, then any other longish number
STAT_PATTERNS = [
    re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?"),
    re.compile(r"\b\d[\d,]*(?:\.\d+)?\s?(?:mph|km/h|kmh|hp|bhp|cc|kg|lbs?|miles?|mm|cm|litres?|liters?|seconds?|secs?|years?|hours?)\b", re.I),
    re.compile(r"\b(1[6-9]\d{2}|20\d{2})\b"),
    re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d{3,}\b"),
]


def find_stat(text):
    """First eye-catching number/fact in a sentence (money, a spec with a unit, a year, or a big number)."""
    for pat in STAT_PATTERNS:
        m = pat.search(text)
        if m: return m.group(0).strip()
    return None


def highlight(chunk_text):
    """Wraps the most eye-catching word/number in a chunk with the accent colour for a kinetic-caption look."""
    for pat in STAT_PATTERNS:
        m = pat.search(chunk_text)
        if m:
            return chunk_text[:m.start()] + "{\\c" + ACCENT + "}" + m.group(0) + "{\\c&HFFFFFF&}" + chunk_text[m.end():]
    m = re.search(r"(?<!^)(?<=\s)[A-Z][a-zA-Z'\-]{2,}", chunk_text)
    if m:
        return chunk_text[:m.start()] + "{\\c" + ACCENT + "}" + m.group(0) + "{\\c&HFFFFFF&}" + chunk_text[m.end():]
    return chunk_text


def write_ass(scene, path):
    lines = [ass_head()]
    chunks = chunks_for(scene)
    for a, b, c in chunks:
        c = c.replace("{", "(").replace("}", ")")
        pop = "\\t(0,120,\\fscx112\\fscy112)\\t(120,220,\\fscx100\\fscy100)"
        lines.append(f"Dialogue: 0,{ts_ass(a)},{ts_ass(b)},Default,,0,0,0,,{{\\fad(120,120){pop}}}{highlight(c)}\n")
    stat = find_stat(scene["narration"])
    if stat:
        hit = next(((a, b) for a, b, c in chunks if stat.lower() in c.lower()), None)
        if hit:
            a, b = hit
            b = max(b, a + 1.6); b = min(b + 0.4, scene["voice_dur"] + LEAD + 0.2)
            pop = "\\t(0,200,\\fscx122\\fscy122)\\t(200,380,\\fscx100\\fscy100)"
            lines.append(f"Dialogue: 1,{ts_ass(a)},{ts_ass(b)},Stat,,0,0,0,,{{\\fad(150,200){pop}}}{stat}\n")
    Path(path).write_text("".join(lines), encoding="utf-8")


def motion(i, N):
    return [("1+0.12*on/%d" % N, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
            ("1.12-0.12*on/%d" % N, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
            ("1.10", "(iw-iw/zoom)*on/%d" % N, "(ih-ih/zoom)/2"),
            ("1.10", "(iw-iw/zoom)*(1-on/%d)" % N, "(ih-ih/zoom)/2")][i % 4]


def ensure_grain_tile(work):
    tile = work / "grain_tile.png"
    if not tile.exists():
        run(["ffmpeg", "-y", "-f", "lavfi", "-i", "nullsrc=s=256x256:d=1", "-vf", "geq=random(1)*255:128:128", "-frames:v", "1", tile.name], cwd=work)
    return tile


def render_segment(s, work, D, style, subs):
    """Renders one sentence's clip. Normally one image/clip for the whole sentence; if the sentence was
    given several image keywords (comma-separated in the image-prompts box), it shows all of them in
    sequence, sharing the sentence's screen time, with a quick internal dissolve between them. No
    per-segment fades or synthetic sound effects here - the whole video is stitched together afterwards
    with real crossfade dissolves (see stage_render), and the only audio is the clean narration (no
    ambient noise bed - that was the source of the 'noise' under the voice in earlier versions)."""
    i, visuals = s["i"], s["visuals"]
    seg = f"seg_{i:03d}.mkv"
    cmd = ["ffmpeg", "-y"]
    for v in visuals:
        cmd += ["-i", v["file"]] if v["kind"] == "image" else ["-stream_loop", "-1", "-i", v["file"]]
    voice_idx = len(visuals)
    cmd += ["-i", s["voice_file"]]
    grain_idx = None
    if style == "vintage":
        cmd += ["-loop", "1", "-i", "grain_tile.png"]; grain_idx = voice_idx + 1

    n = len(visuals)
    T = min(0.25, D / (n * 6)) if n > 1 else 0.0
    dk = (D + (n - 1) * T) / n
    fc, labels = [], []
    for k, v in enumerate(visuals):
        N = int(dk * FPS) + 2; z, x, y = motion(i * 7 + k, N)
        if v["kind"] == "image":
            pre = "crop=iw:ih*0.93:0:0," if (v.get("wm") and not os.getenv("POLLINATIONS_API_KEY", "").strip()) else ""
            OW, OH = int(W * 1.25), int(H * 1.25)
            fc.append(f"[{k}:v]{pre}scale={OW}:{OH}:force_original_aspect_ratio=increase,crop={OW}:{OH},setsar=1,"
                      f"zoompan=z='{z}':x='{x}':y='{y}':d={N}:s={W}x{H}:fps={FPS}[c{k}]")
        else:
            fc.append(f"[{k}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS},trim=duration={dk:.2f}[c{k}]")
        labels.append(f"c{k}")
    cur = labels[0]
    for k in range(1, n):
        nxt = f"vx{k}"
        fc.append(f"[{cur}][{labels[k]}]xfade=transition=fade:duration={T:.3f}:offset={dk - T:.3f}[{nxt}]")
        cur = nxt
    if style == "vintage":
        fc.append(f"[{cur}]eq=contrast=1.06:saturation=0.88:brightness=-0.02,curves=r='0/0.03 1/0.98':b='0/0.05 1/0.88'[vg]")
        fc.append(f"[{grain_idx}:v]scale={W}:{H}:flags=neighbor[gr]")
        fc.append("[vg][gr]blend=all_mode=screen:all_opacity=0.045[vg2]")
        fc.append("[vg2]vignette=angle=PI/5[v1]")
    else:
        fc.append(f"[{cur}]null[v1]")
    fc.append(f"[v1]ass=s_{i:03d}.ass[vout]" if subs else "[v1]null[vout]")
    fc.append(f"[{voice_idx}:a]aresample=44100,aformat=channel_layouts=stereo,adelay={int(LEAD * 1000)}:all=1,apad=whole_dur={D:.2f}[aout]")
    cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]", "-t", f"{D:.2f}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-maxrate", "12M", "-bufsize", "24M",
            "-pix_fmt", "yuv420p", "-r", str(FPS),
            "-c:a", "pcm_s16le", seg]
    run(cmd, cwd=work)
    return seg


def _xfade_pair(work, pair, xfade_dur, tag):
    """Merges exactly 2 clips with a crossfade dissolve. Only 2 inputs open at once, so this
    is safe at any scene count - the crash on a 220-scene video was GitHub's runner running
    out of memory from opening all 220 clips in a single ffmpeg process at once."""
    (pa, da), (pb, db) = pair
    T = max(0.08, min(xfade_dur, 0.4 * da, 0.4 * db))
    out = f"xf_{tag}.mkv"
    fc = (f"[0:v][1:v]xfade=transition=fade:duration={T:.3f}:offset={da - T:.3f}[vout];"
          f"[0:a][1:a]acrossfade=d={T:.3f}:c1=tri:c2=tri[aout]")
    run(["ffmpeg", "-y", "-i", pa, "-i", pb, "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-maxrate", "12M", "-bufsize", "24M",
         "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "pcm_s16le", out], cwd=work)
    return out, da + db - T


def crossfade_merge(work, segs, durs, xfade_dur=0.35, workers=None):
    """Stitches every sentence's clip into one stream with a real dissolve at each image change,
    merging in memory-safe pairs (binary-tree rounds) instead of opening every clip at once."""
    items = list(zip(segs, durs))
    if not items: raise SystemExit("No rendered scenes to stitch together")
    workers = workers or max(1, min(3, (os.cpu_count() or 1) // 2 or 1))
    round_no = 0
    while len(items) > 1:
        round_no += 1
        pairs = [items[i:i + 2] for i in range(0, len(items), 2)]
        def job(args):
            idx, pr = args
            if len(pr) == 1: return pr[0]
            return _xfade_pair(work, pr, xfade_dur, f"r{round_no}_{idx}")
        with ThreadPoolExecutor(workers) as ex:
            items = list(ex.map(job, enumerate(pairs)))
        log(f"  crossfade round {round_no}: {len(items)} clip(s) remaining")
    out_name, total_dur = items[0]
    final = "merged.mkv"
    run(["ffmpeg", "-y", "-i", out_name, "-vf", f"fade=t=in:st=0:d=0.3,fade=t=out:st={max(0, total_dur - 0.3):.2f}:d=0.3",
         "-af", f"afade=t=in:d=0.3,afade=t=out:st={max(0, total_dur - 0.3):.2f}:d=0.3",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-maxrate", "12M", "-bufsize", "24M",
         "-pix_fmt", "yuv420p", "-r", str(FPS), "-c:a", "pcm_s16le", final], cwd=work)
    return final


def stage_render(a, st):
    work, out = Path(a.work), Path(a.out); out.mkdir(parents=True, exist_ok=True)
    total = len(st["scenes"])
    for s in st["scenes"]:
        s["dur"] = max(2.0, s["voice_dur"] + LEAD + TAIL)
        if a.subtitles: write_ass(s, work / f"s_{s['i']:03d}.ass")
    if a.style == "vintage": ensure_grain_tile(work)
    workers = max(1, min(3, (os.cpu_count() or 1) // 2 or 1))
    log(f"  rendering {total} scenes with {workers} parallel worker(s)")
    def job(s):
        seg = render_segment(s, work, s["dur"], a.style, a.subtitles); log(f"  rendered scene {s['i'] + 1}/{total}"); return seg
    with ThreadPoolExecutor(workers) as ex: segs = list(ex.map(job, st["scenes"]))
    srt, n, offset = [], 1, 0.0
    for s in st["scenes"]:
        for c0, c1, c in chunks_for(s):
            srt.append(f"{n}\n{ts_srt(offset + c0)} --> {ts_srt(offset + c1)}\n{c}\n"); n += 1
        offset += s["dur"]
    durs = [s["dur"] for s in st["scenes"]]
    log("  stitching with crossfade dissolves...")
    merged = crossfade_merge(work, segs, durs)
    offset = sum(durs) - sum(max(0.08, min(0.35, 0.4 * durs[i - 1], 0.4 * durs[i])) for i in range(1, len(durs)))
    music = next((p for p in (os.getenv("MUSIC_FILE", ""), "assets/music.mp3") if p and os.path.exists(p)), None)
    cmd = ["ffmpeg", "-y", "-i", merged]
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


# ----------------------------------------------------------------- METADATA
def stage_metadata(a, st):
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    script = " ".join(sc["narration"] for sc in st["scenes"])
    key = os.getenv("GEMINI_API_KEY", "").strip()
    data = None
    if key and not a.offline:
        prompt = (
            "You are a YouTube SEO strategist. Based on this video's narration, write "
            'metadata. Return JSON only: {"titles":["<title 1, under 70 chars, curiosity-driven>","<title 2, different angle>",'
            '"<title 3, different angle>"],"description":"<2-3 paragraph YouTube description: a hook, then what the video '
            'covers, ending with a subscribe line. Plain text, no markdown.>","hashtags":["#tag", ...8 to 12 short hashtags...],'
            '"keywords":["keyword phrase", ...12 to 18 SEO search phrases a viewer might type, comma-style, no # symbol...]}\n'
            "Rules: titles must be honest to the content (no clickbait that misleads), no ALL CAPS, no emoji spam (max 1 emoji "
            "per title). Topic: " + (st.get("topic") or "see narration below") + "\n\nNARRATION:\n" + script[:6000] +
            '\n\nAlso include "thumbnail_prompt": one vivid sentence (25-40 words) describing an outstanding, '
            'photorealistic, HD YouTube-thumbnail image for this video - a single striking hero shot, dramatic '
            'light, sharp focus, no text/logos/watermarks, nothing generic or stock-looking.')
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.7}}
        try:
            data = json.loads(gemini_call(body, key))
        except Exception as e:
            log("METADATA FAILED:\n" + "\n".join(textwrap.wrap(str(e).replace(" || ", "\n"), 60, replace_whitespace=False)))
            st["warnings"].append("SEO metadata generation failed, wrote a basic fallback instead (see 'METADATA FAILED' above)")
    if not data:
        base = (st.get("topic") or "This Video").strip()
        data = {"titles": [f"{base} - The Full Story", f"What You Didn't Know About {base}", f"{base}: Explained"],
                "description": f"A closer look at {base.lower()}. Subscribe for more.",
                "hashtags": ["#video", "#documentary", "#explainer"],
                "keywords": [base.lower()] if base else [],
                "thumbnail_prompt": f"A striking, photorealistic HD hero shot representing {base.lower()}, dramatic light, sharp focus"}
    lines = ["=== TITLES (pick one) ===", ""]
    lines += [f"{i + 1}. {t}" for i, t in enumerate(data.get("titles", []))]
    lines += ["", "=== DESCRIPTION ===", "", data.get("description", ""),
              "", "=== HASHTAGS ===", "", " ".join(data.get("hashtags", [])),
              "", "=== SEO KEYWORDS (comma-separated, paste into YouTube 'Tags') ===", "", ", ".join(data.get("keywords", [])),
              "", "=== THUMBNAIL IMAGE PROMPT (also used to render thumbnail.jpg) ===", "", data.get("thumbnail_prompt", "")]
    (out / "youtube_metadata.txt").write_text("\n".join(lines), encoding="utf-8")
    (out / "youtube_metadata.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    log("Wrote youtube_metadata.txt")
    if data.get("thumbnail_prompt") and not a.offline:
        try:
            got = ai_image(data["thumbnail_prompt"], 9001, out / "thumbnail", st.get("style", ""))
            if got:
                run(["ffmpeg", "-y", "-i", got[0].name, "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720",
                     "thumbnail.jpg"], cwd=out)
                if got[0].name != "thumbnail.jpg": (out / got[0].name).unlink(missing_ok=True)
                log("Wrote thumbnail.jpg")
            else:
                st["warnings"].append("Could not generate a thumbnail image automatically - use the thumbnail prompt above yourself")
        except Exception as e:
            st["warnings"].append(f"Thumbnail image generation failed: {str(e)[:120]}")


# ----------------------------------------------------------------- MAIN
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all", choices=["all", "plan", "voice", "visuals", "render", "metadata"])
    ap.add_argument("--script", default="work/script.txt"); ap.add_argument("--work", default="work"); ap.add_argument("--out", default="out")
    ap.add_argument("--voice", default="en-GB-RyanNeural"); ap.add_argument("--voice-engine", default="auto", choices=["auto", "edge", "eleven", "gemini"])
    ap.add_argument("--style", default="vintage", choices=["vintage", "clean"])
    ap.add_argument("--visuals", default="auto", choices=["auto", "ai", "real"], help="auto: verified real photo else AI; ai: AI image for every sentence; real: photos only")
    ap.add_argument("--subtitles", default="true"); ap.add_argument("--max-scenes", type=int, default=0)
    ap.add_argument("--resolution", default="1080p", choices=["1080p", "720p"])
    ap.add_argument("--offline", action="store_true", help="no network: title cards + silent voice (for testing)")
    a = ap.parse_args(); a.subtitles = str(a.subtitles).lower() in ("1", "true", "yes", "on")
    global W, H
    if a.resolution == "720p": W, H = 1280, 720
    Path(a.work).mkdir(parents=True, exist_ok=True)
    sp = Path(a.work) / "state.json"
    st = json.loads(sp.read_text()) if sp.exists() else {"scenes": [], "warnings": [], "credits": []}
    stages = {"plan": stage_plan, "metadata": stage_metadata, "voice": stage_voice, "visuals": stage_visuals, "render": stage_render}
    for name in (stages if a.stage == "all" else [a.stage]):
        log(f"== {name} =="); t = time.time()
        if name not in ("plan",) and not st["scenes"]: raise SystemExit("Run the plan stage first")
        stages[name](a, st); sp.write_text(json.dumps(st, ensure_ascii=False, indent=1)); log(f"   ({time.time() - t:.0f}s)")
    if st["warnings"]: log("WARNINGS:\n - " + "\n - ".join(st["warnings"]))


if __name__ == "__main__":
    main()
