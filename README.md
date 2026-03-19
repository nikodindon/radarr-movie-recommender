# Radarr Movie Recommender

A Python CLI script that automatically finds movies similar to what you already have in [Radarr](https://radarr.video/), and adds the best ones directly to your library.

Uses a local LLM ([Ollama](https://ollama.com/)) for suggestions, [OMDb](https://www.omdbapi.com/) for validation, and semantic plot embeddings for thematic similarity scoring.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

---

## Preview

![Run preview](docs/preview.png)

---

## What it does

Each run, the script:

1. Picks 10 random films from your Radarr library as source material
2. Asks a local LLM to suggest similar films for each one
3. Validates every suggestion against OMDb (rating, year, genre, not already in your library)
4. Scores candidates using genre match, director, cast, and semantic plot similarity via embeddings
5. Adds the top 10 results directly to Radarr, which starts downloading them

Everything runs locally. No external AI API, no Docker, no web UI — just a single Python script.

---

## How it works

```
Your Radarr library (10 random films)
           |
           v
  Ollama generates 14 similar titles per film
           |
           v
  OMDb validates: rating, year, genre, not already owned
           |
           v
  Semantic scoring via plot embeddings + genre + director + cast
           |
           v
  Top 10 candidates added to Radarr automatically
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

**2. Install dependencies**

```bash
pip install -r requirements.txt
```

**3. Pull the Ollama model**

```bash
ollama pull llama3.1:8b
```

**4. Configure**

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
> `config.yaml` is gitignored and never committed.

---

## Usage

**Interactive mode** -- review and choose which films to add:

```bash
python newmovies.py
```

**Automatic mode** -- finds and adds the top 10 with no prompts:

```bash
python newmovies.py --auto
```

**All options:**

| Argument | Default | Description |
|---|---|---|
| `--auto` | off | Add all recommendations without prompting |
| `--sources` | 10 | Number of random source films to sample |
| `--suggestions` | 14 | Ollama suggestions per source film |
| `--top` | 10 | Final recommendations to keep |
| `--score` | 6.5 | Minimum IMDb rating |
| `--score-relax` | 5.9 | IMDb threshold in relaxed fallback mode |
| `--sd` | 1970 | Minimum release year |
| `--fd` | 2030 | Maximum release year |
| `--no-embed` | off | Disable plot embeddings (faster, less precise) |
| `--genre` | off | Filter by genre (e.g. `Sci-Fi`, `Horror`, `Comedy,Romance`) |
| `--debug` | off | Verbose output |

---

## Daily automation (Windows)

Set it up once with PowerShell and wake up to 10 new movies every morning:

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
| `blacklist.json` | All films already proposed, added, or in your library -- never re-proposed |
| `omdb_apikey.conf` | Currently active OMDb key (auto-rotated on quota) |
| `reco_YYYYMMDD_HHMM.json` | Full scored results of each run |
| `logs/reco_YYYYMMDD_HHMM.log` | Detailed log with all decisions |

---

## Notes

- The blacklist grows over time -- films already in your library or previously proposed are never suggested again
- If Ollama returns too few results for a source film, the script falls back to OMDb search by director/genre/cast
- The `--no-embed` flag disables semantic embeddings if you want faster runs at the cost of some precision
- Works on Windows, Linux and Mac
