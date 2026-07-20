"""
radarr_reco.web.runner — adapter that runs newmovies.main() and exposes
its progress + decisions through a thread-safe in-memory state.

Architecture
------------
We don't refactor newmovies.main() (too risky post-V4.2). Instead we
hijack two side-channels:

  1. The cprint() function. We monkey-patch it for the duration of
     the run so log lines go to a queue (consumed by SSE) AND still
     print to stdout (so a CLI user debugging doesn't get blind).

  2. The confirm_and_add() function. We monkey-patch it so decisions
     go to a per-run state, NOT to Radarr. At the end of the run,
     the user clicks "Flush to Radarr" in the UI which iterates
     the per-run state and pushes the chosen films.

This keeps the existing CLI 100% intact: the original confirm_and_add
is just stashed, replaced during the run, and restored after.

We only run main() in `library` mode for now. Mood/like are separate
flows in the CLI; supporting them here would mean importing more
state from the CLI module. Library is the common case and exercises
the same code path; mood/like are V1.1 of the UI.
"""
import asyncio
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


# --- Run state (one per run, in-memory) -----------------------------------

@dataclass
class Candidate:
    title: str
    year: int | None
    rating: float
    score: float
    reasons: list[str]
    imdb_id: str = ""
    plot: str = ""
    source: str = ""
    lookup: dict = field(default_factory=dict)
    # V5.5: enriched card fields. Only populated when the user
    # ticked the corresponding checkbox in the start-run form.
    # They live on Candidate (not just lookup) so the web UI doesn't
    # need to re-parse the OMDb dict every refresh.
    poster: str = ""
    director: str = ""
    actors: str = ""


@dataclass
class RunState:
    run_id: str
    mode: str  # 'library' for now
    started_at: datetime
    status: str = "running"  # running | done | failed | flushed
    log_lines: list[dict] = field(default_factory=list)  # {ts, level, text}
    candidates: list[Candidate] = field(default_factory=list)
    decisions: dict[str, str] = field(default_factory=dict)
    # decisions: title -> 'accept' | 'refuse' | 'blacklist' | ''
    final_summary: dict = field(default_factory=dict)
    json_path: str = ""  # reco_*.json saved at end
    # V5.5: web UI display toggles. The runner reads these to decide
    # which fields to copy from OMDb into each Candidate.
    ui_options: dict = field(default_factory=lambda: {
        "show_posters": False,
        "show_synopsis": False,
        "show_credits": False,
    })

    def append_log(self, level: str, text: str):
        self.log_lines.append({
            "ts": time.time(),
            "level": level.lower(),
            "text": text,
        })

    def add_candidate(self, c: Candidate):
        self.candidates.append(c)

    def set_decision(self, title: str, decision: str):
        self.decisions[title] = decision

    def accepted(self) -> list[Candidate]:
        return [c for c in self.candidates
                if self.decisions.get(c.title) == "accept"]

    def blacklisted(self) -> list[str]:
        return [c.title for c in self.candidates
                if self.decisions.get(c.title) == "blacklist"]


# --- Registry of runs (in-memory) -----------------------------------------

_RUNS: dict[str, RunState] = {}
_LOCK = threading.Lock()


def new_run(mode: str) -> RunState:
    rid = uuid.uuid4().hex[:12]
    state = RunState(run_id=rid, mode=mode, started_at=datetime.now())
    with _LOCK:
        _RUNS[rid] = state
    return state


def get_run(rid: str) -> RunState | None:
    with _LOCK:
        return _RUNS.get(rid)


def list_runs() -> list[RunState]:
    with _LOCK:
        return list(_RUNS.values())


def cleanup_old_runs(max_age_hours: int = 24):
    """Drop runs older than max_age_hours. Called at server startup."""
    cutoff = datetime.now() - timedelta(hours=max_age_hours)
    with _LOCK:
        old = [rid for rid, r in _RUNS.items() if r.started_at < cutoff]
        for rid in old:
            del _RUNS[rid]
    return len(old)


# --- Runner: wraps newmovies.main() with monkey-patches -------------------

def _level_for_cprint_args(args) -> str:
    """Inspect cprint's first arg to recover the level.
    cprint(text, color, bold=False) — we don't pass a level, so
    we infer: WARNING/FAILED/ERROR -> warning, SUCCESS/added -> success,
    everything else -> info.
    """
    text = str(args[0]) if args else ""
    upper = text.upper()
    if any(w in upper for w in ("ERROR", "FAIL", "MISSING", "WARNING")):
        return "warning"
    if any(w in upper for w in ("SUCCESS", "ADDED", "READY")):
        return "success"
    return "info"


def run_library(start_params: dict, state: RunState) -> bool:
    """Run a library-mode recommendation and populate `state`.
    Returns True on success, False on error.

    start_params (currently unused but kept for future extension):
      - sources: number of source films
      - top: number of recommendations
      - sd: minimum score
      - etc.
    """
    # Lazy import: avoid loading newmovies' heavy module-level work
    # (config, LLM init) until a run actually starts.
    import newmovies  # noqa: PLC0415

    # Stash originals
    orig_cprint = newmovies.cprint
    orig_confirm_and_add = newmovies.confirm_and_add

    captured_candidates: list[Candidate] = []

    def web_cprint(*args, **kwargs):
        level = _level_for_cprint_args(args)
        text = str(args[0]) if args else ""
        state.append_log(level, text)
        # Mirror to stdout so CLI debugging still works
        try:
            orig_cprint(*args, **kwargs)
        except Exception:
            pass

    def web_confirm_and_add(output, missing=None, label="") -> list:
        """Replace the interactive prompt with auto-collect.
        Each candidate is added to state.candidates with no decision.
        The user will click accept/refuse/blacklist in the UI.

        V5.5: if the user ticked one of the display toggles
        (posters/synopsis/credits), we re-fetch OMDb for each film
        to populate the matching fields. The OMDb cache is hit when
        possible, so this is essentially free on warm runs.
        """
        ui = state.ui_options
        want_posters = ui.get("show_posters", False)
        want_synopsis = ui.get("show_synopsis", False)
        want_credits = ui.get("show_credits", False)
        # Only pay the OMDb cost if at least one toggle is on.
        enrich = want_posters or want_synopsis or want_credits
        for m in output:
            poster = ""
            director = ""
            actors = ""
            plot = ""
            if enrich:
                # m["lookup"] is the Radarr lookup result; it doesn't
                # have poster/plot. We need OMDb. The runner's own
                # newmovies module exposes get_omdb_full() with the
                # built-in cache. Falls back to '' on failure (network
                # down, OMDb 404, etc.) so the UI degrades gracefully.
                try:
                    full = newmovies.get_omdb_full(
                        m.get("title", ""), m.get("year"))
                except Exception as e:
                    full = None
                    state.append_log("warning",
                        f"OMDb enrich failed for {m.get('title')!r}: {e}")
                if full:
                    poster = full.get("poster", "") if want_posters else ""
                    director = full.get("director", "") if want_credits else ""
                    actors = full.get("actors", "") if want_credits else ""
                    plot = full.get("plot", "") if want_synopsis else ""
            c = Candidate(
                title=m.get("title", ""),
                year=m.get("year"),
                rating=m.get("rating", 0.0),
                score=m.get("score", 0.0),
                reasons=m.get("reasons", []),
                imdb_id="",  # not in the flat output
                plot=plot,
                source=m.get("source", ""),
                lookup=m.get("lookup", {}),
                poster=poster,
                director=director,
                actors=actors,
            )
            state.add_candidate(c)
            captured_candidates.append(c)
        # No actual adds happen here. Returns empty list (matches CLI
        # semantics for "no decision yet").
        return []

    # Patch
    newmovies.cprint = web_cprint
    newmovies.confirm_and_add = web_confirm_and_add

    # Set args.library (the default mode flag) so newmovies.main()
    # actually runs the library flow. The CLI does this via argparse.
    # We do it manually here.
    if not hasattr(newmovies.args, "auto") or not hasattr(newmovies.args, "saga"):
        # No args object yet (CLI never ran) — use argparse to build a
        # Namespace populated with every default. This way we get all
        # fields argparse defines, including future ones we add.
        # sys.argv[0] is the script name; we pass [] to use defaults.
        newmovies.args = newmovies.parser.parse_args([])
        # Force the bits the web runner needs:
        newmovies.args.dry_run = True   # never actually add during run
        newmovies.args.auto = False     # we want confirm path, candidates get captured
        newmovies.args.yes = True       # skip --resetblacklist confirm
        newmovies.args.web = False      # CRITICAL: don't recursively launch the server
    else:
        # args exists from a CLI invocation — just force the bits we need
        newmovies.args.dry_run = True
        newmovies.args.auto = False
        newmovies.args.yes = True
        newmovies.args.web = False      # CRITICAL: don't recursively launch the server

    state.append_log("info", f"Run started: mode={state.mode} at {state.started_at.isoformat()}")
    state.append_log("info", "Web runner: dry_run=True, auto=False, candidates will be collected for UI")

    try:
        newmovies.main()
        state.status = "done"
        state.append_log("success", f"Run done. {len(state.candidates)} candidates captured.")
    except SystemExit as e:
        # argparse exits with SystemExit on --help. Treat as graceful.
        state.status = "done"
        state.append_log("info", f"SystemExit code={e.code}")
    except Exception as e:
        state.status = "failed"
        state.append_log("warning", f"Run failed: {type(e).__name__}: {e}")
        import traceback
        state.append_log("warning", traceback.format_exc())
        return False
    finally:
        # Restore
        newmovies.cprint = orig_cprint
        newmovies.confirm_and_add = orig_confirm_and_add

    # Capture the saved JSON path
    for line in state.log_lines:
        if "Results saved" in line["text"]:
            # Format: "Results saved -> reco_20260720_1522.json"
            try:
                state.json_path = line["text"].split("->")[-1].strip()
            except Exception:
                pass

    # Final summary
    state.final_summary = {
        "n_candidates": len(state.candidates),
        "n_logs": len(state.log_lines),
        "json_path": state.json_path,
    }
    return True


# --- Sync wrapper to run in a thread (FastAPI is async) -------------------

def run_in_thread(state: RunState, params: dict):
    """Launch a run in a daemon thread. Updates state as it goes."""
    t = threading.Thread(
        target=run_library,
        args=(params, state),
        daemon=True,
    )
    t.start()
    return t
