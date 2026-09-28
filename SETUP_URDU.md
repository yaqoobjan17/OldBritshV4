# Old British Cars — 1-Click Video (V4)

Script paste karo → ek button → cloud me voice, images/clips, SFX, animation, subtitles sab bante hain → 1080p MP4 phone me aa jati hai (Movies/OldBritishCars).

## Setup (sirf ek baar, ~10 minute, sab phone se ho sakta hai)

1. GitHub par naya **Private** repo banao. Is zip ka poora content usme upload karo
   (`.github` folder, `pipeline`, `app`, gradle files — sab).
2. Repo → **Actions → Build APK → Run workflow**. Khatam hone par artifact download karo, `app-debug.apk` phone me install karo.
3. Repo → **Settings → Secrets and variables → Actions → New repository secret**. Ye sab OPTIONAL hain
   (bina keys ke bhi video banti hai — free British voice + Wikimedia photos):
   - `GEMINI_API_KEY` — behtar scene planning aur photo search words
   - `ELEVENLABS_API_KEY` (+ `ELEVENLABS_VOICE_ID`) — premium voice
   - `PEXELS_API_KEY` — free stock video clips (road/engine scenes ke liye)
4. GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate. Sirf isi repo ko select karo.
   Permissions: **Actions = Read and write**, **Contents = Read**.
5. App kholo → **Setup** → `owner/repo` aur token daalo → Save.

## Roz ka kaam
Script paste → **CREATE VIDEO**. Progress bar 4 steps dikhata hai (Plan → Voice → Visuals → Render).
App band bhi kar do to video cloud me banti rehti hai; dobara khologe to khud download kar leti hai.

## Extra
- Background music: koi royalty-free `music.mp3` repo me `assets/music.mp3` par rakh do — voice ke neeche khud halki ho jati hai (ducking).
- `credits.txt` (app me neeche dikhta hai) — Wikimedia/Pexels photos ka credit hai. **YouTube description me paste karna zaroori hai** (CC-BY licenses ke liye).
- `subtitles.srt` bhi release me hoti hai (YouTube CC upload ke liye).
- Purani 8 videos ke baad releases khud delete hoti hain.

## Limits (sach)
- Photos real hain (Wikimedia/Pexels), AI-generated nahi. Har rare model ki photo na mile to fallback generic vintage-car footage/title card lagta hai.
- GitHub free plan me private repo ke ~2000 Actions minutes/month milte hain. Ek lambi video ko cloud me kai minute lagte hain; GitHub → Settings → Billing me usage dekhte raho.
- Free British voice (Edge-TTS) kabhi kabhi cloud IP par block ho sakti hai; tab video fail hoti hai aur app batati hai — ElevenLabs key add kar do.
