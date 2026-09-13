"""Novelty Lens Portable: standard-library-only local web application.

It deliberately has no fallback paths. A provider failure or malformed model
response fails the request; it never substitutes a fabricated search result.
"""
from __future__ import annotations

import json
import math
import os
import sqlite3
import ssl
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "novelty_lens.db"
STATIC_DIR = ROOT / "static"
HOST, PORT = "127.0.0.1", 8000
LENS_URL = "https://api.lens.org/patent/search"


class AppError(RuntimeError):
    """A safe, user-facing failure. Never put credentials in its text."""


def setting(name: str, *, required: bool = True, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if required and not value:
        raise AppError(f"Missing required setting: {name}")
    return value


def api_json(url: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90, context=ssl.create_default_context()) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise AppError(f"Provider request failed (HTTP {error.code}).") from error
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        raise AppError("Provider request failed. Check network access and try again.") from error


def model_json(prompt: str) -> dict[str, Any]:
    base_url = setting("ARAMCO_BASE_URL", default="https://dev-metabrain-api.aramco.com.sa/v1").rstrip("/")
    body = api_json(
        f"{base_url}/chat/completions",
        {"model": setting("LLM_MODEL", default="minimaxm3"), "messages": [
            {"role": "system", "content": "Return only valid JSON. Never use Markdown fences."},
            {"role": "user", "content": prompt},
        ]},
        setting("ARAMCO_API_KEY"),
    )
    try:
        content = body["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise AppError("The language model returned an invalid structured response.") from error
    if not isinstance(parsed, dict):
        raise AppError("The language model returned an invalid structured response.")
    return parsed


def embeddings(texts: list[str]) -> list[list[float]]:
    base_url = setting("ARAMCO_BASE_URL", default="https://dev-metabrain-api.aramco.com.sa/v1").rstrip("/")
    body = api_json(
        f"{base_url}/embeddings",
        {"model": setting("EMBEDDING_MODEL", default="Qwen3-Embedding-4B"), "input": texts},
        setting("ARAMCO_API_KEY"),
    )
    try:
        rows = sorted(body["data"], key=lambda row: row["index"])
        vectors = [row["embedding"] for row in rows]
        if len(vectors) != len(texts) or not all(isinstance(v, list) and v for v in vectors):
            raise ValueError
        return vectors
    except (KeyError, TypeError, ValueError) as error:
        raise AppError("The embedding model returned an invalid response.") from error


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise AppError("Embedding vectors have incompatible dimensions.")
    magnitude = math.sqrt(sum(x * x for x in left)) * math.sqrt(sum(x * x for x in right))
    if not magnitude:
        raise AppError("Embedding model returned a zero vector.")
    return sum(x * y for x, y in zip(left, right)) / magnitude


def text_at(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    if isinstance(value, list):
        english = next((item for item in value if isinstance(item, dict) and item.get("lang") == "en"), None)
        return text_at(english or (value[0] if value else ""))
    return ""


def lens_search(queries: list[str]) -> list[dict[str, str]]:
    token = setting("LENS_API_KEY")
    include = ["lens_id", "jurisdiction", "doc_number", "kind", "date_published", "biblio.invention_title", "abstract", "claims", "description"]
    records: dict[str, dict[str, str]] = {}
    for query in queries[:3]:
        body = api_json(LENS_URL, {"query": query, "size": 5, "include": include}, token)
        if not isinstance(body.get("data"), list):
            raise AppError("Lens returned an invalid search response.")
        for item in body["data"]:
            if not isinstance(item, dict):
                raise AppError("Lens returned an invalid patent record.")
            patent_id = f"{item.get('jurisdiction', '')}{item.get('doc_number', '')}{item.get('kind', '')}".strip()
            lens_id = str(item.get("lens_id") or "")
            title = text_at((item.get("biblio") or {}).get("invention_title"))
            abstract = text_at(item.get("abstract"))
            description = text_at(item.get("description"))
            if not patent_id or not title or not (abstract or description):
                continue
            records.setdefault(patent_id, {"id": patent_id, "title": title, "abstract": abstract,
                "text": f"{title}\n{abstract}\n{description}"[:18000],
                "url": f"https://www.lens.org/lens/patent/{lens_id}" if lens_id else "", "source": "Lens.org"})
    return list(records.values())


def require_list(data: dict[str, Any], key: str, item_type: type) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list) or not value or not all(isinstance(item, item_type) and item for item in value):
        raise AppError(f"The language model returned an invalid '{key}' field.")
    return value


def analyze(description: str) -> dict[str, Any]:
    understanding = model_json("""Read the invention description below. Produce 2-5 precise patent-search queries and 2-8 technical concepts.
Return JSON exactly shaped as {\"queries\":[string],\"concepts\":[string]}.
Description:\n""" + description)
    queries = require_list(understanding, "queries", str)
    concepts = require_list(understanding, "concepts", str)
    candidates = lens_search(queries)
    if not candidates:
        return {"concepts": concepts, "queries": queries, "patents": [], "recommendations": []}

    relevance = model_json("""Given an invention and candidate patents, decide relevance. Return JSON exactly shaped as {\"relevant\":[boolean]} in the same order as candidates.
Invention:\n""" + description + "\nCandidates:\n" + json.dumps([{k: p[k] for k in ("id", "title", "abstract")} for p in candidates]))
    keep = relevance.get("relevant")
    if not isinstance(keep, list) or len(keep) != len(candidates) or not all(isinstance(item, bool) for item in keep):
        raise AppError("The language model returned invalid relevance decisions.")
    candidates = [patent for patent, relevant in zip(candidates, keep) if relevant]
    if not candidates:
        return {"concepts": concepts, "queries": queries, "patents": [], "recommendations": []}

    vectors = embeddings([description] + [patent["text"] for patent in candidates])
    ranked = sorted(((cosine(vectors[0], vector), patent) for vector, patent in zip(vectors[1:], candidates)), reverse=True, key=lambda item: item[0])[:8]
    selected = [patent for _, patent in ranked]
    explanations = model_json("""Assess the candidate patents against the invention. Do not make an overall legal novelty conclusion. For every candidate return a concise evidence-grounded rationale and 1-4 overlap phrases. Also return 1-5 concrete drafting recommendations.
Return JSON exactly shaped as {\"matches\":[{\"id\":string,\"rationale\":string,\"key_overlap\":[string]}],\"recommendations\":[string]}.
Invention:\n""" + description + "\nCandidates:\n" + json.dumps([{k: p[k] for k in ("id", "title", "abstract", "text")} for p in selected]))
    matches = require_list(explanations, "matches", dict)
    recommendations = require_list(explanations, "recommendations", str)
    if [match.get("id") for match in matches] != [patent["id"] for patent in selected]:
        raise AppError("The language model returned explanations that do not match the candidate set.")
    patents = []
    for (score, patent), match in zip(ranked, matches):
        rationale, overlap = match.get("rationale"), match.get("key_overlap")
        if not isinstance(rationale, str) or not rationale.strip() or not isinstance(overlap, list) or not all(isinstance(x, str) and x for x in overlap):
            raise AppError("The language model returned an invalid patent explanation.")
        patents.append({"num": patent["id"], "title": patent["title"], "source": patent["source"], "url": patent["url"], "rationale": rationale, "key_overlap": overlap})
    return {"concepts": concepts, "queries": queries, "patents": patents, "recommendations": recommendations}


def database() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, description TEXT NOT NULL, payload TEXT NOT NULL)")
    return conn


def save_report(description: str, payload: dict[str, Any]) -> int:
    with database() as conn:
        cursor = conn.execute("INSERT INTO reports(created_at, description, payload) VALUES (?, ?, ?)", (datetime.now(timezone.utc).isoformat(), description, json.dumps(payload)))
        return int(cursor.lastrowid)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format % args}")

    def respond(self, status: int, payload: Any) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(encoded))); self.end_headers(); self.wfile.write(encoded)

    def do_GET(self) -> None:
        if self.path == "/":
            content = (STATIC_DIR / "index.html").read_bytes()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.send_header("Content-Length", str(len(content))); self.end_headers(); self.wfile.write(content); return
        if self.path == "/api/history":
            with database() as conn:
                rows = conn.execute("SELECT id, created_at, description, payload FROM reports ORDER BY id DESC LIMIT 50").fetchall()
            self.respond(200, [{"id": row[0], "created_at": row[1], "description": row[2], "report": json.loads(row[3])} for row in rows]); return
        self.respond(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if self.path != "/api/analyze": self.respond(404, {"error": "Not found"}); return
        try:
            length = int(self.headers.get("Content-Length", "0")); body = json.loads(self.rfile.read(length)); description = str(body.get("description") or "").strip()
            if len(description) < 20: raise AppError("Please provide an invention description of at least 20 characters.")
            report = analyze(description); report["id"] = save_report(description, report); self.respond(200, report)
        except (AppError, json.JSONDecodeError, ValueError) as error:
            self.respond(400, {"error": str(error) if isinstance(error, AppError) else "Invalid request."})
        except Exception:
            self.respond(500, {"error": "Unexpected local application error. Check the local console log."})


if __name__ == "__main__":
    print(f"Novelty Lens Portable is running at http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
