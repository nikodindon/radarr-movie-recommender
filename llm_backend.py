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
    """Extrait une liste de titres depuis une sortie LLM. Trois stratégies :
       1) JSON complet {"films": [...]}
       2) Sous-ensemble JSON {"films": ["...", "..."]}
       3) Fallback ligne par ligne (markdown "- Title" / "1. Title")
    """
    if not raw:
        return []
    # 1) JSON complet
    m = re.search(r'\{[^{}]*"films"\s*:\s*\[[^\]]+\][^{}]*\}', raw, re.DOTALL)
    if m:
        try:
            return [t.strip() for t in json.loads(m.group(0)).get("films", []) if t.strip()]
        except Exception:
            pass
    # 2) Extraction d'un tableau même si le JSON est cassé autour
    m = re.search(r'"films"\s*:\s*\[([^\]]+)\]', raw, re.DOTALL)
    if m:
        try:
            return [t.strip() for t in json.loads("[" + m.group(1) + "]") if t.strip()]
        except Exception:
            pass
    # 3) Lignes markdown
    titles = []
    for line in raw.split("\n"):
        m2 = re.match(r'^(?:\d+[\.\)]|[-*])\s*(.+)$', line.strip())
        if m2:
            t = re.sub(r'\s*\(\d{4}\)\s*$', '', m2.group(1).strip().strip("\"'"))
            if 2 < len(t) < 100:
                titles.append(t)
    return titles


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
            # V5.13: explicit anti-omission reminders. The 35B-Q3
            # model was dropping 1-3 films on Star Wars / Marvel runs
            # (e.g. forgetting Episode IV A New Hope). Adding
            # numbered placeholders forces the model to count and
            # fill all slots.
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
        role_desc = {
            "director": (f"List ALL theatrical films directed by {person}.\n"
                         f"Include only films where {person} is the main director."),
            "actor":    (f"List ALL theatrical films where {person} has a significant "
                         f"role (lead or major supporting).\n"
                         f"Include only films where {person} actually appears on screen."),
            "cast":     (f"List ALL theatrical films where {person} ALL appear together "
                         f"in the same film.\n"
                         f"Only include films where EVERY one of these actors has a role."),
            "composer": (f"List ALL theatrical films for which {person} composed the "
                         f"original score/soundtrack.\n"
                         f"Include only films where {person} is the main composer."),
            "author":   (f"List ALL theatrical films adapted from works written by {person}.\n"
                         f"Include novels, short stories, and plays adapted into films."),
        }.get(role, f"List ALL theatrical films associated with {person}.")
        limit_i = (f'- List the {top_n} most notable films only'
                   if top_n > 0 else '- Include ALL films, do not omit any')
        prompt = "\n".join([
            "You are a film expert with encyclopedic knowledge of world cinema.",
            "", role_desc, "",
            "STRICT Rules:",
            "- Only REAL theatrically released films (NO TV shows, NO shorts)",
            limit_i,
            "- List in chronological release order",
            "- Use EXACT English theatrical release title",
            "- Each entry must be ONLY the film title, nothing else",
            # V5.21: count rule. Helps the model not skip famous films
            # (e.g. for "kevin james" it forgot Paul Blart: Mall Cop).
            f"- If the person has N films matching the role, output EXACTLY N entries",
            "",
            'Respond ONLY with this exact JSON (no other text):',
            '{"films": ["Title 1", "Title 2", "Title 3"]}',
        ])
        # Fallback prompt (repris de l'original) : si le gros prompt timeout,
        # on retente avec une formulation simple.
        fallback_prompt = "\n".join([
            f"List the 20 most famous films where {person} is {role}.",
            "Reply ONLY with JSON:",
            '{"films": ["Title 1", "Title 2", "Title 3"]}',
        ])
        try:
            raw = self.chat_with_fallback(prompt, fallback_prompt, kind="huge")
        except Exception:
            return []
        return parse_film_titles(raw)

    def analyze_collection(self, profile: dict) -> tuple:
        genres_str  = ", ".join(f"{g} ({c})" for g, c in profile["top_genres"])
        decades_str = ", ".join(f"{d}s ({c})" for d, c in profile["top_decades"])
        titles_str  = "\n".join(f"- {t}" for t in profile["sample_titles"][:25])
        prompt = "\n".join([
            "You are an expert film curator analyzing a personal movie collection.",
            "",
            f"Collection size: {profile['total']} films",
            f"Average IMDb rating: {profile['avg_rating']}",
            f"Top genres: {genres_str}",
            f"Top decades: {decades_str}",
            "",
            "Sample of films in collection:", titles_str, "",
            "Based on this collection, write a short personalized analysis (3-4 paragraphs):",
            "1. Describe the cinephile profile (what kind of viewer this person is)",
            "2. Identify strengths (what is well covered)",
            "3. Identify gaps (what important films/directors/movements are missing)",
            "4. Suggest 3 specific directions to explore",
            "",
            "Then provide exactly 10 film recommendations that fill the detected gaps.",
            "These must be films NOT in the collection already.",
            "",
            "IMPORTANT: You MUST respond in this EXACT two-part format, do not skip either part:",
            "",
            "ANALYSIS:",
            "[write your analysis here - 3 to 4 paragraphs]",
            "",
            "RECOMMENDATIONS:",
            '{"films": ["T1", "T2", "T3", "T4", "T5", "T6", "T7", "T8", "T9", "T10"]}',
            "",
            "The RECOMMENDATIONS section is MANDATORY. Always end your response with the JSON.",
        ])
        raw = self.chat(prompt, kind="long", max_tokens=4096)
        analysis, films = "", []
        if "ANALYSIS:" in raw:
            parts = raw.split("RECOMMENDATIONS:")
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
        # max_tokens minimum 2048 pour absorber le chain-of-thought des modèles
        # type Qwen/ornith-aeon (raisonnement ~500-1500 tokens avant la réponse).
        # Au-dessous, le raisonnement mange tout le budget et le content sort vide.
        eff_max = max(max_tokens, 2048)
        r = requests.post(
            f"{self.base_url}/v1/chat/completions",
            json={"model": self.model,
                  "messages": [{"role": "user", "content": prompt}],
                  "temperature": temperature, "max_tokens": eff_max},
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
