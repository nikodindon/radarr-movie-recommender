"""
llm_backend.py — Couche d'abstraction LLM pour newmovies.py

Remplace les appels directs à Ollama (CLI `ollama run` + endpoint
`/api/embeddings`) par un backend interchangeable. Un seul backend fourni
depuis la Vague 2 (l'OllamaBackend legacy a été supprimé) :

  - LlamaCppBackend  : utilise l'API OpenAI-compat de llama-server
                       (/v1/chat/completions et /v1/embeddings).
                       C'est ce qu'on utilise sur 192.168.1.32:8080.

Le choix se fait via config.yaml :
    llm_backend: llamacpp

⚠️  Caveats llama.cpp connus (juillet 2026) :
   1. /v1/embeddings ne marche que si llama-server a été lancé avec --embeddings.
      Sinon on tombe en fallback gracieux (embeddings désactivés, --no-embed force).
   2. Le warmup d'un gros modèle 35B prend ~20-30s. Le backend gère ça tout seul
      (un test ping à l'init, retries sur timeout).
   3. Les prompts "Respond ONLY with JSON" ne sont pas 100% suivis par tous les
      modèles. Le parser _parse_titles est volontairement tolérant (3 stratégies
      de fallback comme l'original).
"""

import json
import os
import re
import subprocess
import time
from abc import ABC, abstractmethod
from typing import Optional

import requests


# =============================================================================
# Parseur de sortie (repris de newmovies.py, factorisé)
# =============================================================================

def parse_film_titles(raw: str) -> list:
    """Extrait une liste de titres depuis une sortie LLM. Cinq stratégies,
       appliquées dans l'ordre jusqu'à ce qu'une retourne un résultat
       non-vide :

       1) Dernier bloc JSON {"films": [...]} complet dans la réponse.
          (V7.3: les modèles qui raisonnent en visible — gemma-4-26B-A4B
          par exemple — émettent plusieurs blocs JSON, le dernier
          contenant souvent la version "nettoyée après self-correction".
          On prend le DERNIER, pas le premier.)
       2) Premier sous-ensemble {"films": [...]} — fallback si pas de
          bloc complet (raison : max_tokens a tronqué la réponse avant
          l'accolade fermante).
       3) Format objet {"films": [{"title":..., "year":...}, ...]} — V6.1
          saga style. Le regex matche {"films": [ (n'importe quoi sauf
          ]) ] } en mode non-greedy, plusieurs fois, on prend le DERNIER.
       4) Extraction d'un tableau même si le JSON est cassé autour.
       5) Lignes markdown ("- Title" / "1. Title") en dernier recours.
    """
    if not raw:
        return []

    def _clean(t: str) -> str:
        # Strip surrounding quotes / whitespace, drop "(YYYY)" suffix
        # (the model sometimes includes years in the array form).
        t = t.strip().strip("\"'")
        t = re.sub(r"\s*\(\d{4}\)\s*$", "", t)
        return t

    def _dedupe(seq):
        # Preserve order, drop empties and 1-2 char noise tokens.
        seen = set()
        out = []
        for x in seq:
            x = x.strip()
            if not x or len(x) < 2:
                continue
            k = x.lower()
            if k in seen:
                continue
            seen.add(k)
            out.append(x)
        return out

    # 1) Last complete JSON block {"films": [...]} of strings.
    #    Strategy 1's old regex was {no-braces}[no-braces] which works
    #    for string arrays. We re-use it but find ALL matches and
    #    take the LAST (handles gemma-4 reasoning pattern).
    last_good = None
    for m in re.finditer(
        r'\{\s*"films"\s*:\s*\[\s*(?:"[^"]*"\s*,?\s*)+\s*\]\s*\}',
        raw, re.DOTALL,
    ):
        try:
            parsed = [t for t in json.loads(m.group(0)).get("films", []) if t]
            if parsed:
                last_good = [_clean(t) for t in parsed]
        except Exception:
            continue
    if last_good:
        return _dedupe(last_good)

    # 2) Object-array form: {"films": [{"title":..., "year":...}, ...]}.
    #    Use a non-greedy match that allows nested braces. We try each
    #    occurrence and take the longest one (gemma-4's "Final List"
    #    block is usually the longest).
    candidates = []
    for m in re.finditer(
        r'\{\s*"films"\s*:\s*\[.*?\]\s*\}',
        raw, re.DOTALL,
    ):
        candidates.append((m.start(), m.group(0)))
    # Sort by length desc — longest match is most likely the clean
    # final JSON, not a truncated fragment.
    for _, text in sorted(candidates, key=lambda c: -len(c[1])):
        try:
            data = json.loads(text)
            films = data.get("films", [])
            if not isinstance(films, list) or not films:
                continue
            out = []
            for entry in films:
                if isinstance(entry, dict):
                    title = entry.get("title") or entry.get("name") or ""
                    if title:
                        out.append(_clean(str(title)))
                elif isinstance(entry, str):
                    out.append(_clean(entry))
            if out:
                return _dedupe(out)
        except Exception:
            continue

    # 3) Subset JSON : "films": [...] (the closing brace may be
    #    truncated by max_tokens, so we don't require it).
    for m in re.finditer(r'"films"\s*:\s*\[([^\]]*)\]', raw, re.DOTALL):
        try:
            arr = json.loads("[" + m.group(1) + "]")
            if isinstance(arr, list) and arr:
                out = []
                for entry in arr:
                    if isinstance(entry, str):
                        out.append(_clean(entry))
                if out:
                    return _dedupe(out)
        except Exception:
            continue

    # 3b) Truncated mid-array (no closing ']'): the response was cut
    #     by max_tokens while writing a string entry. E.g.
    #       {"films": ["A", "B", "C", "Ti
    #     The model hasn't even finished "Ti" before the cutoff.
    #     We extract the array contents up to the last complete
    #     string, then close the array manually. The last partial
    #     entry (e.g. "Ti") is dropped (it would be invalid anyway).
    m = re.search(r'"films"\s*:\s*\[(.*)', raw, re.DOTALL)
    if m:
        body = m.group(1)
        # Match complete string entries only (with closing quote + comma
        # or end-of-string). We do this by finding all `"..."` pairs
        # where the closing quote is followed by `,` or end of body.
        # To handle escaped quotes, walk char-by-char.
        out = []
        i = 0
        in_str = False
        cur = []
        while i < len(body):
            c = body[i]
            if not in_str:
                if c == '"':
                    in_str = True
                    cur = []
            else:
                if c == '\\' and i + 1 < len(body):
                    # Escaped char, keep as-is
                    cur.append(body[i:i+2])
                    i += 2
                    continue
                if c == '"':
                    # End of string. Check what follows: must be
                    # `,` (more entries coming) or end of body
                    # (truncated). If neither, abort (malformed).
                    j = i + 1
                    while j < len(body) and body[j] in " \t\n\r":
                        j += 1
                    if j >= len(body) or body[j] == ",":
                        out.append(_clean("".join(cur)))
                        in_str = False
                    else:
                        # Malformed: stop walking, keep what we have
                        break
                else:
                    cur.append(c)
            i += 1
        if out:
            return _dedupe(out)

    # 4) Markdown lines: "- Title" or "1. Title"
    titles = []
    for line in raw.split("\n"):
        m2 = re.match(r"^(?:\d+[\.\)]|[-*])\s*(.+)$", line.strip())
        if m2:
            t = _clean(m2.group(1))
            if 2 < len(t) < 100:
                titles.append(t)
    return _dedupe(titles)


def parse_saga_films(raw: str) -> list:
    """Parse a saga list as [(title, year), ...] tuples.

    V6.1: required for the OMDb year-passing fix. The LLM is asked
    to return {"films": [{"title": "...", "year": 1978}, ...]}. Three
    strategies, mirroring parse_film_titles:
      1) JSON complet {"films": [{"title":..., "year":...}, ...]}
      2) JSON partiel (accepte year en int ou str)
      3) Fallback ligne par ligne "Title (1978)"
    Returns list of (title, year_or_None) tuples. year is int or None.
    """
    if not raw:
        return []
    out = []
    # 1) JSON complet {"films": [{...}, ...]}
    m = re.search(r'\{[^{}]*"films"\s*:\s*\[[^\]]+\][^{}]*\}', raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            for entry in data.get("films", []):
                if not isinstance(entry, dict):
                    continue
                title = str(entry.get("title", "")).strip().strip("\"'")
                if not title or len(title) < 2:
                    continue
                year_raw = entry.get("year")
                try:
                    year = int(year_raw) if year_raw else None
                except (ValueError, TypeError):
                    year = None
                out.append((title, year))
            if out:
                return out
        except Exception:
            pass
    # 2) JSON partiel : on cherche juste le tableau films
    m = re.search(r'"films"\s*:\s*\[([^\]]+)\]', raw, re.DOTALL)
    if m:
        try:
            arr = json.loads("[" + m.group(1) + "]")
            for entry in arr:
                if not isinstance(entry, dict):
                    continue
                title = str(entry.get("title", "")).strip().strip("\"'")
                if not title or len(title) < 2:
                    continue
                year_raw = entry.get("year")
                try:
                    year = int(year_raw) if year_raw else None
                except (ValueError, TypeError):
                    year = None
                out.append((title, year))
            if out:
                return out
        except Exception:
            pass
    # 3) Fallback markdown : "Title (1978)" ou "- Title"
    for line in raw.split("\n"):
        m2 = re.match(r'^(?:\d+[\.\)]|[-*])\s*(.+)$', line.strip())
        if not m2:
            continue
        body = m2.group(1).strip().strip("\"'")
        m3 = re.search(r'^(.*?)\s*\((\d{4})\)\s*$', body)
        if m3:
            out.append((m3.group(1).strip(), int(m3.group(2))))
        elif 2 < len(body) < 100:
            out.append((body, None))
    return out


def parse_sagas(raw: str) -> dict:
    """Parse {"sagas": [{"name": "...", "owned": [...]}, ...]} depuis une sortie LLM."""
    if not raw:
        return {}
    m = re.search(r'\{[^{}]*"sagas"\s*:\s*\[', raw, re.DOTALL)
    if not m:
        return {}
    start = m.start()
    depth, end = 0, start
    for i, ch in enumerate(raw[start:]):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
        if depth == 0:
            end = start + i + 1
            break
    try:
        data = json.loads(raw[start:end])
        return {s["name"]: s.get("owned", []) for s in data.get("sagas", [])}
    except Exception:
        return {}


# =============================================================================
# Backend abstrait
# =============================================================================

class LLMBackend(ABC):
    name = "abstract"

    def __init__(self, model: str, no_timeout: bool = False):
        self.model = model
        self.no_timeout = no_timeout
        # timeouts par type d'appel (s). None = no_timeout activé.
        self._timeouts = {
            "ping":  60,
            "chat":  120,
            "long":  180,
            "huge":  240,
            "embed": 20,
        }

    def _timeout_for(self, kind: str) -> Optional[float]:
        if self.no_timeout:
            return None
        return self._timeouts.get(kind, 120)

    @abstractmethod
    def healthcheck(self) -> bool:
        """Vérifie que le backend répond et que le modèle est prêt."""
        ...

    @abstractmethod
    def chat(self, prompt: str, kind: str = "chat", temperature: float = 0.2,
             max_tokens: int = 1024) -> str:
        """Renvoie le contenu textuel brut. Lève en cas d'échec."""
        ...

    def chat_with_fallback(self, prompt: str, fallback_prompt: str,
                           kind: str = "huge") -> str:
        """Comme chat() mais retry avec fallback_prompt si la première tentative
           lève une exception (timeout, connexion, etc.). Sert pour
           get_filmography : si le gros prompt timeout, on relance un prompt
           simplifié (top-20)."""
        try:
            return self.chat(prompt, kind=kind)
        except Exception:
            return self.chat(fallback_prompt, kind="long")


    @abstractmethod
    def embed(self, text: str) -> Optional[list]:
        """Renvoie un vecteur d'embedding, ou None si non supporté."""
        ...

    # --- Helpers de haut niveau : reprennent les prompts d'origine ----------

    def suggest_titles(self, base: dict, n: int, genre: str = None,
                       mood: str = None) -> list:
        genre_i = f'- Suggestions MUST be {genre} films\n' if genre else ""
        mood_i  = (f'- The mood/atmosphere requested is: "{mood}" — prioritize films '
                   f'that match this feeling\n') if mood else ""
        prompt = (
            f'You are a film expert with encyclopedic knowledge of world cinema.\n\n'
            f'Source film: "{base["title"]}" ({base["year"]})\n'
            f'Genre: {base["genre"]}\n'
            f'Director: {base["director"]}\n'
            f'Cast: {base["actors"]}\n'
            f'Plot: {base["plot"]}\n\n'
            f'Suggest {n} REAL existing films similar in theme, tone, atmosphere, '
            f'or narrative style.\n\n'
            f'Rules:\n'
            f'- Only real theatrically released films\n'
            f'- Preferred IMDb rating above 6.5\n'
            f'- No direct sequels/prequels of the source film\n'
            f'- Vary the eras\n'
            f'- Use exact English/international theatrical title\n'
            f'- Do NOT include the source film itself\n'
            f'{genre_i}{mood_i}'
            f'\nRespond ONLY with valid JSON:\n'
            f'{{"films": ["Title 1", "Title 2", ...]}}'
        )
        return parse_film_titles(self.chat(prompt, kind="long", max_tokens=2048))

    def suggest_from_title(self, film_title: str, n: int,
                           genre: str = None, mood: str = None) -> list:
        genre_i = f'- Suggestions MUST be {genre} films\n' if genre else ""
        mood_i  = (f'- The mood/atmosphere requested is: "{mood}" — prioritize films '
                   f'that match this feeling\n') if mood else ""
        prompt = (
            f'You are a film expert with encyclopedic knowledge of world cinema.\n\n'
            f'The user wants recommendations similar to: "{film_title}"\n\n'
            f'Suggest {n} REAL existing films that share the same theme, tone, '
            f'atmosphere or narrative style as "{film_title}".\n\n'
            f'Rules:\n'
            f'- Only real theatrically released films\n'
            f'- Preferred IMDb rating above 6.5\n'
            f'- Do NOT include "{film_title}" itself\n'
            f'- No direct sequels/prequels\n'
            f'- Vary the eras\n'
            f'- Use exact English/international theatrical title\n'
            f'{genre_i}{mood_i}'
            f'\nRespond ONLY with valid JSON:\n'
            f'{{"films": ["Title 1", "Title 2", ...]}}'
        )
        return parse_film_titles(self.chat(prompt, kind="long", max_tokens=2048))

    def suggest_from_mood(self, mood: str, n: int, genre: str = None) -> list:
        # V5.19: tolerate args.genre being None or missing entirely.
        # The web runner and CLI both pass it as a keyword, but
        # direct callers (e.g. tests) may construct args without
        # that field. Previously crashed with 'Args object has no
        # attribute genre'.
        if not genre:
            genre_i = ""
        else:
            genre_i = f'- Suggestions MUST be {genre} films\n'
        prompt = (
            f'You are a film expert with encyclopedic knowledge of world cinema.\n\n'
            f'The user is looking for films with this specific mood or atmosphere: '
            f'"{mood}"\n\n'
            f'Suggest {n} REAL existing films that perfectly match this mood/atmosphere.\n\n'
            f'Rules:\n'
            f'- Only real theatrically released films\n'
            f'- Each film MUST strongly match the mood/atmosphere "{mood}" — '
            f'do NOT include films just because they are famous or acclaimed\n'
            f'- If the mood mentions a setting (space, ocean, etc.) or theme '
            f'(noir, comedy, horror, etc.), EVERY film must be on-theme\n'
            f'- You may vary the eras (different decades) but keep the theme strict\n'
            f'- Use exact English/international theatrical title\n'
            f'- Be exhaustive — list as many relevant films as possible\n'
            f'{genre_i}'
            f'\nRespond ONLY with valid JSON:\n'
            f'{{"films": ["Title 1", "Title 2", ...]}}'
        )
        return parse_film_titles(self.chat(prompt, kind="long", max_tokens=2048))

    def get_saga_films(self, saga_name: str) -> list:
        """Return [(title, year), ...] for the saga.

        The year is critical: it gets passed to OMDb so a query like
        "Superman: The Movie" can resolve to the actual 1978 film
        instead of OMDb's fuzzy "Bane v Superman: The Movie" 2016
        short. V6.1.
        """
        prompt = (
            f'You are a film expert with encyclopedic knowledge of world cinema.\n\n'
            f'List EVERY theatrically released film in the "{saga_name}" '
            f'saga/franchise in chronological release order.\n\n'
            f'For "{saga_name}", this includes the COMPLETE list — do not omit any film.\n\n'
            f'STRICT Rules:\n'
            f'- Include ALL films: part 1, part 2, part 3... every numbered sequel\n'
            f'- Include spin-offs and anthology films\n'
            f'- NO TV shows, NO animated series, NO shorts, NO special editions\n'
            f'- Use EXACT English theatrical release title\n'
            f'- Do NOT skip any film, do NOT add comments or notes\n'
            f'- Each entry must include BOTH the title AND the year of release\n'
            # V5.13: explicit anti-omission reminders. The 35B-Q3
            # model was dropping 1-3 films on Star Wars / Marvel runs
            # (e.g. forgetting Episode IV A New Hope). Adding
            # numbered placeholders forces the model to count and
            # fill all slots.
            f'- If the saga has N films, output EXACTLY N entries. Count them.\n'
            f'- For numbered sagas (Star Wars, Fast & Furious, etc.) include EVERY episode\n'
            f'\nRespond ONLY with this exact JSON (no other text before or after):\n'
            f'{{"films": [{{"title": "Title 1", "year": 1978}}, '
            f'{{"title": "Title 2", "year": 1980}}]}}'
        )
        return parse_saga_films(self.chat(prompt, kind="long"))

    def get_saga_films_legacy(self, saga_name: str) -> list:
        """Legacy V5.13 single-shot list. Kept as a fallback for
        old callers that expect a flat list of titles. Not used by
        run_saga_mode anymore (V6.1)."""
        prompt = (
            f'You are a film expert with encyclopedic knowledge of world cinema.\n\n'
            f'List EVERY theatrically released film in the "{saga_name}" '
            f'saga/franchise in chronological release order.\n\n'
            f'For "{saga_name}", this includes the COMPLETE list — do not omit any film.\n\n'
            f'STRICT Rules:\n'
            f'- Include ALL films: part 1, part 2, part 3... every numbered sequel\n'
            f'- Include spin-offs and anthology films\n'
            f'- NO TV shows, NO animated series, NO shorts, NO special editions\n'
            f'- Use EXACT English theatrical release title\n'
            f'- Do NOT skip any film, do NOT add comments or notes\n'
            f'- Each entry must be ONLY the film title, nothing else\n'
            f'- If the saga has N films, output EXACTLY N entries. Count them.\n'
            f'- For numbered sagas (Star Wars, Fast & Furious, etc.) include EVERY episode\n'
            f'\nRespond ONLY with this exact JSON (no other text before or after):\n'
            f'{{"films": ["Title 1", "Title 2", "Title 3", "Title 4", "Title 5"]}}'
        )
        return parse_film_titles(self.chat(prompt, kind="long"))

    def detect_sagas(self, titles: list) -> dict:
        sample = titles[:80]
        prompt = (
            f'You are a film expert.\n\n'
            f'From this list of films, identify which ones belong to a saga or '
            f'franchise (series of at least 2 related films).\n\n'
            f'Films:\n' + "\n".join(f'- {t}' for t in sample) +
            f'\n\nFor each saga found, list its name and which films from the list '
            f'belong to it.\nOnly include sagas where the list contains at least 1 film.\n\n'
            f'Respond ONLY with valid JSON:\n'
            f'{{"sagas": [{{"name": "Saga Name", "owned": ["Film 1", "Film 2"]}}, ...]}}'
        )
        return parse_sagas(self.chat(prompt, kind="long", max_tokens=4096))

    def get_filmography(self, person: str, role: str, top_n: int = 0) -> list:
        # (Reprend la même structure de prompt que l'original — multi-acteur géré
        # par l'appelant qui passe déjà "name1 and name2")
        #
        # V7.2: anti-hallucination rewrite. The previous prompt asked
        # the LLM to "List ALL films" / "output EXACTLY N entries",
        # which on lower-bitrate quantizations (IQ2 / IQ3) pushed the
        # model to invent titles to fill the quota. Symptom (real
        # run, jul 2026): --actor "will ferrell" returned 28 titles
        # of which 10+ did not feature Will Ferrell at all (Hallo,
        # Irreversible, Lucky You, Viva Pinata, Dick and Jane, Hall
        # Pass, The Producers 2005, etc.). The post-hoc role-match
        # validator in newmovies.py correctly rejected them, but
        # the visible "Missing" list was anemic (~4 films).
        #
        # V7.3: completeness-first rewrite. The V7.2 "100% CERTAIN"
        # threshold was so strict that gemma-4-26B-A4B (the model
        # the user is running on .32:8080, jul 2026) over-pruned
        # its own output: it knows ~14 Will Ferrell films but
        # only emitted 9 in the JSON because it kept
        # self-correcting ("Wait, is Bewitched really Ferrell? Let
        # me re-verify...") and trimming the list as it went. The
        # reasoning ate the entire 2048-token budget, finish_reason
        # became "length", and the second/clean JSON at the end
        # was past the cutoff.
        #
        # New strategy: ask for completeness, gate the model to
        # output a single clean JSON (no mid-stream self-correction
        # in the visible response), and let the post-hoc OMDb
        # role-match validator in newmovies.py do the final
        # accuracy gate. The validator is already the source of
        # truth (it cross-references OMDb's Actors field), so the
        # LLM doesn't need to be 100% perfect — it needs to be
        # ~85% and not invent.
        role_desc = {
            "director": (f"List theatrical films DIRECTED by {person}.\n"
                         f"Only include films where {person} is credited as the main director."),
            "actor":    (f"List theatrical films FEATURING {person} as a credited "
                         f"cast member (lead, co-lead, or major supporting).\n"
                         f"Only include films where {person} actually appears on screen "
                         f"in a real acting role."),
            "cast":     (f"List theatrical films where ALL of {person} appear together.\n"
                         f"Only include films where EVERY one of these actors has a "
                         f"credited role."),
            "composer": (f"List theatrical films SCORED by {person}.\n"
                         f"Only include films where {person} is credited as the main "
                         f"composer of the original score."),
            "author":   (f"List theatrical films ADAPTED from works written by {person}.\n"
                         f"Include novels, short stories, and plays adapted into films."),
        }.get(role, f"List theatrical films associated with {person}.")
        cap = top_n if top_n > 0 else 30
        prompt = "\n".join([
            "You are a film expert with encyclopedic knowledge of world cinema.",
            "", role_desc, "",
            "STRICT Rules (do not violate):",
            "- Only REAL theatrically released films (NO TV shows, NO shorts, NO docs)",
            f"- Output AT MOST {cap} titles. Aim for completeness within that cap.",
            "- List in chronological release order (oldest first)",
            "- Use EXACT English theatrical release title",
            "- Each entry must be ONLY the film title, nothing else",
            "- Output a single JSON object — do NOT output multiple JSON blocks",
            "- Do NOT add commentary, self-corrections, or reasoning OUTSIDE the JSON",
            "- All self-checking happens BEFORE you start writing the JSON",
            "",
            "ACCURACY RULES (the model has hallucinated before — read carefully):",
            f"- Include a film if you have a STRONG, WELL-ATTRIBUTED belief that {person} "
            f"is credited in the requested role ({role}).",
            f"- A famous, widely-cited film (Anchorman, Elf, etc.) should be INCLUDED even "
            f"if you're not 100% certain of the exact year.",
            f"- A well-attributed, mid-tier Ferrell/actor/etc. film should also be INCLUDED.",
            f"- Do NOT include films where {person} has only a cameo, uncredited appearance, "
            f"archive footage, or is only mentioned by name.",
            f"- Do NOT include films where {person} is only a voice actor (animated films "
            f"where they don't physically appear count as voice roles — be cautious).",
            f"- Do NOT include films by similarly-named people (common-name disambiguation: "
            f"check the person is the exact {role} you mean).",
            f"- Do NOT invent titles. If a title is not a real film, do NOT include it.",
            "",
            "REASONING DISCIPLINE (critical to avoid truncation):",
            "- Do ALL your self-checking internally before you start writing the JSON.",
            "- Do NOT write 'Wait, let me verify...' / 'Self-correction:' / 'I must stop...' "
            "in the response. These eat tokens and cause the JSON to be truncated.",
            "- If you catch yourself unsure mid-write, OMIT that title silently — never "
            "explain the omission in the visible response.",
            "",
            'Respond ONLY with this exact JSON (no markdown fence, no commentary):',
            '{"films": ["Title 1", "Title 2", "Title 3"]}',
        ])
        # Fallback prompt (repris de l'original) : si le gros prompt timeout,
        # on retente avec une formulation simple. V7.2: also accuracy-first
        # to avoid the same hallucination pattern when the model retries.
        fallback_prompt = "\n".join([
            f"List the 15 films you are MOST CERTAIN feature {person} as {role}.",
            f"Only include films where {person} is undeniably credited in the role.",
            f"Do NOT guess or invent titles — if unsure, leave the film out.",
            "Reply ONLY with JSON:",
            '{"films": ["Title 1", "Title 2", "Title 3"]}',
        ])
        try:
            raw = self.chat_with_fallback(prompt, fallback_prompt, kind="huge")
        except Exception:
            return []
        return parse_film_titles(raw)

    def analyze_collection(self, profile: dict) -> tuple:
        # Build stats summary
        genres_str  = ", ".join(f"{g} ({c})" for g, c in profile["top_genres"])
        decades_str = ", ".join(f"{d}s ({c})" for d, c in profile["top_decades"])

        # ── Tour 1 : brouillon (analyse + recos) ──────────────────────
        prompt1 = "\n".join([
            "You are a sharp, opinionated film curator. Below is a user's full",
            "movie collection.",
            f"Collection: {profile['total']} films "
            f"({profile['n_unique']} unique titles), avg IMDb {profile['avg_rating']}.",
            f"Top genres: {genres_str}",
            f"Top decades: {decades_str}",
            "",
            "You know every film, its director, and its place in cinema history.",
            "",
            "USER'S COLLECTION (alphabetical, one per line):",
            profile["titles"],
            "",
            "STRICT RULES (do not violate):",
            "- Base every claim on the list above. If unsure, write 'likely' or 'possibly', never assert.",
            "- Do not self-correct or hedge mid-sentence. Be confident and consistent.",
            "- Geographic precision: Japan is in Asia, not Europe. Belgium is in Western Europe.",
            "- Do not contradict yourself: if you recommend a Korean film, do not claim the user has zero Korean films.",
            "",
            "VOICE:",
            "- Be specific and incisive, not flattering. Use strong adjectives and sharp contrasts.",
            "- Quote specific films the user owns by name to anchor every claim.",
            "- Avoid generic phrases ('great taste', 'well-rounded', 'cinephile').",
            "- Profile the viewer's psychology, not just their filmography.",
            "",
            "Write a sharp, personalized analysis (4-5 paragraphs):",
            "1. Who this viewer is — what they crave, what they avoid, what they pretend to like (1 paragraph).",
            "2. Strengths: directors, movements, eras well-covered (cite films, no flattery).",
            "3. Gaps by region, by director, by movement, by era — be honest about blind spots.",
            "4. Three concrete directions, each anchored in a specific film+director+year that exemplifies the gap.",
            "",
            "Then 12 recommendations (films NOT in the collection) that fill those gaps.",
            "Mix eras, regions, and difficulty levels. Avoid the most obvious IMDb Top 250 picks",
            "unless truly warranted. Prefer films that are not in the user's collection (check the list).",
            "",
            "Reply in this exact format:",
            "",
            "ANALYSIS:",
            "[your analysis]",
            "",
            "RECOMMENDATIONS:",
            '{"films": ["T1","T2","T3","T4","T5","T6","T7","T8","T9","T10","T11","T12"]}',
        ])
        raw1 = self.chat(prompt1, kind="long", max_tokens=12000)

        # ── Tour 2 : self-verification + rewrite ──────────────────────
        # Force a real fact-check pass: the LLM must verify each claim against
        # the collection list, drop the recos that are already in the collection,
        # and produce a clean, confident final version.
        prompt2 = "\n".join([
            "Below is a DRAFT analysis of a user's movie collection. The draft",
            "may contain: (a) factual errors (claims about which films the user",
            "has/hasn't), (b) recommendations that are already in the collection,",
            "(c) stream-of-consciousness or mid-sentence self-corrections.",
            "",
            "Your job: do a FACT-CHECK pass, then output a CLEAN FINAL version.",
            "",
            "USER'S COLLECTION (alphabetical, one per line — SOURCE OF TRUTH):",
            profile["titles"],
            "",
            "DRAFT TO FACT-CHECK:",
            raw1,
            "",
            "STEP 1 — VERIFY each factual claim in the draft:",
            "- For every 'you have X' or 'you lack Y' claim, SCAN the collection",
            "  list above. If the claim is wrong, mark it. If you cannot verify,",
            "  mark it unverifiable.",
            "- For every recommended film, CHECK if it is in the collection list.",
            "  If it is, mark it as 'already owned' — it MUST be replaced.",
            "",
            "STEP 2 — REWRITE the analysis with these corrections:",
            "- Remove all mid-sentence self-corrections, hedges, and thinking traces.",
            "- Replace any incorrect factual claim with a corrected one. If the",
            "  correction would change the meaning too much, soften with 'likely'",
            "  or 'possibly'.",
            "- Preserve the 4-paragraph structure and the 3 concrete directions.",
            "- For any recommendation marked 'already owned', REPLACE it with a",
            "  different film that covers the same gap. Aim for 12 final recos.",
            "- Do NOT add a meta-paragraph about what you changed. Do NOT show",
            "  your fact-check notes. Just output the final, clean version.",
            "",
            "Reply in this exact format:",
            "",
            "ANALYSIS:",
            "[clean rewritten analysis]",
            "",
            "RECOMMENDATIONS:",
            '{"films": ["T1","T2","T3","T4","T5","T6","T7","T8","T9","T10","T11","T12"]}',
        ])
        raw2 = self.chat(prompt2, kind="long", max_tokens=12000)

        # Debug: dump both passes to /tmp
        try:
            with open("/tmp/analyze_raw_response.txt", "w") as f:
                f.write("=== TOUR 1 (DRAFT) ===\n")
                f.write(raw1)
                f.write("\n\n=== TOUR 2 (CLEAN) ===\n")
                f.write(raw2)
        except Exception:
            pass

        # Parse the FINAL output (tour 2)
        analysis, films = "", []
        if "ANALYSIS:" in raw2:
            parts = raw2.split("RECOMMENDATIONS:")
            analysis = parts[0].replace("ANALYSIS:", "").strip()
            if len(parts) > 1:
                rec_part = parts[1].strip()
                m = re.search(r'"films"\s*:\s*\[([^\]]+)\]', rec_part, re.DOTALL)
                if m:
                    try:
                        items = re.findall(r'"([^"]{2,80})"', m.group(0))
                        films = [i for i in items if i != "films"]
                    except Exception:
                        pass
        # Fallback: if tour 2 produced no parseable output, use tour 1
        if not analysis and "ANALYSIS:" in raw1:
            parts = raw1.split("RECOMMENDATIONS:")
            analysis = parts[0].replace("ANALYSIS:", "").strip()
            if len(parts) > 1:
                rec_part = parts[1].strip()
                m = re.search(r'"films"\s*:\s*\[([^\]]+)\]', rec_part, re.DOTALL)
                if m:
                    try:
                        items = re.findall(r'"([^"]{2,80})"', m.group(0))
                        films = [i for i in items if i != "films"]
                    except Exception:
                        pass
        return analysis, films


# =============================================================================
# Backend llama.cpp (OpenAI-compat)
# =============================================================================

class LlamaCppBackend(LLMBackend):
    name = "llamacpp"

    def __init__(self, base_url: str, model: str, no_timeout: bool = False,
                 timeout_health: int = 120):
        super().__init__(model=model, no_timeout=no_timeout)
        self.base_url = base_url.rstrip("/")
        # llama-server répond vite quand le modèle est en RAM, lentement au warmup.
        # On laisse 180s pour le healthcheck afin de couvrir un load complet.
        self._timeouts["ping"] = timeout_health
        # Timeouts plus longs pour un 35B : passer à 240/300 par défaut
        self._timeouts["chat"]  = 240
        self._timeouts["long"]  = 300
        self._timeouts["huge"]  = 360
        self._timeouts["embed"] = 30
        # V7.2: actual_model is the model *currently loaded on the
        # server*, resolved at healthcheck time via GET /v1/models.
        # Falls back to self.model (the config value) when the
        # server doesn't expose /v1/models or returns nothing
        # parseable. This is what the user sees in the banner,
        # because llama-server's OpenAI-compat API IGNORES the
        # "model" field of the request and serves whatever is
        # actually loaded — so the config value can be wrong/stale
        # without anyone noticing. The user swaps models on .32
        # between test runs, so the resolved value matters.
        self.actual_model: Optional[str] = None

    def _resolve_actual_model(self) -> Optional[str]:
        """Ask the server what's actually loaded. Returns the model
        id string (e.g. "/home/niko/mnt/4/models/Qwen3.6-...gguf")
        or None if it can't be determined. Never raises — this is
        best-effort display info, not a health gate."""
        try:
            r = requests.get(f"{self.base_url}/v1/models", timeout=10)
            if not r.ok:
                return None
            data = r.json()
            # llama.cpp returns {"data": [...]} AND {"models": [...]}.
            # The first entry in either is what's actually loaded.
            if isinstance(data.get("data"), list) and data["data"]:
                return data["data"][0].get("id") or data["data"][0].get("name")
            if isinstance(data.get("models"), list) and data["models"]:
                return (data["models"][0].get("name")
                        or data["models"][0].get("id"))
        except Exception:
            pass
        return None

    def _short_model_name(self, model_id: str) -> str:
        """Turn a long filesystem path into a short label like
        'Qwen3.6-35B-A3B-UD-IQ2_M.gguf' (basename) for display.
        Stays the full string if it's already a short name
        (e.g. 'llama3.1:8b' from Ollama)."""
        if not model_id:
            return model_id
        # If it contains a path separator, take the basename.
        if "/" in model_id or "\\" in model_id:
            return model_id.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        return model_id

    def healthcheck(self) -> bool:
        """Vérifie que le serveur répond ET que le modèle est listé/chargé."""
        # 1) /health (rapide)
        try:
            r = requests.get(f"{self.base_url}/health", timeout=10)
            if r.status_code != 200 or r.json().get("status") != "ok":
                return False
        except Exception:
            return False
        # 2) /v1/models (vérifie que le modèle est connu du serveur)
        # V7.2: also cache the actually-loaded model id (first entry)
        # so the banner can show the real name, not the config value
        # which may be stale/wrong (the user swaps models on .32).
        try:
            r = requests.get(f"{self.base_url}/v1/models", timeout=10)
            if not r.ok:
                return False
            data = r.json()
            listed = []
            if "data" in data:
                listed = [m.get("id") for m in data["data"]]
            elif "models" in data:
                # llama.cpp renvoie "models" ET "data" dans la réponse (vu plus haut)
                listed = [m.get("name") or m.get("id") for m in data["models"]]
            if listed:
                self.actual_model = listed[0]
            # On accepte si le modèle est listé OU si aucun modèle n'est listé
            # (le warmup se fera au premier vrai appel).
            if listed and self.model not in listed:
                # Le modèle demandé n'est pas chargé — avertissement mais on
                # continue : llama-server peut en charger à la volée.
                pass
        except Exception:
            return False
        # 3) Ping "Reply OK" pour amorcer le modèle (1 essai).
        try:
            r = requests.post(
                f"{self.base_url}/v1/chat/completions",
                json={"model": self.model,
                      "messages": [{"role": "user", "content": "Reply with only the word OK."}],
                      "temperature": 0, "max_tokens": 10},
                timeout=self._timeout_for("ping"))
            return r.ok
        except Exception:
            return False

    def chat(self, prompt: str, kind: str = "chat", temperature: float = 0.2,
             max_tokens: int = 1024) -> str:
        # V7.5: stop sequences + reduced huge floor.
        #
        # V7.3 set the huge floor to 6144, betting that more headroom
        # would let gemma-4-26B-A4B finish its visible reasoning
        # burn and emit a clean JSON. That was wrong: the floor just
        # let the model ramble for the full budget. Live run (jul
        # 2026, --actor "will ferrell"): 6143/6144 tokens consumed,
        # finish_reason="length", the JSON never closed, parser
        # returned 0 films, run aborted.
        #
        # Real fix is two-pronged:
        #   1. Cut the budget back to 2048 for huge — the model has
        #      30 titles × ~30 chars/entry + ~200 chars of JSON
        #      syntax = ~1100 tokens. 2048 gives headroom for the
        #      gemma-4 reasoning intro (~300 tokens observed) without
        #      letting it loop indefinitely.
        #   2. Pass `stop` sequences to llama-server. The OpenAI-
        #      compat API honors them. Patterns targeted:
        #        - "\n\n" : a double newline never appears in a
        #          well-formed JSON {"films": [...]} array, but
        #          gemma-4 emits tons of "\n\n" between reasoning
        #          paragraphs. Cutting on "\n\n" stops the model
        #          mid-ramble, after the JSON has been emitted.
        #        - "Wait," / "Wait " / "Let me" / "Self-correction" :
        #          signature patterns of visible reasoning. We use
        #          these as a secondary defense in case the model
        #          doesn't emit "\n\n" but starts talking about
        #          verifying.
        # Together: a 30-title JSON + 200 tokens of intro = ~1300
        # tokens, well under the 2048 budget. The model can't ramble
        # for thousands of tokens because the stop sequence fires
        # as soon as it tries.
        #
        # Compatibility: the stop sequences are conservative.
        # "\n\n" never breaks a JSON parser (it never appears inside
        # a string the model is currently writing — even escaped
        # quotes in JSON are `\\n` not actual newlines). The
        # Wait/Self-correction patterns never appear at the start of
        # a film title in any reasonable database. So the stops
        # only fire on reasoning text, never on valid JSON content.
        floor = {
            "chat":  2048,
            "long":  2048,
            "huge":  2048,
            "embed": 2048,
        }.get(kind, 2048)
        eff_max = max(max_tokens, floor)
        # Stop sequences: a "\n\n" cuts mid-ramble, the others are
        # reasoning signatures observed on gemma-4-26B-A4B. We
        # order them so the most common one ("\n\n") is checked
        # first by the server. llama-server supports up to 4 stops
        # in the OpenAI-compat API; we use all 4.
        stop_seqs = ["\n\n", "Wait,", "Self-correction", "Let me verify"]
        r = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json={"model": self.model,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": temperature, "max_tokens": eff_max,
                  "stop": stop_seqs},
            timeout=self._timeout_for(kind))
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        # Priorité au content normal. Si vide (modèle reasoning-only),
        # on tente de récupérer le JSON depuis reasoning_content.
        content = msg.get("content") or ""
        if not content.strip():
            content = msg.get("reasoning_content") or ""
        return content

    def embed(self, text: str) -> Optional[list]:
        # llama.cpp ne supporte /v1/embeddings que si lancé avec --embeddings.
        # On essaie et on retourne None explicitement si 501.
        try:
            r = requests.post(
                f"{self.base_url}/v1/embeddings",
                json={"model": self.model, "input": text[:500]},
                timeout=self._timeout_for("embed"))
            if r.status_code == 501:
                return None
            r.raise_for_status()
            data = r.json().get("data", [])
            if data:
                return data[0].get("embedding")
        except Exception:
            return None
        return None


# =============================================================================
# Factory
# =============================================================================

def make_backend(cfg: dict) -> LLMBackend:
    """Construit le backend selon config.yaml.
       cfg attendu : {"llm_backend": "llamacpp", ...}
       Note: seul 'llamacpp' est supporté (OllamaBackend supprimé en Vague 2).
    """
    kind = (cfg.get("llm_backend") or "llamacpp").lower()
    model = cfg.get("llm_model") or cfg.get("ollama_model", "llama3.1:8b")
    no_timeout = bool(cfg.get("no_timeout"))

    if kind == "llamacpp":
        base = cfg.get("llamacpp_base_url", "http://localhost:8080")
        return LlamaCppBackend(base_url=base, model=model, no_timeout=no_timeout)
    else:
        raise ValueError(f"Unknown llm_backend: {kind!r} (only 'llamacpp' is supported)")
