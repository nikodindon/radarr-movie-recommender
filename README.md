# Radarr Movie Recommender

[![GitHub stars](https://img.shields.io/github/stars/nikodindon/radarr-movie-recommender?style=social)](https://github.com/nikodindon/radarr-movie-recommender)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

**Automatic movie recommendations for Radarr, powered by a local LLM.**

Forget genre-based or actor-based suggestions. This tool uses Ollama to understand the **theme**, **tone** and **atmosphere** of your films — then finds genuinely similar ones and adds them to Radarr automatically.

**Recent examples:**

| Source film | Recommendations |
|---|---|
| Whiplash | La La Land, Birdman, All That Jazz, A Star Is Born |
| Django Unchained | The Hateful Eight, True Grit, 12 Years a Slave |
| Mr. Nobody | Being John Malkovich, Amelie, Mulholland Drive |
| The Thing | In the Mouth of Madness, It Follows, The Descent |
| In Bruges | Seven Psychopaths, Dead Man's Shoes, The Proposition |

Everything runs locally — no external AI API. Only a free OMDb key is needed (1000 req/day, free).

---

## Preview

![Run preview](docs/preview.png)

---

## How it works

Each run, the script:

1. Picks random films from your Radarr library as source material
2. Asks a local Ollama LLM to suggest similar films for each one
3. Validates every suggestion against OMDb (rating, year, genre, not already owned)
4. Scores candidates using genre match, director, cast, and semantic plot similarity
5. Adds the top results directly to Radarr, which starts downloading

```
Your Radarr library (10 random films)
           |
           v
  Ollama generates similar titles per film
           |
           v
  OMDb validates: rating, year, genre, not already owned
           |
           v
  Semantic scoring: genre + director + cast + plot embeddings
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
| Suggested by multiple sources | +0.8 per extra source |

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

# Quality profile to use when adding films (Radarr -> Settings -> Profiles)
quality_profile_id: 6

# Minimum availability before Radarr searches for the film
minimum_availability: announced
```

| Setting | Description |
|---|---|
| `omdb_keys` | Comma-separated OMDb API keys. Get free keys at [omdbapi.com](https://www.omdbapi.com/apikey.aspx) |
| `radarr_api_key` | Found in Radarr -> Settings -> General |
| `root_folder` | Your movies folder path |
| `quality_profile_id` | Found in Radarr -> Settings -> Profiles |
| `minimum_availability` | When Radarr starts searching: `announced`, `inCinemas`, or `released` |
| `ollama_model` | Local Ollama model (default: `llama3.1:8b`) |

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

**Genre mode** -- recommendations in a specific genre only:

```bash
python newmovies.py --genre "Sci-Fi"
python newmovies.py --genre "Horror" --auto
python newmovies.py --genre "Comedy,Romance" --sources 5
```

**All options:**

| Argument | Default | Description |
|---|---|---|
| `--auto` | off | Add all recommendations without prompting |
| `--genre` | off | Filter by genre (e.g. `Sci-Fi`, `Horror`, `Comedy,Romance`) |
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

## Daily automation (Windows)

Set it up once and wake up to 10 new movies every morning:

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
- If Ollama returns too few results, the script falls back to OMDb search by director/genre/cast
- The `--no-embed` flag disables semantic embeddings for faster runs
- Works on Windows, Linux and Mac

---

If you find this useful, feel free to leave a star on GitHub -- it helps a lot! :star:
