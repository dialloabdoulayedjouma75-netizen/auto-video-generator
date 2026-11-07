"""Generateur automatique de videos YouTube : script, voix off, images, montage."""
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

TOPIC = os.environ.get("TOPIC", "").strip() or (
    sys.argv[1] if len(sys.argv) > 1 else "L'histoire de Rome"
)
VOICE = os.environ.get("VOICE", "fr-FR-DeniseNeural")
TOTAL_WORDS = int(os.environ.get("TOTAL_WORDS", "1650"))  # environ 10 minutes
N_CHAPTERS = 8
SECONDS_PER_IMAGE = 7
FPS = 25
MODELS = [
    m
    for m in [os.environ.get("GEMINI_MODEL"), "gemini-3.8-flash", "gemini-2.5-flash"]
    if m
]

OUT = Path("output")
WORK = OUT / "work"
WORK.mkdir(parents=True, exist_ok=True)

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
PIXABAY_KEY = os.environ["PIXABAY_API_KEY"]
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


def make_outline():
    prompt = (
        "Tu es scenariste de videos YouTube educatives en francais.\n"
        f"Sujet : {TOPIC}\n"
        f"Propose un plan de {N_CHAPTERS} chapitres pour une video de 10 minutes.\n"
        "Reponds UNIQUEMENT avec du JSON : une liste d'objets avec les cles "
        '"titre" (en francais) et "mots_cles" (2 ou 3 mots en ANGLAIS pour '
        'chercher une photo illustrative, par exemple "roman colosseum ruins").'
    )
    text = ask(prompt, as_json=True).replace("```json", "").replace("```", "")
    data = json.loads(text)
    chapters = []
    for c in data[:N_CHAPTERS]:
        chapters.append(
            {
                "titre": str(c.get("titre", "Chapitre")).strip(),
                "mots_cles": str(c.get("mots_cles") or TOPIC).strip(),
            }
        )
    if len(chapters) < 3:
        raise RuntimeError("Plan trop court")
    return chapters


def write_chapter(i, chapters):
    titres = " | ".join(c["titre"] for c in chapters)
    words = TOTAL_WORDS // len(chapters)
    extra = ""
    if i == 0:
        extra += "Commence par une accroche forte qui donne envie de rester. "
    else:
        extra += "Ne dis pas bonjour et ne te presente pas : la video est deja commencee. "
    if i == len(chapters) - 1:
        extra += "Termine par une conclusion et invite a s'abonner. "
    prompt = (
        f"Ecris le texte parle du chapitre {i + 1}/{len(chapters)} "
        f"intitule \"{chapters[i]['titre']}\" d'une video YouTube sur \"{TOPIC}\".\n"
        f"Plan complet de la video : {titres}\n"
        f"Longueur : environ {words} mots. Ton vivant, phrases courtes. {extra}\n"
        "Pas de titre, pas de liste, pas de markdown, pas d'emoji, pas d'indication "
        "scenique : uniquement le texte lu a voix haute."
    )
    text = re.sub(r"[*#_`>\[\]]", "", ask(prompt))
    return re.sub(r"\s+", " ", text).strip()


# ---------- 2. Voix off (edge-tts) ----------
async def tts(text, path):
    for attempt in range(1, 4):
        try:
            await edge_tts.Communicate(text, VOICE).save(str(path))
            return
        except Exception as e:  # noqa: BLE001
            log(f"  Voix essai {attempt} : {str(e)[:150]}")
            await asyncio.sleep(5 * attempt)
    raise RuntimeError("Voix indisponible")


def duration(path):
    out = run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", str(path),
        ]
    )
    return float(out.strip())


# ---------- 3. Images (Pixabay) ----------
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
    return [h for h in r.json().get("hits", []) if h["id"] not in used and h["id"] not in ids]


def get_images(keywords, count, used):
    words = keywords.split()
    queries = [keywords, " ".join(words[:2]), words[0] if words else TOPIC, "nature landscape"]
    found = []
    for q in queries:
        if len(found) >= count:
            break
        try:
            found += search_images(q, used, found)
        except Exception as e:  # noqa: BLE001
            log(f"  Pixabay ({q}) : {str(e)[:150]}")
    paths = []
    for h in found:
        if len(paths) >= count:
            break
        p = WORK / f"img_{h['id']}.jpg"
        try:
            data = requests.get(h["largeImageURL"], headers=HEADERS, timeout=60)
            data.raise_for_status()
            p.write_bytes(data.content)
            used.add(h["id"])
            paths.append((p, h["pageURL"]))
        except Exception as e:  # noqa: BLE001
            log(f"  Telechargement image : {str(e)[:150]}")
    if not paths:
        raise RuntimeError(f"Aucune image trouvee pour {keywords}")
    return paths


# ---------- 4. Montage (FFmpeg) ----------
def make_clip(img, frames, out, zoom_in):
    z = f"1+0.15*on/{frames}" if zoom_in else f"1.15-0.15*on/{frames}"
    vf = (
        "scale=2560:1440:force_original_aspect_ratio=increase,crop=2560:1440,"
        f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s=1920x1080:fps={FPS},format=yuv420p"
    )
    run(
        [
            "ffmpeg", "-y", "-i", str(img), "-vf", vf, "-frames:v", str(frames),
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(FPS),
            str(out),
        ]
    )


def concat(files, out, extra):
    lst = Path(str(out) + ".txt")
    lst.write_text("".join(f"file '{Path(f).resolve()}'\n" for f in files))
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst)] + extra + [str(out)])


def stamp(seconds):
    s = int(seconds)
    return f"{s // 60:02d}:{s % 60:02d}"


def main():
    log(f"Sujet : {TOPIC}")
    chapters = make_outline()
    used, credits = set(), []
    clips, voices, script_parts, timeline = [], [], [], []
    elapsed = 0.0

    for i, ch in enumerate(chapters):
        log(f"Chapitre {i + 1}/{len(chapters)} : {ch['titre']}")
        text = write_chapter(i, chapters)
        script_parts.append(f"## {ch['titre']}\n{text}")

        mp3 = WORK / f"voice_{i}.mp3"
        asyncio.run(tts(text, mp3))
        dur = duration(mp3)
        voices.append(mp3)
        timeline.append(f"{stamp(elapsed)} {ch['titre']}")
        elapsed += dur

        n = max(1, round(dur / SECONDS_PER_IMAGE))
        images = get_images(ch["mots_cles"], n, used)
        credits += [url for _, url in images]
        total = round(dur * FPS)
        for j in range(n):
            frames = max(1, round((j + 1) * total / n) - round(j * total / n))
            img, _ = images[j % len(images)]
            clip = WORK / f"clip_{i}_{j}.mp4"
            make_clip(img, frames, clip, zoom_in=(j % 2 == 0))
            clips.append(clip)

    log("Assemblage final...")
    concat(clips, WORK / "video_all.mp4", ["-c", "copy"])
    concat(voices, WORK / "audio_all.m4a", ["-c:a", "aac", "-b:a", "192k"])
    run(
        [
            "ffmpeg", "-y", "-i", str(WORK / "video_all.mp4"),
            "-i", str(WORK / "audio_all.m4a"), "-c", "copy", "-shortest",
            "-movflags", "+faststart", str(OUT / "video.mp4"),
        ]
    )

    (OUT / "script.txt").write_text("\n\n".join(script_parts), encoding="utf-8")
    (OUT / "description.txt").write_text(
        f"{TOPIC}\n\nChapitres :\n" + "\n".join(timeline)
        + "\n\nImages : Pixabay (https://pixabay.com)\n"
        + "\n".join(credits),
        encoding="utf-8",
    )
    log(f"Termine : duree {stamp(elapsed)}")


if __name__ == "__main__":
    main()
