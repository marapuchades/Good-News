"""
Good News · busca noticias positivas, las resume en español con Gemini
y las añade a news.json. GitHub Actions lo ejecuta cada mañana y abre
un pull request para que lo revises antes de publicar.
"""
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

import feedparser
import requests

# ---------- Configuración (cámbiala a tu gusto) ----------
FEEDS = [
    "https://www.goodnewsnetwork.org/feed/",
    "https://www.positive.news/feed/",
    "https://reasonstobecheerful.world/feed/",
    "https://www.sciencedaily.com/rss/top/science.xml",
    "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
]
MAX_NEW = int(os.getenv("MAX_NEW", "6"))          # noticias nuevas por día
MAX_CANDIDATES = 30                                # titulares que se envían a la IA
MAX_AGE_DAYS = 3                                   # ignora noticias más antiguas
MAX_ITEMS = 300                                    # tamaño máximo de news.json
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
CATEGORIES = ["ciencia", "salud", "medio-ambiente", "tecnologia", "historias"]
# ---------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
NEWS_FILE = ROOT / "news.json"


def slugify(text):
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:70] or "noticia"


def clean_html(text):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def load_news():
    if NEWS_FILE.exists():
        return json.loads(NEWS_FILE.read_text(encoding="utf-8"))
    return {"updated": "", "items": []}


def collect_candidates(known_urls):
    limit = datetime.now(timezone.utc) - timedelta(days=MAX_AGE_DAYS)
    out = []
    for url in FEEDS:
        try:
            feed = feedparser.parse(url)
            source = feed.feed.get("title", url)
        except Exception as e:  # una fuente caída no rompe el resto
            print(f"[aviso] No se pudo leer {url}: {e}")
            continue
        for e in feed.entries[:15]:
            link = e.get("link")
            if not link or link in known_urls:
                continue
            published = e.get("published_parsed") or e.get("updated_parsed")
            if published and datetime(*published[:6], tzinfo=timezone.utc) < limit:
                continue
            out.append({
                "title": clean_html(e.get("title")),
                "text": clean_html(e.get("summary"))[:600],
                "source": source,
                "url": link,
            })
    print(f"Candidatas: {len(out)}")
    return out[:MAX_CANDIDATES]


PROMPT = """Eres editor de "Good News", una web en español de noticias positivas internacionales.
Te paso una lista numerada de noticias en inglés u otros idiomas.

Elige como máximo {max_new} que cumplan TODO esto:
- Son noticias buenas de verdad (avances, logros, mejoras medibles), no clickbait ni anuncios.
- Tienen interés internacional o para el público hispanohablante.
- Son concretas y verificables (hay datos, lugares o instituciones).
Si ninguna cumple, devuelve una lista vacía.

Para cada una escribe EN ESPAÑOL Y CON PALABRAS PROPIAS (no traduzcas frase a frase):
- "index": el número de la noticia en la lista
- "title": titular claro, máximo 90 caracteres, sin exagerar
- "summary": 2-3 frases: qué ha pasado, dónde y quién
- "why": 1-2 frases explicando por qué importa, con un dato si lo hay
- "category": una de {categories}
- "country": país o región ("Internacional" si es global)
- "score": del 1 al 10, lo interesante que es

Responde SOLO con JSON: {{"items": [ ... ]}}

Noticias:
{news}
"""


def ask_gemini(candidates):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        sys.exit("Falta GEMINI_API_KEY (añádela en Settings → Secrets and variables → Actions).")
    listing = "\n\n".join(
        f"[{i}] {c['title']}\n{c['text']}" for i, c in enumerate(candidates)
    )
    body = {
        "contents": [{"parts": [{"text": PROMPT.format(
            max_new=MAX_NEW, categories=", ".join(CATEGORIES), news=listing)}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
    }
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    for attempt in range(3):
        r = requests.post(url, params={"key": key}, json=body, timeout=120)
        if r.status_code == 429 or r.status_code >= 500:
            print(f"[aviso] Gemini respondió {r.status_code}, reintentando…")
            time.sleep(20 * (attempt + 1))
            continue
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return json.loads(text).get("items", [])
    sys.exit("Gemini no respondió después de 3 intentos.")


def main():
    data = load_news()
    items = [n for n in data.get("items", []) if not n.get("id", "").startswith("ejemplo-")]
    known_urls = {n.get("source_url") for n in items}
    known_ids = {n.get("id") for n in items}

    candidates = collect_candidates(known_urls)
    if not candidates:
        print("No hay noticias nuevas hoy.")
        return

    picked = ask_gemini(candidates)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    new = []
    for p in sorted(picked, key=lambda x: -int(x.get("score", 0)))[:MAX_NEW]:
        try:
            c = candidates[int(p["index"])]
        except (KeyError, ValueError, IndexError):
            continue
        cat = p.get("category") if p.get("category") in CATEGORIES else "historias"
        slug = slugify(p.get("title", c["title"]))
        while slug in known_ids:
            slug += "-2"
        known_ids.add(slug)
        new.append({
            "id": slug,
            "title": p.get("title", "").strip(),
            "summary": p.get("summary", "").strip(),
            "why": p.get("why", "").strip(),
            "category": cat,
            "country": p.get("country", "Internacional"),
            "source": c["source"],
            "source_url": c["url"],   # la URL sale del RSS, nunca de la IA
            "date": today,
        })

    if not new:
        print("La IA no ha encontrado noticias que cumplan los criterios.")
        return

    for n in items:
        n.pop("featured", None)
    new[0]["featured"] = True       # la mejor del día va destacada

    data["items"] = (new + items)[:MAX_ITEMS]
    data["updated"] = today
    NEWS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Añadidas {len(new)} noticias:")
    for n in new:
        print(" -", n["title"])


if __name__ == "__main__":
    main()
