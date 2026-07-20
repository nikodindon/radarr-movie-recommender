"""
radarr_reco.web — FastAPI server exposing the recommendation engine
through a browser-friendly UI.

Vague 5 (web UI). Server is started via `python newmovies.py --web`
and binds to 127.0.0.1:8080 by default. No auth (LAN exposure is
out of scope for V1).

Public surface:
  - server.py : FastAPI app + routes
  - runner.py : adapter that wraps newmovies.main() in a streaming
                event stream we can pipe to the browser via SSE
  - templates/ : Jinja2 templates (base, index, run)
  - static/    : CSS + HTMX 1.9
"""
