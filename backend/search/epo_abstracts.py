"""OPS discovery with abstracts, process-shared throttling and bounded retries."""
from dataclasses import dataclass
import threading
import re
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import requests
import db
from config import settings
from models import PatentRecord

BASE = 'https://ops.epo.org/3.2'
NS = {'e': 'http://www.epo.org/exchange'}
OPS_NS = 'http://ops.epo.org'
DOCDB_PUBLICATION = re.compile(r'([A-Z]{2})([0-9]+)([A-Z][0-9]?)')
_lock = threading.Lock()
_token = ''
_expires = 0.0


def check_deadline(deadline):
    if time.time() >= deadline:
        raise TimeoutError('Abstract search exceeded its execution deadline; no partial report was published')


def pause(seconds, deadline):
    check_deadline(deadline)
    if time.time() + seconds >= deadline:
        raise TimeoutError('EPO wait exceeds the search execution deadline')
    time.sleep(max(0, seconds))


def reserve(deadline):
    """One shared clock for API and workers, without sleeping under a DB lock."""
    while True:
        check_deadline(deadline)
        with db.connect() as conn, conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT next_at > clock_timestamp(), GREATEST(0, EXTRACT(EPOCH FROM next_at-clock_timestamp())) FROM epo_request_clock WHERE id=1 FOR UPDATE")
            row = cur.fetchone()
            if row is None:
                raise RuntimeError('EPO request clock missing; apply database migrations')
            blocked, delay = row
            if not blocked:
                cur.execute("UPDATE epo_request_clock SET next_at=clock_timestamp()+interval '7.5 seconds' WHERE id=1")
                return
        pause(min(float(delay), 5), deadline)


def defer(seconds):
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("UPDATE epo_request_clock SET next_at=GREATEST(next_at, clock_timestamp()+(%s * interval '1 second')) WHERE id=1", (seconds,))


def token(deadline, refresh=False):
    global _token, _expires
    with _lock:
        if not refresh and _token and time.time() < _expires:
            return _token
        if not settings.epo_key or not settings.epo_secret:
            raise RuntimeError('EPO_CONSUMER_KEY and EPO_CONSUMER_SECRET are required')
        check_deadline(deadline)
        r = requests.post(BASE + '/auth/accesstoken', auth=(settings.epo_key, settings.epo_secret),
                          data={'grant_type': 'client_credentials'}, timeout=min(30, deadline-time.time()))
        if r.status_code != 200:
            raise RuntimeError(f'EPO authentication returned HTTP {r.status_code}')
        data = r.json()
        _token = data['access_token']
        _expires = time.time() + max(0, float(data['expires_in']) - 60)
        return _token


@dataclass
class Page:
    patents: list[PatentRecord]
    total: int
    received: int
    unavailable: int = 0


def parse_page(content):
    root = ET.fromstring(content)
    search = next((n for n in root.iter() if n.tag.split('}')[-1] == 'biblio-search'), None)
    if search is None:
        raise ValueError('EPO response has no bibliographic search result')
    total = int(search.attrib['total-result-count'])
    docs = root.findall('.//e:exchange-document', NS)
    records = _parse_documents(docs)
    references = [n for n in root.iter() if n.tag.split('}')[-1] == 'publication-reference']
    received = max(len(docs), len(references))
    return Page(records, total, received, received-len(records))


def _parse_documents(docs):
    records = []
    def text(node):
        return ''.join(node.itertext()).strip() if node is not None else ''
    for doc in docs:
        if doc.attrib.get('status') == 'not found':
            continue
        country, number, kind = (doc.get(k, '') for k in ('country', 'doc-number', 'kind'))
        if not country or not number or not kind:
            raise ValueError('EPO returned an incomplete publication identifier')
        pub = country + number + kind
        abstracts = doc.findall('e:abstract', NS)
        chosen = next((a for a in abstracts if a.get('lang') == 'en'), abstracts[0] if abstracts else None)
        titles = doc.findall('.//e:invention-title', NS)
        title = next((t for t in titles if t.get('lang') == 'en'), titles[0] if titles else None)
        date = doc.find('.//e:publication-reference/e:document-id/e:date', NS)
        classifications = sorted({text(n).replace(' ', '') for n in
            doc.findall('.//e:classification-cpc/e:text', NS)
            if re.fullmatch(r'[A-HY]\d{2}[A-Z]\d+/\d+', text(n).replace(' ', ''))})
        records.append(PatentRecord(id=pub, title=text(title), abstract=text(chosen),
            abstract_language=chosen.get('lang', '') if chosen is not None else '',
            classifications=classifications,
            publication_date=text(date), family_id=('epo:' + doc.get('family-id')) if doc.get('family-id') else '',
            assignee=text(doc.find('.//e:applicant/e:applicant-name/e:name', NS)),
            inventors=[text(n) for n in doc.findall('.//e:inventor/e:inventor-name/e:name', NS)],
            source='EPO OPS', url='https://worldwide.espacenet.com/patent/search?q=pn%3D' + pub))
    return records


def search_page(cql, start, deadline):
    content = _get_ops('published-data/search/abstract,biblio', deadline,
                       params={'q': cql, 'Range': f'{start}-{start+49}'})
    return Page([], 0, 0) if content is None else parse_page(content)


def family_publications(publication, deadline):
    """Fetch bibliographic records for a provider-confirmed extended family."""
    match = DOCDB_PUBLICATION.fullmatch(publication)
    if match is None:
        raise ValueError('Invalid DOCDB publication identifier for family lookup')
    docdb = '.'.join(match.groups())
    content = _get_ops(f'family/publication/docdb/{docdb}/biblio', deadline)
    if content is None:
        return []
    root = ET.fromstring(content)
    family = root.find(f'.//{{{OPS_NS}}}patent-family')
    if family is None:
        raise ValueError('EPO response has no patent family result')
    return _parse_documents(family.findall('.//e:exchange-document', NS))


def _get_ops(path, deadline, params=None):
    access = token(deadline)
    refreshed = False
    for attempt in range(4):
        reserve(deadline)
        check_deadline(deadline)
        try:
            r = requests.get(BASE + '/rest-services/' + path, params=params,
                headers={'Authorization': 'Bearer ' + access, 'Accept': 'application/xml'},
                timeout=min(45, deadline-time.time()))
        except (requests.Timeout, requests.ConnectionError):
            if attempt >= 2:
                raise RuntimeError('EPO transport failure after bounded retries') from None
            pause(2 ** attempt, deadline)
            continue
        throttle = r.headers.get('X-Throttling-Control', '').lower()
        if 'black' in throttle or 'red' in throttle:
            defer(60 if 'black' in throttle else 15)
        if r.status_code == 401 and not refreshed:
            access, refreshed = token(deadline, refresh=True), True
            continue
        if r.status_code == 404:
            root = ET.fromstring(r.content)
            code = next((n.text for n in root.iter() if n.tag.split('}')[-1] == 'code'), '')
            if code == 'SERVER.EntityNotFound':
                return None
        if r.status_code == 429 or r.status_code >= 500:
            if attempt >= (3 if r.status_code == 429 else 2):
                raise RuntimeError(f'EPO retry budget exhausted, HTTP {r.status_code}')
            retry = r.headers.get('Retry-After', '15')
            try:
                delay = float(retry)
            except ValueError:
                delay = (parsedate_to_datetime(retry)-datetime.now(timezone.utc)).total_seconds()
            defer(max(1, delay))
            continue
        if r.status_code != 200:
            raise RuntimeError(f'EPO {path.split("/")[0]} returned HTTP {r.status_code}')
        return r.content
    raise RuntimeError('EPO authentication/retry budget exhausted')
