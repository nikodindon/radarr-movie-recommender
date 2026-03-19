#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
newmovies_v12.py — Radarr Movie Recommender

v12 changes:
- All log messages in English
- TV Movie, TV Series, Mini-Series added to BAD_GENRES filter
"""

import requests
import argparse
import json
import re
import math
import logging
import os
import random
import subprocess
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

# =========================
# CHARGEMENT .env
# =========================
def _load_env():
    """Load variables from .env file if present."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

# =========================
# CONFIG — read from .env or defaults
# =========================
_omdb_raw = os.environ.get("OMDB_KEYS", "")
OMDB_KEYS = [k.strip() for k in _omdb_raw.split(",") if k.strip()]
if not OMDB_KEYS:
    print("[ERROR] No OMDb key found. Check your .env file (OMDB_KEYS=key1,key2,...)")
    exit(1)

RADARR_API_KEY   = os.environ.get("RADARR_API_KEY", "")
RADARR_URL       = os.environ.get("RADARR_URL",     "http://localhost:7878/api/v3")
ROOT_FOLDER      = os.environ.get("ROOT_FOLDER",    "F:\\Movies")
OLLAMA_MODEL     = os.environ.get("OLLAMA_MODEL",   "llama3.1:8b")

if not RADARR_API_KEY:
    print("[ERROR] RADARR_API_KEY missing. Check your .env file")
    exit(1)

CONFIG_FILE      = "omdb_apikey.conf"
BLACKLIST_FILE   = "blacklist.json"
OLLAMA_EMBED_URL = f"http://localhost:11434/api/embeddings"
LOG_DIR          = "logs"

# Adjacent genres — used in relaxed mode when source film yields few candidates
ADJACENT_GENRES = {
    "action":    ["adventure", "thriller"],
    "adventure": ["action", "drama"],
    "sci-fi":    ["thriller", "horror", "adventure"],
    "horror":    ["thriller", "mystery"],
    "drama":     ["biography", "history", "mystery"],
    "thriller":  ["crime", "mystery", "drama"],
    "crime":     ["drama", "thriller", "mystery"],
    "comedy":    ["romance", "drama"],
    "romance":   ["comedy", "drama"],
    "biography": ["drama", "history"],
    "history":   ["drama", "biography", "war"],
    "war":       ["history", "drama", "action"],
    "mystery":   ["thriller", "horror", "drama"],
    "western":   ["action", "drama"],
    "fantasy":   ["adventure", "sci-fi"],
    "animation": ["adventure", "comedy", "fantasy"],
}

# =========================
# ARGUMENTS
# =========================
parser = argparse.ArgumentParser(description="Radarr Movie Recommender v12")
parser.add_argument("--sd",          type=int,   default=1970)
parser.add_argument("--fd",          type=int,   default=2030)
parser.add_argument("--score",       type=float, default=6.5,  help="Minimum IMDb rating (normal)")
parser.add_argument("--score-relax", type=float, default=5.9,  help="Minimum IMDb rating (relaxed fallback mode)")
parser.add_argument("--sources",     type=int,   default=10)
parser.add_argument("--suggestions", type=int,   default=14)
parser.add_argument("--top",         type=int,   default=10)
parser.add_argument("--auto",        action="store_true")
parser.add_argument("--no-embed",    action="store_true")
parser.add_argument("--debug",       action="store_true")
args = parser.parse_args()

# =========================
# LOGGING
# =========================
os.makedirs(LOG_DIR, exist_ok=True)
today_str = datetime.now().strftime("%Y%m%d_%H%M")
log_file  = os.path.join(LOG_DIR, f"reco_{today_str}.log")

logging.basicConfig(
    level=logging.DEBUG if args.debug else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("reco")

COLORS = {
    "SUCCESS": "\033[92m", "ERROR": "\033[91m", "SELECT": "\033[96m",
    "WARNING": "\033[93m", "OLLAMA": "\033[95m", "FALLBACK": "\033[94m",
    "DEBUG":   "\033[90m", "INFO":  "",
}
RESET = "\033[0m"

def log(msg, level="INFO"):
    color = COLORS.get(level, "")
    print(f"{color}[{level}] {msg}{RESET}")
    getattr(logger, level.lower() if level in ("DEBUG","INFO","WARNING","ERROR") else "info")(msg)

# =========================
# BLACKLIST
# =========================
def load_blacklist():
    if os.path.exists(BLACKLIST_FILE):
        try:
            with open(BLACKLIST_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except:
            pass
    return set()

def save_blacklist(bl):
    try:
        with open(BLACKLIST_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(bl), f, indent=2, ensure_ascii=False)
    except Exception as e:
        log(f"Error saving blacklist: {e}", "ERROR")

BLACKLIST = load_blacklist()
log(f"Blacklist loaded: {len(BLACKLIST)} titles")

# =========================
# CLÉS OMDb
# =========================
def load_current_key():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                k = f.read().strip()
                if k in OMDB_KEYS:
                    return k
        except:
            pass
    return OMDB_KEYS[0]

def save_current_key(key):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(key)
    except:
        pass

def test_omdb_key(key):
    try:
        r = requests.get(f"http://www.omdbapi.com/?t=Inception&apikey={key}", timeout=8)
        return r.json().get("Response") == "True"
    except:
        return False

CURRENT_OMDB_KEY = load_current_key()
if not test_omdb_key(CURRENT_OMDB_KEY):
    log("Invalid OMDb key, rotating...", "WARNING")
    for key in OMDB_KEYS:
        if test_omdb_key(key):
            CURRENT_OMDB_KEY = key
            save_current_key(key)
            log(f"New active key: {key[:8]}...", "SUCCESS")
            break
    else:
        log("No working OMDb key found!", "ERROR")
        exit(1)

# =========================
# TEST OLLAMA
# =========================
def test_ollama():
    try:
        result = subprocess.run(
            ["ollama", "run", OLLAMA_MODEL],
            input="Reply with only the word OK.",
            text=True, capture_output=True,
            timeout=30, encoding="utf-8", errors="replace"
        )
        return "OK" in result.stdout.upper()
    except:
        return False

OLLAMA_OK = test_ollama()
log(f"Ollama {'ready' if OLLAMA_OK else 'UNAVAILABLE'}",
    "SUCCESS" if OLLAMA_OK else "WARNING")

# =========================
# CACHES
# =========================
OMDB_CACHE      = {}
EMBEDDING_CACHE = {}  # shared globally across all source films

# =========================
# OMDb
# =========================
def _clean_title(raw: str) -> str:
    t = raw.strip()
    t = re.sub(r'^\d+[\.\)]\s*', '', t)
    t = re.sub(r'\s*\(\d{4}\)\s*$', '', t)
    t = t.strip('"\'')
    t = re.sub(r'^[-*•]\s*', '', t)
    return t.strip()

def _omdb_request(params: dict, retries=2):
    global CURRENT_OMDB_KEY
    for _ in range(retries * len(OMDB_KEYS)):
        params["apikey"] = CURRENT_OMDB_KEY
        try:
            r = requests.get("http://www.omdbapi.com/", params=params, timeout=10)
            data = r.json()
            time.sleep(1.1)
            if data.get("Response") == "False":
                if "limit" in data.get("Error", "").lower():
                    log(f"Quota reached {CURRENT_OMDB_KEY[:8]}... rotating key", "WARNING")
                    idx = OMDB_KEYS.index(CURRENT_OMDB_KEY)
                    CURRENT_OMDB_KEY = OMDB_KEYS[(idx + 1) % len(OMDB_KEYS)]
                    save_current_key(CURRENT_OMDB_KEY)
                    continue
                return None
            return data
        except Exception as e:
            log(f"OMDb err: {e}", "DEBUG")
            time.sleep(1.5)
    return None

def get_omdb_full(raw_title: str, year=None):
    title = _clean_title(raw_title)
    if not title:
        return None
    cache_key = f"{title}|{year or ''}"
    if cache_key in OMDB_CACHE:
        return OMDB_CACHE[cache_key]

    params = {"t": title, "type": "movie", "plot": "short"}
    if year:
        params["y"] = year

    data = _omdb_request(params)
    # Retry without year if not found
    if not data and year:
        data = _omdb_request({"t": title, "type": "movie", "plot": "short"})

    if not data:
        OMDB_CACHE[cache_key] = None
        return None

    try:
        year_val = int(data.get("Year", "0")[:4])
    except:
        year_val = 0
    try:
        rating = float(data.get("imdbRating", "0"))
    except:
        rating = 0.0

    result = {
        "title":    data.get("Title", title),
        "year":     year_val,
        "genre":    data.get("Genre", ""),
        "actors":   data.get("Actors", ""),
        "director": data.get("Director", ""),
        "rating":   rating,
        "plot":     data.get("Plot", ""),
        "imdb_id":  data.get("imdbID", ""),
    }
    OMDB_CACHE[cache_key] = result
    return result

def search_omdb(keyword, max_results=8):
    data = _omdb_request({"s": keyword, "type": "movie"})
    if not data:
        return []
    return [item["Title"] for item in data.get("Search", [])[:max_results]]

# =========================
# RADARR
# =========================
def get_radarr_movies():
    try:
        r = requests.get(f"{RADARR_URL}/movie?apikey={RADARR_API_KEY}", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log(f"Radarr error: {e}", "ERROR")
        return []

def get_radarr_lookup(title, year=None):
    """
    Radarr movie lookup. Retries without year if first attempt fails.
    """
    def _lookup(term):
        try:
            r = requests.get(
                f"{RADARR_URL}/movie/lookup?term={quote(term)}&apikey={RADARR_API_KEY}",
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            if not data:
                return None
            tl = title.lower().strip()
            for item in data[:5]:
                if item.get("title", "").lower().strip() == tl:
                    return item
            return data[0]
        except:
            return None

    result = _lookup(f"{title} {year}" if year else title)
    # Retry without year if not found or tmdbId missing
    if (not result or not result.get("tmdbId")) and year:
        result = _lookup(title)
    return result

def add_to_radarr(movie):
    payload = {
        "title":            movie["title"],
        "qualityProfileId": 1,
        "tmdbId":           movie["tmdbId"],
        "titleSlug":        movie["titleSlug"],
        "images":           movie.get("images", []),
        "year":             movie["year"],
        "rootFolderPath":   ROOT_FOLDER,
        "monitored":        True,
        "addOptions":       {"searchForMovie": True}
    }
    try:
        r = requests.post(
            f"{RADARR_URL}/movie?apikey={RADARR_API_KEY}",
            json=payload, timeout=10
        )
        if r.status_code in [200, 201]:
            log(f"Added: {movie['title']} ({movie['year']})", "SUCCESS")
            return True
        log(f"Error adding {movie['title']}: {r.status_code}", "ERROR")
        return False
    except Exception as e:
        log(f"Exception while adding: {e}", "ERROR")
        return False

# =========================
# EMBEDDINGS (cache global)
# =========================
def get_embedding(text):
    if args.no_embed or not text:
        return None
    key = text[:300]  # clé normalisée
    if key in EMBEDDING_CACHE:
        return EMBEDDING_CACHE[key]
    try:
        r = requests.post(
            OLLAMA_EMBED_URL,
            json={"model": OLLAMA_MODEL, "prompt": text[:500]},
            timeout=20
        )
        emb = r.json().get("embedding")
        EMBEDDING_CACHE[key] = emb
        return emb
    except:
        EMBEDDING_CACHE[key] = None
        return None

def cosine_similarity(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot    = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x*x for x in a))
    norm_b = math.sqrt(sum(x*x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)

def plot_sim(plot_a, plot_b):
    return cosine_similarity(get_embedding(plot_a), get_embedding(plot_b))

# =========================
# FILTRES
# =========================
BAD_PATTERNS = re.compile(
    r"(making of|life of|best of|roast|tribute|live from|behind the scenes|"
    r"documentary|compilation|interview|homage|salutes|user.s guide|presents:|"
    r"untold story|the story of|in concert|anniversary|directors cut|short film|"
    r"nominated short|oscar short|rifftrax|mystery science)",
    re.IGNORECASE
)
BAD_GENRES = {"Short", "TV Movie", "TV Series", "Mini-Series", "Documentary", "Game-Show",
              "Reality-TV", "Talk-Show", "Music"}

def is_junk(title):
    return bool(BAD_PATTERNS.search(title))

def is_valid_candidate(movie, min_score=None, extra_genres=None):
    """
    min_score: IMDb threshold (args.score by default, args.score_relax in relaxed mode)
    extra_genres: additional accepted genres (relaxed mode)
    """
    if not movie:
        return False
    threshold = min_score if min_score is not None else args.score
    if movie["rating"] < threshold:
        return False
    if movie["year"] < args.sd or movie["year"] > args.fd:
        return False
    cand_genres = {g.strip() for g in movie["genre"].split(",")}
    if cand_genres <= BAD_GENRES:  # que des genres interdits
        return False
    if is_junk(movie["title"]):
        return False
    return True

# =========================
# SCORING
# =========================
def get_extended_genres(base_genres: set) -> set:
    """Return adjacent genres for relaxed mode."""
    extended = set(base_genres)
    for g in base_genres:
        extended.update(ADJACENT_GENRES.get(g.lower(), []))
    return extended

def score_candidate(base, candidate, relaxed=False):
    score   = 0.0
    reasons = []

    base_genres = {g.strip().lower() for g in base["genre"].split(",")}
    cand_genres = {g.strip().lower() for g in candidate["genre"].split(",")}

    if relaxed:
        accepted_genres = get_extended_genres(base_genres)
    else:
        accepted_genres = base_genres

    common_genres = cand_genres & accepted_genres
    if not common_genres:
        return 0.0, []

    # Bonus réduit si match uniquement sur genre adjacent
    direct_match = cand_genres & base_genres
    if direct_match:
        score += 3.0 + len(direct_match) * 0.5
        reasons.append(f"genres:{','.join(sorted(direct_match))}")
    else:
        score += 1.5  # match adjacent seulement
        reasons.append(f"adj_genres:{','.join(sorted(common_genres))}")

    base_dir = base["director"].lower().strip()
    cand_dir = candidate["director"].lower().strip()
    if base_dir and base_dir != "n/a" and base_dir in cand_dir:
        score += 4.0
        reasons.append("same_director")

    base_actors = {a.strip().lower() for a in base["actors"].split(",") if a.strip()}
    shared = sum(1 for a in base_actors if a and a != "n/a" and a in candidate["actors"].lower())
    if shared:
        score += shared * 2.0
        reasons.append(f"actors:{shared}")

    sem = plot_sim(base.get("plot", ""), candidate.get("plot", ""))
    score += sem * 6.0
    if sem > 0.45:
        reasons.append(f"plot_sim:{sem:.2f}")

    # En mode normal : si plot_sim très faible et aucun autre bonus → pénalité
    if not relaxed and sem < 0.4 and not direct_match and not shared:
        score -= 2.0

    score += candidate["rating"] / 3.0

    diff = abs(candidate["year"] - base["year"])
    if diff < 5:
        score += 1.5
    elif diff < 15:
        score += 0.8

    return round(score, 2), reasons

# =========================
# OLLAMA
# =========================
def _parse_ollama_titles(raw: str) -> list[str]:
    # Stratégie 1 : JSON complet
    m = re.search(r'\{[^{}]*"films"\s*:\s*\[([^\]]+)\][^{}]*\}', raw, re.DOTALL)
    if m:
        try:
            return [t.strip() for t in json.loads(m.group(0)).get("films", []) if t.strip()]
        except:
            pass
    # Stratégie 2 : tableau seul
    m = re.search(r'"films"\s*:\s*\[([^\]]+)\]', raw, re.DOTALL)
    if m:
        try:
            return [t.strip() for t in json.loads("[" + m.group(1) + "]") if t.strip()]
        except:
            pass
    # Stratégie 3 : strings JSON
    items = re.findall(r'"([^"]{3,80})"', raw)
    bl = {"films","titles","suggestions","recommendations","similar","movies","title","film"}
    cleaned = [i for i in items if i.lower() not in bl]
    if len(cleaned) >= 3:
        return cleaned
    # Stratégie 4 : lignes numérotées/tirets
    titles = []
    for line in raw.split("\n"):
        m2 = re.match(r'^(?:\d+[\.\)]|[-*•])\s*(.+)$', line.strip())
        if m2:
            t = re.sub(r'\s*\(\d{4}\)\s*$', '', m2.group(1).strip().strip('"\''))
            if 2 < len(t) < 100:
                titles.append(t)
    return titles

def ollama_suggest_titles(base: dict) -> list[str]:
    if not OLLAMA_OK:
        return []
    prompt = f"""You are a film expert with encyclopedic knowledge of world cinema.

Source film: "{base['title']}" ({base['year']})
Genre: {base['genre']}
Director: {base['director']}
Cast: {base['actors']}
Plot: {base['plot']}

Suggest {args.suggestions} REAL existing films similar in theme, tone, atmosphere, or narrative style.

Rules:
- Only real theatrically released films
- Preferred IMDb rating above 6.5
- No direct sequels/prequels of the source film
- Vary the eras
- Use exact English/international theatrical title
- Do NOT include the source film itself

Respond ONLY with valid JSON:
{{"films": ["Title 1", "Title 2", ...]}}"""

    log(f"Ollama → generating suggestions for '{base['title']}'...", "OLLAMA")
    try:
        result = subprocess.run(
            ["ollama", "run", OLLAMA_MODEL],
            input=prompt, text=True, capture_output=True,
            timeout=120, encoding="utf-8", errors="replace"
        )
        titles = _parse_ollama_titles(result.stdout)
        log(f"Ollama: {len(titles)} titles extracted", "OLLAMA")
        return titles
    except subprocess.TimeoutExpired:
        log("Ollama timeout (120s)", "WARNING")
        return []
    except Exception as e:
        log(f"Ollama error: {e}", "ERROR")
        return []

# =========================
# FALLBACK OMDb
# =========================
def fallback_omdb_search(base: dict) -> list[str]:
    found = []
    log(f"OMDb fallback for '{base['title']}'...", "FALLBACK")
    if base["director"] and base["director"] != "N/A":
        for t in search_omdb(base["director"].split()[-1], 10):
            if t not in found:
                found.append(t)
    if base["genre"]:
        g = base["genre"].split(",")[0].strip()
        dec = (base["year"] // 10) * 10
        for t in search_omdb(f"{g} {dec}", 8):
            if t not in found:
                found.append(t)
    if base["actors"] and base["actors"] != "N/A":
        actor = base["actors"].split(",")[0].strip().split()[-1]
        for t in search_omdb(actor, 8):
            if t not in found:
                found.append(t)
    log(f"Fallback: {len(found)} raw candidates found", "FALLBACK")
    return found

# =========================
# VALIDATION D'UN CANDIDAT
# =========================
def validate_candidate(raw_title, base, radarr_titles, radarr_tmdb,
                        relaxed=False) -> dict | None:
    """
    Validate a candidate title and return a candidate dict or None.
    relaxed=True: lower IMDb threshold + adjacent genres accepted.
    """
    title = _clean_title(raw_title)
    if not title:
        return None
    if title.lower() == base["title"].lower():
        return None
    if title in BLACKLIST or title in radarr_titles:
        log(f"  Skip (blacklist/Radarr): {title}", "DEBUG")
        return None
    if is_junk(title):
        log(f"  Junk title filtered: {title}", "DEBUG")
        return None

    min_score = args.score_relax if relaxed else args.score
    omdb = get_omdb_full(title)
    if not omdb:
        log(f"  OMDb not found: {title}", "DEBUG")
        return None
    if omdb["title"] in radarr_titles or omdb["title"] in BLACKLIST:
        log(f"  Skip (blacklist/Radarr OMDb title): {omdb['title']}", "DEBUG")
        return None
    if not is_valid_candidate(omdb, min_score=min_score):
        log(f"  Filtered out: {omdb['title']} (IMDb:{omdb['rating']} {omdb['year']})", "DEBUG")
        return None

    sc, reasons = score_candidate(base, omdb, relaxed=relaxed)
    min_sc = 3.5 if relaxed else 4.0
    if sc < min_sc:
        log(f"  Score too low: {omdb['title']} → {sc}", "DEBUG")
        return None

    lookup = get_radarr_lookup(omdb["title"], omdb["year"])
    if not lookup:
        log(f"  Radarr lookup failed: {omdb['title']}", "DEBUG")
        return None
    if lookup.get("tmdbId") in radarr_tmdb:
        log(f"  Already in Radarr (tmdbId): {omdb['title']}", "DEBUG")
        return None

    log(f"  ✓ {omdb['title']} ({omdb['year']}) IMDb:{omdb['rating']} "
        f"score:{sc} [{', '.join(reasons)}]{'  [RELAX]' if relaxed else ''}", "SELECT")

    return {
        "title":   omdb["title"],
        "year":    omdb["year"],
        "rating":  omdb["rating"],
        "plot":    omdb["plot"],
        "score":   sc,
        "reasons": reasons,
        "lookup":  lookup,
        "source":  base["title"],
        "relaxed": relaxed,
    }

# =========================
# TRAITEMENT D'UN FILM SOURCE
# =========================
def process_source(base: dict, radarr_titles: set, radarr_tmdb: set) -> list[dict]:
    # 1. Ollama suggestions
    suggested = ollama_suggest_titles(base)

    if len(suggested) < 4:
        log(f"Too few Ollama suggestions → fallback", "FALLBACK")
        extra = fallback_omdb_search(base)
        seen  = {t.lower() for t in suggested}
        for t in extra:
            if t.lower() not in seen:
                suggested.append(t)
                seen.add(t.lower())

    log(f"Validating {len(suggested)} titles...", "INFO")

    # 2. Normal pass
    validated = []
    for raw in suggested:
        c = validate_candidate(raw, base, radarr_titles, radarr_tmdb, relaxed=False)
        if c:
            validated.append(c)

    # 3. Relaxed pass if fewer than 2 candidates
    if len(validated) < 2:
        log(f"Only {len(validated)} candidate(s) → relaxed pass (threshold:{args.score_relax}, adjacent genres)", "FALLBACK")
        validated_titles = {c["title"] for c in validated}
        for raw in suggested:
            title = _clean_title(raw)
            if title in validated_titles:
                continue
            c = validate_candidate(raw, base, radarr_titles, radarr_tmdb, relaxed=True)
            if c and c["title"] not in validated_titles:
                validated.append(c)
                validated_titles.add(c["title"])

        # Still not enough — reinforced fallback in relaxed mode
        if len(validated) < 2:
            log("Reinforced fallback in relaxed mode...", "FALLBACK")
            extra2 = fallback_omdb_search(base)
            for t in extra2:
                if _clean_title(t) not in {_clean_title(s) for s in suggested}:
                    c = validate_candidate(t, base, radarr_titles, radarr_tmdb, relaxed=True)
                    if c and c["title"] not in {x["title"] for x in validated}:
                        validated.append(c)

    validated.sort(key=lambda x: x["score"], reverse=True)
    return validated[:4]

# =========================
# RAPPORT
# =========================
def print_report(results, added):
    sep = "=" * 90
    print(f"\n{sep}")
    print(f"  REPORT — {datetime.now().strftime('%Y-%m-%d %H:%M')} — {len(results)} films")
    print(sep)
    for i, m in enumerate(results, 1):
        tag = "✅ ADDED" if m["title"] in added else "📋 proposed"
        rlx = " [relax]" if m.get("relaxed") else ""
        rsn = ", ".join(m.get("reasons", []))
        print(f"  {i:2d}. [{tag}]{rlx} {m['title']} ({m['year']}) "
              f"IMDb:{m['rating']:.1f} score:{m['score']}")
        print(f"       ↳ {rsn}")
        print(f"       ↳ from: {m['source']}")
    print(sep)
    logger.info(f"Report: {len(added)} added / {len(results)} proposed")

# =========================
# MAIN
# =========================
def main():
    radarr = get_radarr_movies()
    if not radarr:
        log("Cannot reach Radarr.", "ERROR")
        return

    radarr_titles = {m["title"] for m in radarr}
    radarr_tmdb   = {m.get("tmdbId") for m in radarr if m.get("tmdbId")}
    BLACKLIST.update(radarr_titles)

    pool = [m for m in radarr if m.get("title")]
    random.shuffle(pool)
    sources = pool[:args.sources]
    log(f"{len(sources)} source films selected")

    all_results: dict[str, dict] = {}

    for i, r in enumerate(sources):
        title = r["title"]
        log(f"\n{'='*60}")
        log(f"[{i+1}/{len(sources)}] {title}")

        base = get_omdb_full(title)
        if not base:
            log(f"OMDb not found for '{title}' — skipped", "WARNING")
            continue

        # Pre-compute source film embedding (cached globally)
        if base.get("plot"):
            get_embedding(base["plot"])

        candidates = process_source(base, radarr_titles, radarr_tmdb)
        log(f"→ {len(candidates)} candidates for '{title}'")

        for c in candidates:
            key = c["title"]
            if key not in all_results or c["score"] > all_results[key]["score"]:
                all_results[key] = c

    # Final selection with diversity (max 2 per source film)
    sorted_all = sorted(all_results.values(), key=lambda x: x["score"], reverse=True)
    final = []
    source_count: dict[str, int] = {}
    for c in sorted_all:
        src = c["source"]
        if source_count.get(src, 0) < 2:
            final.append(c)
            source_count[src] = source_count.get(src, 0) + 1
        if len(final) >= args.top:
            break
    if len(final) < args.top:
        for c in sorted_all:
            if c not in final:
                final.append(c)
            if len(final) >= args.top:
                break

    # Save JSON output
    output = []
    for m in final:
        lk = m["lookup"]
        output.append({
            "title":     lk["title"],
            "year":      lk.get("year"),
            "rating":    m["rating"],
            "score":     m["score"],
            "reasons":   m["reasons"],
            "tmdbId":    lk["tmdbId"],
            "titleSlug": lk["titleSlug"],
            "images":    lk.get("images", []),
            "source":    m["source"],
        })

    json_file = f"reco_{today_str}.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)
    log(f"Results saved → {json_file}")

    if not output:
        log("No recommendations found.", "WARNING")
        return

    added = []
    if args.auto:
        log(f"AUTO mode — adding {len(output)} films to Radarr", "INFO")
        for m in output:
            if add_to_radarr(m):
                added.append(m["title"])
                BLACKLIST.add(m["title"])
    else:
        print_report(final, added=[])
        choice = input("\nAdd to Radarr? (a=all / o=one by one / n=no): ").lower().strip()
        if choice == "a":
            for m in output:
                if add_to_radarr(m):
                    added.append(m["title"])
                    BLACKLIST.add(m["title"])
        elif choice == "o":
            for m in output:
                rep = input(f"  '{m['title']}' ({m['year']}) IMDb:{m['rating']} - add? (y/n): ").lower()
                if rep == "y":
                    if add_to_radarr(m):
                        added.append(m["title"])
                        BLACKLIST.add(m["title"])
                else:
                    BLACKLIST.add(m["title"])

    print_report(final, added)
    save_blacklist(BLACKLIST)
    log(f"Blacklist: {len(BLACKLIST)} titles | Log: {log_file}")

if __name__ == "__main__":
    main()
