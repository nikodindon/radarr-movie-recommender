#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
newmovies_v22.py -- Radarr Movie Recommender

v22 changes:
- --synopsis: show plot synopsis when reviewing films one by one
- --imdb-min: set minimum IMDb rating on the fly
- --export FILE: export recommendations to CSV or HTML
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
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

# LLM backend (llm_backend.py) — abstraction Ollama/llama.cpp
try:
    from llm_backend import make_backend as _make_llm_backend, LLMBackend
    LLM_BACKEND_AVAILABLE = True
except ImportError:
    LLM_BACKEND_AVAILABLE = False

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
            "TMDB_API_KEY":        str(cfg.get("tmdb_api_key",   "")),
            "TMDB_REGION":         str(cfg.get("tmdb_region",    "FR")),
        }
    env_file = base / ".env"
    if env_file.exists():
        # The .env parser is a simple KEY=VALUE one (no shell quoting,
        # no $VAR expansion). All supported variables are loaded
        # into os.environ via setdefault — anything already set in
        # the real env wins. This covers V1-V5 variables (OMDB,
        # Radarr, quality profile) and V5.4+ ones (web host/port,
        # LLM backend/model/base_url).
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
TMDB_API_KEY         = _cfg["TMDB_API_KEY"]
TMDB_REGION          = _cfg["TMDB_REGION"]

if not OMDB_KEYS:
    print("[ERROR] No OMDb key found. Check config.yaml (OMDB_KEYS=key1,key2,...)")
    exit(1)
if not RADARR_API_KEY:
    print("[ERROR] RADARR_API_KEY missing. Check config.yaml")
    exit(1)

CONFIG_FILE      = "omdb_apikey.conf"
BLACKLIST_FILE   = "blacklist.json"
# OLLAMA_EMBED_URL conservé pour rétro-compat (OllamaBackend lit $OLLAMA_EMBED_URL
# ou retombe sur cette valeur). Ignoré quand llm_backend=llamacpp.
OLLAMA_EMBED_URL = os.environ.get("OLLAMA_EMBED_URL", "http://localhost:11434/api/embeddings")
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
parser = argparse.ArgumentParser(description="Radarr Movie Recommender v22")
parser.add_argument("--sd",            type=int,   default=1970)
parser.add_argument("--fd",            type=int,   default=2030)
parser.add_argument("--score",         type=float, default=6.5)
parser.add_argument("--score-relax",   type=float, default=5.9)
parser.add_argument("--sources",       type=int,   default=10)
parser.add_argument("--suggestions",   type=int,   default=14)
parser.add_argument("--top",           type=int,   default=10)
parser.add_argument("--auto",          action="store_true")
parser.add_argument("--dry-run",       action="store_true",
    help="Compute recommendations but DO NOT call Radarr's POST /movie. "
         "Logs each film that would be added with a [DRY-RUN] tag.")
parser.add_argument("--limit",         type=int,   default=0,
    help="Hard cap on the number of films added per run (0 = no cap). "
         "Combined with --auto, prevents runaway adds. Applies AFTER --top.")
parser.add_argument("--yes",           action="store_true",
    help="Skip interactive confirmations (e.g. --resetblacklist). "
         "Use in CI/cron contexts.")
parser.add_argument("--no-embed",      action="store_true")
parser.add_argument("--debug",         action="store_true")
parser.add_argument("--genre",         type=str,   default=None,
    help="Filter by genre (e.g. Comedy, Sci-Fi, Horror). Comma-separated for multiple.")
parser.add_argument("--mood",          type=str,   default=None,
    help="Describe the atmosphere you want (e.g. feel good, dark and intense)")
parser.add_argument("--like",          type=str,   default=None,
    help="Get recommendations based on a specific film (even if not in your library)")
parser.add_argument("--resetblacklist", action="store_true",
    help="Reset the blacklist to empty")
parser.add_argument("--web", action="store_true",
    help="Launch the web UI (FastAPI server on 127.0.0.1:8080) instead "
         "of running a recommendation in the terminal. Vague 5.")
parser.add_argument("--web-port", type=int, default=None,
    help="Port for --web (default: env RADARR_RECO_WEB_PORT, then config.yaml "
         "web.port, then 8080). Useful when 8080 is taken (e.g. by llama-server).")
parser.add_argument("--web-host", type=str, default=None,
    help="Host for --web (default: env RADARR_RECO_WEB_HOST, then config.yaml "
         "web.host, then 127.0.0.1). Set to 0.0.0.0 to expose on the LAN "
         "(no auth, only do this on a trusted network).")
parser.add_argument("--saga",          type=str,   default=None, nargs="?", const="__auto__",
    help="Complete saga films. Use alone for auto-detection or specify a saga name")
parser.add_argument("--director",      type=str,   default=None,
    help="Add missing films by a director")
parser.add_argument("--actor",         type=str,   default=None,
    help="Add missing films featuring actor(s) — comma-separated: --actor \"Pacino, De Niro\"")
parser.add_argument("--cast",          type=str,   default=None,
    help="Add missing films where ALL listed actors appear together: --cast \"Stiller, Wilson\"")
parser.add_argument("--composer",      type=str,   default=None,
    help="Add missing films scored by a composer")
parser.add_argument("--author",        type=str,   default=None,
    help="Add missing film adaptations of an author")
parser.add_argument("--artist-top",    type=int,   default=0,
    help="Limit filmography results (0 = all, e.g. --artist-top 20)")
parser.add_argument("--no-timeout",    action="store_true",
    help="Disable Ollama timeouts (useful for large models or nightly runs)")
parser.add_argument("--stats",         action="store_true",
    help="Display collection statistics")
parser.add_argument("--watchlist",     type=str,   default=None,
    help="Import films from Letterboxd or IMDb CSV watchlist file")
parser.add_argument("--analyze",       action="store_true",
    help="AI-powered collection analysis with personalized recommendations")
parser.add_argument("--discovery",     action="store_true",
    help="Discover recent & upcoming films via TMDB (independent of your collection)")
parser.add_argument("--blu-ray",       action="store_true",
    help="With --discovery, also include recent/upcoming Blu-ray/DVD/4K releases")
parser.add_argument("--synopsis", action="store_true",
    help="Show full plot synopsis when reviewing films one by one")
# V5.5 toggles were removed in V5.7: poster/synopsis/credits are
# always shown in the web UI now. The OMDb cache keeps the cost
# at zero on warm runs.
parser.add_argument("--imdb-min", type=float, default=None,
    help="Minimum IMDb rating override (e.g. --imdb-min 7.5)")
parser.add_argument("--export",        type=str,   default=None,
    help="Export recommendations to file (e.g. --export reco.csv or --export reco.html)")
# V5.22: OMDb search fallback for artist modes. When set, after
# the LLM returns its list, we query OMDb's search API for the
# person (e.g. "kevin james actor") and merge any additional
# films found. Helps when the LLM omits a famous title
# (e.g. Paul Blart: Mall Cop for Kevin James).
parser.add_argument("--omdb-fallback", action="store_true",
    help="(artist modes) after the LLM returns, query OMDb search to find any missing films by the person")
# V5.25: interactive onboarding. For new Radarr libraries: ask the
# user a few questions about their taste, then propose 2 films
# per genre (Action, Comedy, Drama, Sci-Fi, Horror, Romance,
# Thriller) to kickstart the collection. CLI-only for now; the
# web UI version will be a separate piece.
parser.add_argument("--onboard", action="store_true",
    help="(CLI) interactive onboarding for new Radarr libraries. Asks a few taste questions then proposes 2 films per genre to add.")
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
    cprint(f"  RADARR MOVIE RECOMMENDER  v22          {now}", "white", bold=True)
    # V5.8: model name was hardcoded to OLLAMA_MODEL (legacy, default
    # "llama3.1:8b") even when the user is on llamacpp. Now we use
    # the LLM backend's own label which knows the real model.
    # Falls back to OLLAMA_MODEL if LLM is None (offline / init fail).
    if LLM is not None:
        model_label = f"{LLM.name} ({LLM.model})"
    else:
        model_label = OLLAMA_MODEL
    cprint(f"  Model: {model_label:<30} Blacklist: {blacklist_size} titles", "gray")
    cprint(f"  Quality profile: {QUALITY_PROFILE_ID:<10} Availability: {MINIMUM_AVAILABILITY}", "gray")
    if genre_filter:
        cprint(f"  Genre filter: {genre_filter}", "cyan")
    if hasattr(args, "mood") and args.mood:
        cprint(f"  Mood: {args.mood}", "cyan")
    if hasattr(args, "like") and args.like:
        cprint(f"  Based on: {args.like}", "cyan")
    if hasattr(args, "saga") and args.saga and args.saga != "__auto__":
        cprint(f"  Saga: {args.saga}", "cyan")
    elif hasattr(args, "saga") and args.saga == "__auto__":
        cprint(f"  Saga: auto-detection", "cyan")
    if hasattr(args, "director") and args.director:
        cprint(f"  Director: {args.director}", "cyan")
    if hasattr(args, "actor") and args.actor:
        cprint(f"  Actor: {args.actor}", "cyan")
    if hasattr(args, "cast") and args.cast:
        cprint(f"  Cast together: {args.cast}", "cyan")
    if hasattr(args, "composer") and args.composer:
        cprint(f"  Composer: {args.composer}", "cyan")
    if hasattr(args, "author") and args.author:
        cprint(f"  Author: {args.author}", "cyan")
    if getattr(args, "no_timeout", False):
        cprint(f"  Mode: no timeout (large model)", "yellow")
    if getattr(args, "imdb_min", None):
        cprint(f"  IMDb min: {args.imdb_min}", "cyan")
    if getattr(args, "synopsis", False):
        cprint(f"  Synopsis: on", "cyan")
    if getattr(args, "watchlist", None):
        cprint(f"  Watchlist: {args.watchlist}", "cyan")
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
        except (OSError, json.JSONDecodeError, ValueError) as e:
            log(f"Blacklist file unreadable ({type(e).__name__}: {e}), starting empty", "WARNING")
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
    if not getattr(args, "yes", False):
        cprint(f"  About to wipe {count} titles from {BLACKLIST_FILE}.", "yellow", bold=True)
        cprint("  This cannot be undone (the blacklist will re-fill from your "
               "Radarr library on the next run, but any manual additions will be lost).",
               "yellow")
        try:
            ans = input("  Type 'yes' to confirm, anything else to abort: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ""
        if ans != "yes":
            cprint("  Aborted. Blacklist untouched.", "gray")
            exit(0)
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
        except (OSError, ValueError) as e:
            log(f"OMDb config file unreadable ({type(e).__name__}: {e})", "DEBUG")
    return OMDB_KEYS[0]

def save_current_key(key):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(key)
    except OSError as e:
        log(f"OMDb config file unwritable ({type(e).__name__}: {e})", "DEBUG")

def test_omdb_key(key):
    try:
        r = requests.get(f"http://www.omdbapi.com/?t=Inception&apikey={key}", timeout=8)
        return r.json().get("Response") == "True"
    except (requests.RequestException, ValueError) as e:
        log(f"OMDb test failed for key ...{key[-4:]}: {type(e).__name__}: {e}", "DEBUG")
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
# LLM BACKEND (remplace l'ancien bloc OLLAMA)
# =========================
def _build_llm_backend():
    """Construit le backend LLM selon config.yaml (llm_backend: llamacpp|ollama).
       Lit aussi les paramètres additionnels (llamacpp_base_url, etc.).

       V5.30: resolution order is YAML > os.environ > default. The
       .env file is loaded as os.environ.setdefault at startup
       (see _load_config above), so any LLM_BACKEND/LLM_MODEL/
       LLAMACPP_BASE_URL in .env is automatically picked up here.
       Command-line env vars (RADARR_RECO_LLM_*) take precedence
       over .env because they hit the real env, not setdefault.
    """
    if not LLM_BACKEND_AVAILABLE:
        return None
    base = Path(__file__).parent
    cfg_file = base / "config.yaml"
    cfg = {}
    if cfg_file.exists() and YAML_AVAILABLE:
        try:
            with open(cfg_file, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            pass
    backend_cfg = {
        # Priority: YAML value > os.environ value > default
        "llm_backend":       (cfg.get("llm_backend")
                               or os.environ.get("LLM_BACKEND", "ollama")),
        "llm_model":         (cfg.get("llm_model")
                               or cfg.get("ollama_model")
                               or os.environ.get("LLM_MODEL")
                               or OLLAMA_MODEL),
        "llamacpp_base_url": (cfg.get("llamacpp_base_url")
                               or os.environ.get("LLAMACPP_BASE_URL", "http://localhost:8080")),
        "no_timeout":        getattr(args, "no_timeout", False),
    }
    try:
        return _make_llm_backend(backend_cfg)
    except Exception as e:
        log(f"LLM backend init error: {e}", "ERROR")
        return None

LLM = _build_llm_backend()
if LLM is not None:
    LLM_OK = LLM.healthcheck()
    label = f"{LLM.name} ({LLM.model})"
    cprint(f"  LLM: {label}  base={getattr(LLM, 'base_url', 'local')}", "gray")
else:
    LLM_OK = False
    cprint("  LLM: backend unavailable (llm_backend.py import failed)", "WARNING")

# OLLAMA_OK conservé comme alias rétro-compatible utilisé partout dans le code.
# Si tu passes à un autre backend, change OLLAMA_OK -> LLM_OK dans une seule
# étape de refactor (voir TODO plus bas).
OLLAMA_OK = LLM_OK
log(f"LLM {'ready' if LLM_OK else 'UNAVAILABLE'}",
    "SUCCESS" if LLM_OK else "WARNING")

# =========================
# CACHES + STATS
# =========================
OMDB_CACHE      = {}
EMBEDDING_CACHE = {}
# OMDb disk cache (Vague 3). Persisted at end of run, reloaded at boot.
# Located in XDG cache dir. Note: we only cache successful lookups;
# failed lookups (None) are retried next run (Vague 3 fix on the audit's
# bug #8 — caching negatives means a transient OMDb outage or a typo
# would silently kill that film forever).
OMDB_CACHE_DIR  = Path.home() / ".cache" / "radarr-reco"
OMDB_CACHE_FILE = OMDB_CACHE_DIR / "omdb_cache.json"

def _load_omdb_cache():
    """Charge le cache OMDb depuis le disque. Silencieux en cas d'erreur."""
    try:
        if OMDB_CACHE_FILE.exists():
            with open(OMDB_CACHE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Validation minimale : doit être un dict[str, dict|None]
            if isinstance(data, dict):
                # On filtre les None par sécurité (cohérence avec le fix V3.3)
                return {k: v for k, v in data.items() if v is not None}
    except (OSError, json.JSONDecodeError, ValueError) as e:
        log(f"OMDb cache file unreadable ({type(e).__name__}): {e}, starting empty", "DEBUG")
    return {}

def save_omdb_cache():
    """Persiste le cache OMDb sur disque. Écriture atomique (tmp + rename)."""
    try:
        OMDB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = OMDB_CACHE_FILE.with_suffix(".tmp")
        # Filtre défensif : on ne sauvegarde jamais de None (V3.3)
        to_save = {k: v for k, v in OMDB_CACHE.items() if v is not None}
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=1)
        tmp.replace(OMDB_CACHE_FILE)
    except OSError as e:
        log(f"OMDb cache file unwritable ({type(e).__name__}: {e})", "DEBUG")

# Charge le cache existant en mémoire (Vague 3 V3.2)
OMDB_CACHE.update(_load_omdb_cache())
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

# Lock guarding CURRENT_OMDB_KEY rotation when multiple threads call
# _omdb_request in parallel (Vague 3 mood-mode parallelisation).
# The dict OMDB_CACHE itself is GIL-safe for atomic get/set so it doesn't
# need explicit protection, but the key rotation + file save is not atomic.
import threading
_omdb_lock = threading.Lock()

def _omdb_request(params: dict, retries=2):
    global CURRENT_OMDB_KEY
    # The lock here is held for the entire request (HTTP + 1.1s sleep),
    # which means concurrent callers serialise on the rate limit anyway.
    # This is intentional: OMDb's free tier is 1 req/sec/key, and going
    # faster gets us banned. The parallel gain comes from doing other
    # work (logging, scoring) while one thread holds the OMDb lock.
    with _omdb_lock:
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
    # Fallback 1: if title has subtitle after ":", try without subtitle
    if not data and ":" in title:
        short_title = title.split(":")[0].strip()
        data = _omdb_request({"t": short_title, "type": "movie", "plot": "short"})
    # Fallback 2: if title has subtitle after " - ", try without
    if not data and " - " in title:
        short_title = title.split(" - ")[0].strip()
        data = _omdb_request({"t": short_title, "type": "movie", "plot": "short"})
    if not data:
        # V3.3: do NOT cache the negative result. A transient OMDb outage
        # or a typo would otherwise silently kill that film forever in
        # subsequent runs. We pay one retry per run per failed title,
        # but failures are rare and the OMDb rate limit absorbs it.
        return None
    try:
        year_val = int(data.get("Year", "0")[:4])
    except (ValueError, TypeError):
        year_val = 0
    try:
        rating = float(data.get("imdbRating", "0"))
    except (ValueError, TypeError):
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
        # V5.5: OMDb exposes a public Poster URL. Used by the web UI
        # when --show-posters is on. No local download (see design
        # note in radarr_reco/web/server.py around _read_web_config).
        "poster":   data.get("Poster", ""),
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
        except (requests.RequestException, ValueError, KeyError, TypeError) as e:
            log(f"OMDb search lookup failed for {title!r}: {type(e).__name__}: {e}", "DEBUG")
            return None
    result = _lookup(f"{title} {year}" if year else title)
    if (not result or not result.get("tmdbId")) and year:
        result = _lookup(title)
    return result

def add_to_radarr(movie):
    # --limit is enforced here (single chokepoint) before either the dry-run
    # short-circuit or the real Radarr POST. This way, every code path that
    # counts an "add" goes through the same gate.
    if getattr(args, "limit", 0) > 0 and RUN_STATS["added"] >= args.limit:
        log(f"[LIMIT] {args.limit} film(s) already added this run, "
            f"skipping {movie['title']}", "WARNING")
        return False
    # Dry-run short-circuits BEFORE any network call. The function still
    # returns True so the calling code path (counter increment, blacklist
    # update) is preserved, but Radarr is not mutated.
    if getattr(args, "dry_run", False):
        log(f"[DRY-RUN] Would add: {movie['title']} ({movie['year']})", "WARNING")
        return True
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
# CONFIRM AND ADD (centralised, Vague 4)
# =========================
def _build_add_payload(m: dict) -> dict:
    """Construit le payload Radarr à partir d'un candidate. Utilisé par
       le mode 'o' (one-by-one) où on n'a pas accès à output[]."""
    lk = m["lookup"]
    return {
        "title":     lk["title"],
        "year":      lk.get("year"),
        "rating":    m["rating"],
        "score":     m["score"],
        "reasons":   m["reasons"],
        "tmdbId":    lk["tmdbId"],
        "titleSlug": lk["titleSlug"],
        "images":    lk.get("images", []),
        "source":    m.get("source", ""),
    }

def _print_one_synopsis(m: dict) -> None:
    """Affiche le synopsis d'un candidate (utilisé par mode 'o' avec --synopsis).
       Stratégie : 1) plot dans m, 2) cache OMDb par titre+année, 3) cache sans
       année, 4) fetch OMDb live. Évite l'appel réseau si déjà cached."""
    plot = m.get("plot", "")
    if not plot:
        cached = OMDB_CACHE.get(f"{m['title']}|{m.get('year','')}")
        if not cached:
            cached = OMDB_CACHE.get(f"{m['title']}|")
        if isinstance(cached, dict):
            plot = cached.get("plot", "")
    if not plot:
        omdb = get_omdb_full(m["title"])
        plot = omdb.get("plot", "") if omdb else ""
    _print_synopsis(m["title"], plot)

def _add_one_with_prompt(m: dict) -> bool:
    """Prompt y/n pour un candidate. Retourne True si ajouté.
       Gère aussi le prompt 'Blacklist X?' si refusé."""
    show_syn = getattr(args, "synopsis", False)
    cprint(f"  + {m['title']} ({m['year']})  IMDb:{m['rating']:.1f}", "cyan")
    if show_syn:
        _print_one_synopsis(m)
    rep = input("  add? (y/n): ").lower().strip()
    if rep == "y":
        payload = _build_add_payload(m)
        return add_to_radarr(payload)
    bl_rep = input(f"    Blacklist '{m['title']}'? (y/n): ").lower()
    if bl_rep == "y":
        BLACKLIST.add(m["title"])
    return False

def confirm_and_add(output: list, missing: list = None, label: str = "") -> list:
    """Demande à l'utilisateur comment procéder ('a'=all, 'o'=one-by-one, 'n'=no)
       et applique le choix. Centralise le pattern dupliqué dans 7 modes
       (saga, filmography, watchlist, analyze, mood, like, default).
       Renvoie la liste des titres effectivement ajoutés."""
    if not output:
        return []
    cprint(f"\n  Add to Radarr? ({len(output)} candidates){f' -- {label}' if label else ''}",
           "white", bold=True)
    try:
        choice = input("  (a=all / o=one by one / n=no): ").lower().strip()
    except (EOFError, KeyboardInterrupt):
        choice = "n"
    added = []
    if choice == "a":
        for m in output:
            if add_to_radarr(m):
                added.append(m["title"])
                RUN_STATS["added"] += 1
                BLACKLIST.add(m["title"])
    elif choice == "o":
        # Si 'missing' est fourni, on itère dessus (variante saga/filmography)
        # sinon sur output directement (variante mood/default).
        iterable = missing if missing is not None else output
        for m in iterable:
            if _add_one_with_prompt(m):
                added.append(m["title"])
                RUN_STATS["added"] += 1
                BLACKLIST.add(m["title"])
    # 'n' ou autre: rien à faire
    return added

# =========================
# EMBEDDINGS
# =========================
def get_embedding(text):
    if args.no_embed or not text:
        return None
    key = text[:300]
    if key in EMBEDDING_CACHE:
        return EMBEDDING_CACHE[key]
    # Délégation au backend LLM (llama.cpp /v1/embeddings ou Ollama /api/embeddings).
    emb = None
    if LLM is not None:
        try:
            emb = LLM.embed(text)
        except Exception as e:
            log(f"Embedding error: {e}", "DEBUG")
            emb = None
    EMBEDDING_CACHE[key] = emb
    return emb

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
    # --imdb-min overrides default score threshold
    imdb_override = getattr(args, "imdb_min", None)
    threshold = imdb_override if imdb_override is not None else (min_score if min_score is not None else args.score)
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
    # Extra penalty if plot_sim is very low AND we have no director/actor
    # signal. This avoids off-topic results like Ice Age from Independence Day.
    # Note: we used to OR `sem == 0.0` here, which double-penalised candidates
    # when embeddings were unavailable (--no-embed or llama-server without
    # --embeddings). Now the no-embedding case is a neutral flag, not a penalty.
    no_signal = (sem < 0.55 and not shared and bd == "n/a")
    if no_signal:
        if not direct or len(direct) < 2:
            score -= 1.5
    # NOTE: we used to add 'plot_sim:unavailable' as a reason when sem==0.0
    # (--no-embed mode). It's a valid diagnostic, but in practice it
    # appeared on 90% of candidates and added visual noise to the
    # [SELECT] line. The user already knows they disabled embeddings.
    # Removed in V4.1.1; restore it under a --verbose flag if needed.
    score += candidate["rating"] / 3.0
    diff = abs(candidate["year"] - base["year"])
    score += 1.5 if diff < 5 else (0.8 if diff < 15 else 0)
    return round(score, 2), reasons

# =========================
# OLLAMA SUGGESTIONS
# =========================
# Note: _parse_ollama_titles was deleted (was dead code, duplicated by
# llm_backend.parse_film_titles which all 7 generation helpers now use).

def ollama_suggest_titles(base: dict) -> list:
    if not OLLAMA_OK or LLM is None:
        return []
    cprint(f"  [{LLM.name}] Generating suggestions...", "magenta")
    try:
        titles = LLM.suggest_titles(base, n=args.suggestions,
                                    genre=args.genre, mood=args.mood)
        cprint(f"  [{LLM.name}] {len(titles)} titles extracted", "magenta")
        logger.info(f"{LLM.name}: {len(titles)} titles for '{base['title']}'")
        return titles
    except Exception as e:
        log(f"{LLM.name} error: {e}", "ERROR")
        return []


def ollama_suggest_from_title(film_title: str) -> list:
    """Generate suggestions based on a film title not in the library."""
    if not OLLAMA_OK or LLM is None:
        return []
    cprint(f'  [{LLM.name}] Generating suggestions based on "{film_title}"...', "magenta")
    try:
        titles = LLM.suggest_from_title(film_title, n=args.suggestions,
                                        genre=args.genre, mood=args.mood)
        cprint(f'  [{LLM.name}] {len(titles)} titles extracted', "magenta")
        logger.info(f'{LLM.name} --like: {len(titles)} titles for "{film_title}"')
        return titles
    except Exception as e:
        log(f"{LLM.name} error: {e}", "ERROR")
        return []


def ollama_suggest_from_mood(mood: str) -> list:
    """Generate suggestions purely based on a mood/atmosphere description."""
    if not OLLAMA_OK or LLM is None:
        return []
    # Pour le mode mood on demande plus de suggestions pour compenser le filtrage
    # par blacklist (logique d'origine préservée).
    mood_suggestions = max(args.suggestions, 25)
    cprint(f'  [{LLM.name}] Generating suggestions for mood: "{mood}"...', "magenta")
    # V5.19: tolerate args.genre being None or missing. Without this,
    # some web payloads where the genre field is absent would crash
    # inside the prompt construction with "'Args' object has no
    # attribute 'genre'" and silently return 0 titles.
    mood_genre = getattr(args, "genre", None)
    try:
        titles = LLM.suggest_from_mood(mood, n=mood_suggestions, genre=mood_genre)
        cprint(f'  [{LLM.name}] {len(titles)} titles extracted', "magenta")
        logger.info(f'{LLM.name} --mood: {len(titles)} titles for mood "{mood}"')
        return titles
    except Exception as e:
        log(f"{LLM.name} error: {e}", "ERROR")
        return []


def ollama_get_saga_films(saga_name: str) -> list:
    """Ask the LLM backend for the complete list of films in a saga."""
    if not OLLAMA_OK or LLM is None:
        return []
    cprint(f'  [{LLM.name}] Getting complete film list for saga: "{saga_name}"...', "magenta")
    try:
        titles = LLM.get_saga_films(saga_name)
        cprint(f'  [{LLM.name}] {len(titles)} films found in saga', "magenta")
        return titles
    except Exception as e:
        log(f"{LLM.name} error: {e}", "ERROR")
        return []


def ollama_detect_sagas(radarr_titles: list) -> dict:
    """Ask the LLM backend to identify which films belong to sagas and group them."""
    if not OLLAMA_OK or LLM is None:
        return {}
    cprint(f"  [{LLM.name}] Detecting incomplete sagas in your library...", "magenta")
    try:
        sagas = LLM.detect_sagas(radarr_titles)
        cprint(f'  [{LLM.name}] {len(sagas)} saga(s) detected', "magenta")
        return sagas
    except Exception as e:
        log(f"{LLM.name} error: {e}", "ERROR")
        return {}


def run_saga_mode(radarr_titles: set, radarr_tmdb: set):
    """Handle --saga mode: find and add missing saga films."""
    saga_name = args.saga

    if saga_name == "__auto__":
        # Auto-detection mode
        cprint("-" * 70, "gray")
        title_list = sorted(radarr_titles)
        detected = ollama_detect_sagas(title_list)
        if not detected:
            log("No sagas detected in your library.", "WARNING")
            return
        cprint(f"\n  Sagas found in your library:", "white", bold=True)
        for name, owned in detected.items():
            cprint(f"  - {name} ({len(owned)} film(s) owned)", "cyan")
        print()
        sagas_to_process = list(detected.keys())
    else:
        # Specific saga mode
        cprint("-" * 70, "gray")
        sagas_to_process = [saga_name]

    all_missing = []

    # Filter out hallucinated saga names (too long = likely a comment)
    sagas_to_process = [s for s in sagas_to_process
                        if len(s.split()) <= 8 and len(s) < 60]

    for saga in sagas_to_process:
        cprint(f'\n  Processing: {saga}', "white", bold=True)
        cprint("-" * 50, "gray")

        # Get complete film list for this saga
        saga_films_raw = ollama_get_saga_films(saga)
        if not saga_films_raw:
            log(f'No films found for saga "{saga}"', "WARNING")
            continue

        # Clean and validate each film
        missing = []
        for raw in saga_films_raw:
            # Clean saga title: remove year, comments, and anything after ' - '
            raw_clean = re.sub(r'\s*\(\d{4}\)\s*$', '', str(raw).strip())
            raw_clean = re.sub(r'\s+is\s+not.*$', '', raw_clean, flags=re.IGNORECASE)
            raw_clean = re.sub(r'\s+was\s+.*$', '', raw_clean, flags=re.IGNORECASE)
            raw_clean = re.sub(r'\s*,.*$', '', raw_clean)  # remove trailing comments
            title = _clean_title(raw_clean)
            if not title or len(title) < 3:
                continue
            # Skip if title looks like a sentence/comment
            if len(title.split()) > 12:
                log(f'  Skipped (looks like comment): {title[:50]}', 'DEBUG')
                continue

            # Already owned?
            if title in radarr_titles or title in BLACKLIST:
                log(f'  Already owned: {title}', "INFO")
                continue

            # Validate via OMDb. If the LLM returned a bare title
            # (e.g. "Solo" or "Rogue One" for a Star Wars saga),
            # OMDb may not find it. Try with the saga name prefixed
            # as a fallback. V5.12.
            omdb = get_omdb_full(title)
            if not omdb:
                # Try "Saga: Title" (Star Wars convention)
                omdb = get_omdb_full(f"{saga}: {title}")
            if not omdb:
                log(f'  OMDb not found (saga {saga}): {title}', "WARNING")
                continue
            if omdb["title"] in radarr_titles or omdb["title"] in BLACKLIST:
                log(f'  Already owned: {omdb["title"]}', "INFO")
                continue
            # V5.11: cumulative filters from the advanced panel.
            # Same logic as run_artist_mode: --imdb-min (explicit) > hardcoded
            # default (5.0 for saga, more permissive because franchises vary
            # wildly in quality). --sd/--fd already handled V5.10. --genre
            # is new: at least one overlap with the OMDb genre string.
            if args.imdb_min is not None:
                min_rating_saga = args.imdb_min
            else:
                min_rating_saga = 5.0  # saga default
            if omdb["rating"] == 0.0 or (omdb["rating"] < min_rating_saga and omdb["rating"] > 0):
                log(f'  Filtered (low rating): {omdb["title"]} IMDb:{omdb["rating"]}', "DEBUG")
                continue
            if args.genre:
                wanted = {g.strip().lower() for g in args.genre.split(",") if g.strip()}
                film_genres = {g.strip().lower() for g in omdb.get("genre", "").split(",") if g.strip()}
                if wanted and not (wanted & film_genres):
                    log(f'  Filtered (genre): {omdb["title"]} ({omdb.get("genre", "?")}) not in {args.genre}', "DEBUG")
                    continue

            # Radarr lookup
            lookup = get_radarr_lookup(omdb["title"], omdb["year"])
            if not lookup:
                log(f'  Radarr lookup failed: {omdb["title"]}', "DEBUG")
                continue
            if lookup.get("tmdbId") in radarr_tmdb:
                log(f'  Already in Radarr (tmdbId): {omdb["title"]}', "DEBUG")
                continue

            log(f'  Missing: {omdb["title"]} ({omdb["year"]}) IMDb:{omdb["rating"]:.1f}', "SELECT")
            missing.append({
                "title":     omdb["title"],
                "year":      omdb["year"],
                "rating":    omdb["rating"],
                "score":     omdb["rating"],
                "reasons":   [f"saga:{saga}"],
                "lookup":    lookup,
                "source":    f'saga:{saga}',
                "relaxed":   False,
            })

        if not missing:
            cprint(f'  Your {saga} collection is complete!', "green")
        else:
            cprint(f'  {len(missing)} missing film(s) in {saga}:', "yellow", bold=True)
            for m in missing:
                cprint(f'    - {m["title"]} ({m["year"]}) IMDb:{m["rating"]:.1f}', "cyan")
            all_missing.extend(missing)

    if not all_missing:
        cprint("\n  All your sagas are complete!", "green", bold=True)
        return

    # Build output for Radarr
    output = []
    for m in all_missing:
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
            "lookup":    lk,  # backward-compat for mode 'o' / _build_add_payload (V4.1.1)
        })

    # Save JSON
    json_file = f"reco_{today_str}.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)
    log(f"Results saved -> {json_file}")

    # Add to Radarr
    added = []
    if args.auto:
        log(f"AUTO mode -- adding {len(output)} missing saga films to Radarr")
        for m in output:
            if add_to_radarr(m):
                added.append(m["title"])
                RUN_STATS["added"] += 1
                BLACKLIST.add(m["title"])
    else:
        # Show summary
        print()
        cprint("=" * 90, "white", bold=True)
        cprint(f"  MISSING SAGA FILMS  --  {len(all_missing)} film(s) to add", "white", bold=True)
        cprint("=" * 90, "white", bold=True)
        print()
        for i, m in enumerate(all_missing, 1):
            cprint(f"  {i:2d}.  {m['title']} ({m['year']})  IMDb:{m['rating']:.1f}  [{m['source']}]", "cyan")
        print()
        cprint("Add to Radarr?", "white", bold=True)
        # Vague 4: replaced ~30 lines of duplicated prompt logic with the
        # centralised confirm_and_add() helper.
        added = confirm_and_add(output, missing=all_missing, label="saga")
    if added:
        cprint(f"\n  {len(added)} film(s) added to Radarr!", "green", bold=True)

    save_blacklist(BLACKLIST)
    cprint(f"  Blacklist updated: {len(BLACKLIST)} titles", "gray")
    cprint(f"  Log saved: {log_file}", "gray")


def ollama_get_filmography(person: str, role: str) -> list:
    """Ask the LLM backend for the complete filmography of a person based on their role."""
    if not OLLAMA_OK or LLM is None:
        return []

    # Handle multiple actors (comma-separated) — la couche newmovies construit
    # names_str en amont, le backend reçoit une seule string "X and Y".
    names = [n.strip() for n in person.split(",")]
    is_multi = len(names) > 1
    names_str = " and ".join(names) if is_multi else person

    # Reformulation du prompt pour multi-acteur (le backend a une version simple).
    # On le fait ici pour préserver le comportement d'origine exactement.
    if is_multi and role == "actor":
        names_str_prompt = names_str  # utilisé tel quel par le backend
    else:
        names_str_prompt = person

    top_n = getattr(args, 'artist_top', 0)
    role_labels = {
        "director": "films directed by",
        "actor":    "films featuring",
        "cast":     "films with cast",
        "composer": "films scored by",
        "author":   "adaptations of",
    }
    label = role_labels.get(role, "films for")
    cprint(f'  [{LLM.name}] Getting {label} "{person}"...', "magenta")

    if args.debug:
        cprint(f'  [DEBUG] Calling LLM.get_filmography for {names_str_prompt}...', "gray")
    try:
        titles = LLM.get_filmography(names_str_prompt, role, top_n=top_n)
        cprint(f'  [{LLM.name}] {len(titles)} films found', "magenta")
        return titles
    except Exception as e:
        log(f"{LLM.name} error in filmography: {type(e).__name__}: {e}", "ERROR")
        import traceback
        if args.debug:
            cprint(f"  [DEBUG] Full traceback: {traceback.format_exc()}", "red")
        return []


def run_artist_mode(person: str, role: str, radarr_titles: set, radarr_tmdb: set):
    """Handle --director / --actor / --composer / --author modes."""

    role_labels = {
        "director": "directed by",
        "actor":    "featuring",
        "composer": "scored by",
        "author":   "adapted from",
    }
    label = role_labels.get(role, "by")

    cprint("-" * 70, "gray")
    cprint(f'  Looking for films {label} "{person}"...', "white", bold=True)
    print()

    films_raw = ollama_get_filmography(person, role)
    if not films_raw:
        log(f'No films found for {role}: "{person}"', "WARNING")
        return

    # V5.22: OMDb search fallback. The LLM sometimes drops famous
    # films (e.g. "kevin james" forgets Paul Blart: Mall Cop). When
    # the user sets --omdb-fallback, we also query OMDb's search
    # API for the person and merge the additional titles. Each
    # added title is validated through the same OMDb + Radarr +
    # role-match pipeline below. This complements the LLM
    # without replacing it (the LLM still picks the bulk of
    # titles, OMDb just fills gaps).
    if getattr(args, "omdb_fallback", False):
        cprint(f"  [omdb-fallback] Searching OMDb for: {person} ({role})", "magenta")
        try:
            data = _omdb_request({"s": person, "type": "movie"})
            if data and data.get("Search"):
                omdb_titles = [m.get("Title") for m in data["Search"] if m.get("Title")]
                # Dedupe against LLM output (case-insensitive)
                llm_set = {t.lower().strip() for t in films_raw}
                added = [t for t in omdb_titles if t.lower().strip() not in llm_set]
                if added:
                    cprint(f"  [omdb-fallback] +{len(added)} extra titles from OMDb", "magenta")
                    log(f"omdb-fallback: {len(omdb_titles)} results, "
                        f"{len(added)} new vs LLM", "INFO")
                    films_raw = list(films_raw) + added
                else:
                    cprint(f"  [omdb-fallback] no new titles (LLM already complete)", "magenta")
            elif data and data.get("Error"):
                log(f"omdb-fallback: {data['Error']}", "WARNING")
        except Exception as e:
            log(f"omdb-fallback error: {e}", "WARNING")

    cprint(f"  Validating {len(films_raw)} titles against your library...", "gray")
    print()

    missing  = []
    owned    = []
    seen_titles = set()  # deduplication

    for raw in films_raw:
        # Clean title
        raw_clean = re.sub(r'\s*\(\d{4}\)\s*$', '', str(raw).strip())
        raw_clean = re.sub(r'\s+is\s+not.*$', '', raw_clean, flags=re.IGNORECASE)
        raw_clean = re.sub(r'\s*,.*$', '', raw_clean)
        title = _clean_title(raw_clean)
        if not title or len(title) < 2 or len(title.split()) > 12:
            continue

        # Already owned?
        if title in radarr_titles or title in BLACKLIST:
            log(f'  Already owned: {title}', "INFO")
            owned.append(title)
            continue

        # Validate via OMDb
        omdb = get_omdb_full(title)
        if not omdb:
            log(f'  OMDb not found: {title}', "DEBUG")
            continue
        if omdb["title"] in radarr_titles or omdb["title"] in BLACKLIST:
            log(f'  Already owned: {omdb["title"]}', "INFO")
            owned.append(omdb["title"])
            continue
        # V5.11: cumulative filters from the advanced panel.
        # Priority: --imdb-min (explicit) > role default (5.5 for
        # actor/author, 4.0 for director/composer). --genre filters
        # by overlap with the OMDb genre string. Both only apply
        # when set (None / empty = no filter).
        if args.imdb_min is not None:
            min_rating = args.imdb_min
        else:
            min_rating = 5.5 if role in ("actor", "author") else 4.0
        if omdb["rating"] == 0.0 or (omdb["rating"] < min_rating and omdb["rating"] > 0):
            log(f'  Filtered (low rating): {omdb["title"]} IMDb:{omdb["rating"]}', "DEBUG")
            continue
        if args.genre:
            # args.genre is comma-separated ("Comedy, Sci-Fi"). We
            # require at least one overlap with the OMDb genre string.
            # Case-insensitive on the genre side; OMDb already returns
            # comma-separated.
            wanted = {g.strip().lower() for g in args.genre.split(",") if g.strip()}
            film_genres = {g.strip().lower() for g in omdb.get("genre", "").split(",") if g.strip()}
            if wanted and not (wanted & film_genres):
                log(f'  Filtered (genre): {omdb["title"]} ({omdb.get("genre", "?")}) not in {args.genre}', "DEBUG")
                continue
        # V5.10: year filter from --sd/--fd. Only filters when the
        # user set these (default None means no filter). The artist
        # lookup itself doesn't apply the year, so we post-filter.
        if args.sd is not None and omdb["year"] and omdb["year"] < args.sd:
            log(f'  Filtered (before sd={args.sd}): {omdb["title"]} ({omdb["year"]})', "DEBUG")
            continue
        if args.fd is not None and omdb["year"] and omdb["year"] > args.fd:
            log(f'  Filtered (after fd={args.fd}): {omdb["title"]} ({omdb["year"]})', "DEBUG")
            continue

        # Radarr lookup
        lookup = get_radarr_lookup(omdb["title"], omdb["year"])
        if not lookup:
            log(f'  Radarr lookup failed: {omdb["title"]}', "DEBUG")
            continue
        if lookup.get("tmdbId") in radarr_tmdb:
            log(f'  Already in Radarr (tmdbId): {omdb["title"]}', "INFO")
            owned.append(omdb["title"])
            continue

        # V5.18: verify the OMDb role field actually matches the
        # requested person. The LLM may return a title that exists
        # in OMDb but is NOT by the requested director / does NOT
        # feature the requested actor / etc. Without this check,
        # the user gets 30%+ false positives (e.g. for "robert
        # zemeckis" we returned Cape Fear 1991 by Martin Scorsese,
        # Hugo 2011 by Scorsese, Sleepers 1996 by Barry Levinson,
        # etc.; for "kevin james" we returned Zoolander 2001 by
        # Ben Stiller, Austin Powers 1997 by Jay Roach, etc.).
        # V5.20: role-aware. Check the right OMDb field for each
        # role (director -> Director, actor/cast -> Actors,
        # composer -> unspecified, author -> unspecified).
        if role == "director":
            role_field = "director"
        elif role in ("actor", "cast"):
            role_field = "actors"
        else:
            # composer / author: OMDb doesn't have dedicated
            # fields we can verify against reliably. Skip the
            # role check (preserves previous V5.x behaviour).
            role_field = None
        if role_field is not None:
            omdb_role = (omdb.get(role_field) or "").lower()
            person_lower = person.lower().strip()
            parts = person_lower.split()
            first_last = person_lower
            last_first = f"{parts[-1]}, {parts[0]}" if len(parts) >= 2 else person_lower
            if person_lower not in omdb_role and first_last not in omdb_role \
                    and (len(parts) < 2 or parts[-1] not in omdb_role):
                log(f'  Skipped (wrong {role_field}: {omdb_role!r} != {person!r}): '
                    f'{omdb["title"]} ({omdb["year"]})', "WARNING")
                continue

        if omdb["title"] in seen_titles:
            log(f'  Duplicate skipped: {omdb["title"]}', "DEBUG")
            continue
        seen_titles.add(omdb["title"])
        log(f'  Missing: {omdb["title"]} ({omdb["year"]}) IMDb:{omdb["rating"]:.1f}', "SELECT")
        missing.append({
            "title":   omdb["title"],
            "year":    omdb["year"],
            "rating":  omdb["rating"],
            "score":   omdb["rating"],
            "plot":    omdb.get("plot", ""),
            "reasons": [f"{role}:{person}"],
            "lookup":  lookup,
            "source":  f"{role}:{person}",
            "relaxed": False,
        })

    # Summary
    print()
    cprint(f"  Already in your library: {len(owned)} film(s)", "gray")

    if not missing:
        cprint(f'  Your {person} collection is complete!', "green", bold=True)
        return

    cprint(f"  Missing: {len(missing)} film(s)", "yellow", bold=True)

    # Sort by year
    missing.sort(key=lambda x: x["year"])

    # Build output
    output = []
    for m in missing:
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
            "lookup":    lk,  # backward-compat for mode 'o' / _build_add_payload (V4.1.1)
        })

    # Save JSON
    json_file = f"reco_{today_str}.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)
    log(f"Results saved -> {json_file}")
    if getattr(args, "export", None):
        export_recommendations(output, args.export)

    # Display
    print()
    cprint("=" * 90, "white", bold=True)
    display_role = "CAST TOGETHER" if role == "cast" else role.upper()
    cprint(
        f"  MISSING FILMS ({display_role}: {person})"
        f"  --  {len(missing)} film(s) to add",
        "white", bold=True)
    cprint("=" * 90, "white", bold=True)
    print()
    for i, m in enumerate(missing, 1):
        cprint(f"  {i:2d}.  {m['title']} ({m['year']})  IMDb:{m['rating']:.1f}", "cyan")
    print()

    # Add to Radarr
    added = []
    if args.auto:
        log(f"AUTO mode -- adding {len(output)} films to Radarr")
        for m in output:
            if add_to_radarr(m):
                added.append(m["title"])
                RUN_STATS["added"] += 1
                BLACKLIST.add(m["title"])
    else:
        cprint("Add to Radarr?", "white", bold=True)
        # Vague 4: replaced ~30 lines of duplicated prompt logic.
        added = confirm_and_add(output, missing=missing)
        cprint(f"\n  {len(added)} film(s) added to Radarr!", "green", bold=True)

    save_blacklist(BLACKLIST)
    cprint(f"  Blacklist updated: {len(BLACKLIST)} titles", "gray")
    cprint(f"  Log saved: {log_file}", "gray")


# =========================
# STATS
# =========================
def run_stats(radarr: list):
    """Display collection statistics."""
    w = 70
    print()
    cprint("=" * w, "white", bold=True)
    cprint(f"  COLLECTION STATS          {datetime.now().strftime('%Y-%m-%d  %H:%M')}", "white", bold=True)
    cprint("=" * w, "white", bold=True)
    print()

    total = len(radarr)
    cprint(f"  Total films: {total}", "white", bold=True)
    print()

    # Genres
    genre_count = {}
    for m in radarr:
        for g in m.get("genres", []):
            genre_count[g] = genre_count.get(g, 0) + 1
    top_genres = sorted(genre_count.items(), key=lambda x: x[1], reverse=True)[:8]
    cprint("  Top genres:", "cyan", bold=True)
    for g, c in top_genres:
        bar = "█" * int(c / total * 30)
        pct = int(c / total * 100)
        cprint(f"    {g:<20} {bar:<30} {c} ({pct}%)", "cyan")
    print()

    # Decades
    decade_count = {}
    for m in radarr:
        year = m.get("year", 0)
        if year:
            decade = (year // 10) * 10
            decade_count[decade] = decade_count.get(decade, 0) + 1
    top_decades = sorted(decade_count.items(), key=lambda x: x[1], reverse=True)[:6]
    cprint("  By decade:", "magenta", bold=True)
    for d, c in top_decades:
        bar = "█" * int(c / total * 30)
        pct = int(c / total * 100)
        cprint(f"    {d}s{'':<17} {bar:<30} {c} ({pct}%)", "magenta")
    print()

    # Ratings distribution
    ratings = [m.get("ratings", {}).get("value", 0) for m in radarr
               if m.get("ratings", {}).get("value", 0) > 0]
    if ratings:
        avg = round(sum(ratings) / len(ratings), 1)
        above8 = sum(1 for r in ratings if r >= 8.0)
        above7 = sum(1 for r in ratings if 7.0 <= r < 8.0)
        cprint("  Ratings:", "yellow", bold=True)
        cprint(f"    Average IMDb rating: {avg}", "yellow")
        cprint(f"    8.0+  (masterpieces): {above8} films ({int(above8/total*100)}%)", "yellow")
        cprint(f"    7.0-8.0 (great):      {above7} films ({int(above7/total*100)}%)", "yellow")
    print()

    # Blacklist info
    cprint("  Blacklist:", "gray", bold=True)
    cprint(f"    {len(BLACKLIST)} titles already proposed or owned", "gray")
    print()

    cprint("=" * w, "white", bold=True)
    print()


# =========================
# WATCHLIST IMPORT
# =========================
def run_watchlist(filepath: str, radarr_titles: set, radarr_tmdb: set):
    """Import films from Letterboxd or IMDb CSV watchlist."""
    import csv

    if not os.path.exists(filepath):
        log(f"Watchlist file not found: {filepath}", "ERROR")
        return

    titles = []
    try:
        with open(filepath, encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            log(f"CSV headers: {headers}", "DEBUG")

            for row in reader:
                # Letterboxd format
                if "Name" in row:
                    title = row.get("Name", "").strip()
                    year  = row.get("Year", "").strip()
                    if title:
                        titles.append((title, year))
                # IMDb format
                elif "Title" in row:
                    title = row.get("Title", "").strip()
                    year  = row.get("Year", "").strip()
                    if title:
                        titles.append((title, year))
                # Generic fallback
                elif row:
                    first_val = list(row.values())[0].strip()
                    if first_val:
                        titles.append((first_val, ""))

    except Exception as e:
        log(f"Error reading watchlist: {e}", "ERROR")
        return

    if not titles:
        log("No titles found in watchlist file.", "WARNING")
        return

    cprint(f"  {len(titles)} films found in watchlist", "white", bold=True)
    cprint("-" * 70, "gray")
    print()

    missing  = []
    owned    = []
    seen     = set()

    for title, year in titles:
        if title in radarr_titles or title in BLACKLIST:
            log(f"  Already owned: {title}", "INFO")
            owned.append(title)
            continue

        omdb = get_omdb_full(title, year=int(year) if year.isdigit() else None)
        if not omdb:
            log(f"  OMDb not found: {title}", "DEBUG")
            continue
        if omdb["title"] in radarr_titles or omdb["title"] in BLACKLIST:
            log(f"  Already owned: {omdb['title']}", "INFO")
            owned.append(omdb["title"])
            continue
        if omdb["title"] in seen:
            continue
        seen.add(omdb["title"])

        lookup = get_radarr_lookup(omdb["title"], omdb["year"])
        if not lookup:
            log(f"  Radarr lookup failed: {omdb['title']}", "DEBUG")
            continue
        if lookup.get("tmdbId") in radarr_tmdb:
            log(f"  Already in Radarr: {omdb['title']}", "INFO")
            owned.append(omdb["title"])
            continue

        log(f"  Missing: {omdb['title']} ({omdb['year']}) IMDb:{omdb['rating']:.1f}", "SELECT")
        missing.append({
            "title":   omdb["title"], "year":    omdb["year"],
            "rating":  omdb["rating"], "score":  omdb["rating"],
            "plot":    omdb.get("plot", ""),
            "reasons": ["watchlist"], "lookup":  lookup,
            "source":  f"watchlist:{os.path.basename(filepath)}",
            "relaxed": False,
        })

    print()
    cprint(f"  Already in your library: {len(owned)} film(s)", "gray")
    cprint(f"  Missing: {len(missing)} film(s)", "yellow", bold=True)

    if not missing:
        cprint("  Your watchlist is already complete in Radarr!", "green", bold=True)
        return

    missing.sort(key=lambda x: x["year"])

    output = []
    for m in missing:
        lk = m["lookup"]
        output.append({
            "title": lk["title"], "year": lk.get("year"),
            "rating": m["rating"], "score": m["score"],
            "reasons": m["reasons"], "tmdbId": lk["tmdbId"],
            "titleSlug": lk["titleSlug"], "images": lk.get("images", []),
            "source": m["source"],
            "lookup": lk,  # backward-compat for mode 'o' / _build_add_payload (V4.1.1)
        })

    json_file = f"reco_{today_str}.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)
    log(f"Results saved -> {json_file}")
    if getattr(args, "export", None):
        export_recommendations(output, args.export)

    print()
    cprint("=" * 90, "white", bold=True)
    cprint(f"  WATCHLIST MISSING  --  {len(missing)} film(s) to add", "white", bold=True)
    cprint("=" * 90, "white", bold=True)
    print()
    for i, m in enumerate(missing, 1):
        cprint(f"  {i:2d}.  {m['title']} ({m['year']})  IMDb:{m['rating']:.1f}", "cyan")
    print()

    added = []
    if args.auto:
        for m in output:
            if add_to_radarr(m):
                added.append(m["title"])
                RUN_STATS["added"] += 1
                BLACKLIST.add(m["title"])
    else:
        cprint("Add to Radarr?", "white", bold=True)
        # Vague 4: replaced ~30 lines of duplicated prompt logic.
        added = confirm_and_add(output, missing=missing)

    if added:
        cprint(f"\n  {len(added)} film(s) added to Radarr!", "green", bold=True)

    save_blacklist(BLACKLIST)
    cprint(f"  Blacklist updated: {len(BLACKLIST)} titles", "gray")
    cprint(f"  Log saved: {log_file}", "gray")


# =========================
# ANALYZE
# =========================
def build_collection_profile(radarr: list) -> dict:
    """Build full profile of the collection: stats + unique titles list."""
    from collections import defaultdict
    genre_count   = defaultdict(int)
    decade_count  = defaultdict(int)
    ratings       = []
    seen_titles   = set()
    unique_titles = []
    total         = len(radarr)

    for m in radarr:
        title = m.get("title", "").strip()
        if not title or title in seen_titles:
            continue
        seen_titles.add(title)
        year = m.get("year", 0) or 0
        rating = (m.get("ratings", {}).get("imdb", {}) or {}).get("value", 0) or 0
        if rating:
            ratings.append(rating)
        for g in m.get("genres", []) or []:
            genre_count[g] += 1
        if year:
            decade = (year // 10) * 10
            decade_count[decade] += 1
        # Compact: "title (year, rating)"
        line = (f"  - {title} ({year}, IMDb {rating:.1f})" if rating
                else f"  - {title} ({year})")
        unique_titles.append((title, year, rating, line))

    # Sort alphabetically by title for a stable, scannable list
    unique_titles.sort(key=lambda x: x[0].lower())
    titles_full = "\n".join(t[3] for t in unique_titles)

    top_genres  = sorted(genre_count.items(), key=lambda x: x[1], reverse=True)
    top_decades = sorted(decade_count.items(), key=lambda x: x[1], reverse=True)
    avg_rating  = round(sum(ratings) / len(ratings), 1) if ratings else 0

    return {
        "total":         total,
        "avg_rating":    avg_rating,
        "n_unique":      len(unique_titles),
        "titles":        titles_full,            # the full unique list
        "top_genres":    top_genres,             # [(name, count), ...]
        "top_decades":   top_decades,            # [(decade, count), ...]
    }


def _tmdb_get(path: str, params: dict) -> dict:
    """Single TMDB API call. Returns parsed JSON or {} on error."""
    if not TMDB_API_KEY:
        return {}
    qp = {"api_key": TMDB_API_KEY, "language": "en-US"}
    qp.update(params)
    try:
        r = requests.get(
            f"https://api.themoviedb.org/3{path}",
            params=qp, timeout=15
        )
        if r.status_code != 200:
            log(f"TMDB HTTP {r.status_code} on {path}: {r.text[:200]}", "WARNING")
            return {}
        return r.json()
    except Exception as e:
        log(f"TMDB error on {path}: {e}", "ERROR")
        return {}


def _tmdb_now_playing(region: str) -> list:
    """Films in theaters this week in the given region. Up to 2 pages = ~40 films."""
    out = []
    for page in (1, 2):
        data = _tmdb_get("/movie/now_playing", {"region": region, "page": page})
        out.extend(data.get("results", []))
    return out


def _tmdb_upcoming(region: str) -> list:
    """Films releasing in the next 60 days in the given region. Up to 2 pages."""
    out = []
    for page in (1, 2):
        data = _tmdb_get("/movie/upcoming", {"region": region, "page": page})
        out.extend(data.get("results", []))
    return out


def _tmdb_physical_releases(region: str) -> list:
    """Films with a recent or upcoming physical release (Blu-ray/DVD/4K)
    in the given region, via TMDB /discover with_release_type=5.
    Window: 90 days in the past through 60 days in the future.
    Up to 2 pages = ~40 films.
    """
    from datetime import date, timedelta
    today = date.today()
    window_start = (today - timedelta(days=90)).isoformat()
    window_end   = (today + timedelta(days=60)).isoformat()
    out = []
    for page in (1, 2):
        data = _tmdb_get("/discover/movie", {
            "region":                  region,
            "with_release_type":       "5",  # 5 = Physical (Blu-ray/DVD/4K)
            "primary_release_date.gte": window_start,
            "primary_release_date.lte": window_end,
            "sort_by":                 "popularity.desc",
            "page":                    page,
        })
        out.extend(data.get("results", []))
    return out


def _tmdb_watch_providers(tmdb_id: int, region: str) -> list:
    """Return the list of flatrate/streaming providers for a film in a region."""
    data = _tmdb_get(f"/movie/{tmdb_id}/watch/providers", {})
    if not data:
        return []
    region_data = data.get("results", {}).get(region, {})
    return region_data.get("flatrate", [])


def _tmdb_director(tmdb_id: int) -> str:
    """Return the primary director name for a TMDB film, or ''."""
    data = _tmdb_get(f"/movie/{tmdb_id}/credits", {})
    if not data:
        return ""
    for crew_member in data.get("crew", []):
        if crew_member.get("job") == "Director":
            return crew_member.get("name", "")
    return ""


def run_discovery(radarr_titles: set, radarr_tmdb: set):
    """Discover recent and upcoming films via TMDB, filter against the
    user's Radarr collection, and present the top picks.
    """
    if not TMDB_API_KEY:
        log("TMDB_API_KEY missing in config.yaml. Get a free key at "
            "https://www.themoviedb.org/settings/api", "ERROR")
        return
    region = TMDB_REGION or "FR"
    cprint(f"  [TMDB] Region: {region}", "gray")
    cprint(f"  [TMDB] Fetching now_playing...", "magenta")
    now_playing = _tmdb_now_playing(region)
    cprint(f"  [TMDB] Fetching upcoming (next 60 days)...", "magenta")
    upcoming = _tmdb_upcoming(region)
    physical = []
    if args.blu_ray:
        cprint(f"  [TMDB] Fetching physical releases (Blu-ray/DVD/4K, ±90d)...", "magenta")
        physical = _tmdb_physical_releases(region)
    cprint(f"  [TMDB] {len(now_playing)} in theaters, {len(upcoming)} upcoming, "
           f"{len(physical)} physical", "gray")
    # Combine + dedupe by tmdb id
    seen = set()
    candidates = []
    for film in now_playing + upcoming + physical:
        tid = film.get("id")
        if not tid or tid in seen:
            continue
        seen.add(tid)
        candidates.append(film)
    cprint(f"  [TMDB] {len(candidates)} unique films to evaluate", "gray")
    print()

    retained = []
    seen_titles_lower = set(t.lower() for t in radarr_titles)
    for film in candidates:
        title = (film.get("title") or film.get("original_title") or "").strip()
        if not title or title.lower() in seen_titles_lower:
            continue
        if title.lower() in BLACKLIST:
            continue
        tid = film["id"]
        release_date = film.get("release_date", "")
        year = int(release_date[:4]) if release_date[:4].isdigit() else 0
        if not (args.sd <= year <= args.fd):
            continue
        # Validate via OMDb to get IMDb rating (TMDB vote_average is biased)
        omdb = get_omdb_full(title, year=year if year else None)
        if not omdb:
            continue
        if omdb["title"] in radarr_titles or omdb["title"].lower() in BLACKLIST:
            continue
        if omdb["rating"] < args.score_relax:
            continue
        # Verify not already in Radarr via lookup
        lookup = get_radarr_lookup(omdb["title"], omdb["year"])
        if not lookup or lookup.get("tmdbId") in radarr_tmdb:
            continue
        # Get watch providers + director (skipped silently on failure)
        providers = _tmdb_watch_providers(tid, region)
        provider_names = [p.get("provider_name", "?") for p in providers[:3]]
        director = _tmdb_director(tid)
        # Score: IMDb rating weighted by TMDB popularity
        popularity = film.get("popularity", 0) or 0
        score = round(omdb["rating"] * 1.5 + min(popularity, 100) / 50, 2)
        log(f"  + {omdb['title']} ({omdb['year']})  IMDb:{omdb['rating']:.1f}  "
            f"{', '.join(provider_names) if provider_names else 'no streaming in region'}",
            "SELECT")
        retained.append({
            "title":       omdb["title"],
            "year":        omdb["year"],
            "rating":      omdb["rating"],
            "score":       score,
            "plot":        omdb.get("plot", ""),
            "director":    director,
            "providers":   provider_names,
            "release_date": release_date,
            "tmdb_id":     tid,
            "source":      "discovery",
            "lookup":      lookup,
        })

    if not retained:
        log("No recent/upcoming films match your criteria.", "WARNING")
        return
    # Sort by score desc, take top 10
    retained.sort(key=lambda x: x["score"], reverse=True)
    top = retained[:10]
    print()
    cprint("=" * 90, "white", bold=True)
    cprint(f"  RECENTLY RELEASED & UPCOMING  --  {len(top)} films", "white", bold=True)
    cprint("=" * 90, "white", bold=True)
    print()
    for i, m in enumerate(top, 1):
        providers = f"  [{', '.join(m['providers'])}]" if m['providers'] else ""
        cprint(f"  {i:>2}.  {m['title']} ({m['year']})  IMDb {m['rating']:.1f}  "
               f"score {m['score']:.2f}{providers}", "white")
        if m.get("director"):
            cprint(f"        dir. {m['director']}", "gray")
    print()
    if args.dry_run:
        cprint("  (dry-run: not adding to Radarr)", "WARNING")
        return
    # Offer to add all / one by one / no
    answer = input("\n  Add to Radarr? (a=all / o=one by one / n=no): ").strip().lower()
    if answer == "n" or not answer:
        return
    if answer == "a":
        for m in top:
            lk = m["lookup"]
            payload = _build_add_payload({
                "title": lk["title"], "year": lk.get("year"),
                "rating": m["rating"], "score": m["score"],
                "reasons": ["recent_release"], "tmdbId": lk["tmdbId"],
                "titleSlug": lk["titleSlug"], "images": lk.get("images", []),
                "source": m["source"], "lookup": lk,
            })
            if add_to_radarr(payload):
                log(f"[ADDED] {m['title']} ({m['year']})", "SUCCESS")
    elif answer == "o":
        for m in top:
            lk = m["lookup"]
            yn = input(f"  Add {m['title']} ({m['year']})? [y/N]: ").strip().lower()
            if yn != "y":
                continue
            payload = _build_add_payload({
                "title": lk["title"], "year": lk.get("year"),
                "rating": m["rating"], "score": m["score"],
                "reasons": ["recent_release"], "tmdbId": lk["tmdbId"],
                "titleSlug": lk["titleSlug"], "images": lk.get("images", []),
                "source": m["source"], "lookup": lk,
            })
            if add_to_radarr(payload):
                log(f"[ADDED] {m['title']} ({m['year']})", "SUCCESS")


def ollama_analyze_collection(profile: dict) -> tuple:
    """Ask the LLM backend to analyze the collection and suggest directions."""
    if not OLLAMA_OK or LLM is None:
        return "", []
    cprint(f"  [{LLM.name}] Analyzing your collection...", "magenta")
    try:
        return LLM.analyze_collection(profile)
    except Exception as e:
        log(f"{LLM.name} error: {e}", "ERROR")
        return "", []


def run_analyze(radarr: list, radarr_titles: set, radarr_tmdb: set):
    """Run AI-powered collection analysis."""
    w = 90
    print()
    cprint("=" * w, "white", bold=True)
    cprint(f"  COLLECTION ANALYSIS       {datetime.now().strftime('%Y-%m-%d  %H:%M')}", "white", bold=True)
    cprint("=" * w, "white", bold=True)
    print()

    cprint(f"  Analyzing {len(radarr)} films...", "gray")
    print()

    profile = build_collection_profile(radarr)
    analysis, film_suggestions = ollama_analyze_collection(profile)

    if analysis:
        cprint("-" * w, "gray")
        print()
        # Print analysis word-wrapped at 86 chars
        for para in analysis.split("\n"):
            para = para.strip()
            if not para:
                print()
                continue
            # Word wrap
            words = para.split()
            line  = "  "
            for word in words:
                if len(line) + len(word) + 1 > 88:
                    cprint(line, "white")
                    line = "  " + word + " "
                else:
                    line += word + " "
            if line.strip():
                cprint(line, "white")
        print()
        cprint("-" * w, "gray")
        print()

    if not film_suggestions:
        log("No recommendations generated.", "WARNING")
        return

    cprint(f"  Finding films that match your gaps...", "gray")
    print()

    missing   = []
    seen      = set()
    for raw in film_suggestions:
        title = _clean_title(raw)
        if not title or title in radarr_titles or title in BLACKLIST or title in seen:
            continue
        omdb = get_omdb_full(title)
        if not omdb:
            continue
        if omdb["title"] in radarr_titles or omdb["title"] in BLACKLIST:
            continue
        if omdb["rating"] < args.score_relax:
            continue
        lookup = get_radarr_lookup(omdb["title"], omdb["year"])
        if not lookup or lookup.get("tmdbId") in radarr_tmdb:
            continue
        seen.add(omdb["title"])
        log(f"  + {omdb['title']} ({omdb['year']})  IMDb:{omdb['rating']:.1f}", "SELECT")
        missing.append({
            "title":   omdb["title"], "year":    omdb["year"],
            "rating":  omdb["rating"], "score":  round(omdb["rating"] * 1.5, 2),
            "plot":    omdb.get("plot", ""),
            "reasons": ["analysis_gap"], "lookup": lookup,
            "source":  "analyze", "relaxed": False,
        })

    if not missing:
        log("No new films to add.", "WARNING")
        return

    output = []
    for m in missing:
        lk = m["lookup"]
        output.append({
            "title": lk["title"], "year": lk.get("year"),
            "rating": m["rating"], "score": m["score"],
            "reasons": m["reasons"], "tmdbId": lk["tmdbId"],
            "titleSlug": lk["titleSlug"], "images": lk.get("images", []),
            "source": m["source"],
            "lookup": lk,  # backward-compat for mode 'o' / _build_add_payload (V4.1.1)
        })

    json_file = f"reco_{today_str}.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)
    if getattr(args, "export", None):
        export_recommendations(output, args.export)

    print()
    cprint("=" * w, "white", bold=True)
    cprint(f"  RECOMMENDED TO FILL YOUR GAPS  --  {len(missing)} films", "white", bold=True)
    cprint("=" * w, "white", bold=True)
    print()
    for i, m in enumerate(missing, 1):
        cprint(f"  {i:2d}.  {m['title']} ({m['year']})  IMDb:{m['rating']:.1f}", "cyan")
    print()

    added = []
    if args.auto:
        for m in output:
            if add_to_radarr(m):
                added.append(m["title"])
                RUN_STATS["added"] += 1
                BLACKLIST.add(m["title"])
    else:
        cprint("Add to Radarr?", "white", bold=True)
        # Vague 4: replaced ~30 lines of duplicated prompt logic.
        added = confirm_and_add(output, missing=missing)
    if added:
        cprint(f"\n  {len(added)} film(s) added to Radarr!", "green", bold=True)

    save_blacklist(BLACKLIST)
    cprint(f"  Blacklist updated: {len(BLACKLIST)} titles", "gray")
    cprint(f"  Log saved: {log_file}", "gray")


def _print_synopsis(title: str, plot: str = ""):
    """Print synopsis — auto-fetches from OMDb if plot is empty."""
    if not plot or plot == "N/A":
        for key in [k for k in OMDB_CACHE if k.startswith(f"{title}|")]:
            cached = OMDB_CACHE.get(key)
            if isinstance(cached, dict) and cached.get("plot") and cached["plot"] != "N/A":
                plot = cached["plot"]
                break
        if not plot or plot == "N/A":
            try:
                omdb = get_omdb_full(title)
                if omdb:
                    plot = omdb.get("plot", "")
            except Exception:
                pass
    if not plot or plot == "N/A":
        return
    # Word-wrap the full plot at 80 chars
    words = plot.split()
    line = "    │ "
    lines = []
    for word in words:
        if len(line) + len(word) + 1 > 84:
            lines.append(line)
            line = "      " + word + " "
        else:
            line += word + " "
    if line.strip():
        lines.append(line)
    for l in lines:
        cprint(l, "gray")

def export_recommendations(output: list, filepath: str):
    """Export recommendations to CSV or HTML."""
    import csv as csv_module
    ext = filepath.lower().split(".")[-1]

    if ext == "csv":
        try:
            with open(filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv_module.DictWriter(f,
                    fieldnames=["title", "year", "rating", "score", "source", "reasons"])
                writer.writeheader()
                for m in output:
                    writer.writerow({
                        "title":   m.get("title", ""),
                        "year":    m.get("year", ""),
                        "rating":  m.get("rating", ""),
                        "score":   m.get("score", ""),
                        "source":  m.get("source", ""),
                        "reasons": ", ".join(m.get("reasons", [])),
                    })
            cprint(f"  Exported {len(output)} films to {filepath}", "green")
        except Exception as e:
            log(f"Export error: {e}", "ERROR")

    elif ext in ("html", "htm"):
        try:
            rows = ""
            for i, m in enumerate(output, 1):
                rows += (
                    f"<tr><td>{i}</td><td>{m.get('title','')}</td>"
                    f"<td>{m.get('year','')}</td>"
                    f"<td>{m.get('rating','')}</td>"
                    f"<td>{m.get('score','')}</td>"
                    f"<td>{m.get('source','')}</td>"
                    f"<td>{', '.join(m.get('reasons',[]))}</td></tr>\n"
                )
            html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8">
<title>Radarr Recommendations</title>
<style>
  body {{ font-family: Arial, sans-serif; background: #1a1a2e; color: #eee; padding: 20px; }}
  h1 {{ color: #00d4ff; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th {{ background: #16213e; color: #00d4ff; padding: 10px; text-align: left; }}
  td {{ padding: 8px 10px; border-bottom: 1px solid #2a2a4a; }}
  tr:hover {{ background: #16213e; }}
  .score {{ color: #00d4ff; font-weight: bold; }}
  .rating {{ color: #f5c518; }}
</style>
</head>
<body>
<h1>Radarr Recommendations — {len(output)} films</h1>
<table>
<tr><th>#</th><th>Title</th><th>Year</th><th>IMDb</th><th>Score</th><th>Source</th><th>Reasons</th></tr>
{rows}
</table>
</body></html>"""
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(html)
            cprint(f"  Exported {len(output)} films to {filepath}", "green")
        except Exception as e:
            log(f"Export error: {e}", "ERROR")
    else:
        log(f"Unknown export format: {ext} (use .csv or .html)", "ERROR")

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

    # V5.24: cumulative filters from the advanced panel, mirroring
    # the mood branch (V5.14). Without these, the --like mode
    # silently ignores --imdb-min, --sd, --fd even though the user
    # set them in the web UI. Now all artist-style modes behave
    # consistently: imdb_min overrides the score floor, sd/fd are
    # hard year filters, genre is already handled below.
    if args.imdb_min is not None and omdb["rating"] > 0 and omdb["rating"] < args.imdb_min:
        RUN_STATS["filtered_rating"] += 1
        log(f'  Filtered (imdb-min {args.imdb_min}): {omdb["title"]} IMDb:{omdb["rating"]}', "DEBUG")
        return None
    if args.sd is not None and omdb["year"] and omdb["year"] < args.sd:
        log(f'  Filtered (before sd={args.sd}): {omdb["title"]} ({omdb["year"]})', "DEBUG")
        return None
    if args.fd is not None and omdb["year"] and omdb["year"] > args.fd:
        log(f'  Filtered (after fd={args.fd}): {omdb["title"]} ({omdb["year"]})', "DEBUG")
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
    # Lowered from 4.0 — the previous floor was over-rejecting candidates
    # with no plot_sim (--no-embed or embeddings disabled). With the fixed
    # score_candidate() the no-embedding case no longer double-penalises,
    # so 3.5 keeps the noise out without losing good matches.
    min_sc = 5.5 if relaxed else 3.5
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
    # Was hardcoded to [:4] — disconnected from --suggestions (default 14)
    # and from --top. Capping at the number of suggestions the LLM actually
    # generated lets downstream --top do the final cut.
    return validated[:args.suggestions]

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
# ONBOARD (V5.25)
# =========================

# Genres we propose during onboarding. Classic, well-known
# genres that everyone can relate to. 2 films per genre, so
# 14 films total = a reasonable starter collection.
_ONBOARD_GENRES = [
    "Action", "Comedy", "Drama", "Sci-Fi",
    "Horror", "Romance", "Thriller",
]

# Onboarding questionnaire. 8 questions, mostly closed-form
# (a/b/c) so the LLM gets a structured taste profile.
_ONBOARD_QUESTIONS = [
    ("decade", "Which decade of cinema speaks to you most?",
     ["1970s-1980s (classic)", "1990s (grunge/indie era)", "2000s (modern blockbusters)", "2010s-now (recent streaming era)"]),
    ("pace",    "What pace do you prefer?",
     ["Slow burn / contemplative", "Balanced", "Fast-paced / action-heavy"]),
    ("tone",    "What tone do you enjoy?",
     ["Dark and serious", "Mixed / it depends", "Light and uplifting"]),
    ("realism", "Realistic or fantastical?",
     ["Grounded in reality", "Mix of both", "Escapist / fantastical"]),
    ("origin",  "Hollywood or international?",
     ["Mostly Hollywood", "Mix of both", "Mostly international / foreign"]),
    ("novelty", "Do you like experimental / unconventional films?",
     ["Yes, I love surprises", "Sometimes", "No, I prefer accessible / mainstream"]),
    ("classic", "How do you feel about black-and-white / very old films?",
     ["Love them", "Open to it", "Prefer modern colour"]),
    ("series",  "How do you feel about franchises / series?",
     ["Love them, bring on the sequels", "Mix", "Standalone films only"]),
]


def _ask_onboard_question(idx: int, total: int, label: str, text: str, choices: list) -> str:
    """Print a single onboarding question and read the answer.
    Returns the chosen answer text (the LLM uses it to build the prompt).
    """
    cprint(f"\n[{idx + 1}/{total}] {text}", "white", bold=True)
    for i, choice in enumerate(choices, 1):
        cprint(f"  {i}) {choice}", "cyan")
    while True:
        try:
            raw = input(f"Your choice [1-{len(choices)}] (or 'q' to quit): ").strip()
        except EOFError:
            return choices[0]  # non-interactive: default to first
        if raw.lower() == "q":
            raise KeyboardInterrupt("User quit during onboarding")
        try:
            n = int(raw)
            if 1 <= n <= len(choices):
                return choices[n - 1]
        except ValueError:
            pass
        cprint(f"  Please enter a number between 1 and {len(choices)}", "red")


def run_onboard(radarr_titles: set, radarr_tmdb: set) -> None:
    """V5.25: interactive onboarding for a new/empty Radarr library.

    Asks the user 8 taste questions, builds a structured taste
    profile, and asks the LLM to recommend 2 films per genre
    (14 total) from a fixed genre list. Each film is validated
    via OMDb, and the user is prompted to add or skip each one.
    """
    if not OLLAMA_OK or LLM is None:
        log("LLM unavailable — cannot run onboarding", "ERROR")
        return
    cprint("=" * 70, "white", bold=True)
    cprint("  RADARR ONBOARDING — let's build your starter collection", "white", bold=True)
    cprint("=" * 70, "white", bold=True)
    cprint(
        f"  We'll ask {len(_ONBOARD_QUESTIONS)} quick questions about your taste,\n"
        f"  then recommend 2 films per genre ({len(_ONBOARD_GENRES) * 2} films total)\n"
        f"  to kickstart your Radarr library.\n"
        f"  Press Ctrl+C at any time to quit.", "gray"
    )

    # 1) Ask questions
    answers = []
    for i, (label, text, choices) in enumerate(_ONBOARD_QUESTIONS):
        try:
            ans = _ask_onboard_question(i, len(_ONBOARD_QUESTIONS), label, text, choices)
        except KeyboardInterrupt:
            cprint("\n  Onboarding cancelled.", "yellow")
            return
        answers.append((label, ans))

    # 2) Build the prompt
    profile_lines = [f"- {label}: {ans}" for label, ans in answers]
    profile = "\\n".join(profile_lines)
    genres_str = ", ".join(_ONBOARD_GENRES)
    n_per_genre = 3  # V5.28: 3 films/genre (was 2) for more variety

    # V5.28: free-form question. This is the strongest signal —
    # what the user actually types about films/books/TV they
    # like. We ask for 1-2 minutes of stream-of-consciousness,
    # no editing, just whatever comes to mind. Empty answer is
    # OK (we fall back to the structured QCM profile).
    cprint("\n" + "=" * 70, "white", bold=True)
    cprint("  ONE MORE THING — free-form", "white", bold=True)
    cprint("=" * 70, "white", bold=True)
    cprint(
        "  In a minute or two, without overthinking, tell us about your\n"
        "  favourite films, books and TV series — whatever comes to mind.\n"
        "  This is the strongest signal we have for your taste, so be\n"
        "  honest (Press Enter on an empty line to skip).", "gray"
    )
    print()
    freeform_lines = []
    while True:
        try:
            line = input("  > ")
        except EOFError:
            break
        if not line.strip():
            break
        freeform_lines.append(line)
        # Stop if user pressed Enter twice (empty line)
    freeform = " ".join(freeform_lines).strip()

    if freeform:
        cprint(f"  Got {len(freeform.split())} words — thanks!", "green")
    else:
        cprint("  Skipped (no free-form input).", "yellow")

    cprint("\n" + "=" * 70, "white", bold=True)
    cprint(f"  Generating recommendations ({n_per_genre} per genre, {len(_ONBOARD_GENRES) * n_per_genre} total)...", "white", bold=True)
    cprint("=" * 70, "white", bold=True)

    # 3) Call the LLM
    try:
        titles = LLM.suggest_onboard(profile, genres_str, n_per_genre)
    except AttributeError:
        # Fallback if the backend doesn't implement suggest_onboard.
        # Build a generic prompt and reuse suggest_from_mood.
        # V5.28: explicit instructions — decade is a soft hint,
        # not a constraint; the user may like recent films even
        # if they answered "1990s". And the free-form answer
        # (movies/books/TV they like) is the strongest signal.
        generic = (
            f"Based on this taste profile:\\n{profile}\\n\\n"
            f"User's own free-form mentions (most important signal):\\n"
            f"  {freeform if freeform else '(user skipped this step)'}\\n\\n"
            f"Recommend exactly {n_per_genre} films for each of these genres: {genres_str}.\\n"
            f"Decade preference is a soft hint, NOT a hard filter. If a film fits the taste "
            f"profile but is from a different era, include it anyway.\\n"
            f"Output as JSON: {{\"Action\": [\"Film1\", \"Film2\", \"Film3\"], \"Comedy\": [\"F1\", \"F2\", \"F3\"]}}"
        )
        try:
            raw = LLM.chat(generic, kind="long", max_tokens=2048)
            # V5.27: robust JSON parse. The LLM may return JSON with
            # nested objects (escape sequences, multi-line, comments),
            # so we use a brace-matching algorithm instead of a
            # simple regex. The previous regex `\{[^{}]*\}` failed
            # on any LLM output that included inner braces
            # (e.g. nested objects) or characters between objects.
            import json as _json
            # Find the first { that starts a balanced object
            obj_start = raw.find("{")
            if obj_start == -1:
                log("LLM output has no JSON object", "ERROR")
                return
            depth = 0
            obj_end = -1
            for i, ch in enumerate(raw[obj_start:], start=obj_start):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        obj_end = i + 1
                        break
            if obj_end == -1:
                log("LLM output JSON is unbalanced", "ERROR")
                return
            data = _json.loads(raw[obj_start:obj_end])
            titles = []
            for genre, lst in data.items():
                for t in lst:
                    titles.append({"genre": genre, "title": t})
        except Exception as e:
            log(f"Onboard LLM error: {e}", "ERROR")
            return
    except Exception as e:
        log(f"Onboard LLM error: {e}", "ERROR")
        return

    if not titles:
        log("LLM returned no titles for onboarding", "WARNING")
        return

    cprint(f"  [{LLM.name}] {len(titles)} titles proposed", "magenta")

    # 4) Validate each via OMDb + ask user to add
    cprint("\n" + "=" * 70, "white", bold=True)
    cprint("  REVIEW — choose which films to add to your Radarr", "white", bold=True)
    cprint("=" * 70, "white", bold=True)
    print()
    added = []
    for entry in titles:
        # entry may be a dict {genre, title} or a bare string (fallback)
        if isinstance(entry, dict):
            genre = entry.get("genre", "?")
            title = entry.get("title", "")
        else:
            genre = "?"
            title = str(entry)
        title = _clean_title(title)
        if not title:
            continue
        omdb = get_omdb_full(title)
        if not omdb:
            cprint(f"  [{genre:>10s}] {title:<40s} OMDb not found — skipped", "yellow")
            continue
        if omdb["title"] in radarr_titles or omdb["title"] in BLACKLIST:
            cprint(f"  [{genre:>10s}] {omdb['title']:<40s} already in library — skipped", "gray")
            continue
        # Mini-report + prompt
        cprint(f"  [{genre:>10s}] {omdb['title']} ({omdb['year']})  IMDb:{omdb['rating']:.1f}", "white", bold=True)
        if omdb.get("plot"):
            plot_short = omdb["plot"][:160] + ("..." if len(omdb["plot"]) > 160 else "")
            cprint(f"               {plot_short}", "gray")
        try:
            choice = input("  Add? [y/N/q] ").strip().lower()
        except EOFError:
            choice = "n"
        if choice == "q":
            cprint("  Onboarding stopped.", "yellow")
            break
        if choice == "y":
            lookup = get_radarr_lookup(omdb["title"], omdb["year"])
            if not lookup:
                cprint(f"               Radarr lookup failed — skipping", "yellow")
                continue
            if args.auto or choice == "y" and not args.dry_run:
                if add_to_radarr({
                    # V5.29: flatten the lookup dict. add_to_radarr
                    # expects tmdbId / titleSlug / images at the
                    # root (same shape as the other modes). The
                    # raw `lookup` is the full Radarr movie dict
                    # and we need to spread its fields.
                    **lookup,
                    "title": omdb["title"],
                    "year": omdb["year"],
                    "rating": omdb["rating"],
                    "score": omdb["rating"],
                    "reasons": [f"onboard:{genre}"],
                    "source": f"onboard:{genre}",
                }):
                    added.append(omdb["title"])
                    RUN_STATS["added"] += 1
                    BLACKLIST.add(omdb["title"])
                    cprint(f"               added to Radarr", "green")
                else:
                    cprint(f"               add_to_radarr failed", "red")
            else:
                # Dry-run: just show what would be added
                cprint(f"               [DRY-RUN] would add to Radarr", "cyan")
        print()

    # 5) Wrap up
    save_blacklist(BLACKLIST)
    cprint("=" * 70, "white", bold=True)
    cprint(f"  ONBOARDING DONE — {len(added)} film(s) added to Radarr", "white", bold=True)
    cprint("=" * 70, "white", bold=True)
    for t in added:
        cprint(f"    + {t}", "green")
    cprint(f"  Blacklist updated: {len(BLACKLIST)} titles", "gray")
    cprint(f"  Log saved: {log_file}", "gray")


# =========================
# MAIN
# =========================
def main():
    # Vague 5: --web launches the FastAPI server directly. This is a
    # separate entry point from main(); main() is called per-run from
    # the web runner (in a thread), not from --web.
    if getattr(args, "web", False):
        from radarr_reco.web.server import main as web_main
        # Pass the port + host through env so web server.main() picks
        # them up. Priority: --web-* flag > RADARR_RECO_WEB_* env >
        # config.yaml web: section > default.
        if getattr(args, "web_port", None) is not None:
            import os as _os
            _os.environ["RADARR_RECO_WEB_PORT"] = str(args.web_port)
        if getattr(args, "web_host", None) is not None:
            import os as _os
            _os.environ["RADARR_RECO_WEB_HOST"] = str(args.web_host)
        web_main()
        # web_main() blocks (uvicorn.run). We never reach here unless
        # the server is killed.
        return
    # V5.26: --onboard runs even if Radarr is unreachable.
    # Onboarding is for new installs that may not have a configured
    # library yet. We dispatch here before the Radarr health check
    # so a transient network error doesn't block the user from
    # using this entry point. The Radarr lookup is still done
    # per-film inside run_onboard (skipping already-in-library
    # films), so if Radarr comes back online later, no duplicate
    # adds.
    if args.onboard:
        run_onboard(set(), set())
        return

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
            # V5.17: if no films in the library match the genre, fall
            # back to mood mode. The user clearly wants films of that
            # genre, so the LLM will suggest them. We rebuild args
            # so the mood branch below sees the right state.
            available = sorted({
                g for m in pool
                for g in m.get("genres", [])
                if g
            })
            log(f"No films found in your library for genre: {args.genre}", "WARNING")
            log(f"Available genres in your library: {', '.join(available)}", "INFO")
            log(f"Falling back to mood mode with prompt: 'best {args.genre} films'", "INFO")
            args.mood = f"best {args.genre} films"
            # Don't return — let the mood branch handle it.
            # The pool remains empty but that's OK: the mood branch
            # doesn't use it.
        else:
            log(f"Genre filter '{args.genre}': {len(filtered_pool)} matching films in library", "INFO")
            pool = filtered_pool

    # ── --stats mode ─────────────────────────────────────────────────────
    if args.stats:
        run_stats(radarr)
        return
    # ─────────────────────────────────────────────────────────────────────

    # ── --watchlist mode ─────────────────────────────────────────────────
    if args.watchlist:
        run_watchlist(args.watchlist, radarr_titles, radarr_tmdb)
        return
    # ─────────────────────────────────────────────────────────────────────

    # ── --analyze mode ────────────────────────────────────────────────────
    if args.analyze:
        run_analyze(radarr, radarr_titles, radarr_tmdb)
        return
    # ─────────────────────────────────────────────────────────────────────

    # ── --discovery mode ─────────────────────────────────────────────────
    if args.discovery:
        run_discovery(radarr_titles, radarr_tmdb)
        return
    # ─────────────────────────────────────────────────────────────────────

    # ── --saga mode: complete film sagas/franchises ──────────────────────
    if args.saga:
        run_saga_mode(radarr_titles, radarr_tmdb)
        return
    # ─────────────────────────────────────────────────────────────────────

    # ── artist modes: --director / --actor / --composer / --author ─────────
    artist_mode = None
    if args.director:
        artist_mode = ("director", args.director)
    elif args.actor:
        artist_mode = ("actor", args.actor)
    elif getattr(args, "cast", None):
        artist_mode = ("cast", args.cast)
    elif args.composer:
        artist_mode = ("composer", args.composer)
    elif args.author:
        artist_mode = ("author", args.author)

    if artist_mode:
        role, person = artist_mode
        run_artist_mode(person, role, radarr_titles, radarr_tmdb)
        return
    # ─────────────────────────────────────────────────────────────────────

    # ── --mood mode: generate directly from atmosphere description ────────
    if args.mood and not args.like:
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
        # Parallelise OMDb lookups (Vague 3). The OMDb rate limit (1 req/sec
        # per key, with 1.1s sleep inside _omdb_request) is per-process, so
        # 4 workers don't 4x the speedup, but they do eliminate the serial
        # latency between requests. With 25 mood candidates and ~200ms OMDb
        # response, this cuts ~25*1.1s = 27s to ~7-8s. OMDB_CACHE is a dict,
        # which is GIL-safe for atomic get/set; CURRENT_OMDB_KEY rotation
        # is protected by _omdb_lock.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(get_omdb_full, _clean_title(raw)): raw
                       for raw in mood_titles}
            for fut in as_completed(futures):
                omdb = fut.result()
                if not omdb:
                    continue
                if omdb["title"] in radarr_titles or omdb["title"] in BLACKLIST:
                    continue
                # V5.14: cumulative filters from the advanced panel.
                # --imdb-min (already partially supported, just fixed the
                # broken getattr check). --genre is new: at least one overlap
                # with the OMDb genre string. --sd/--fd is new: post-filter
                # on year range. All are optional (None/empty = no filter).
                if args.imdb_min is not None:
                    min_r = args.imdb_min
                else:
                    min_r = 4.0  # mood default (trust LLM)
                if omdb["rating"] < min_r and omdb["rating"] > 0:
                    continue
                if args.genre:
                    wanted = {g.strip().lower() for g in args.genre.split(",") if g.strip()}
                    film_genres = {g.strip().lower() for g in omdb.get("genre", "").split(",") if g.strip()}
                    if wanted and not (wanted & film_genres):
                        continue
                if args.sd is not None and omdb["year"] and omdb["year"] < args.sd:
                    log(f'  Filtered (before sd={args.sd}): {omdb["title"]} ({omdb["year"]})', "DEBUG")
                    continue
                if args.fd is not None and omdb["year"] and omdb["year"] > args.fd:
                    log(f'  Filtered (after fd={args.fd}): {omdb["title"]} ({omdb["year"]})', "DEBUG")
                    continue
                # For mood mode, skip score-based filtering — Ollama chose these for the mood
                lookup = get_radarr_lookup(omdb["title"], omdb["year"])
                if not lookup or lookup.get("tmdbId") in radarr_tmdb:
                    continue
                RUN_STATS["selected"] += 1
                log(f"  + {omdb['title']} ({omdb['year']})  IMDb:{omdb['rating']:.1f}  genres:{omdb['genre']}", "SELECT")
                validated_mood.append({
                    "title": omdb["title"], "year": omdb["year"],
                    "rating": omdb["rating"], "plot": omdb["plot"],
                    "score": round(omdb["rating"] * 1.5, 2),
                "reasons": [],
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
                "lookup": lk,  # backward-compat for mode 'o' / _build_add_payload (V4.1.1)
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
            # Vague 4: replaced ~25 lines of duplicated prompt logic.
            added = confirm_and_add(output, label="mood")
        if args.auto or added:
            print_report(final_mood, added)
        save_blacklist(BLACKLIST)
        cprint(f"  Blacklist updated: {len(BLACKLIST)} titles", "gray")
        cprint(f"  Log saved: {log_file}", "gray")
        return
    # ─────────────────────────────────────────────────────────────────────

    # ── --like mode: use a specific film as the only source ──────────────
    if args.like:
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
                "lookup": lk,  # backward-compat for mode 'o' / _build_add_payload (V4.1.1)
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
            # Vague 4: replaced ~25 lines of duplicated prompt logic.
            added = confirm_and_add(output, label="like")
        if args.auto or added:
            print_report(final_like, added)
        save_blacklist(BLACKLIST)
        cprint(f"  Blacklist updated: {len(BLACKLIST)} titles", "gray")
        cprint(f"  Log saved: {log_file}", "gray")
        return

    # V5.25: interactive onboarding. Note: the actual --onboard
    # dispatch happens earlier in main() (right after the --web
    # branch), so we can run it even when Radarr is unreachable.
    # This block is kept for documentation but is effectively
    # dead code now — it can only be reached if you somehow set
    # args.onboard AND passed the earlier health check, which
    # would already have run run_onboard.
    # (Removed in V5.26; the earlier dispatch handles everything.)

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
        # NOTE: we keep both the flat fields (used by add_to_radarr in mode 'a')
        # AND the original `lookup` key (used by _build_add_payload in mode 'o'
        # via the missing-candidate reconstruction). V4.1 centralised both prompts
        # but the flat dict has no 'lookup' — preserving it here avoids a
        # KeyError when the user picks 'o' on a library-mode run.
        output.append({
            "title":     lk["title"],    "year":      lk.get("year"),
            "rating":    m["rating"],    "score":     m["score"],
            "reasons":   m["reasons"],   "tmdbId":    lk["tmdbId"],
            "titleSlug": lk["titleSlug"],"images":    lk.get("images", []),
            "source":    m["source"],
            "lookup":    lk,  # backward-compat for mode 'o' / _build_add_payload
        })
    json_file = f"reco_{today_str}.json"
    with open(json_file, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=4, ensure_ascii=False)
    log(f"Results saved -> {json_file}")
    if getattr(args, "export", None):
        export_recommendations(output, args.export)
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
        # Vague 4: replaced ~30 lines of duplicated prompt logic.
        added = confirm_and_add(output, label="library")
    if args.auto or added:
        print_report(final, added)
    save_blacklist(BLACKLIST)
    cprint(f"  Blacklist updated: {len(BLACKLIST)} titles", "gray")
    cprint(f"  Log saved: {log_file}", "gray")
    # Persist OMDb cache for next run (Vague 3). Atomic write: tmp + rename,
    # so a crash mid-write doesn't corrupt the existing file.
    save_omdb_cache()

if __name__ == "__main__":
    main()
