#!/usr/bin/env python3
"""Old British Cars - one-click pipeline: script -> 1080p MP4.

Stages (each reads/writes work/state.json):
  plan    split script into scenes (Gemini if key, else built-in splitter)
  voice   narration per scene (ElevenLabs if key, else free Edge-TTS British voice)
  visuals photos/clips per scene (Wikimedia Commons + Pexels, fallback cards)
  render  Ken-Burns animation, vintage grade, SFX, subtitles -> final.mp4 (1920x1080)
"""
import argparse, asyncio, html, json, os, re, subprocess, sys, time
from pathlib import Path
import requests

W, H, FPS = 1920, 1080, 30
LEAD, TAIL = 0.25, 0.75          # silence before / after narration in each scene
UA = "OldBritishCarsVideoBot/1.0 (personal YouTube documentary project)"
SFX_KINDS = {"engine", "road", "factory", "none"}
GENERIC = ["vintage british car", "classic car road", "old car engine",
           "vintage car interior", "british countryside road", "classic car showroom"]


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
STOP = set("""The A An In On At It This That These Those He She They We I You When While By For With But And Or As After
Before Then So If Its His Her Their Our One Some Many Most Today Now From To Of What Why How Where Who There Here Even
Still Yet During Despite Although Because Since Once Every Each Both Was Were Is Are Had Has Have""".split())


def extract_query(text):
    seqs = re.findall(r"(?:[A-Z][A-Za-z0-9\-]+|\b\d{2,4}\b)(?:\s+(?:[A-Z][A-Za-z0-9\-]+|\d{2,4}))*", text)
    good = []
    for s in seqs:
        toks = s.split()
        while toks and toks[0] in STOP: toks.pop(0)
        if toks and not all(t.isdigit() for t in toks): good.append(" ".join(toks[:4]))
    good.sort(key=lambda g: (-(any(c.isdigit() for c in g) or len(g.split()) > 1), -len(g)))
    return good[0] if good else ""


def guess_sfx(text, i):
    t = text.lower()
    if re.search(r"engine|horsepower|race|racing|speed|motor|v8|v12|cylinder|rev", t): return "engine"
    if re.search(r"factory|plant|assembly|built|production|workers|workshop", t): return "factory"
    if re.search(r"drive|driving|road|journey|highway|motorway|travel", t): return "road"
    return "road" if i % 3 == 0 else "none"


def heuristic_plan(script):
    sents = re.split(r"(?<=[.!?])\s+", script.strip())
    parts = []
    for s in sents:                       # split very long sentences at commas
        if wc(s) > 40:
            parts += [p.strip() for p in re.split(r"(?<=,)\s+", s) if p.strip()]
        else:
            parts.append(s)
    scenes, cur, n = [], [], 0
    def flush():
        nonlocal cur, n
        if cur: scenes.append(" ".join(cur)); cur, n = [], 0
    for p in parts:
        w = wc(p)
        if cur and n + w > 30: flush()
        cur.append(p); n += w
        if n >= 18: flush()
    flush()
    out = []
    for i, t in enumerate(scenes):
        q = extract_query(t)
        out.append(dict(narration=t, search_query=q, fallback_query=GENERIC[i % len(GENERIC)], sfx=guess_sfx(t, i)))
    return out


def gemini_plan(script, key):
    prompt = (
        "You are the director of an Old British Cars YouTube documentary.\n"
        "Split the script into scenes of 15-30 words each, in order. Return JSON only:\n"
        '{"scenes":[{"narration":"<exact script text, unchanged>","search_query":"<2-4 English words: the specific car '
        'model, marque, place or object shown, suited to a photo search>","fallback_query":"<generic 2-3 word visual>",'
        '"sfx":"engine|road|factory|none"}]}\n'
        "Rules: never add, remove or rewrite any word of the script. Do not invent facts.\n\nSCRIPT:\n" + script)
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2}}
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
    r = requests.post(url, headers={"x-goog-api-key": key}, json=body, timeout=180)
    r.raise_for_status()
    data = json.loads(r.json()["candidates"][0]["content"]["parts"][0]["text"])
    items = data["scenes"] if isinstance(data, dict) else data
    out = []
    for s in items:
        n = str(s.get("narration", "")).strip()
        if not n: continue
        sfx = s.get("sfx", "none")
        out.append(dict(narration=n, search_query=str(s.get("search_query", "")).strip(),
                        fallback_query=str(s.get("fallback_query", "")).strip() or GENERIC[len(out) % len(GENERIC)],
                        sfx=sfx if sfx in SFX_KINDS else "none"))
    ratio = sum(wc(s["narration"]) for s in out) / max(1, wc(script))
    if not out or not 0.93 <= ratio <= 1.07:
        raise ValueError(f"Gemini plan does not match script (ratio {ratio:.2f})")
    return out


def stage_plan(a, st):
    script = re.sub(r"[ \t]+", " ", Path(a.script).read_text(encoding="utf-8")).strip()
    if not script: raise SystemExit("Script is empty")
    scenes, key = None, os.getenv("GEMINI_API_KEY", "").strip()
    if key and not a.offline:
        try:
            scenes = gemini_plan(script, key); log(f"Gemini planned {len(scenes)} scenes")
        except Exception as e:
            st["warnings"].append(f"Gemini plan failed, used built-in splitter: {str(e)[:120]}")
    if not scenes:
        scenes = heuristic_plan(script); log(f"Built-in splitter made {len(scenes)} scenes")
    if a.max_scenes: scenes = scenes[:a.max_scenes]
    for i, s in enumerate(scenes): s["i"] = i
    st["scenes"] = scenes


# ----------------------------------------------------------------- VOICE
def eleven_tts(text, out):
    key, voice = os.environ["ELEVENLABS_API_KEY"].strip(), os.getenv("ELEVENLABS_VOICE_ID", "").strip() or "JBFqnCBsd6RMkjVDRZzb"
    r = requests.post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
                      headers={"xi-api-key": key}, timeout=180,
                      json={"text": text, "model_id": "eleven_multilingual_v2"})
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
            if engine == "eleven": eleven_tts(s["narration"], f)
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
    r = requests.get("https://commons.wikimedia.org/w/api.php", headers={"User-Agent": UA}, timeout=30, params=dict(
        action="query", format="json", generator="search", gsrsearch=f"{query} filetype:bitmap", gsrnamespace=6,
        gsrlimit=20, prop="imageinfo", iiprop="url|size|mime|extmetadata", iiurlwidth=1920))
    pages = sorted(((r.json().get("query") or {}).get("pages") or {}).values(), key=lambda p: p.get("index", 99))
    for p in pages:
        ii = (p.get("imageinfo") or [{}])[0]
        if ii.get("mime") not in ("image/jpeg", "image/png"): continue
        if ii.get("width", 0) < 1200 or ii.get("width", 0) < ii.get("height", 1) * 1.2: continue
        url = ii.get("thumburl") or ii.get("url")
        md = ii.get("extmetadata", {})
        lic = (md.get("LicenseShortName") or {}).get("value", "")
        if not url or url in used or not lic or "fair use" in lic.lower(): continue
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
                         params={"query": query, "per_page": 12, "orientation": "landscape"})
        for v in r.json().get("videos", []):
            files = [f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4" and (f.get("width") or 0) >= 1280]
            if v.get("duration", 0) < 4 or not files or v["id"] in used: continue
            f = min(files, key=lambda f: abs(f["width"] - 1920))
            path = Path(str(path_base) + ".mp4"); download(f["link"], path); used.add(v["id"])
            credits.append(f"Pexels video by {v.get('user', {}).get('name', '')} - {v.get('url', '')}")
            return path, "video"
        return None
    r = requests.get("https://api.pexels.com/v1/search", headers=h, timeout=30,
                     params={"query": query, "per_page": 12, "orientation": "landscape"})
    for p in r.json().get("photos", []):
        if p["id"] in used: continue
        path = Path(str(path_base) + ".jpg"); download(p["src"]["large2x"], path); used.add(p["id"])
        credits.append(f"Pexels photo by {p.get('photographer', '')} - {p.get('url', '')}")
        return path, "image"
    return None


FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf"]


def make_card(text, path, i):
    cols = ["0x16301c", "0x2b1d12", "0x1a2233", "0x2a1a1a"]
    tf = Path(str(path) + ".txt"); tf.write_text(re.sub(r"(.{1,34})(\s+|$)", r"\1\n", text or "Old British Cars").strip(), encoding="utf-8")
    font = next((f for f in FONTS if os.path.exists(f)), None)
    vf = "vignette=PI/3"
    if font: vf += f",drawtext=fontfile={font}:textfile={tf.name}:fontcolor=white@0.9:fontsize=70:line_spacing=12:x=(w-text_w)/2:y=(h-text_h)/2"
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c={cols[i % 4]}:s={W}x{H}", "-vf", vf, "-frames:v", "1", path.name], cwd=path.parent)
    return path


def stage_visuals(a, st):
    work, used, credits = Path(a.work), set(), st.setdefault("credits", [])
    have_pexels = bool(os.getenv("PEXELS_API_KEY", "").strip())
    for s in st["scenes"]:
        i, base, got = s["i"], Path(a.work) / f"vis_{s['i']:03d}", None
        queries = [q for q in (s["search_query"], s["fallback_query"], GENERIC[i % len(GENERIC)]) if q]
        prefer_video = have_pexels and s["sfx"] in ("engine", "road")
        if not a.offline:
            attempts = []
            for q in queries:
                if prefer_video: attempts.append(lambda q=q: pexels(q, base, used, credits, True))
                attempts.append(lambda q=q: wikimedia(q, base, used, credits))
                if have_pexels:
                    if not prefer_video: attempts.append(lambda q=q: pexels(q, base, used, credits, True))
                    attempts.append(lambda q=q: pexels(q, base, used, credits, False))
            for fn in attempts:
                try:
                    got = fn()
                except Exception as e:
                    log(f"  visual lookup error: {str(e)[:100]}"); got = None
                if got: break
                time.sleep(0.4)
        if got:
            s["visual_file"], s["visual_kind"] = got[0].name, got[1]
        else:
            make_card(s["search_query"] or s["fallback_query"], Path(str(base) + ".png"), i)
            s["visual_file"], s["visual_kind"] = base.name + ".png", "image"
            if not a.offline: st["warnings"].append(f"Scene {i + 1}: no photo found, used title card")
        log(f"  visual {i + 1}/{len(st['scenes'])}: {s['visual_kind']} ({s['search_query'] or s['fallback_query']})")


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
    return [("1+0.15*on/%d" % N, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
            ("1.15-0.15*on/%d" % N, "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),
            ("1.12", "(iw-iw/zoom)*on/%d" % N, "(ih-ih/zoom)/2"),
            ("1.12", "(iw-iw/zoom)*(1-on/%d)" % N, "(ih-ih/zoom)/2")][i % 4]


def render_segment(s, work, D, style, subs):
    i, kind = s["i"], s["visual_kind"]
    seg = f"seg_{i:03d}.mkv"
    cmd = ["ffmpeg", "-y"]
    cmd += ["-i", s["visual_file"]] if kind == "image" else ["-stream_loop", "-1", "-i", s["visual_file"]]
    cmd += ["-i", s["voice_file"]]
    fc, mix = [], ["[va]"]
    if kind == "image":
        N = int(D * FPS) + 2; z, x, y = motion(i, N)
        fc.append(f"[0:v]scale=2400:1350:force_original_aspect_ratio=increase,crop=2400:1350,setsar=1,"
                  f"zoompan=z='{z}':x='{x}':y='{y}':d={N}:s={W}x{H}:fps={FPS}[v0]")
    else:
        fc.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1,fps={FPS}[v0]")
    if style == "vintage":
        fc.append("[v0]eq=contrast=1.06:saturation=0.88:brightness=-0.02,curves=r='0/0.03 1/0.98':b='0/0.05 1/0.88',"
                  "vignette=angle=PI/5[v1]")
    else:
        fc.append("[v0]null[v1]")
    fc.append(f"[v1]fade=t=in:st=0:d=0.4,fade=t=out:st={D - 0.4:.2f}:d=0.4[v2]")
    fc.append(f"[v2]ass=s_{i:03d}.ass[vout]" if subs else "[v2]null[vout]")
    fc.append(f"[1:a]aresample=44100,aformat=channel_layouts=stereo,adelay={int(LEAD * 1000)}:all=1,apad=whole_dur={D:.2f}[va]")
    k = 2
    src = sfx_source(s["sfx"], D)
    if src:
        cmd += ["-f", "lavfi", "-i", src[0]]
        fc.append(f"[{k}:a]{src[1]},aformat=channel_layouts=stereo,volume={src[2]},afade=t=in:d=0.6,afade=t=out:st={D - 0.6:.2f}:d=0.6[sa]")
        mix.append("[sa]"); k += 1
    if i > 0:  # whoosh accent at scene change
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
        s["dur"] = max(3.0, s["voice_dur"] + LEAD + TAIL)
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
