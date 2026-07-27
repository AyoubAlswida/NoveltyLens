import requests
import xml.etree.ElementTree as ET

from config import settings
from models import PatentRecord
from tracing import trace

AUTH_URL = "https://ops.epo.org/3.2/auth/accesstoken"
SEARCH_URL = "https://ops.epo.org/3.2/rest-services/published-data/search/biblio"



def get_access_token() -> str:
    """
    Authenticate with the EPO OPS API and return an access token.
    """
    if not settings.epo_key or not settings.epo_secret:
        raise RuntimeError("EPO_KEY/EPO_SECRET are not configured")

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
    """
    Search the European Patent Office and return normalized PatentRecord objects.
    """
    trace("epo.search.request", query=query, size=size)

    token = get_access_token()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/xml",
    }

    response = requests.get(
        SEARCH_URL,
        headers=headers,
        params={"q": query},
        timeout=30,
    )

    response.raise_for_status()

    root = ET.fromstring(response.content)

    patents = []

    namespace = {
        "ops": "http://ops.epo.org",
        "exchange": "http://www.epo.org/exchange",
    }

    for doc in root.findall(".//exchange:exchange-document", namespace)[:size]:

        publication = doc.attrib.get("doc-number", "")

        title = ""

        abstract = ""

        title_node = doc.find(
            ".//exchange:invention-title",
            namespace,
        )

        if title_node is not None and title_node.text:
            title = title_node.text.strip()

        abstract_node = doc.find(
            ".//exchange:abstract",
            namespace,
        )

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