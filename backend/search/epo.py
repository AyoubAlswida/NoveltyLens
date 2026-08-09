"""European Patent Office provider via the EPO Open Patent Services API."""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import xml.etree.ElementTree as ET

from config import settings
from models import PatentRecord
from tracing import trace

logger = logging.getLogger(__name__)

AUTH_URL = "https://ops.epo.org/3.2/auth/accesstoken"
SEARCH_URL = "https://ops.epo.org/3.2/rest-services/published-data/search/biblio"
PUBLISHED_DATA_URL = "https://ops.epo.org/3.2/rest-services/published-data/publication/epodoc"
NAMESPACE = {"ops": "http://ops.epo.org", "exchange": "http://www.epo.org/exchange"}
FULLTEXT_NAMESPACE = {"ftxt": "http://www.epo.org/fulltext"}


def get_access_token() -> str:
    """Authenticate with the EPO OPS API and return an access token."""
    response = requests.post(
        AUTH_URL,
        data={"grant_type": "client_credentials"},
        auth=(settings.epo_key, settings.epo_secret),
        timeout=30,
    )
    response.raise_for_status()
    trace("epo.auth.ok")

    return response.json()["access_token"]


def search_epo(query: str, size: int = 10) -> list[PatentRecord]:
    """Search the European Patent Office and return normalized PatentRecord objects."""
    trace("epo.search.request", query=query, size=size)
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/xml"}
    response = requests.get(SEARCH_URL, headers=headers, params={"q": query}, timeout=30)

    if response.status_code == 404:
        # EPO OPS uses 404 to mean "zero matches for this query," not a
        # missing resource - confirmed against the live API: the body is
        # <fault><code>SERVER.EntityNotFound</code><message>No results
        # found</message></fault>. That is a genuine empty result, the same
        # as Google returning no organic_results, not a provider failure -
        # raising here would fail the whole request over one query legitimately
        # matching nothing. Any OTHER fault code (or a body that doesn't parse
        # as this fault shape) falls through to raise_for_status() below and
        # still fails loudly, same as any other unexpected error.
        try:
            fault_code = ET.fromstring(response.content).findtext(".//ops:code", namespaces=NAMESPACE)
        except ET.ParseError:
            fault_code = None
        if fault_code == "SERVER.EntityNotFound":
            trace("epo.search.no_results", query=query)
            return []

    response.raise_for_status()
    root = ET.fromstring(response.content)

    patents = []
    for doc in root.findall(".//exchange:exchange-document", NAMESPACE)[:size]:
        # ``epodoc``-format identifier: country + doc-number, deliberately
        # WITHOUT the kind code - the published-data full-text endpoints
        # (see get_patent_full_text) return 404 for a kind-suffixed id but
        # 200 for this form. Verified live against the real API.
        country = doc.attrib.get("country", "")
        doc_number = doc.attrib.get("doc-number", "")
        publication = f"{country}{doc_number}"

        title = ""
        title_node = doc.find(".//exchange:invention-title", NAMESPACE)
        if title_node is not None and title_node.text:
            title = title_node.text.strip()

        abstract = ""
        abstract_node = doc.find(".//exchange:abstract", NAMESPACE)
        if abstract_node is not None:
            abstract = "".join(abstract_node.itertext()).strip()

        patents.append(
            PatentRecord(
                id=publication,
                title=title,
                abstract=abstract,
                source="European Patent Office",
                url=f"https://worldwide.espacenet.com/patent/search?q={publication}",
            )
        )
    trace("epo.search.response", query=query, count=len(patents), ids=[p.id for p in patents])
    return patents


def _fulltext_section_text(root: ET.Element, tag: str) -> str:
    """Extract and join every ``<p>``/``<claim-text>``-style text node under
    the named full-text section (``description`` or ``claims``)."""
    section = root.find(f".//ftxt:{tag}", FULLTEXT_NAMESPACE)
    if section is None:
        return ""
    return " ".join(text.strip() for text in section.itertext() if text.strip())


def get_patent_full_text(epo_id: str, timeout: int = 30) -> str:
    """Fetch full description + claims text for one patent via EPO OPS's
    published-data ``description`` and ``claims`` endpoints.

    ``epo_id`` must be the kind-free epodoc identifier ``search_epo`` builds
    (e.g. ``"EP4778509"``) - the kind-suffixed form 404s on this endpoint.
    """
    if not epo_id:
        return ""
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/xml"}

    description_text = ""
    try:
        response = requests.get(f"{PUBLISHED_DATA_URL}/{epo_id}/description", headers=headers, timeout=timeout)
        response.raise_for_status()
        description_text = _fulltext_section_text(ET.fromstring(response.content), "description")
    except Exception:
        logger.warning("EPO description fetch failed for %s; continuing with claims only", epo_id)
        trace("epo.description.failed", epo_id=epo_id)

    claims_text = ""
    try:
        response = requests.get(f"{PUBLISHED_DATA_URL}/{epo_id}/claims", headers=headers, timeout=timeout)
        response.raise_for_status()
        claims_text = _fulltext_section_text(ET.fromstring(response.content), "claims")
    except Exception:
        logger.warning("EPO claims fetch failed for %s; continuing with description only", epo_id)
        trace("epo.claims.failed", epo_id=epo_id)

    full_text = f"{description_text} {claims_text}".strip()
    trace("epo.details.fulltext", epo_id=epo_id, chars=len(full_text))
    return full_text


def enrich_with_full_text(patents: list[PatentRecord], max_workers: int = 8) -> list[PatentRecord]:
    """Populate ``full_text`` on each EPO-sourced patent via parallel
    published-data calls. Mirrors ``search/google.py``'s
    ``enrich_with_full_text`` - non-EPO records, or any record whose call
    fails, are left with an empty ``full_text`` so callers fall back to the
    abstract.
    """
    targets = [patent for patent in patents if patent.source == "European Patent Office" and patent.id]
    if not targets:
        return patents

    succeeded = 0
    failed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_patent = {executor.submit(get_patent_full_text, patent.id, 30): patent for patent in targets}
        for future in as_completed(future_to_patent):
            patent = future_to_patent[future]
            try:
                patent.full_text = future.result()
                if patent.full_text:
                    succeeded += 1
                else:
                    failed += 1
                    logger.warning("EPO full-text EMPTY: %s", patent.id)
            except Exception:
                failed += 1
                logger.exception("EPO full-text FAILED: %s", patent.id)

    logger.info("EPO full-text enrichment done: %d/%d patents got text (%d failed/empty)", succeeded, len(targets), failed)
    return patents
