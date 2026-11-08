"""Generateur automatique de videos YouTube.

Accroche (hook) en clips video, bienvenue avec le nom de la chaine, puis storytelling
en chapitres. Script (Gemini), voix masculine (edge-tts), clips video et photos
(Pixabay), montage FFmpeg : fondus, look sombre, cartes style Notion, musique.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import edge_tts
import requests
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFilter, ImageFont

TOPIC = os.environ.get("TOPIC", "").strip() or (
    sys.argv[1] if len(sys.argv) > 1 else "L'histoire de Rome"
)
CHANNEL = os.environ.get("CHANNEL", "").strip()  # nom de ta chaine
MINUTES = int(os.environ.get("MINUTES", "20") or 20)  # duree visee de la video
WORDS_PER_MIN = 150  # debit de la voix (avec le rythme un peu lent choisi)
TOTAL_WORDS = MINUTES * WORDS_PER_MIN
N_CHAPTERS = max(4, round(MINUTES * 0.6))
SECONDS_PER_IMAGE = 7
VIDEO_RATIO = float(os.environ.get("VIDEO_RATIO", "0.6"))  # part de clips video
FPS = 25
OVERLAP = 15  # images du fondu enchaine (0,6 s)
LOOK = os.environ.get("LOOK", "cosmic")  # "cosmic" ou "none"
MUSIC = os.environ.get("MUSIC", "on") != "off"
VOICES = [os.environ.get("VOICE") or "fr-FR-HenriNeural", "fr-FR-RemyMultilingualNeural"]
VOICE_RATE = os.environ.get("VOICE_RATE", "-6%")  # un peu plus lent
VOICE_PITCH = os.environ.get("VOICE_PITCH", "-6Hz")  # un peu plus grave
MODELS = [
    m
    for m in [os.environ.get("GEMINI_MODEL"), "gemini-3.8-flash", "gemini-2.5-flash"]
    if m
]
LOOKS = {
    "cosmic": (
        "eq=contrast=1.12:brightness=-0.08:saturation=0.85,"
        "colorbalance=rs=-0.05:bs=0.09:rm=-0.03:bm=0.06:rh=-0.02:bh=0.04,"
        "vignette=angle=PI/5"
    )
}

OUT = Path("output")
WORK = OUT / "work"
WORK.mkdir(parents=True, exist_ok=True)

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
PIXABAY_KEY = os.environ["PIXABAY_API_KEY"].strip()
HEADERS = {"User-Agent": "Mozilla/5.0 (auto-video-generator)"}


def log(msg):
    print(msg, flush=True)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        log(p.stderr[-1500:])
        raise RuntimeError(f"Echec de la commande {cmd[0]}")
    return p.stdout


# ---------- 1. Script (Gemini) ----------
def ask(prompt, as_json=False):
    config = (
        types.GenerateContentConfig(response_mime_type="application/json")
        if as_json
        else None
    )
    last = None
    for model in MODELS:
        for attempt in range(1, 5):
            try:
                resp = client.models.generate_content(
                    model=model, contents=prompt, config=config
                )
                if resp.text:
                    time.sleep(5)
                    return resp.text.strip()
                last = "reponse vide"
            except Exception as e:  # noqa: BLE001
                last = e
                log(f"  Gemini ({model}) essai {attempt} : {str(e)[:150]}")
                if "404" in str(e) or "NOT_FOUND" in str(e):
                    break  # modele inconnu : on passe au suivant
            time.sleep(15 * attempt)
    raise RuntimeError(f"Gemini indisponible : {last}")


def clean(text):
    text = re.sub(r"[*#_`>\[\]]", "", text)
    return re.sub(r"\s+", " ", text).strip().strip('"')


def make_outline():
    prompt = (
        "Tu es scenariste de videos YouTube de vulgarisation (science et mysteres) "
        "en francais, specialiste de la retention d'audience.\n"
        f"Sujet : {TOPIC}\n"
        f"Prepare le plan d'une video de {MINUTES} minutes en {N_CHAPTERS} chapitres. "
        "La tension doit monter d'un chapitre a l'autre : chacun relance la curiosite "
        "et le dernier apporte la revelation ou la conclusion la plus marquante.\n"
        "Reponds UNIQUEMENT avec du JSON de cette forme :\n"
        '{"titre_video": "titre YouTube accrocheur de moins de 70 caracteres", '
        '"hook_mots_cles": "3 mots en ANGLAIS pour trouver des videos spectaculaires", '
        '"chapitres": [{"titre": "titre court en francais", '
        '"mots_cles": "2 ou 3 mots en ANGLAIS pour une image illustrative, '
        'par exemple roman colosseum ruins"}]}'
    )
    text = ask(prompt, as_json=True).replace("```json", "").replace("```", "")
    data = json.loads(text)
    if isinstance(data, list):
        data = {"chapitres": data}
    chapters = []
    for c in data.get("chapitres", [])[:N_CHAPTERS]:
        chapters.append(
            {
                "titre": str(c.get("titre", "Chapitre")).strip(),
                "mots_cles": str(c.get("mots_cles") or TOPIC).strip(),
            }
        )
    if len(chapters) < 3:
        raise RuntimeError("Plan trop court")
    return {
        "titre_video": str(data.get("titre_video") or TOPIC).strip(),
        "hook_mots_cles": str(data.get("hook_mots_cles") or chapters[0]["mots_cles"]).strip(),
        "chapitres": chapters,
    }


def write_hook(outline):
    prompt = (
        f"Ecris l'accroche (hook) d'une video YouTube de vulgarisation sur : \"{TOPIC}\" "
        f"(titre de la video : {outline['titre_video']}).\n"
        "Regles :\n"
        "- 60 a 80 mots maximum (environ 25 secondes a voix haute).\n"
        "- Commence par l'idee la plus surprenante : un fait choquant, un scenario glacant "
        "ou une question derangeante. Jamais de bonjour, jamais « dans cette video ».\n"
        "- Phrases tres courtes, rythme rapide, vocabulaire concret.\n"
        "- Cree une boucle ouverte : promets une revelation sans la donner.\n"
        "- Termine par une phrase qui donne envie de rester.\n"
        "- Vouvoiement. N'invente aucun chiffre ni fait dont tu n'es pas certain.\n"
        "Imagine trois accroches differentes, puis renvoie UNIQUEMENT la plus percutante, "
        "sans guillemets ni commentaire."
    )
    return clean(ask(prompt))


def welcome_text():
    name = f" sur {CHANNEL}" if CHANNEL else ""
    return (
        f"Bienvenue{name}. Si ce genre de sujet vous passionne, abonnez-vous. "
        "Et surtout, restez jusqu'a la fin : la conclusion pourrait bien changer "
        "votre regard. Commencons."
    )


def write_chapter(i, chapters, words):
    n = len(chapters)
    titres = " | ".join(c["titre"] for c in chapters)
    rules = (
        "Storytelling : raconte, ne liste pas. Fais vivre une scene concrete, une "
        "image mentale, un exemple precis. Parle directement au spectateur (vouvoiement), "
        "pose une question de temps en temps, alterne phrases courtes et plus longues. "
        "Cree de la tension : un mystere, un paradoxe ou un enjeu qui monte. "
        "N'invente aucun chiffre, aucune citation ni aucune etude dont tu n'es pas certain. "
        "Evite les formules creuses (« il est important de noter »). "
    )
    if i == 0:
        rules += "Entre directement dans le sujet, l'accroche vient d'etre dite. "
    else:
        rules += "Ne dis pas bonjour, la video est deja commencee. "
    if i < n - 1:
        rules += (
            f"Termine par une phrase de transition qui relance la curiosite vers la suite "
            f"(« {chapters[i + 1]['titre']} ») sans rien reveler. "
        )
    else:
        rules += (
            "Termine par une conclusion marquante qui reprend la promesse du debut, puis "
            "une derniere phrase qui invite a s'abonner et a commenter. "
        )
    prompt = (
        f"Ecris le texte parle de la partie {i + 1}/{n} intitulee "
        f"\"{chapters[i]['titre']}\" d'une video YouTube sur \"{TOPIC}\".\n"
        f"Plan complet : {titres}\n"
        f"Longueur : environ {words} mots. {rules}\n"
        "Pas de titre, pas de liste, pas de markdown, pas d'emoji, pas d'indication "
        "scenique, ne dis jamais le mot « chapitre » : uniquement le texte lu a voix haute."
    )
    text = clean(ask(prompt))
    if len(text.split()) < words * 0.75:  # texte trop court : on redemande une fois
        log(f"  Texte court ({len(text.split())} mots), nouvel essai")
        longer = clean(
            ask(prompt + f"\nTon essai precedent etait trop court. Ecris vraiment {words} mots.")
        )
        if len(longer.split()) > len(text.split()):
            text = longer
    return text


# ---------- 2. Voix off masculine (edge-tts) ----------
_voice = None  # voix retenue : la meme pour toute la video


async def tts(text, path):
    global _voice
    last = None
    for voice in [_voice] if _voice else VOICES:
        for attempt in range(1, 4):
            try:
                await edge_tts.Communicate(
                    text, voice, rate=VOICE_RATE, pitch=VOICE_PITCH
                ).save(str(path))
                _voice = voice
                return
            except Exception as e:  # noqa: BLE001
                last = e
                log(f"  Voix {voice} essai {attempt} : {str(e)[:150]}")
                await asyncio.sleep(4 * attempt)
    raise RuntimeError(f"Voix indisponible : {last}")


def duration(path):
    out = run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ]
    )
    return float(out.strip())


# ---------- 3. Videos et images (Pixabay) ----------
def download(url, path):
    with requests.get(url, headers=HEADERS, timeout=(15, 120), stream=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)


def queries_for(keywords, extra):
    words = keywords.split()
    return [keywords, " ".join(words[:2]), words[0] if words else TOPIC] + extra


def pick_rendition(videos):
    for name in ("medium", "large", "small"):
        v = videos.get(name) or {}
        if v.get("url") and v.get("width", 0) >= 1280 and v.get("size", 0) < 200_000_000:
            return v["url"]
    return None


def search_videos(query, used, found):
    r = requests.get(
        "https://pixabay.com/api/videos/",
        params={
            "key": PIXABAY_KEY,
            "q": query[:100],
            "min_width": 1280,
            "per_page": 30,
            "safesearch": "true",
        },
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    ids = {h["id"] for h in found}
    out = []
    for h in r.json().get("hits", []):
        if ("v", h["id"]) in used or h["id"] in ids or h.get("duration", 0) < 3:
            continue
        url = pick_rendition(h.get("videos", {}))
        if url:
            h["_url"] = url
            out.append(h)
    return out


def get_videos(keywords, count, used):
    found = []
    for q in queries_for(keywords, ["nature sky"]):
        if len(found) >= count:
            break
        try:
            found += search_videos(q, used, found)
        except Exception as e:  # noqa: BLE001
            log(f"  Pixabay videos ({q}) : {str(e)[:150]}")
    items = []
    for h in found:
        if len(items) >= count:
            break
        p = WORK / f"vid_{h['id']}.mp4"
        try:
            download(h["_url"], p)
            used.add(("v", h["id"]))
            items.append(("video", p, h["pageURL"]))
        except Exception as e:  # noqa: BLE001
            log(f"  Telechargement video : {str(e)[:150]}")
    return items


def search_images(query, used, found):
    r = requests.get(
        "https://pixabay.com/api/",
        params={
            "key": PIXABAY_KEY,
            "q": query[:100],
            "image_type": "photo",
            "orientation": "horizontal",
            "min_width": 1280,
            "per_page": 50,
            "safesearch": "true",
        },
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    ids = {h["id"] for h in found}
    return [
        h for h in r.json().get("hits", [])
        if ("p", h["id"]) not in used and h["id"] not in ids
    ]


def get_images(keywords, count, used):
    found = []
    for q in queries_for(keywords, ["nature landscape"]):
        if len(found) >= count:
            break
        try:
            found += search_images(q, used, found)
        except Exception as e:  # noqa: BLE001
            log(f"  Pixabay images ({q}) : {str(e)[:150]}")
    items = []
    for h in found:
        if len(items) >= count:
            break
        p = WORK / f"img_{h['id']}.jpg"
        try:
            download(h["largeImageURL"], p)
            used.add(("p", h["id"]))
            items.append(("photo", p, h["pageURL"]))
        except Exception as e:  # noqa: BLE001
            log(f"  Telechargement image : {str(e)[:150]}")
    if not items:
        raise RuntimeError(f"Aucune image trouvee pour {keywords}")
    return items


def arrange(videos, photos, n):
    """Repartit les clips video regulierement parmi les photos."""
    nv = min(len(videos), n)
    seq, vi, pi = [], 0, 0
    for i in range(n):
        want_video = ((i + 1) * nv) // n > (i * nv) // n
        if want_video and vi < len(videos):
            seq.append(videos[vi])
            vi += 1
        elif pi < len(photos):
            seq.append(photos[pi])
            pi += 1
        elif vi < len(videos):
            seq.append(videos[vi])
            vi += 1
        elif seq:
            seq.append(seq[i % len(seq)])  # plus rien de neuf : on reutilise
        else:
            raise RuntimeError("Aucun visuel disponible")
    return seq


# ---------- 4. Cartes de titre style Notion (Pillow) ----------
FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
]
NOTION_TAGS = [  # (fond, texte) : les couleurs des etiquettes de Notion
    ((211, 229, 239), (24, 51, 71)),
    ((219, 237, 219), (28, 56, 41)),
    ((253, 236, 200), (64, 44, 27)),
    ((245, 224, 233), (76, 35, 55)),
    ((232, 222, 238), (65, 39, 89)),
    ((250, 222, 201), (73, 41, 14)),
]


def load_font(size):
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for word in text.split():
        test = (cur + " " + word).strip()
        if not cur or draw.textlength(test, font=font) <= max_w:
            cur = test
        else:
            lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines[:3]


def make_card_png(tag, title, path, color=0):
    """Carte blanche sobre, facon page Notion : etiquette coloree + titre."""
    w, h = 1920, 1080
    f_title, f_tag = load_font(68), load_font(30)
    card_w, pad, line_h, tag_h = 1300, 70, 88, 52
    d = ImageDraw.Draw(Image.new("RGBA", (w, h)))
    lines = wrap(d, title, f_title, card_w - 2 * pad)
    card_h = pad + tag_h + 36 + len(lines) * line_h + pad - 12
    x0, y0 = (w - card_w) // 2, (h - card_h) // 2
    shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle(
        [x0, y0 + 14, x0 + card_w, y0 + card_h + 14], radius=20, fill=(0, 0, 0, 120)
    )
    img = shadow.filter(ImageFilter.GaussianBlur(24))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle(
        [x0, y0, x0 + card_w, y0 + card_h], radius=18,
        fill=(255, 255, 255, 245), outline=(233, 233, 231, 255), width=2,
    )
    bg, fg = NOTION_TAGS[color % len(NOTION_TAGS)]
    tw = int(d.textlength(tag, font=f_tag))
    d.rounded_rectangle(
        [x0 + pad, y0 + pad, x0 + pad + tw + 40, y0 + pad + tag_h], radius=8, fill=bg + (255,)
    )
    d.text((x0 + pad + 20, y0 + pad + tag_h // 2), tag, font=f_tag, fill=fg + (255,), anchor="lm")
    y = y0 + pad + tag_h + 36
    for ln in lines:
        d.text((x0 + pad, y), ln, font=f_title, fill=(55, 53, 47, 255), anchor="lt")
        y += line_h
    img.save(path)


# ---------- 5. Montage (FFmpeg) ----------
def encode_args(out, crf):
    return [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf),
        "-r", str(FPS), str(out),
    ]


def make_clip(img, frames, out, zoom_in):
    z = f"1+0.15*on/{frames}" if zoom_in else f"1.15-0.15*on/{frames}"
    vf = (
        "scale=2560:1440:force_original_aspect_ratio=increase,crop=2560:1440,"
        f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s=1920x1080:fps={FPS},format=yuv420p,setsar=1"
    )
    run(["ffmpeg", "-y", "-i", str(img), "-vf", vf, "-frames:v", str(frames)] + encode_args(out, 20))


def make_video_clip(src, frames, out):
    vf = (
        "scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,"
        f"fps={FPS},format=yuv420p,setsar=1"
    )
    run(
        ["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(src), "-vf", vf,
         "-frames:v", str(frames), "-an"] + encode_args(out, 20)
    )


def build_segment(clips, vis, card_png, card_span, fades, out):
    """Fondus enchaines + look sombre + (fondu au noir) + carte de titre."""
    n = len(clips)
    total = sum(vis)
    secs = total / FPS
    cmd = ["ffmpeg", "-y"]
    for c in clips:
        cmd += ["-i", str(c)]
    use_card = bool(card_png)
    if use_card:
        a, b = card_span
        b = min(b, secs - 0.2)
        a = min(a, max(0, b - 1.5))
        cmd += ["-loop", "1", "-framerate", str(FPS), "-t", f"{b + 1:.2f}", "-i", str(card_png)]
    parts, last, acc = [], "0:v", 0
    for k in range(1, n):
        acc += vis[k - 1]
        parts.append(
            f"[{last}][{k}:v]xfade=transition=fade:duration={OVERLAP / FPS}"
            f":offset={acc / FPS}[x{k}]"
        )
        last = f"x{k}"
    chain = [LOOKS[LOOK]] if LOOK in LOOKS else []
    if fades:
        chain += ["fade=t=in:st=0:d=0.5", f"fade=t=out:st={max(0, secs - 0.5)}:d=0.5"]
    chain = chain or ["null"]
    parts.append(f"[{last}]" + ",".join(chain) + "[base]")
    if use_card:
        parts.append(
            f"[{n}:v]format=rgba,fade=t=in:st={a:.2f}:d=0.6:alpha=1,"
            f"fade=t=out:st={max(0, b - 0.6):.2f}:d=0.6:alpha=1[t]"
        )
        parts.append("[base][t]overlay=0:0:eof_action=pass,format=yuv420p[v]")
    else:
        parts.append("[base]format=yuv420p[v]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-an", "-frames:v", str(total)]
    run(cmd + encode_args(out, 24))


def concat(files, out, extra):
    lst = Path(str(out) + ".txt")
    lst.write_text("".join(f"file '{Path(f).resolve()}'\n" for f in files))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst)] + extra + [str(out)])


# ---------- 6. Musique de fond ----------
def make_drone(secs, out):
    """Nappe sonore ambiante generee par FFmpeg (aucun droit d'auteur)."""
    freqs = [55, 82.4, 110.3, 164.8, 220.6]
    cmd = ["ffmpeg", "-y"]
    for f in freqs:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency={f}:duration={secs}:sample_rate=44100"]
    cmd += ["-f", "lavfi", "-i", f"anoisesrc=color=pink:amplitude=0.4:duration={secs}:sample_rate=44100"]
    fc = (
        "[0]volume=1.0[a0];"
        "[1]volume=0.7,tremolo=f=0.1:d=0.4[a1];"
        "[2]volume=0.5,tremolo=f=0.13:d=0.5[a2];"
        "[3]volume=0.35,tremolo=f=0.17:d=0.6[a3];"
        "[4]volume=0.2,tremolo=f=0.11:d=0.5[a4];"
        "[5]lowpass=f=500,volume=0.25[n];"
        "[a0][a1][a2][a3][a4][n]amix=inputs=6:normalize=0,lowpass=f=900,"
        "aecho=0.8:0.9:70|140:0.35|0.25,aformat=channel_layouts=stereo,"
        "stereowiden=delay=20:feedback=0.3:crossfeed=0.3:drymix=0.8[out]"
    )
    run(cmd + ["-filter_complex", fc, "-map", "[out]", "-c:a", "aac", "-b:a", "128k", str(out)])


def mix_music(voice, out, secs):
    mine = sorted(p for e in ("mp3", "m4a", "wav", "ogg", "flac") for p in Path("music").glob(f"*.{e}"))
    if mine:
        log(f"Musique : {mine[0].name}")
        music_in = ["-stream_loop", "-1", "-i", str(mine[0])]
    else:
        log("Musique : nappe ambiante generee")
        bed = WORK / "drone.m4a"
        make_drone(int(secs) + 10, bed)
        music_in = ["-i", str(bed)]
    fade_out = max(0, secs - 6)
    fc = (
        "[0:a]loudnorm=I=-17:TP=-2:LRA=9,aresample=44100,"
        "aformat=sample_rates=44100:channel_layouts=stereo[v];"
        "[1:a]loudnorm=I=-34:TP=-6:LRA=7,aresample=44100,"
        "aformat=sample_rates=44100:channel_layouts=stereo,"
        f"afade=t=in:st=0:d=4,afade=t=out:st={fade_out}:d=6[m];"
        "[v][m]amix=inputs=2:duration=first:normalize=0:dropout_transition=0,"
        "alimiter=limit=0.95[a]"
    )
    run(
        ["ffmpeg", "-y", "-i", str(voice)] + music_in
        + ["-filter_complex", fc, "-map", "[a]", "-t", f"{secs:.2f}",
           "-c:a", "aac", "-b:a", "192k", str(out)]
    )


def stamp(seconds):
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


# ---------- 7. Un segment = voix + visuels + carte ----------
def render_segment(idx, seg, used, credits):
    mp3 = WORK / f"voice_{idx}.mp3"
    asyncio.run(tts(seg["text"], mp3))
    dur = duration(mp3)
    total = round(dur * FPS)
    n = max(1, min(round(dur / seg["clip_secs"]), total // 45))
    want_videos = round(n * seg["ratio"])
    videos = get_videos(seg["mots_cles"], want_videos, used) if want_videos else []
    photos = get_images(seg["mots_cles"], n - len(videos), used) if n > len(videos) else []
    log(f"  {len(videos)} videos, {len(photos)} photos")
    items = arrange(videos, photos, n)
    credits += [url for _, _, url in items if url not in credits]

    vis = [round((j + 1) * total / n) - round(j * total / n) for j in range(n)]
    clips = []
    for j, (kind, path, _) in enumerate(items):
        frames = vis[j] + (OVERLAP if j < n - 1 else 0)
        clip = WORK / f"clip_{idx}_{j}.mp4"
        if kind == "video":
            try:
                make_video_clip(path, frames, clip)
            except RuntimeError:
                fallback = next((p for k, p, _ in items if k == "photo"), None)
                if fallback is None:
                    raise
                make_clip(fallback, frames, clip, zoom_in=True)
        else:
            make_clip(path, frames, clip, zoom_in=(j % 2 == 0))
        clips.append(clip)

    card_png, span = None, None
    if seg["card"]:
        card_png = WORK / f"card_{idx}.png"
        try:
            make_card_png(seg["card"][0], seg["card"][1], card_png, color=idx)
            span = seg["card_span"] if seg["card_span"] != "full" else (0.4, dur - 0.4)
        except Exception as e:  # noqa: BLE001
            log(f"  Carte non affichee : {str(e)[:150]}")
            card_png = None
    out = WORK / f"segment_{idx}.mp4"
    build_segment(clips, vis, card_png, span, seg["fades"], out)

    for kind, path, _ in items:
        if kind == "video":
            path.unlink(missing_ok=True)  # libere de la place
    for c in clips:
        c.unlink(missing_ok=True)
    return mp3, out, dur


def main():
    log(f"Sujet : {TOPIC} ({MINUTES} min) - chaine : {CHANNEL or 'non indiquee'}")
    outline = make_outline()
    chapters = outline["chapitres"]
    words = max(120, (TOTAL_WORDS - 110) // len(chapters))

    log("Ecriture de l'accroche...")
    segments = [
        {
            "title": "Introduction", "text": write_hook(outline),
            "mots_cles": outline["hook_mots_cles"], "ratio": 1.0, "clip_secs": 3.5,
            "card": None, "card_span": None, "fades": False,
        },
        {
            "title": "Bienvenue", "text": welcome_text(),
            "mots_cles": outline["hook_mots_cles"], "ratio": 1.0, "clip_secs": 5,
            "card": ("BIENVENUE", CHANNEL or "Bienvenue"), "card_span": "full",
            "fades": False,
        },
    ]
    for i, ch in enumerate(chapters):
        log(f"Ecriture {i + 1}/{len(chapters)} : {ch['titre']}")
        segments.append(
            {
                "title": ch["titre"], "text": write_chapter(i, chapters, words),
                "mots_cles": ch["mots_cles"], "ratio": VIDEO_RATIO, "clip_secs": SECONDS_PER_IMAGE,
                "card": (f"CHAPITRE {i + 1}", ch["titre"]), "card_span": (0.8, 5.3),
                "fades": True, "chapter": True,
            }
        )
    script = "\n\n".join(f"## {s['title']}\n{s['text']}" for s in segments)
    (OUT / "script.txt").write_text(script, encoding="utf-8")
    log(f"Script pret : {len(script.split())} mots")

    used, credits = set(), []
    voices, files, timeline = [], [], ["00:00 Introduction"]
    elapsed = 0.0
    for idx, seg in enumerate(segments):
        log(f"Montage {idx + 1}/{len(segments)} : {seg['title']}")
        if seg.get("chapter"):
            timeline.append(f"{stamp(elapsed)} {seg['title']}")
        mp3, video, dur = render_segment(idx, seg, used, credits)
        voices.append(mp3)
        files.append(video)
        elapsed += dur

    log("Assemblage final...")
    concat(files, WORK / "video_all.mp4", ["-c", "copy"])
    concat(voices, WORK / "voice_all.m4a", ["-c:a", "aac", "-b:a", "192k"])
    audio = WORK / "voice_all.m4a"
    if MUSIC:
        mixed = WORK / "audio_mixed.m4a"
        mix_music(audio, mixed, duration(audio))
        audio = mixed
    run(
        [
            "ffmpeg", "-y", "-i", str(WORK / "video_all.mp4"), "-i", str(audio),
            "-c", "copy", "-shortest", "-movflags", "+faststart", str(OUT / "video.mp4"),
        ]
    )

    (OUT / "description.txt").write_text(
        f"{outline['titre_video']}\n\nChapitres :\n" + "\n".join(timeline)
        + "\n\nImages et videos : Pixabay (https://pixabay.com)\n"
        + "\n".join(credits),
        encoding="utf-8",
    )
    log(f"Termine : duree {stamp(elapsed)}")


if __name__ == "__main__":
    main()
