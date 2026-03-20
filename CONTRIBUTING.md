# Contributing

Thanks for your interest in Radarr Movie Recommender!

Pull requests are very welcome. Here are the areas where contributions would have the most impact:

- **Better Ollama prompts** -- reduce hallucinations, improve relevance
- **New filters** -- avoid sequels, filter by decade, exclude already-seen films
- **Scoring improvements** -- better weighting, new signals
- **Integrations** -- Plex/Emby/Jellyfin watch history as source
- **Export formats** -- CSV, Letterboxd, Notion
- **Linux/Mac testing** -- the core logic is cross-platform but needs more testing

## How to contribute

1. Open an issue first to discuss what you want to change
2. Fork the repo and create a branch
3. Make your changes and test them
4. Open a pull request with a clear description

## Setup for development

```bash
git clone https://github.com/nikodindon/radarr-movie-recommender.git
cd radarr-movie-recommender
pip install -r requirements.txt
cp config.yaml.example config.yaml
# Fill in your config.yaml
python newmovies.py --sources 2 --suggestions 5 --no-embed --debug
```

## Questions?

Open an issue -- happy to help.
