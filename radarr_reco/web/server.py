"""
radarr_reco.web.server — FastAPI app exposing the recommendation UI.

Run:  python -m radarr_reco.web.server
      or:  python newmovies.py --web [--port 8080]

Binds 127.0.0.1:8080 by default (no auth, local-only).
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import runner

HERE = Path(__file__).parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))

app = FastAPI(title="Radarr Recommender", version="0.1.0")
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


# Drop runs older than 24h on startup
runner.cleanup_old_runs()


# --- Routes ----------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    """Page d'accueil: choix du mode + lancement."""
    return TEMPLATES.TemplateResponse(
        request, "index.html",
        {"runs": runner.list_runs()[-5:]},  # last 5
    )


@app.post("/run")
async def start_run(
    mode: str = Form("library"),
    mood: str = Form(""),
    like_title: str = Form(""),
):
    """Démarre un run en background et redirige vers la page de run."""
    if mode not in ("library", "mood", "like"):
        raise HTTPException(400, f"Unknown mode: {mode}")
    state = runner.new_run(mode=mode)
    params = {"mood": mood, "like": like_title}
    runner.run_in_thread(state, params)
    return JSONResponse({"run_id": state.run_id, "redirect": f"/run/{state.run_id}"})


@app.get("/run/{run_id}", response_class=HTMLResponse)
async def run_page(run_id: str, request: Request):
    """Page d'un run: logs live + cartes films + boutons de décision."""
    state = runner.get_run(run_id)
    if state is None:
        raise HTTPException(404, f"Run {run_id} not found")
    return TEMPLATES.TemplateResponse(
        request, "run.html",
        {"state": state},
    )


@app.get("/run/{run_id}/state")
async def run_state(run_id: str):
    """Endpoint JSON: état complet d'un run (logs + candidates + decisions).
    Utilisé par le JS de polling (1 req/s) pour rafraîchir la page."""
    state = runner.get_run(run_id)
    if state is None:
        raise HTTPException(404, f"Run {run_id} not found")
    return JSONResponse({
        "run_id": state.run_id,
        "status": state.status,
        "logs": state.log_lines[-200:],  # last 200 lines
        "candidates": [
            {
                "title": c.title,
                "year": c.year,
                "rating": c.rating,
                "score": c.score,
                "reasons": c.reasons,
                "source": c.source,
                "decision": state.decisions.get(c.title, ""),
            }
            for c in state.candidates
        ],
        "summary": state.final_summary,
    })


@app.post("/run/{run_id}/decide")
async def decide(
    run_id: str,
    title: str = Form(...),
    decision: str = Form(...),  # 'accept' | 'refuse' | 'blacklist' | 'reset'
):
    """Enregistre la décision de l'utilisateur pour un film."""
    state = runner.get_run(run_id)
    if state is None:
        raise HTTPException(404, f"Run {run_id} not found")
    if decision not in ("accept", "refuse", "blacklist", "reset"):
        raise HTTPException(400, f"Bad decision: {decision}")
    if decision == "reset":
        state.decisions.pop(title, None)
    else:
        state.set_decision(title, decision)
    return JSONResponse({"ok": True, "title": title, "decision": state.decisions.get(title, "")})


@app.post("/run/{run_id}/flush")
async def flush(run_id: str):
    """Push les films acceptés vers Radarr + ajoute les refusés à la blacklist.
    Appelé quand l'user clique 'Tout pousser à Radarr' en bas de la page."""
    state = runner.get_run(run_id)
    if state is None:
        raise HTTPException(404, f"Run {run_id} not found")
    if state.status != "done":
        raise HTTPException(400, "Run not done yet")

    # Lazy import (newmovies is heavy)
    import newmovies  # noqa: PLC0415

    accepted = state.accepted()
    blacklisted = state.blacklisted()

    added = []
    bl_added = []
    # Temporarily disable dry-run so add_to_radarr actually pushes to
    # Radarr. The runner sets args.dry_run=True during the run to avoid
    # auto-adds; we flip it off here for the explicit user action.
    saved_dry_run = getattr(newmovies.args, "dry_run", False)
    newmovies.args.dry_run = False
    try:
        for c in accepted:
            # Use the original add_to_radarr (not the patched one)
            try:
                if newmovies.add_to_radarr(c.lookup or {
                    "title": c.title, "year": c.year,
                    "tmdbId": 0, "titleSlug": c.title.lower().replace(" ", "-"),
                    "images": [],
                }):
                    added.append(c.title)
            except Exception as e:
                state.append_log("warning", f"Failed to add {c.title}: {e}")
    finally:
        newmovies.args.dry_run = saved_dry_run

    for title in blacklisted:
        newmovies.BLACKLIST.add(title)
        bl_added.append(title)

    # Persist blacklist
    try:
        newmovies.save_blacklist(newmovies.BLACKLIST)
    except Exception as e:
        state.append_log("warning", f"Failed to save blacklist: {e}")

    state.status = "flushed"
    state.final_summary["flush_result"] = {
        "added": added, "blacklisted": bl_added,
    }
    return JSONResponse({"added": added, "blacklisted": bl_added})


@app.get("/runs", response_class=HTMLResponse)
async def runs_list(request: Request):
    """Historique des runs récents (en mémoire)."""
    return TEMPLATES.TemplateResponse(
        request, "runs.html",
        {"runs": runner.list_runs()},
    )


# --- Entry point ----------------------------------------------------------

def main():
    """Lance uvicorn.

    Bind configuration resolution order (highest to lowest):
      1. --web-port / --web-host flags (set by newmovies.main() via env)
      2. config.yaml 'web:' section
      3. RADARR_RECO_WEB_PORT / RADARR_RECO_WEB_HOST env vars
      4. Defaults (127.0.0.1:8080)
    """
    import uvicorn
    # Read config.yaml for the 'web' subsection. Use a minimal load so
    # server.main() can be called without newmovies being imported
    # (e.g. `python -m radarr_reco.web.server`).
    web_cfg = _read_web_config()
    # --web-port flag is already pushed into env by newmovies.main()
    # (see 'if args.web:' branch). So env wins unless empty.
    port_str = os.environ.get("RADARR_RECO_WEB_PORT")
    if not port_str:
        port_str = str(web_cfg.get("port", "8080"))
    host = os.environ.get("RADARR_RECO_WEB_HOST")
    if not host:
        host = str(web_cfg.get("host", "127.0.0.1"))
    port = int(port_str)
    print(f"Radarr Recommender UI -> http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")


def _read_web_config() -> dict:
    """Load just the 'web:' subsection of config.yaml.

    Returns {} if config.yaml is missing, invalid, or has no 'web:' key.
    Server config is intentionally NOT in the same _load_config() path
    that newmovies uses for runtime config — the webserver is a
    separate entry point and shouldn't force newmovies to be importable.
    """
    cfg_file = Path(__file__).parent.parent.parent / "config.yaml"
    if not cfg_file.exists():
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    try:
        with open(cfg_file, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:
        return {}
    web = data.get("web", {})
    return web if isinstance(web, dict) else {}


if __name__ == "__main__":
    main()
