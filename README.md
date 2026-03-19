@"
# ðŸŽ¬ Radarr Movie Recommender

Automatic daily movie recommendation engine for [Radarr](https://radarr.video/).

Uses your existing Radarr library as a source, generates similar movie suggestions via a local **Ollama** LLM, validates them against **OMDb**, and optionally adds them to Radarr automatically.

## How it works
``````
10 random films from your Radarr library
        â†“
Ollama (llama3.1:8b) generates similar titles
        â†“
OMDb validates each title (rating, year, genre filters)
        â†“
Semantic similarity via Ollama embeddings
        â†“
Top 10 scored candidates â†’ added to Radarr
``````

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com/) running locally with `llama3.1:8b` pulled
- A running [Radarr](https://radarr.video/) instance
- One or more free [OMDb API keys](https://www.omdbapi.com/apikey.aspx)

## Installation

**1. Clone the repo**
``````powershell
git clone https://github.com/nikodindon/radarr-movie-recommender.git
cd radarr-movie-recommender
``````

**2. Install Python dependencies**
``````powershell
pip install -r requirements.txt
``````

**3. Pull the Ollama model**
``````powershell
ollama pull llama3.1:8b
``````

**4. Edit configuration** â€” open ``newmovies.py`` and set these values at the top:

| Variable | Description |
|---|---|
| ``OMDB_KEYS`` | List of your OMDb API keys |
| ``RADARR_API_KEY`` | Found in Radarr â†’ Settings â†’ General |
| ``RADARR_URL`` | Default: ``http://localhost:7878/api/v3`` |
| ``ROOT_FOLDER`` | Your movies folder path, e.g. ``D:\Movies`` |
| ``OLLAMA_MODEL`` | Default: ``llama3.1:8b`` |

## Usage

**Interactive mode** (review and choose which films to add):
``````powershell
python newmovies.py
``````

**Fully automatic mode** (finds and adds the top 10 films without prompting):
``````powershell
python newmovies.py --auto
``````

**All options:**

| Argument | Default | Description |
|---|---|---|
| ``--auto`` | off | Add films automatically without prompting |
| ``--sources`` | 10 | Number of source films to sample from library |
| ``--suggestions`` | 14 | Number of Ollama suggestions per source film |
| ``--top`` | 10 | Number of final recommendations to keep |
| ``--score`` | 6.5 | Minimum IMDb rating |
| ``--score-relax`` | 5.9 | Minimum IMDb rating in relaxed fallback mode |
| ``--sd`` | 1970 | Minimum release year |
| ``--fd`` | 2030 | Maximum release year |
| ``--no-embed`` | off | Disable semantic embeddings (faster, less accurate) |
| ``--debug`` | off | Verbose logging |

## Automation (daily run on Windows)

Open **Task Scheduler** and create a task with:
- **Trigger**: Daily at your preferred time
- **Action**: ``python C:\path\to\radarr-movie-recommender\newmovies.py --auto``
- **Condition**: Start only if network is available

Or with PowerShell directly:
``````powershell
$action = New-ScheduledTaskAction -Execute "python" -Argument "C:\Users\niko_\Documents\radarr-movie-recommender\newmovies.py --auto"
$trigger = New-ScheduledTaskTrigger -Daily -At "03:00"
Register-ScheduledTask -TaskName "RadarrRecommender" -Action $action -Trigger $trigger -RunLevel Highest
``````

## Files created at runtime

| File | Description |
|---|---|
| ``blacklist.json`` | Titles already proposed or in library (never re-proposed) |
| ``omdb_apikey.conf`` | Currently active OMDb key (auto-rotated on quota) |
| ``reco_YYYYMMDD_HHMM.json`` | Full results of each run |
| ``logs/reco_YYYYMMDD_HHMM.log`` | Detailed log of each run |

## How recommendations are scored

Each candidate film receives a score based on:
- **Genre match** with source film (+3 to +5 pts)
- **Same director** (+4 pts)
- **Shared cast** (+2 pts per actor)
- **Semantic plot similarity** via Ollama embeddings (+0 to +6 pts)
- **IMDb rating** (+0 to +3.3 pts)
- **Release year proximity** (+0.8 to +1.5 pts)

Films already in your Radarr library and previously proposed films are automatically excluded via the persistent blacklist.

## Example output
``````
  1. [âœ… ADDED]  In the Mouth of Madness (1994) IMDb:7.1 score:15.19
      â†³ genres:horror, same_director, plot_sim:0.75
      â†³ from: The Thing

  2. [ðŸ“‹ proposed] Mystic River (2003) IMDb:7.9 score:13.11
      â†³ genres:crime,drama,mystery, plot_sim:0.75
      â†³ from: Zodiac
``````

## License

MIT
"@ | Out-File -FilePath README.md -Encoding utf8