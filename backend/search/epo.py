"""European Patent Office provider via the EPO Open Patent Services API."""

import requests
import xml.etree.ElementTree as ET

from config import settings
from models import PatentRecord

AUTH_URL = "https://ops.epo.org/3.2/auth/accesstoken"
SEARCH_URL = "https://ops.epo.org/3.2/rest-services/published-data/search/biblio"
NAMESPACE = {"ops": "http://ops.epo.org", "exchange": "http://www.epo.org/exchange"}


def get_access_token() -> str:
    """Authenticate with the EPO OPS API and return an access token."""
    response = requests.post(
        AUTH_URL,
        data={"grant_type": "client_credentials"},
        auth=(settings.epo_key, settings.epo_secret),
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def search_epo(query: str, size: int = 10) -> list[PatentRecord]:
    """Search the European Patent Office and return normalized PatentRecord objects."""
    token = get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/xml"}
    response = requests.get(SEARCH_URL, headers=headers, params={"q": query}, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.content)

    patents = []
    for doc in root.findall(".//exchange:exchange-document", NAMESPACE)[:size]:
        publication = doc.attrib.get("doc-number", "")

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
    return patents
