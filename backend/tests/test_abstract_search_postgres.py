"""Real SQL checks; unavailable PostgreSQL is a skip, never a passing check."""
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
import psycopg
from models import PatentRecord
from reports import save_report, list_history

pytestmark = pytest.mark.postgres


def require_migration(conn):
    if conn.execute("SELECT to_regclass('epo_request_clock')").fetchone()[0] is None:
        pytest.skip('Apply migration 0027 before testing abstract report persistence')


def test_abstract_snapshot_and_coverage_round_trip(postgres_url):
    with psycopg.connect(postgres_url) as conn:
        require_migration(conn)
        p=PatentRecord(id='EP999999991A1',title='Snapshot fixture',abstract='A filter coupled to an emitter.')
        match={'num':p.id,'title':p.title,'risk':'Medium','rationale':'Shared filtering mechanism.',
            'key_overlap':['filter'],'source':'EPO OPS','url':'https://example.invalid/',
            'family_id':'epo:test','family_size':0,'abstract':p.abstract,
            'evidence':[{'quote':'filter','element_ids':['L1'],'relationship':False}]}
        metadata={'analysis_basis':'abstract','analysis_version':'epo-abstract-v1',
            'coverage':{'analyzed_abstracts':1},'elements':{'L1':'filter'},'abstract_snapshots':[{'abstract':p.abstract}]}
        run_id=save_report(conn,description='abstract SQL regression',claims=None,concepts=['filter'],
            query_count=6,recommendations=['Review source.'],matches=[match],patents=[p],metadata=metadata)
        entry=next(r for r in list_history(conn) if r['id']==run_id)
        assert entry['analysis_basis']=='abstract'
        assert entry['coverage']==metadata['coverage']
        assert all(entry['patents'][0][k]==v for k,v in match.items())
        conn.rollback()


def test_parallel_clients_share_one_request_clock(postgres_url, real_db):
    from search.epo_abstracts import reserve
    with psycopg.connect(postgres_url) as conn:
        require_migration(conn)
    def claim(_):
        reserve(time.time()+60)
        return time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        stamps=sorted(pool.map(claim,range(2)))
    assert stamps[1]-stamps[0]>=7.3
