#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
newmovies_v18.py -- Radarr Movie Recommender

v18 changes:
- --mood: direct Ollama generation from atmosphere description (no random source)
- --like "Film Title": recommendations based on any film, even outside your library
- --like + --mood: mood enriches the --like prompt
- Fix: low plot_sim + no strong signal = score penalty to avoid off-topic results
- Interactive mode: optional blacklisting when refusing a film
"""

import sys
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

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# =========================
# CONFIG LOADING
# =========================
def _load_config():
    base = Path(__file__).parent
    cfg_file = base / "config.yaml"
    if cfg_file.exists() and YAML_AVAILABLE:
        with open(cfg_file, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return {
            "OMDB_KEYS":           [k.strip() for k in str(cfg.get("omdb_keys", "")).split(",") if k.strip()],
            "RADARR_API_KEY":      str(cfg.get("radarr_api_key", "")),
            "RADARR_URL":          str(cfg.get("radarr_url",     "http://localhost:7878/api/v3")),
            "ROOT_FOLDER":         str(cfg.get("root_folder",    "F:\\Movies")),
            "OLLAMA_MODEL":        str(cfg.get("ollama_model",   "llama3.1:8b")),
            "QUALITY_PROFILE_ID":  int(cfg.get("quality_profile_id", 1)),
            "MINIMUM_AVAILABILITY":str(cfg.get("minimum_availability", "announced")),
        }
    env_file = base / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
    omdb_raw = os.environ.get("OMDB_KEYS", "")
    return {
        "OMDB_KEYS":           [k.strip() for k in omdb_raw.split(",") if k.strip()],
        "RADARR_API_KEY":      os.environ.get("RADARR_API_KEY", ""),
        "RADARR_URL":          os.environ.get("RADARR_URL",     "http://localhost:7878/api/v3"),
        "ROOT_FOLDER":         os.environ.get("ROOT_FOLDER",    "F:\\Movies"),
        "OLLAMA_MODEL":        os.environ.get("OLLAMA_MODEL",   "llama3.1:8b"),
        "QUALITY_PROFILE_ID":  int(os.environ.get("QUALITY_PROFILE_ID", "1")),
        "MINIMUM_AVAILABILITY":os.environ.get("MINIMUM_AVAILABILITY", "announced"),
    }

_cfg           = _load_config()
OMDB_KEYS            = _cfg["OMDB_KEYS"]
RADARR_API_KEY       = _cfg["RADARR_API_KEY"]
RADARR_URL           = _cfg["RADARR_URL"]
ROOT_FOLDER          = _cfg["ROOT_FOLDER"]
OLLAMA_MODEL         = _cfg["OLLAMA_MODEL"]
QUALITY_PROFILE_ID   = _cfg["QUALITY_PROFILE_ID"]
MINIMUM_AVAILABILITY = _cfg["MINIMUM_AVAILABILITY"]

if not OMDB_KEYS:
    print("[ERROR] No OMDb key found. Check config.yaml (OMDB_KEYS=key1,key2,...)")
    exit(1)
if not RADARR_API_KEY:
    print("[ERROR] RADARR_API_KEY missing. Check config.yaml")
    exit(1)

CONFIG_FILE      = "omdb_apikey.conf"
BLACKLIST_FILE   = "blacklist.json"
OLLAMA_EMBED_URL = "http://localhost:11434/api/embeddings"
LOG_DIR          = "logs"

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
parser = argparse.ArgumentParser(description="Radarr Movie Recommender v18")
parser.add_argument("--sd",          type=int,   default=1970)
parser.add_argument("--fd",          type=int,   default=2030)
parser.add_argument("--score",       type=float, default=6.5)
parser.add_argument("--score-relax", type=float, default=5.9)
parser.add_argument("--sources",     type=int,   default=10)
parser.add_argument("--suggestions", type=int,   default=14)
parser.add_argument("--top",         type=int,   default=10)
parser.add_argument("--auto",        action="store_true")
parser.add_argument("--no-embed",    action="store_true")
parser.add_argument("--debug",       action="store_true")
parser.add_argument("--genre",  type=str, default=None,
    help="Filter by genre (e.g. Comedy, Sci-Fi, Horror). Comma-separated for multiple.")
parser.add_argument("--mood",   type=str, default=None,
    help="Describe the atmosphere you want (e.g. 'feel good', 'dark and intense', 'mind-bending')")
parser.add_argument("--like",          type=str, default=None,
    help="Get recommendations based on a specific film title (even if not in your library)")
parser.add_argument("--resetblacklist",  action="store_true",
    help="Reset the blacklist file (keeps Radarr library titles)")
args = parser.parse_args()

# =========================
# CONSOLE SETUP (Windows fix)
# =========================
if os.name == "nt":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7)
    except Exception:
        pass

COLORS_ENABLED = sys.stdout.isatty()

C = {
    "green":   "\033[92m", "red":     "\033[91m", "cyan":    "\033[96m",
    "yellow":  "\033[93m", "magenta": "\033[95m", "blue":    "\033[94m",
    "gray":    "\033[90m", "white":   "\033[97m", "bold":    "\033[1m",
    "reset":   "\033[0m",
}
LEVEL_COLORS = {
    "SUCCESS": C["green"], "ERROR":   C["red"],     "SELECT":  C["cyan"],
    "WARNING": C["yellow"],"OLLAMA":  C["magenta"], "FALLBACK":C["blue"],
    "DEBUG":   C["gray"],  "INFO":    "",
}

def cprint(text, color="reset", bold=False):
    if COLORS_ENABLED:
        prefix = C["bold"] if bold else ""
        print(f"{prefix}{C.get(color, '')}{text}{C['reset']}")
    else:
        print(text)

def log(msg, level="INFO"):
    if level == "DEBUG" and not args.debug:
        logger.debug(msg)
        return
    color = LEVEL_COLORS.get(level, "") if COLORS_ENABLED else ""
    reset = C["reset"] if COLORS_ENABLED else ""
    tag   = f"[{level}]" if level != "INFO" else "      "
    print(f"{color}{tag} {msg}{reset}")
    getattr(logger, level.lower() if level in ("DEBUG","INFO","WARNING","ERROR") else "info")(msg)

def print_header(blacklist_size=0, genre_filter=None):
    w   = 70
    now = datetime.now().strftime("%Y-%m-%d  %H:%M")
    cprint("=" * w, "white", bold=True)
    cprint(f"  RADARR MOVIE RECOMMENDER  v17          {now}", "white", bold=True)
    cprint(f"  Model: {OLLAMA_MODEL:<20} Blacklist: {blacklist_size} titles", "gray")
    cprint(f"  Quality profile: {QUALITY_PROFILE_ID:<10} Availability: {MINIMUM_AVAILABILITY}", "gray")
    if genre_filter:
        cprint(f"  Genre filter: {genre_filter}", "cyan")
    if hasattr(args, "mood") and args.mood:
        cprint(f"  Mood: {args.mood}", "cyan")
    if hasattr(args, "like") and args.like:
        cprint(f"  Based on: {args.like}", "cyan")
    cprint("=" * w, "white", bold=True)
    print()

def print_source_header(index, total, title, genre=""):
    print()
    cprint(f"  [{index}/{total}]  {title}", "white", bold=True)
    if genre:
        cprint(f"         {genre}", "gray")
    cprint("-" * 70, "gray")

# =========================
# LOGGING (file only)
# =========================
os.makedirs(LOG_DIR, exist_ok=True)
today_str = datetime.now().strftime("%Y%m%d_%H%M")
log_file  = os.path.join(LOG_DIR, f"reco_{today_str}.log")

_fh = logging.FileHandler(log_file, encoding="utf-8")
_fh.setFormatter(logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logger = logging.getLogger("reco")
logger.setLevel(logging.DEBUG if args.debug else logging.INFO)
logger.addHandler(_fh)
logger.propagate = False

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

# Handle --resetblacklist immediately at startup
if hasattr(args, "resetblacklist") and args.resetblacklist:
    count = len(BLACKLIST)
    BLACKLIST.clear()
    save_blacklist(BLACKLIST)
    cprint(f"  Blacklist reset: {count} titles removed.", "yellow", bold=True)
    cprint("  The blacklist will be repopulated with your Radarr library on next run.", "gray")
    exit(0)

# =========================
# OMDB KEYS
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
# OLLAMA
# =========================
def test_ollama():
    try:
        result = subprocess.run(
            ["ollama", "run", OLLAMA_MODEL],
            input="Reply with only the word OK.",
            text=True, capture_output=True,
            timeout=30, encoding="utf-8", errors="replace")
        return "OK" in result.stdout.upper()
    except:
        return False

OLLAMA_OK = test_ollama()
log(f"Ollama {'ready' if OLLAMA_OK else 'UNAVAILABLE'}",
    "SUCCESS" if OLLAMA_OK else "WARNING")

# =========================
# CACHES + STATS
# =========================
OMDB_CACHE      = {}
EMBEDDING_CACHE = {}
RUN_STATS = {
    "sources_processed": 0, "ollama_suggestions": 0,
    "candidates_tested":  0, "filtered_rating":   0,
    "filtered_genre":     0, "filtered_blacklist": 0,
    "filtered_score":     0, "selected":           0,
    "added":              0,
}

# =========================
# OMDB
# =========================
def _clean_title(raw: str) -> str:
    t = re.sub(r'^\d+[\.\)]\s*', '', raw.strip())
    t = re.sub(r'\s*\(\d{4}\)\s*$', '', t)
    t = t.strip('"\'')
    t = re.sub(r'^[-*]\s*', '', t)
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
    def _lookup(term):
        try:
            r = requests.get(
                f"{RADARR_URL}/movie/lookup?term={quote(term)}&apikey={RADARR_API_KEY}",
                timeout=10)
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
    if (not result or not result.get("tmdbId")) and year:
        result = _lookup(title)
    return result

def add_to_radarr(movie):
    payload = {
        "title":               movie["title"],
        "qualityProfileId":    QUALITY_PROFILE_ID,
        "tmdbId":              movie["tmdbId"],
        "titleSlug":           movie["titleSlug"],
        "images":              movie.get("images", []),
        "year":                movie["year"],
        "rootFolderPath":      ROOT_FOLDER,
        "monitored":           True,
        "minimumAvailability": MINIMUM_AVAILABILITY,
        "addOptions":          {"searchForMovie": True}
    }
    try:
        r = requests.post(
            f"{RADARR_URL}/movie?apikey={RADARR_API_KEY}",
            json=payload, timeout=10)
        if r.status_code in [200, 201]:
            radarr_year = r.json().get("year", movie["year"])
            if abs(radarr_year - movie["year"]) > 1:
                log(f"Added: {movie['title']} ({movie['year']}) "
                    f"[WARNING: Radarr year={radarr_year}]", "SUCCESS")
            else:
                log(f"Added: {movie['title']} ({movie['year']})", "SUCCESS")
            return True
        log(f"Error adding {movie['title']}: {r.status_code}", "ERROR")
        return False
    except Exception as e:
        log(f"Exception while adding: {e}", "ERROR")
        return False

# =========================
# EMBEDDINGS
# =========================
def get_embedding(text):
    if args.no_embed or not text:
        return None
    key = text[:300]
    if key in EMBEDDING_CACHE:
        return EMBEDDING_CACHE[key]
    try:
        r = requests.post(OLLAMA_EMBED_URL,
            json={"model": OLLAMA_MODEL, "prompt": text[:500]}, timeout=20)
        emb = r.json().get("embedding")
        EMBEDDING_CACHE[key] = emb
        return emb
    except:
        EMBEDDING_CACHE[key] = None
        return None

def cosine_similarity(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na  = math.sqrt(sum(x*x for x in a))
    nb  = math.sqrt(sum(x*x for x in b))
    return dot / (na * nb) if na and nb else 0.0

def plot_sim(plot_a, plot_b):
    return cosine_similarity(get_embedding(plot_a), get_embedding(plot_b))

# =========================
# FILTERS
# =========================
BAD_PATTERNS = re.compile(
    r"(making of|life of|best of|roast|tribute|live from|behind the scenes|"
    r"documentary|compilation|interview|homage|salutes|user.s guide|presents:|"
    r"untold story|the story of|in concert|anniversary|directors cut|short film|"
    r"nominated short|oscar short|rifftrax|mystery science)", re.IGNORECASE)

BAD_GENRES = {"Short", "TV Movie", "TV Series", "Mini-Series", "Documentary",
              "Game-Show", "Reality-TV", "Talk-Show", "Music"}

def is_junk(title):
    return bool(BAD_PATTERNS.search(title))

def is_valid_candidate(movie, min_score=None):
    if not movie:
        return False
    threshold = min_score if min_score is not None else args.score
    if movie["rating"] < threshold:
        return False
    if movie["year"] < args.sd or movie["year"] > args.fd:
        return False
    if {g.strip() for g in movie["genre"].split(",")} <= BAD_GENRES:
        return False
    return not is_junk(movie["title"])

# =========================
# SCORING
# =========================
def get_extended_genres(base_genres: set) -> set:
    extended = set(base_genres)
    for g in base_genres:
        extended.update(ADJACENT_GENRES.get(g.lower(), []))
    return extended

def score_candidate(base, candidate, relaxed=False):
    score, reasons = 0.0, []
    bg       = {g.strip().lower() for g in base["genre"].split(",")}
    cg       = {g.strip().lower() for g in candidate["genre"].split(",")}
    accepted = get_extended_genres(bg) if relaxed else bg
    common   = cg & accepted
    if not common:
        return 0.0, []
    direct = cg & bg
    if direct:
        score += 3.0 + len(direct) * 0.5
        reasons.append(f"genres:{','.join(sorted(direct))}")
    else:
        score += 1.5
        reasons.append(f"adj:{','.join(sorted(common))}")
    bd = base["director"].lower().strip()
    cd = candidate["director"].lower().strip()
    if bd and bd != "n/a" and bd in cd:
        score += 4.0
        reasons.append("same_director")
    ba = {a.strip().lower() for a in base["actors"].split(",") if a.strip()}
    shared = sum(1 for a in ba if a and a != "n/a" and a in candidate["actors"].lower())
    if shared:
        score += shared * 2.0
        reasons.append(f"actors:{shared}")
    sem = plot_sim(base.get("plot", ""), candidate.get("plot", ""))
    score += sem * 6.0
    if sem > 0.45:
        reasons.append(f"plot_sim:{sem:.2f}")
    if not relaxed and sem < 0.4 and not direct and not shared:
        score -= 2.0
    # Extra penalty if plot_sim is very low and no director/actor signal
    # This avoids off-topic results like Ice Age from Independence Day
    if sem < 0.55 and not shared and bd == "n/a" or sem == 0.0:
        if not direct or len(direct) < 2:
            score -= 1.5
    score += candidate["rating"] / 3.0
    diff = abs(candidate["year"] - base["year"])
    score += 1.5 if diff < 5 else (0.8 if diff < 15 else 0)
    return round(score, 2), reasons

# =========================
# OLLAMA SUGGESTIONS
# =========================
def _parse_ollama_titles(raw: str) -> list:
    m = re.search(r'\{[^{}]*"films"\s*:\s*\[([^\]]+)\][^{}]*\}', raw, re.DOTALL)
    if m:
        try:
            return [t.strip() for t in json.loads(m.group(0)).get("films", []) if t.strip()]
        except:
            pass
    m = re.search(r'"films"\s*:\s*\[([^\]]+)\]', raw, re.DOTALL)
    if m:
        try:
            return [t.strip() for t in json.loads("[" + m.group(1) + "]") if t.strip()]
        except:
            pass
    items = re.findall(r'"([^"]{3,80})"', raw)
    bl = {"films","titles","suggestions","recommendations","similar","movies","title","film"}
    cleaned = [i for i in items if i.lower() not in bl]
    if len(cleaned) >= 3:
        return cleaned
    titles = []
    for line in raw.split("\n"):
        m2 = re.match(r'^(?:\d+[\.\)]|[-*])\s*(.+)$', line.strip())
        if m2:
            t = re.sub(r'\s*\(\d{4}\)\s*$', '', m2.group(1).strip().strip('"\''))
            if 2 < len(t) < 100:
                titles.append(t)
    return titles

def ollama_suggest_titles(base: dict) -> list:
    if not OLLAMA_OK:
        return []
    genre_instruction = (
        f'- Suggestions MUST be {args.genre} films\n'
        if args.genre else ""
    )
    mood_instruction = (
        f'- The mood/atmosphere requested by the user is: "{args.mood}" -- prioritize films that match this feeling\n'
        if args.mood else ""
    )
    prompt = (
        f'You are a film expert with encyclopedic knowledge of world cinema.\n\n'
        f'Source film: "{base["title"]}" ({base["year"]})\n'
        f'Genre: {base["genre"]}\n'
        f'Director: {base["director"]}\n'
        f'Cast: {base["actors"]}\n'
        f'Plot: {base["plot"]}\n\n'
        f'Suggest {args.suggestions} REAL existing films similar in theme, tone, atmosphere, or narrative style.\n\n'
        f'Rules:\n'
        f'- Only real theatrically released films\n'
        f'- Preferred IMDb rating above 6.5\n'
        f'- No direct sequels/prequels of the source film\n'
        f'- Vary the eras\n'
        f'- Use exact English/international theatrical title\n'
        f'- Do NOT include the source film itself\n'
        f'{genre_instruction}'
        f'{mood_instruction}'
        f'\nRespond ONLY with valid JSON:\n'
        f'{{"films": ["Title 1", "Title 2", ...]}}'
    )
    cprint(f"  [Ollama] Generating suggestions...", "magenta")
    try:
        result = subprocess.run(
            ["ollama", "run", OLLAMA_MODEL],
            input=prompt, text=True, capture_output=True,
            timeout=120, encoding="utf-8", errors="replace")
        titles = _parse_ollama_titles(result.stdout)
        cprint(f"  [Ollama] {len(titles)} titles extracted", "magenta")
        logger.info(f"Ollama: {len(titles)} titles for '{base['title']}'")
        return titles
    except subprocess.TimeoutExpired:
        log("Ollama timeout (120s)", "WARNING")
        return []
    except Exception as e:
        log(f"Ollama error: {e}", "ERROR")
        return []


def ollama_suggest_from_title(film_title: str) -> list:
    """Generate suggestions based on a film title not in the library."""
    if not OLLAMA_OK:
        return []
    mood_instruction = (
        f'- The mood/atmosphere requested is: "{args.mood}" -- prioritize films that match this feeling\n'
        if args.mood else ""
    )
    genre_instruction = (
        f'- Suggestions MUST be {args.genre} films\n'
        if args.genre else ""
    )
    prompt = (
        f'You are a film expert with encyclopedic knowledge of world cinema.\n\n'
        f'The user wants recommendations similar to: "{film_title}"\n\n'
        f'Suggest {args.suggestions} REAL existing films that share the same theme, tone, '
        f'atmosphere or narrative style as "{film_title}".\n\n'
        f'Rules:\n'
        f'- Only real theatrically released films\n'
        f'- Preferred IMDb rating above 6.5\n'
        f'- Do NOT include "{film_title}" itself\n'
        f'- No direct sequels/prequels\n'
        f'- Vary the eras\n'
        f'- Use exact English/international theatrical title\n'
        f'{genre_instruction}'
        f'{mood_instruction}'
        f'\nRespond ONLY with valid JSON:\n'
        f'{{"films": ["Title 1", "Title 2", ...]}}'
    )
    cprint(f'  [Ollama] Generating suggestions based on "{film_title}"...', "magenta")
    try:
        result = subprocess.run(
            ["ollama", "run", OLLAMA_MODEL],
            input=prompt, text=True, capture_output=True,
            timeout=120, encoding="utf-8", errors="replace")
        titles = _parse_ollama_titles(result.stdout)
        cprint(f'  [Ollama] {len(titles)} titles extracted', "magenta")
        logger.info(f'Ollama --like: {len(titles)} titles for "{film_title}"')
        return titles
    except subprocess.TimeoutExpired:
        log("Ollama timeout (120s)", "WARNING")
        return []
    except Exception as e:
        log(f"Ollama error: {e}", "ERROR")
        return []


def ollama_suggest_from_mood(mood: str) -> list:
    """Generate suggestions purely based on a mood/atmosphere description."""
    if not OLLAMA_OK:
        return []
    genre_instruction = (
        f'- Suggestions MUST be {args.genre} films\n'
        if args.genre else ""
    )
    prompt = (
        f'You are a film expert with encyclopedic knowledge of world cinema.\n\n'
        f'The user is looking for films with this specific mood or atmosphere: "{mood}"\n\n'
        f'Suggest {args.suggestions} REAL existing films that perfectly match this mood/atmosphere.\n\n'
        f'Rules:\n'
        f'- Only real theatrically released films\n'
        f'- Preferred IMDb rating above 6.5\n'
        f'- Vary the eras and genres\n'
        f'- Use exact English/international theatrical title\n'
        f'{genre_instruction}'
        f'\nRespond ONLY with valid JSON:\n'
        f'{{"films": ["Title 1", "Title 2", ...]}}'
    )
    cprint(f'  [Ollama] Generating suggestions for mood: "{mood}"...', "magenta")
    try:
        result = subprocess.run(
            ["ollama", "run", OLLAMA_MODEL],
            input=prompt, text=True, capture_output=True,
            timeout=120, encoding="utf-8", errors="replace")
        titles = _parse_ollama_titles(result.stdout)
        cprint(f'  [Ollama] {len(titles)} titles extracted', "magenta")
        logger.info(f'Ollama --mood: {len(titles)} titles for mood "{mood}"')
        return titles
    except subprocess.TimeoutExpired:
        log("Ollama timeout (120s)", "WARNING")
        return []
    except Exception as e:
        log(f"Ollama error: {e}", "ERROR")
        return []

# =========================
# FALLBACK
# =========================
def fallback_omdb_search(base: dict) -> list:
    found = []
    log(f"OMDb fallback for '{base['title']}'...", "FALLBACK")
    if base["director"] and base["director"] != "N/A":
        for t in search_omdb(base["director"].split()[-1], 10):
            if t not in found:
                found.append(t)
    if base["genre"]:
        g   = base["genre"].split(",")[0].strip()
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

def _build_target_genres(genre_arg: str) -> set:
    """Build a set of target genres including aliases (sci-fi <-> science fiction etc.)."""
    ALIASES = {
        "sci-fi":           "science fiction",
        "scifi":            "science fiction",
        "sf":               "science fiction",
        "science fiction":  "sci-fi",
    }
    result = set()
    for g in genre_arg.split(","):
        g = g.strip().lower()
        result.add(g)
        result.add(ALIASES.get(g, g))
    return result


def _sequel_candidates(radarr_movies: list, blacklist_titles: set) -> list:
    """Find sequels/prequels of films in the library that are NOT yet owned."""
    # We ask Ollama to find sequels for each source film
    # Simple heuristic: titles sharing main keyword with source
    pass  # Handled via Ollama suggestions naturally

def _is_sequel_of(candidate_title: str, source_title: str) -> bool:
    """Detect if candidate is likely a sequel/prequel of source."""
    # Extract main words (ignore articles, numbers)
    stop = {"the","a","an","of","in","on","at","and","or","part","chapter"}
    src_words = {w.lower() for w in re.findall(r'[a-zA-Z]+', source_title)
                 if w.lower() not in stop and len(w) > 2}
    cnd_words = {w.lower() for w in re.findall(r'[a-zA-Z]+', candidate_title)
                 if w.lower() not in stop and len(w) > 2}
    common = src_words & cnd_words
    # If 2+ main words in common -> likely sequel/related
    return len(common) >= 2 and len(src_words) >= 2

# =========================
# CANDIDATE VALIDATION
# =========================
def validate_candidate(raw_title, base, radarr_titles, radarr_tmdb, relaxed=False):
    title = _clean_title(raw_title)
    if not title or title.lower() == base["title"].lower():
        return None
    RUN_STATS["candidates_tested"] += 1
    if title in BLACKLIST or title in radarr_titles:
        RUN_STATS["filtered_blacklist"] += 1
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
        RUN_STATS["filtered_blacklist"] += 1
        log(f"  Skip (blacklist/Radarr): {omdb['title']}", "DEBUG")
        return None
    if not is_valid_candidate(omdb, min_score=min_score):
        if omdb["rating"] < min_score:
            RUN_STATS["filtered_rating"] += 1
        else:
            RUN_STATS["filtered_genre"] += 1
        log(f"  Filtered out: {omdb['title']} (IMDb:{omdb['rating']} {omdb['year']})", "DEBUG")
        return None

    # Genre filter when --genre is active
    # Hard reject only if no adjacent genre match either
    # Otherwise apply a score penalty (handled in score_candidate)
    if args.genre:
        target_genres   = _build_target_genres(args.genre)
        cand_genres_set = {g.strip().lower() for g in omdb["genre"].split(",")}
        adjacent        = set()
        for g in target_genres:
            adjacent.update(ADJACENT_GENRES.get(g, []))
        if not (target_genres & cand_genres_set) and not (adjacent & cand_genres_set):
            RUN_STATS["filtered_genre"] += 1
            log(f"  Filtered out (genre mismatch): {omdb['title']}", "DEBUG")
            return None
        if not (target_genres & cand_genres_set) and (adjacent & cand_genres_set):
            log(f"  Genre adjacent (soft match): {omdb['title']}", "DEBUG")
    sc, reasons = score_candidate(base, omdb, relaxed=relaxed)
    min_sc = 5.5 if relaxed else 4.0
    if sc < min_sc:
        RUN_STATS["filtered_score"] += 1
        log(f"  Score too low: {omdb['title']} -> {sc}", "DEBUG")
        return None
    lookup = get_radarr_lookup(omdb["title"], omdb["year"])
    if not lookup:
        log(f"  Radarr lookup failed: {omdb['title']}", "DEBUG")
        return None
    if lookup.get("tmdbId") in radarr_tmdb:
        log(f"  Already in Radarr (tmdbId): {omdb['title']}", "DEBUG")
        return None
    # Sequel/prequel boost: if candidate shares main words with source
    if _is_sequel_of(omdb["title"], base["title"]):
        sc = min(sc + 2.0, 20.0)
        reasons.append("sequel_related")
        log(f"  Sequel boost: {omdb['title']} <-> {base['title']}", "DEBUG")

    RUN_STATS["selected"] += 1
    relax_tag  = " [~]" if relaxed else ""
    title_str  = f"{omdb['title']} ({omdb['year']})"
    score_str  = f"IMDb:{omdb['rating']:.1f}  score:{sc}"
    reason_str = ", ".join(reasons)
    log(f"  + {title_str:<45} {score_str:<22} {reason_str}{relax_tag}", "SELECT")
    return {
        "title":   omdb["title"], "year":    omdb["year"],
        "rating":  omdb["rating"], "plot":   omdb["plot"],
        "score":   sc,             "reasons": reasons,
        "lookup":  lookup,         "source":  base["title"],
        "relaxed": relaxed,
    }

# =========================
# PROCESS SOURCE FILM
# =========================
def process_source(base: dict, radarr_titles: set, radarr_tmdb: set) -> list:
    suggested = ollama_suggest_titles(base)
    if len(suggested) < 4:
        log("Too few suggestions, using OMDb fallback...", "FALLBACK")
        extra = fallback_omdb_search(base)
        seen  = {t.lower() for t in suggested}
        for t in extra:
            if t.lower() not in seen:
                suggested.append(t)
                seen.add(t.lower())
    RUN_STATS["ollama_suggestions"] += len(suggested)
    cprint(f"  Validating {len(suggested)} candidates...", "gray")
    validated = []
    for raw in suggested:
        c = validate_candidate(raw, base, radarr_titles, radarr_tmdb, relaxed=False)
        if c:
            validated.append(c)
    if len(validated) < 2:
        log(f"Only {len(validated)} candidate(s) -> relaxed pass "
            f"(threshold:{args.score_relax}, adjacent genres)", "FALLBACK")
        seen_titles = {c["title"] for c in validated}
        for raw in suggested:
            t = _clean_title(raw)
            if t in seen_titles:
                continue
            c = validate_candidate(raw, base, radarr_titles, radarr_tmdb, relaxed=True)
            if c and c["title"] not in seen_titles:
                validated.append(c)
                seen_titles.add(c["title"])
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
# REPORT
# =========================
def print_report(results, added):
    w        = 90
    tested   = RUN_STATS["candidates_tested"]
    rejected = (RUN_STATS["filtered_rating"] + RUN_STATS["filtered_genre"]
                + RUN_STATS["filtered_blacklist"] + RUN_STATS["filtered_score"])
    print()
    cprint("=" * w, "white", bold=True)
    cprint(
        f"  RECOMMENDATIONS  --  {datetime.now().strftime('%Y-%m-%d  %H:%M')}"
        f"  --  {len(results)} films",
        "white", bold=True)
    cprint("=" * w, "white", bold=True)
    print()
    for i, m in enumerate(results, 1):
        is_added = m["title"] in added
        status   = "ADDED   " if is_added else "proposed"
        rlx      = " [~]" if m.get("relaxed") else ""
        rsn      = ", ".join(m.get("reasons", []))
        title_yr = f"{m['title']} ({m['year']})"
        scores   = f"IMDb {m['rating']:.1f}  score {m['score']}"
        color    = "green" if is_added else "cyan"
        cprint(f"  {i:2d}.  [{status}]  {title_yr:<45} {scores}{rlx}", color)
        cprint(f"        from: {m['source']:<30} {rsn}", "gray")
    print()
    cprint("-" * w, "gray")
    cprint(
        f"  STATS   sources:{RUN_STATS['sources_processed']}  "
        f"suggestions:{RUN_STATS['ollama_suggestions']}  "
        f"tested:{tested}  "
        f"rejected:{rejected} "
        f"(imdb:{RUN_STATS['filtered_rating']} "
        f"genre:{RUN_STATS['filtered_genre']} "
        f"bl:{RUN_STATS['filtered_blacklist']} "
        f"score:{RUN_STATS['filtered_score']})  "
        f"added:{RUN_STATS['added']}",
        "gray")
    cprint("=" * w, "white", bold=True)
    print()
    logger.info(
        f"Report: {len(added)} added / {len(results)} proposed | "
        f"tested:{tested} rejected:{rejected} selected:{RUN_STATS['selected']}")

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
    log(f"Blacklist loaded: {len(BLACKLIST)} titles")
    print_header(len(BLACKLIST), genre_filter=args.genre)

    # Build source pool — filter by genre if --genre is specified
    pool = [m for m in radarr if m.get("title")]
    if args.genre:
        target_genres = _build_target_genres(args.genre)
        filtered_pool = [
            m for m in pool
            if any(
                g.lower() in target_genres
                for g in m.get("genres", [])
            )
        ]
        if not filtered_pool:
            log(f"No films found in your library for genre: {args.genre}", "WARNING")
            log("Available genres in your library:", "INFO")
            all_genres = sorted({
                g for m in pool
                for g in m.get("genres", [])
                if g
            })
            cprint(f"  {', '.join(all_genres)}", "cyan")
            return
        log(f"Genre filter '{args.genre}': {len(filtered_pool)} matching films in library", "INFO")
        pool = filtered_pool

    # ── --mood mode: generate directly from atmosphere description ────────
    if args.mood and not args.like:
        cprint(f'\n  Mood: "{args.mood}"', "cyan", bold=True)
        cprint("-" * 70, "gray")
        mood_titles = ollama_suggest_from_mood(args.mood)
        if not mood_titles:
            log(f'No suggestions found for mood: "{args.mood}"', "WARNING")
            return
        RUN_STATS["ollama_suggestions"] += len(mood_titles)
        cprint(f"  Validating {len(mood_titles)} candidates...", "gray")
        # Use a neutral base for scoring
        base_for_mood = {
            "title": f'mood:{args.mood}', "year": 2000,
            "genre": args.genre or "Drama,Thriller,Crime,Horror,Action,Comedy,Sci-Fi",
            "actors": "", "director": "n/a", "rating": 0.0, "plot": args.mood
        }
        validated_mood = []
        for raw in mood_titles:
            omdb = get_omdb_full(_clean_title(raw))
            if not omdb:
                continue
            if omdb["title"] in radarr_titles or omdb["title"] in BLACKLIST:
                continue
            if omdb["rating"] < args.score_relax:
                continue
            # For mood mode, skip genre/score filtering — Ollama chose these for the mood
            lookup = get_radarr_lookup(omdb["title"], omdb["year"])
            if not lookup or lookup.get("tmdbId") in radarr_tmdb:
                continue
            RUN_STATS["selected"] += 1
            log(f"  + {omdb['title']} ({omdb['year']})  IMDb:{omdb['rating']:.1f}  genres:{omdb['genre']}", "SELECT")
            validated_mood.append({
                "title": omdb["title"], "year": omdb["year"],
                "rating": omdb["rating"], "plot": omdb["plot"],
                "score": round(omdb["rating"] * 1.5, 2),
                "reasons": [f"mood:{args.mood}"],
                "lookup": lookup, "source": f'mood:{args.mood}',
                "relaxed": False,
            })
        if not validated_mood:
            log(f'No valid candidates found for mood: "{args.mood}"', "WARNING")
            return
        validated_mood.sort(key=lambda x: x["score"], reverse=True)
        final_mood = validated_mood[:args.top]
        output = []
        for m in final_mood:
            lk = m["lookup"]
            output.append({
                "title": lk["title"], "year": lk.get("year"),
                "rating": m["rating"], "score": m["score"],
                "reasons": m["reasons"], "tmdbId": lk["tmdbId"],
                "titleSlug": lk["titleSlug"], "images": lk.get("images", []),
                "source": m["source"],
            })
        json_file = f"reco_{today_str}.json"
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=4, ensure_ascii=False)
        log(f"Results saved -> {json_file}")
        added = []
        if args.auto:
            for m in output:
                if add_to_radarr(m):
                    added.append(m["title"])
                    RUN_STATS["added"] += 1
                    BLACKLIST.add(m["title"])
        else:
            print_report(final_mood, added=[])
            cprint("\nAdd to Radarr?", "white", bold=True)
            choice = input("  (a=all / o=one by one / n=no): ").lower().strip()
            if choice == "a":
                for m in output:
                    if add_to_radarr(m):
                        added.append(m["title"])
                        RUN_STATS["added"] += 1
                        BLACKLIST.add(m["title"])
            elif choice == "o":
                for m in output:
                    rep = input(f"  + {m['title']} ({m['year']}) IMDb:{m['rating']:.1f}  add? (y/n): ").lower()
                    if rep == "y":
                        if add_to_radarr(m):
                            added.append(m["title"])
                            RUN_STATS["added"] += 1
                            BLACKLIST.add(m["title"])
                    else:
                        bl_rep = input(f"    Blacklist '{m['title']}'? (y/n): ").lower()
                        if bl_rep == "y":
                            BLACKLIST.add(m["title"])
        print_report(final_mood, added)
        save_blacklist(BLACKLIST)
        cprint(f"  Blacklist updated: {len(BLACKLIST)} titles", "gray")
        cprint(f"  Log saved: {log_file}", "gray")
        return
    # ─────────────────────────────────────────────────────────────────────

    # ── --like mode: use a specific film as the only source ──────────────
    if args.like:
        cprint(f'\n  Based on: "{args.like}"', "cyan", bold=True)
        cprint("-" * 70, "gray")
        like_titles = ollama_suggest_from_title(args.like)
        if not like_titles:
            log(f'No suggestions found for "{args.like}"', "WARNING")
            return
        RUN_STATS["ollama_suggestions"] += len(like_titles)
        cprint(f"  Validating {len(like_titles)} candidates...", "gray")
        # Create a fake base dict for scoring
        like_omdb = get_omdb_full(args.like)
        base_for_like = like_omdb if like_omdb else {
            "title": args.like, "year": 2000, "genre": "", "actors": "",
            "director": "", "rating": 0.0, "plot": ""
        }
        validated_like = []
        for raw in like_titles:
            c = validate_candidate(raw, base_for_like, radarr_titles, radarr_tmdb, relaxed=False)
            if c:
                c["source"] = f'like:{args.like}'
                validated_like.append(c)
        if not validated_like:
            log(f'No valid candidates found for "{args.like}"', "WARNING")
            return
        validated_like.sort(key=lambda x: x["score"], reverse=True)
        final_like = validated_like[:args.top]
        output = []
        for m in final_like:
            lk = m["lookup"]
            output.append({
                "title": lk["title"], "year": lk.get("year"),
                "rating": m["rating"], "score": m["score"],
                "reasons": m["reasons"], "tmdbId": lk["tmdbId"],
                "titleSlug": lk["titleSlug"], "images": lk.get("images", []),
                "source": m["source"],
            })
        json_file = f"reco_{today_str}.json"
        with open(json_file, "w", encoding="utf-8") as f:
            json.dump(output, f, indent=4, ensure_ascii=False)
        log(f"Results saved -> {json_file}")
        added = []
        if args.auto:
            for m in output:
                if add_to_radarr(m):
                    added.append(m["title"])
                    RUN_STATS["added"] += 1
                    BLACKLIST.add(m["title"])
        else:
            print_report(final_like, added=[])
            cprint("\nAdd to Radarr?", "white", bold=True)
            choice = input("  (a=all / o=one by one / n=no): ").lower().strip()
            if choice == "a":
                for m in output:
                    if add_to_radarr(m):
                        added.append(m["title"])
                        RUN_STATS["added"] += 1
                        BLACKLIST.add(m["title"])
            elif choice == "o":
                for m in output:
                    rep = input(f"  + {m['title']} ({m['year']}) IMDb:{m['rating']:.1f}  add? (y/n): ").lower()
                    if rep == "y":
                        if add_to_radarr(m):
                            added.append(m["title"])
                            RUN_STATS["added"] += 1
                            BLACKLIST.add(m["title"])
                    else:
                        bl_rep = input(f"    Blacklist '{m['title']}'? (y/n): ").lower()
                        if bl_rep == "y":
                            BLACKLIST.add(m["title"])
        print_report(final_like, added)
        save_blacklist(BLACKLIST)
        cprint(f"  Blacklist updated: {len(BLACKLIST)} titles", "gray")
        cprint(f"  Log saved: {log_file}", "gray")
        return
    # ─────────────────────────────────────────────────────────────────────

    random.shuffle(pool)
    sources = pool[:args.sources]
    log(f"{len(sources)} source films selected from your library")
    # Pre-validate sources against OMDb genre if --genre is active
    validated_sources = []
    for r in sources:
        title = r["title"]
        base  = get_omdb_full(title)
        if not base:
            log(f"OMDb not found for '{title}' -- skipped", "WARNING")
            continue
        if args.genre:
            target_genres = _build_target_genres(args.genre)
            omdb_genres   = {g.strip().lower() for g in base.get("genre", "").split(",")}
            if not target_genres & omdb_genres:
                log(f"Source '{title}' skipped (OMDb genre mismatch: {base.get('genre','')})", "DEBUG")
                continue
        validated_sources.append((r, base))

    if not validated_sources:
        log("No valid source films found for the requested genre.", "WARNING")
        return

    all_results  = {}
    source_count_map = {}  # track how many sources suggested each film

    for i, (r, base) in enumerate(validated_sources):
        title = r["title"]
        print_source_header(i + 1, len(validated_sources), title, base.get("genre", ""))
        RUN_STATS["sources_processed"] += 1
        if base.get("plot"):
            get_embedding(base["plot"])
        candidates = process_source(base, radarr_titles, radarr_tmdb)
        cprint(f"  -> {len(candidates)} candidate(s) retained", "cyan")
        for c in candidates:
            key = c["title"]
            source_count_map[key] = source_count_map.get(key, 0) + 1
            if key not in all_results or c["score"] > all_results[key]["score"]:
                all_results[key] = c

    # Apply multi-source bonus + score cap
    for key, c in all_results.items():
        n = source_count_map.get(key, 1)
        if n > 1:
            bonus = round((n - 1) * 0.8, 2)
            c["score"] = round(c["score"] + bonus, 2)
            c["reasons"] = c.get("reasons", []) + [f"multi_source:{n}"]
        c["score"] = min(c["score"], 20.0)  # cap score
    sorted_all = sorted(all_results.values(), key=lambda x: x["score"], reverse=True)
    final, source_count = [], {}
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
    output = []
    for m in final:
        lk = m["lookup"]
        output.append({
            "title":     lk["title"],    "year":      lk.get("year"),
            "rating":    m["rating"],    "score":     m["score"],
            "reasons":   m["reasons"],   "tmdbId":    lk["tmdbId"],
            "titleSlug": lk["titleSlug"],"images":    lk.get("images", []),
            "source":    m["source"],
        })
    json_file = f"reco_{today_str}.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)
    log(f"Results saved -> {json_file}")
    if not output:
        log("No recommendations found.", "WARNING")
        return
    added = []
    if args.auto:
        log(f"AUTO mode -- adding {len(output)} films to Radarr")
        for m in output:
            if add_to_radarr(m):
                added.append(m["title"])
                RUN_STATS["added"] += 1
                BLACKLIST.add(m["title"])
    else:
        print_report(final, added=[])
        cprint("\nAdd to Radarr?", "white", bold=True)
        choice = input("  (a=all / o=one by one / n=no): ").lower().strip()
        if choice == "a":
            for m in output:
                if add_to_radarr(m):
                    added.append(m["title"])
                    RUN_STATS["added"] += 1
                    BLACKLIST.add(m["title"])
        elif choice == "o":
            for m in output:
                rep = input(
                    f"  + {m['title']} ({m['year']}) IMDb:{m['rating']:.1f}  add? (y/n): "
                ).lower()
                if rep == "y":
                    if add_to_radarr(m):
                        added.append(m["title"])
                        RUN_STATS["added"] += 1
                        BLACKLIST.add(m["title"])
                else:
                    bl_rep = input(f"    Blacklist '{m['title']}'? (y/n): ").lower()
                    if bl_rep == "y":
                        BLACKLIST.add(m["title"])
    print_report(final, added)
    save_blacklist(BLACKLIST)
    cprint(f"  Blacklist updated: {len(BLACKLIST)} titles", "gray")
    cprint(f"  Log saved: {log_file}", "gray")

if __name__ == "__main__":
    main()
