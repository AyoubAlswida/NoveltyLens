import os
import re
import time
import html
import threading
import requests
from urllib.parse import quote

AUTH_URL = "https://ops.epo.org/3.2/auth/accesstoken"
SEARCH_URL = "https://ops.epo.org/3.2/rest-services/published-data/search/biblio"
GOOGLE_URL = "https://patents.google.com/xhr/query"
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


class PatentSearchError(Exception):
    """Raised when every patent source failed to respond for a query, so the
    caller can tell the user the search failed instead of inventing data."""

STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "with", "using", "via",
    "to", "in", "on", "is", "that", "this", "system", "method", "device",
    "based", "into", "from", "by", "your", "their",
}

_token_cache = {"access_token": None, "expires_at": 0.0}
_token_lock = threading.Lock()
# When EPO throttles us (403 / robot detection), pause EPO calls until this time
# so we stop hammering a service that has explicitly asked us to back off.
_epo_cooldown_until = 0.0


def _get_access_token():
    if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
        return _token_cache["access_token"]

    # Serialize token fetches so concurrent search threads don't all hit the auth
    # endpoint at once (which itself looks like robot behaviour to EPO).
    with _token_lock:
        if _token_cache["access_token"] and time.time() < _token_cache["expires_at"]:
            return _token_cache["access_token"]

        consumer_key = os.environ.get("EPO_CONSUMER_KEY", "")
        consumer_secret = os.environ.get("EPO_CONSUMER_SECRET", "")

        resp = requests.post(
            AUTH_URL,
            auth=(consumer_key, consumer_secret),
            data={"grant_type": "client_credentials"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        _token_cache["access_token"] = data["access_token"]
        # Refresh a minute early so we never fire a search on an about-to-expire token.
        _token_cache["expires_at"] = time.time() + int(data.get("expires_in", 1199)) - 60
        return _token_cache["access_token"]


def _build_cql(query: str) -> str:
    """Turns a free-text search phrase into a CQL full-text query, ANDing the
    meaningful keywords so we get recall similar to a search engine rather than
    an exact-phrase match."""
    words = re.findall(r"[a-zA-Z0-9]+", query.lower())
    keywords = [w for w in words if w not in STOPWORDS and len(w) > 2][:6]
    if not keywords:
        keywords = words[:3] or ["invention"]
    return " and ".join(f"txt={w}" for w in keywords)


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text_of(node):
    """OPS's XML->JSON conversion puts element text under a "$" key."""
    if isinstance(node, dict):
        return str(node.get("$", "")).strip()
    if node is None:
        return ""
    return str(node).strip()


def _pick_english(field):
    """A field like invention-title or abstract may come back as a single dict
    or a list of language variants. Prefer the English one, else take the first."""
    nodes = _as_list(field)
    for n in nodes:
        if isinstance(n, dict) and n.get("@lang") == "en":
            return n
    return nodes[0] if nodes else None


def _parse_documents(payload):
    try:
        search_result = payload["ops:world-patent-data"]["ops:biblio-search"]["ops:search-result"]
    except (KeyError, TypeError):
        return []

    results = []
    for wrapper in _as_list(search_result.get("exchange-documents")):
        # Parse each record defensively - one malformed patent shouldn't discard
        # the whole query's worth of otherwise-good results.
        try:
            doc = wrapper.get("exchange-document") or {}
            # A single <exchange-documents> block can hold several documents.
            for doc in _as_list(doc):
                biblio = doc.get("bibliographic-data") or {}

                title_node = _pick_english(biblio.get("invention-title"))
                title = _text_of(title_node)

                abstract_node = _pick_english(doc.get("abstract"))
                paragraphs = _as_list(abstract_node.get("p")) if isinstance(abstract_node, dict) else []
                abstract = " ".join(filter(None, (_text_of(p) for p in paragraphs)))

                country = doc.get("@country", "")
                doc_number = doc.get("@doc-number", "")
                if not doc_number:
                    continue

                results.append({
                    "number": doc_number,
                    "country": country,
                    "title": title or "Untitled Patent Document",
                    "abstract": abstract or "No abstract available for this record.",
                    "source": "live",
                })
        except Exception as e:
            print(f"Skipping a malformed EPO record: {e}")
            continue
    return results


def _search_once(cql: str, size: int):
    token = _get_access_token()
    return requests.get(
        SEARCH_URL,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        params={"q": cql, "Range": f"1-{size}"},
        timeout=10,
    )


def _search_epo(query: str, size: int):
    """Queries EPO Open Patent Services. Returns [] on a legitimate no-match
    (HTTP 404); raises on any hard failure (network, auth, throttle)."""
    global _epo_cooldown_until
    if time.time() < _epo_cooldown_until:
        raise RuntimeError("EPO OPS is in local cooldown after throttling")

    cql = _build_cql(query)
    print(f"Querying EPO OPS for: '{query}' -> CQL: {cql}")

    resp = _search_once(cql, size)
    if resp.status_code == 401:
        # Token may have just expired/been revoked - refresh once and retry.
        _token_cache["access_token"] = None
        resp = _search_once(cql, size)
    if resp.status_code == 404:
        print(f"No EPO matches for '{query}'.")
        return []
    if resp.status_code in (403, 429):
        # Throttling / robot detection: stop querying EPO for a while so we don't
        # dig the hole deeper (EPO can black-list the account for days).
        _epo_cooldown_until = time.time() + 900  # 15-minute local backoff
        reason = resp.headers.get("X-Rejection-Reason") or resp.headers.get("X-Throttling-Control", "throttled")
        raise RuntimeError(f"EPO OPS throttled ({reason}); pausing EPO calls for 15 min")
    resp.raise_for_status()
    return _parse_documents(resp.json())


def _clean_html(text: str) -> str:
    """Google Patents wraps matched terms in <b> tags and HTML-escapes text."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _split_pubnum(pub: str):
    """'US-10736508-B2' -> ('US', '10736508-B2')."""
    parts = (pub or "").split("-")
    if len(parts) >= 2:
        return parts[0], "-".join(parts[1:])
    return "", pub


def _search_google(query: str, size: int):
    """Queries Google Patents' public XHR endpoint (JSON). Retries once on a
    transient 503, then raises so the orchestrator can decide overall success."""
    print(f"Querying Google Patents for: '{query}'")
    url = f"{GOOGLE_URL}?url=q%3D{quote(query)}&exp="

    resp = None
    for attempt in range(2):
        resp = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=10)
        if resp.status_code == 503:
            time.sleep(2 * (attempt + 1))  # brief backoff, Google throttles bursts
            continue
        break
    resp.raise_for_status()
    data = resp.json()

    results = []
    for cluster in data.get("results", {}).get("cluster", []):
        for item in cluster.get("result", []):
            pat = item.get("patent", {})
            pub = pat.get("publication_number", "")
            if not pub:
                continue
            country, number = _split_pubnum(pub)
            results.append({
                "number": number,
                "country": country,
                "title": _clean_html(pat.get("title", "")) or "Untitled Patent Document",
                "abstract": _clean_html(pat.get("snippet", "")) or "No abstract available for this record.",
                "source": "google-live",
            })
            if len(results) >= size:
                return results
    return results


# Each source: (label, function). Add more sources here later if needed.
_SOURCES = [("EPO OPS", _search_epo), ("Google Patents", _search_google)]


def search_patents(query: str, size: int = 3):
    """Searches every configured live patent source and merges the results.
    Raises PatentSearchError only if NO source could be reached at all (a real
    outage) - a source that responds with zero matches is not a failure. There
    is no synthetic fallback: real data or an explicit failure."""
    results = []
    errors = []
    any_source_ok = False

    for label, fn in _SOURCES:
        try:
            results.extend(fn(query, size))
            any_source_ok = True
        except Exception as e:
            errors.append(f"{label}: {e}")
            print(f"{label} search failed for '{query}': {e}")

    if not any_source_ok:
        raise PatentSearchError(f"All sources failed for '{query}' -> " + " | ".join(errors))
    return results
