# Radarr Movie Recommender

[![GitHub stars](https://img.shields.io/github/stars/nikodindon/radarr-movie-recommender?style=social)](https://github.com/nikodindon/radarr-movie-recommender)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![GitHub last commit](https://img.shields.io/github/last-commit/nikodindon/radarr-movie-recommender)](https://github.com/nikodindon/radarr-movie-recommender/commits/main)

**A local AI companion for Radarr** — finds films you'll actually want to watch, completes your collections, and analyzes your taste. No API key, no cloud, no subscription. Just Ollama running on your machine.

---

## What you can do that no other tool offers

### 🎭 Describe what you're in the mood for — in plain English
```bash
python newmovies.py --mood "dark and intense psychological thriller"
python newmovies.py --mood "samurai and honor in feudal japan"
python newmovies.py --mood "noir detective in a rainy city"
python newmovies.py --mood "feel good sunday afternoon comedy"
python newmovies.py --mood "mind-bending sci-fi with a twist ending"
python newmovies.py --mood "heist with a brilliant plan" --imdb-min 7.5
```

### 🎬 Start from any film — even one you don't own
```bash
python newmovies.py --like "Parasite"
python newmovies.py --like "2001: A Space Odyssey" --mood "existential and slow burn"
python newmovies.py --like "Inception" --mood "mind-bending"
```

### 🎞️ Complete an entire franchise automatically
```bash
python newmovies.py --saga "Star Wars"          # finds every missing episode
python newmovies.py --saga "Planet of the Apes" # original + reboot series
python newmovies.py --saga                      # auto-detects ALL incomplete sagas
```

### 🎥 Explore complete filmographies
```bash
python newmovies.py --director "Stanley Kubrick"
python newmovies.py --actor "Al Pacino"
python newmovies.py --composer "Ennio Morricone" --export morricone.html
python newmovies.py --author "Cormac McCarthy"   # all film adaptations
```

### 👥 Multi-actor search — unique to this tool
```bash
# Films featuring ANY of these actors
python newmovies.py --actor "Ben Stiller, Owen Wilson"

# Films where ALL of them appear TOGETHER
python newmovies.py --cast "Ben Stiller, Owen Wilson"
# → Zoolander, Starsky & Hutch, Night at the Museum...

python newmovies.py --cast "Robert De Niro, Al Pacino"
# → The Godfather Part II, Heat, Righteous Kill
```

### 🧠 AI analysis of your collection
```bash
python newmovies.py --analyze
# → "Your collection excels at 90s drama but lacks Kurosawa, French New Wave..."
# → Suggests 10 films to fill the gaps

python newmovies.py --stats
# → Genre breakdown, decade distribution, average rating

python newmovies.py --watchlist letterboxd_watchlist.csv
# → Import directly from your Letterboxd or IMDb watchlist
```

### 📋 Review with full plot synopsis
```bash
python newmovies.py --actor "Al Pacino" --synopsis
# Shows full plot before you decide to add each film:
#   + Serpico (1973)  IMDb:7.7
#   │ An honest New York cop named Frank Serpico blows the whistle
#     on rampant corruption in the police department.
#   add? (y/n):
```

### 📤 Export to HTML or CSV
```bash
python newmovies.py --actor "Ennio Morricone" --export morricone.html
python newmovies.py --analyze --export gaps.csv
# → Beautiful dark-themed HTML report you can share
```

---

## Actual results

| Command | What it found |
|---|---|
| `--mood "dark and intense"` | No Country for Old Men, Martyrs, Let the Right One In |
| `--mood "samurai feudal japan"` | Seven Samurai, Rashomon, Yojimbo, Throne of Blood, Samurai Rebellion |
| `--mood "noir detective rainy city"` | Se7en, Double Indemnity, Chinatown, Maltese Falcon, Big Sleep |
| `--like "2001: A Space Odyssey"` | Solaris, Stalker, Silent Running, Moon, Arrival |
| `--saga "Star Wars"` | All 10 missing episodes + Rogue One + Solo |
| `--saga "Planet of the Apes"` | 6 missing films across original + reboot series |
| `--director "Stanley Kubrick"` | Full filmography, only missing titles |
| `--actor "Al Pacino"` | Serpico, Godfather I & II, Dog Day Afternoon, Scarface... |
| `--composer "Ennio Morricone"` | 19 missing films incl. GBU, Once Upon a Time in America |
| `--cast "De Niro, Pacino"` | Exactly 3 films where both appear: Godfather II, Heat, Righteous Kill |
| `--analyze` | Detected gaps in Kurosawa, Bergman, French New Wave → 10 suggestions |
| `--watchlist` | Imports Letterboxd/IMDb CSV directly into Radarr |

---

## How it works

```
Your Radarr library
        │
        ▼
  Ollama (local LLM) understands theme, tone, atmosphere
        │
        ▼
  OMDb validates each suggestion
  (rating, year, genre, not already owned)
        │
        ▼
  Scoring: genre + director + cast + plot embeddings
        │
        ▼
  Results added to Radarr — with your approval
```

---

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com/) running locally
- A running [Radarr](https://radarr.video/) instance
- A free [OMDb API key](https://www.omdbapi.com/apikey.aspx) (1000 req/day)

---

## Installation

```bash
git clone https://github.com/nikodindon/radarr-movie-recommender.git
cd radarr-movie-recommender
pip install -r requirements.txt
ollama pull llama3.1:8b
cp config.yaml.example config.yaml   # edit with your settings
```

**config.yaml:**
```yaml
omdb_keys: your_key1,your_key2
radarr_api_key: your_radarr_api_key
radarr_url: http://localhost:7878/api/v3
root_folder: "D:\\Movies"
ollama_model: llama3.1:8b
quality_profile_id: 6
minimum_availability: announced
```

---

## All commands

```bash
# Classic — based on your library
python newmovies.py
python newmovies.py --auto           # add everything without prompting
python newmovies.py --genre "Horror"

# Mood & discovery
python newmovies.py --mood "dark and intense"
python newmovies.py --mood "heist" --imdb-min 7.5
python newmovies.py --like "Parasite"
python newmovies.py --like "Inception" --mood "mind-bending"

# Sagas & franchises
python newmovies.py --saga "Star Wars"
python newmovies.py --saga            # auto-detect all incomplete sagas

# Filmographies
python newmovies.py --director "Stanley Kubrick"
python newmovies.py --actor "Al Pacino"
python newmovies.py --actor "Ben Stiller, Owen Wilson"     # any of them
python newmovies.py --cast "Ben Stiller, Owen Wilson"      # together only
python newmovies.py --cast "Robert De Niro, Al Pacino"
python newmovies.py --composer "Hans Zimmer"
python newmovies.py --author "Stephen King"
python newmovies.py --actor "Al Pacino" --artist-top 20   # top 20 only

# Collection intelligence
python newmovies.py --stats
python newmovies.py --analyze
python newmovies.py --analyze --no-timeout    # with large model
python newmovies.py --watchlist watchlist.csv

# Output options
python newmovies.py --actor "Pacino" --synopsis            # show plot before adding
python newmovies.py --actor "Pacino" --export pacino.html  # export to HTML
python newmovies.py --analyze --export gaps.csv            # export to CSV

# Tuning
python newmovies.py --imdb-min 7.5
python newmovies.py --sources 15 --suggestions 20 --top 15
python newmovies.py --sd 1960 --fd 1990                    # era filter

# Reset
python newmovies.py --resetblacklist
```

---

## All options

| Argument | Default | Description |
|---|---|---|
| `--auto` | off | Add all recommendations without prompting |
| `--mood` | off | Describe the atmosphere in plain language |
| `--like` | off | Base recommendations on any film title |
| `--genre` | off | Filter by genre (`Sci-Fi`, `Horror`, `Comedy`...) |
| `--saga` | off | Complete a franchise — specify name or use alone for auto-detection |
| `--director` | off | Missing films by a director |
| `--actor` | off | Missing films by actor(s) — comma-separated for multiple |
| `--cast` | off | Missing films where ALL listed actors appear together |
| `--composer` | off | Missing films scored by a composer |
| `--author` | off | Missing film adaptations of an author |
| `--artist-top` | 0 | Limit filmography results (0 = all) |
| `--analyze` | off | AI analysis of your collection + gap-filling recommendations |
| `--stats` | off | Collection statistics: genres, decades, ratings |
| `--watchlist` | off | Import from Letterboxd or IMDb CSV export |
| `--synopsis` | off | Show full plot synopsis when reviewing films one by one |
| `--imdb-min` | off | Minimum IMDb rating override (e.g. `--imdb-min 7.5`) |
| `--export` | off | Export to CSV or HTML (e.g. `--export reco.html`) |
| `--no-timeout` | off | Disable timeouts for large models |
| `--sources` | 10 | Source films sampled from your library |
| `--suggestions` | 14 | Ollama suggestions per source |
| `--top` | 10 | Final recommendations to keep |
| `--score` | 6.5 | Minimum IMDb rating (classic mode) |
| `--score-relax` | 5.9 | IMDb threshold in relaxed fallback |
| `--sd` | 1970 | Minimum release year |
| `--fd` | 2030 | Maximum release year |
| `--no-embed` | off | Disable plot embeddings (faster) |
| `--resetblacklist` | off | Clear the blacklist |
| `--debug` | off | Verbose output |

---

## Recommended models

| Model | Size | Best for |
|---|---|---|
| `llama3.1:8b` | 4.9 GB | Daily use, fast, good quality |
| `mistral:7b` | 4.4 GB | Best balance speed/quality ⭐ |
| `mistral-small:22b` | 12 GB | Filmographies, best accuracy 🏆 |
| `llama3.2:3b` | 2.0 GB | Very fast, limited RAM |

```bash
ollama pull mistral:7b
# then in config.yaml:
# ollama_model: mistral:7b
```

Use `--no-timeout` with 22b+ models:
```bash
python newmovies.py --analyze --no-timeout
```

---

## Daily automation

**Windows:**
```powershell
$action = New-ScheduledTaskAction -Execute "python" -Argument "C:\path\to\newmovies.py --auto"
$trigger = New-ScheduledTaskTrigger -Daily -At "03:00"
Register-ScheduledTask -TaskName "RadarrRecommender" -Action $action -Trigger $trigger
```

**Linux/Mac:**
```bash
0 3 * * * cd /path/to/radarr-movie-recommender && python newmovies.py --auto
```

---

## Update

```bash
git pull
```

`config.yaml`, `blacklist.json` and logs are never overwritten.

---

If this is useful, a ⭐ on GitHub is always appreciated!
