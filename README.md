# Radarr Movie Recommender

A Python CLI script that uses a local LLM ([Ollama](https://ollama.com/)) to suggest movies based on your existing [Radarr](https://radarr.video/) library, then adds the best ones automatically.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

---

## What it does

I built this as a personal project to discover films I might enjoy, based on what I already have in my library. It picks 10 random films from Radarr, asks a local LLM for similar suggestions, validates them against OMDb, scores them by thematic similarity, and optionally adds the top results to Radarr automatically.

The main components:

- A **local LLM** (Ollama) generates suggestions -- no external AI API needed
- **OMDb** validates each suggestion (rating, year, genre)
- **Semantic plot embeddings** score thematic similarity
- Everything integrates directly with **Radarr** via its API
- Runs as a **single Python script** with no server or database required

---

## How it works

```
Your Radarr library (10 random films)
           |
           v
  Ollama generates 14 similar titles per film
           |
           v
  OMDb validates: rating >= 6.5, year, genre, not junk
           |
           v
  Semantic scoring: genre + director + cast + plot embeddings
           |
           v
  Top 10 candidates -> added to Radarr automatically
```

**Scoring breakdown:**

| Signal | Points |
|---|---|
| Genre match | +3 to +5 |
| Same director | +4 |
| Shared cast | +2 per actor |
| Plot similarity (embeddings) | 0 to +6 |
| IMDb rating | 0 to +3.3 |
| Era proximity | +0.8 to +1.5 |

---

## Example output

```
==========================================================================================
  REPORT -- 2026-03-19 14:36 -- 10 recommendations
==========================================================================================
   1. [ADDED]    The Witch (2015) IMDb:7.0 score:16.8
       from: The Lighthouse | genres:horror,drama, same_director, plot_sim:0.74

   2. [proposed] Working Girl (1988) IMDb:6.8 score:13.07
       from: Pretty Woman | genres:comedy,romance, plot_sim:0.88

   3. [proposed] Mystic River (2003) IMDb:7.9 score:13.11
       from: Zodiac | genres:crime,drama,mystery, plot_sim:0.75

  STATS  |  sources: 10  |  suggestions: 132  |  tested: 47
         |  rejected: 31 (rating:12 genre:8 bl:6 score:5)
         |  selected: 16  |  added: 1
==========================================================================================
```

---

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com/) running locally with `llama3.1:8b` pulled
- A running [Radarr](https://radarr.video/) instance
- One or more free [OMDb API keys](https://www.omdbapi.com/apikey.aspx) (1000 req/day each, free)

---

## Installation

**1. Clone the repo**

```bash
git clone https://github.com/nikodindon/radarr-movie-recommender.git
cd radarr-movie-recommender
```

**2. Install Python dependencies**

```bash
pip install -r requirements.txt
```

**3. Pull the Ollama model**

```bash
ollama pull llama3.1:8b
```

**4. Configure**

Copy the example config and fill in your values:

```bash
copy config.yaml.example config.yaml   # Windows
cp config.yaml.example config.yaml     # Linux/Mac
```

Edit `config.yaml`:

```yaml
omdb_keys: your_key1,your_key2
radarr_api_key: your_radarr_api_key
radarr_url: http://localhost:7878/api/v3
root_folder: "D:\\Movies"
ollama_model: llama3.1:8b
```

> Get free OMDb keys at [omdbapi.com/apikey.aspx](https://www.omdbapi.com/apikey.aspx).
> Find your Radarr API key in Radarr -> Settings -> General.
> The `config.yaml` file is gitignored and never committed.

---

## Usage

**Interactive mode** — review and pick which films to add:

```bash
python newmovies.py
```

**Automatic mode** — finds and adds the top 10 films with no prompts:

```bash
python newmovies.py --auto
```

**All options:**

| Argument | Default | Description |
|---|---|---|
| `--auto` | off | Add all recommendations automatically |
| `--sources` | 10 | Number of random source films to sample |
| `--suggestions` | 14 | Ollama suggestions per source film |
| `--top` | 10 | Final recommendations to keep |
| `--score` | 6.5 | Minimum IMDb rating |
| `--score-relax` | 5.9 | IMDb threshold in relaxed fallback mode |
| `--sd` | 1970 | Minimum release year |
| `--fd` | 2030 | Maximum release year |
| `--no-embed` | off | Disable plot embeddings (faster, less precise) |
| `--debug` | off | Verbose output |

---

## Daily automation (Windows Task Scheduler)

Run automatically every day with PowerShell:

```powershell
$action = New-ScheduledTaskAction `
    -Execute "python" `
    -Argument "C:\path\to\radarr-movie-recommender\newmovies.py --auto"
$trigger = New-ScheduledTaskTrigger -Daily -At "03:00"
Register-ScheduledTask -TaskName "RadarrRecommender" `
    -Action $action -Trigger $trigger -RunLevel Highest
```

---

## Files created at runtime

| File | Description |
|---|---|
| `blacklist.json` | All films already proposed, added, or in your library. Never re-proposed. |
| `omdb_apikey.conf` | Currently active OMDb key (auto-rotated when quota is hit) |
| `reco_YYYYMMDD_HHMM.json` | Full scored results of each run |
| `logs/reco_YYYYMMDD_HHMM.log` | Detailed log with all decisions |

---

## How the blacklist works

Every film that passes through the system is added to `blacklist.json`:

- Films **already in your Radarr library** are added on startup
- Films **proposed and added** are blacklisted after the run
- Films **manually declined** (in interactive mode) are also blacklisted

This means each daily run discovers genuinely new films.


