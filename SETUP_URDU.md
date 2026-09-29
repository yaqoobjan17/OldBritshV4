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
   - `POLLINATIONS_API_KEY` — AI images tez aur bina watermark (bina key ke bhi chalta hai, magar ~15 sec per image)
4. GitHub → Settings → Developer settings → **Fine-grained tokens** → Generate. Sirf isi repo ko select karo.
   Permissions: **Actions = Read and write**, **Contents = Read**.
5. App kholo → **Setup** → `owner/repo` aur token daalo → Save.

## Har sentence = alag image (V5)
Script sentence-by-sentence toot-ti hai. Har sentence ki apni image hoti hai jo usi sentence se match karti hai.
App me 3 modes: **Auto** (asli photo tab jab wo sentence se sach me match kare, warna AI image), **AI har sentence ke liye**, **Real photos only**.
**GEMINI_API_KEY zaroor daalo** (free): wo har sentence ke liye sahi search words aur image description likhta hai, is se matching bohat behtar hoti hai.

## V11: Kinetic captions + number/fact stat-cards
Subtitles ab simple safed text nahi, **kinetic style** hain: har phrase halka sa 'pop' karke aata hai, aur us phrase ka khaas lafz (number ya khaas naam) sunehri rang me highlight hota hai.

Agar sentence me koi number/fact ho (jaise '150 mph', '\$499,902', '1965', '220 hp'), to wo apne aap ek **bara animated stat-card** ban kar beech screen par pop hota hai — bilkul us reference video jaisa asar, lekin humari HD car photo ke oper.

Kuch bhi extra setup nahi karna, ye khud kaam karta hai. Bas Unpack chalana hai.

## V10: Kisi bhi niche ke liye + aapke apne image prompts + asli crossfade
Ab app kisi bhi topic ki script ke liye kaam karta hai (cars, kahani, cooking, tareekh — kuch bhi).

**Naya box: 'Image prompts (optional)'.** Script ke neeche ek aur box hai. Agar aap chahte ho ke har sentence ki apni tay-shuda image ho, to yahan har line par ek prompt/keyword paste karo — jitne sentences hain utni hi lines, isi tarteeb me. Pipeline har sentence ke liye wahi prompt istemal karega (na Gemini na keyword-guessing, seedha aapka prompt). Khali chhod do to pehle jaisa auto-matching chalega.

**Crossfade fix:** ab har sentence ki image asli dissolve (crossfade) ke sath badalti hai, hard-cut nahi hota, aur har image apne sentence ki lambai (2s ho ya 5s) ke hisab se hi chalti hai.

**Noise fix:** engine/road jaisi synthetic ambient awaz jo narration ke neeche baji jati thi, poori tarah hata di gayi hai. Ab sirf saaf narration hoti hai (aur agar music.mp3 lagayi ho to wo).

## V9: SEO pack + cinematic look
Video ke saath ab `youtube_metadata.txt` bhi banti hai: 3 titles, description, hashtags, tags — Gemini khud likhta hai.
App me video ready hone par **COPY TITLE, DESCRIPTION & TAGS** button aata hai, ek tap me sab clipboard me copy ho jata hai.
Vintage look me ab halka film-grain aur vignette bhi hota hai (extra render time bohat kam).

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
