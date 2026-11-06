"""Hermetic tests of the new retrieval and evidence contract."""
import json
import time
from unittest.mock import patch
import pytest
from models import PatentRecord
from graph import abstract_search as a
from search import epo_abstracts as epo


def record(i=1, family=''):
    return PatentRecord(id=f'EP{i}A1', title='Bottle', abstract='A filter coupled to a UV emitter sterilizes water.', family_id=family)


def response(p):
    return {'num':p.id, 'relevant':True, 'core_mechanism':True,
        'evidence':[{'span_id':'A1',
        'element_ids':['L1','L2'], 'support':'full', 'relationship':True}]}


def test_cql_is_compiled_from_literals():
    assert a.compile_query([['filter','membrane'],['UV emitter']]) == '(ta="filter" or ta="membrane") and (ta="UV emitter")'


def test_quick_search_uses_added_requirements_and_excludes_bare_absence():
    with patch.object(a, 'emit_progress'), patch.object(a, 'ask', return_value=['valve closes when pressure rises']) as ask:
        state = a.understand_node({'description':'A pressure controller.',
                                   'claims':'A valve closes when pressure rises.'})
    prompt = ask.call_args.args[0]
    assert 'bare absence or negation' in prompt
    assert 'A valve closes when pressure rises.' in prompt
    assert state['invention_text'] == 'A pressure controller.\n\nA valve closes when pressure rises.'
    assert state['similarity_units'] == ['valve closes when pressure rises']


def test_report_fills_to_100_screened_publications_with_related_first():
    rows = [
        {'num':f'EP{i}A1', 'family_id':f'family-{i}', 'relevant':i < 18,
         'core_mechanism':False, 'risk':'Low', 'evidence':[], 'covered_element_ids':[],
         'rationale':'The analysis found no mention in the abstract of the reviewed elements.'}
        for i in range(120)
    ]
    selected, related = a._select_report_matches(rows)
    assert related == 18
    assert len(selected) == 100
    assert [row['num'] for row in selected[:18]] == [row['num'] for row in rows[:18]]
    assert selected[-1]['rationale'].startswith('The analysis found no mention')


def test_related_publications_in_one_family_are_all_retained_in_both_modes():
    rows = [
        {'num':f'EP{i}A1', 'family_id':'shared-family', 'relevant':True,
         'core_mechanism':False, 'risk':'Medium', 'evidence':[],
         'covered_element_ids':['L1'], 'rationale':f'Evidence for publication {i}.'}
        for i in (1, 2)
    ]
    selected, related = a._select_report_matches(rows)
    assert related == len(selected) == 2
    assert [row['num'] for row in selected] == ['EP1A1', 'EP2A1']
    assert selected[0]['rationale'] != selected[1]['rationale']


@pytest.mark.parametrize('groups', [[], [['bad"term']], [['bad\\term']], [['']], ['not a list']])
def test_query_injection_and_empty_queries_fail(groups):
    with pytest.raises(ValueError):
        a.compile_query(groups)


def test_initial_plan_must_include_broad_recall_angles():
    rows=[{'groups':[[f'domain{i}'],['mechanism'],['extra']], 'rationale':'angle',
           'covered_element_ids':['L1']} for i in range(6)]
    with patch('llm._llm_chat',return_value=json.dumps({'queries':rows})):
        with pytest.raises(ValueError,match='two initial recall queries'):
            a.plan('description',['element'],time.time()+60)


def test_low_yield_expansion_receives_counts_and_rejects_more_constraints():
    rows=[{'groups':[['domain'],['mechanism'],['extra']], 'rationale':'angle',
           'covered_element_ids':['L1']},
          {'groups':[['domain synonym']], 'rationale':'recall',
           'covered_element_ids':['L1']}]
    with patch('llm._llm_chat',return_value=json.dumps({'queries':rows})) as call:
        with pytest.raises(ValueError,match='single-group recall'):
            a.plan('description',['element'],time.time()+60,previous=[],samples=[],
                   retrieval_feedback=[{'query_id':'Q1','received':0}],retrieved_count=0)
        prompt=call.call_args.args[0]
        assert 'retrieval_outcomes' in prompt and '"received": 0' in prompt
        assert 'retrieved_unique_count": 0' in prompt


def test_exact_evidence_and_risk():
    p = record()
    rows = a.validate_screen({'results':[response(p)]},[p],['filter','emitter'])
    assert rows[0]['risk']=='High'
    assert rows[0]['abstract']==p.abstract
    evidence=rows[0]['evidence'][0]
    assert evidence['quote']==p.abstract[evidence['start']:evidence['end']]


def test_unknown_source_span_is_rejected():
    p=record(); row=response(p)
    row['evidence'][0]['span_id']='A999'
    with pytest.raises(ValueError,match='select a span'):
        a.validate_screen({'results':[row]},[p],['filter','emitter'])


def test_spans_preserve_exact_source_ranges_including_unicode():
    source='A first sentence.\nA filter   coupled to an emitter. 中文摘要。 The final sentence.'
    spans=a.abstract_spans(source,max_chars=30)
    assert len(spans)>1
    for span in spans:
        assert span['text']==source[span['start']:span['end']]
        assert len(span['text'])<=30
    assert ''.join(''.join(s['text'].split()) for s in spans)==''.join(source.split())


@pytest.mark.parametrize('mutation', ['quote','id','duplicate','missing','relationship','grade','conflict'])
def test_evidence_failures_are_not_silently_passed(mutation):
    p=record()
    row=response(p)
    if mutation=='quote': row['evidence'][0]['quote']='invented text'
    if mutation=='id': row['evidence'][0]['element_ids']=['L99']
    if mutation=='duplicate': row['evidence'][0]['element_ids']=['L1','L1']
    if mutation=='missing': row['evidence']=[]
    if mutation=='relationship': row['evidence'][0]['relationship']='yes'
    if mutation=='grade': row['evidence'][0]['support']='unverified'
    if mutation=='conflict': row['evidence'].append({'span_id':'A1', 'element_ids':['L1'],
        'support':'partial', 'relationship':False})
    with pytest.raises(ValueError): a.validate_screen({'results':[row]},[p],['filter','emitter'])


def test_partial_overlap_does_not_become_high():
    p=record(); row=response(p)
    assert a.validate_screen({'results':[row]},[p],['filter','emitter','battery','sensor'])[0]['risk']=='Medium'


def test_explanation_uses_only_validated_elements_and_distinguishes_partial_support():
    p = PatentRecord(id='EP1A1', title='Panel', abstract='A roof panel uses a spring-loaded hinge.')
    row = {'num':p.id, 'relevant':True, 'core_mechanism':False,
           'overlap_summary':'The panel contains a battery and lacks a display.',
           'evidence':[{'span_id':'A1', 'element_ids':['L1'], 'support':'full', 'relationship':True},
                       {'span_id':'A1', 'element_ids':['L2'], 'support':'partial', 'relationship':True}]}
    match, = a.validate_screen({'results':[row]}, [p],
                               ['roof panel', 'hinge with a locking pin', 'rain sensor'])
    assert match['covered_element_ids'] == ['L1']
    assert match['partial_element_ids'] == ['L2']
    assert match['not_mentioned_element_ids'] == ['L3']
    assert match['risk'] == 'Low'
    assert match['key_overlap'] == ['roof panel']
    assert not any(e['relationship'] for e in match['evidence'])
    assert 'partial evidence for “hinge with a locking pin”' in match['rationale']
    assert 'no mention in the abstract of “rain sensor”' in match['rationale']
    assert 'battery' not in match['rationale'] and 'display' not in match['rationale']


def test_missing_result_fails():
    with pytest.raises(ValueError): a.validate_screen({'results':[]},[record()],['filter'])


def test_parser_preserves_kind_language_and_family():
    xml=b'''<world xmlns="http://ops.epo.org" xmlns:e="http://www.epo.org/exchange"><biblio-search total-result-count="1"><e:exchange-document country="US" doc-number="123" kind="A1" family-id="42"><e:bibliographic-data><e:invention-title lang="en">Filter</e:invention-title></e:bibliographic-data><e:abstract lang="de"><e:p>Deutsch</e:p></e:abstract><e:abstract lang="en"><e:p>A filter.</e:p></e:abstract></e:exchange-document></biblio-search></world>'''
    page=epo.parse_page(xml)
    assert page.received==1
    assert page.patents[0].id=='US123A1'
    assert page.patents[0].abstract=='A filter.'
    assert page.patents[0].family_id=='epo:42'


def test_family_lookup_keeps_each_members_simple_family_and_abstract():
    xml = b'''<ops:world-patent-data xmlns:ops="http://ops.epo.org" xmlns:e="http://www.epo.org/exchange">
      <ops:patent-family>
        <ops:family-member><e:exchange-document country="CA" doc-number="2971299" kind="C" family-id="42">
          <e:abstract lang="en"><e:p>First abstract.</e:p></e:abstract>
        </e:exchange-document></ops:family-member>
        <ops:family-member><e:exchange-document country="AU" doc-number="2017204150" kind="A1" family-id="43">
          <e:abstract lang="en"><e:p>Second abstract.</e:p></e:abstract>
        </e:exchange-document></ops:family-member>
      </ops:patent-family>
    </ops:world-patent-data>'''
    deadline = time.time() + 60
    with patch.object(epo, '_get_ops', return_value=xml) as fetch:
        members = epo.family_publications('CA2971299A1', deadline)
    fetch.assert_called_once_with('family/publication/docdb/CA.2971299.A1/biblio', deadline)
    assert [(p.id, p.family_id, p.abstract) for p in members] == [
        ('CA2971299C', 'epo:42', 'First abstract.'),
        ('AU2017204150A1', 'epo:43', 'Second abstract.'),
    ]


def test_pagination_budget_and_all_unique_abstracts():
    queries=[{'query_id':f'Q{i}', 'query_text':str(i)} for i in range(12)]
    calls=[]
    def fetch(cql,start,deadline):
        calls.append((cql,start))
        patents=[record(int(cql)*100+start+i) for i in range(50)]
        return epo.Page(patents,10000,50)
    state={'retrieval_plan':queries, 'invention_text':'water filter', 'similarity_units':['filter'], 'execution_deadline':time.time()+1800}
    with patch.object(a,'search_page',fetch), patch.object(a,'plan',return_value=[]), patch.object(a,'emit_progress'):
        found=a.discover_node(state)
    assert len(calls)==20
    assert all(start==1 for _,start in calls[:12])
    assert found['coverage']['received']==1000
    assert found['coverage']['analyzed_abstracts']==1000
    assert found['coverage']['budget_exhausted'] is True


def test_analysis_batches_every_abstract_and_keeps_related_family_members():
    patents=[record(i, 'one-family' if i<3 else '') for i in range(1,126)]
    state={'candidates':patents,'similarity_units':['filter','emitter'],
        'execution_deadline':time.time()+1800,'invention_text':'water filter', 'coverage':{}}
    screened=[]
    def ask(prompt,validator,deadline):
        if prompt.startswith('Suggest'):
            findings = json.loads(prompt.split('\n', 1)[1])['findings']
            assert [row['num'] for row in findings] == [patent.id for patent in patents[:20]]
            with pytest.raises(ValueError, match='supplied related publication'):
                validator({'recommendations':[{'publication':'UNKNOWN', 'points':['Compare mechanisms.']}]})
            return validator({'recommendations':[{'publication':findings[0]['num'], 'element_ids':list(json.loads(prompt.split('\n', 1)[1])['elements']),
                                                  'points':['Compare the filter arrangement in the claims.']}]})
        data=json.loads(prompt.split('\n',1)[1]); rows=[]
        for p in data['patents']:
            screened.append(p['num'])
            if p['num'] in related_ids:
                rows.append(response(next(x for x in patents if x.id == p['num'])))
            else:
                rows.append({'num':p['num'], 'relevant':False, 'core_mechanism':False, 'evidence':[]})
        return validator({'results':rows})
    related_ids = {patent.id for patent in patents[:20]}
    with patch.object(a,'ask',ask),patch.object(a,'emit_progress'):
        result=a.explain_node(state)
    assert len(screened)==125 and len(set(screened))==125
    assert len(result['abstract_matches'])==100
    assert result['abstract_recommendations'][0].startswith('EP1A1. ')
    assert result['abstract_recommendations'][-1] == (
        'The report shows 20 publications identified as related to your search. '
        '80 additional screened candidates are included for further review.'
    )
    assert len([r for r in result['abstract_matches'] if r['family_id']=='one-family'])==2
    assert result['coverage']['returned_families']==99


def test_execution_deadline_fails_instead_of_partial_success():
    with pytest.raises(TimeoutError): epo.check_deadline(time.time()-1)


@pytest.mark.parametrize('status', [400, 403, 404])
def test_ops_errors_are_not_empty_searches(status):
    from unittest.mock import Mock
    reply=Mock(status_code=status, headers={}, content=b'<fault><code>OTHER</code></fault>')
    with patch.object(epo,'token',return_value='test'), patch.object(epo,'reserve'), patch.object(epo.requests,'get',return_value=reply) as call:
        with pytest.raises(RuntimeError): epo.search_page('ta=filter',1,time.time()+60)
        assert call.call_count==1


def test_ops_only_known_empty_fault_is_empty():
    from unittest.mock import Mock
    reply=Mock(status_code=404,headers={},content=b'<fault><code>SERVER.EntityNotFound</code></fault>')
    with patch.object(epo,'token',return_value='test'),patch.object(epo,'reserve'),patch.object(epo.requests,'get',return_value=reply):
        assert epo.search_page('ta=filter',1,time.time()+60).patents==[]


def test_ops_retries_same_page_and_honors_shared_backoff():
    from unittest.mock import Mock
    retry=Mock(status_code=429,headers={'Retry-After':'20'})
    success=Mock(status_code=200,headers={},content=b'<unused/>')
    with patch.object(epo,'token',return_value='test'),patch.object(epo,'reserve'),patch.object(epo,'defer') as defer,patch.object(epo,'parse_page',return_value=epo.Page([],0,0)),patch.object(epo.requests,'get',side_effect=[retry,success]) as call:
        epo.search_page('ta=filter',51,time.time()+60)
        defer.assert_called_once_with(20)
        assert call.call_args_list[0].kwargs['params']==call.call_args_list[1].kwargs['params']


def test_old_advanced_run_must_restart():
    from graph.adapter import initial_input
    with pytest.raises(ValueError,match='start a new search'):
        initial_input({'retrieval_version':'legacy'})


def test_empty_search_report_has_no_risk_or_recommendations():
    state={'description':'invention','analysis':a.InventionAnalysis(concepts=['filter']),
        'similarity_units':['filter'],'execution_deadline':time.time()+30,'coverage':{},'queries':['q']}
    data=a.result_data(state)
    assert data['patents']==[] and data['recommendations']==[]
    assert data['analysis_basis']=='abstract'


def test_model_validation_reasks_once_then_raises():
    with patch('llm._llm_chat',return_value='{}') as call:
        with pytest.raises(ValueError): a.ask('prompt',lambda d:d['required'],time.time()+60)
    assert call.call_count==2


def test_graph_end_to_end_no_fulltext_calls(report_store):
    from graph.pipeline import run_graph_pipeline
    queries=[{'query_id':f'Q{i}', 'query_text':str(i)} for i in range(6)]
    seed = record(1, 'epo:42')
    sibling = record(2, 'epo:42')
    def ask(prompt,validator,deadline):
        if prompt.startswith('Extract'): return validator({'elements':['filter','emitter']})
        if prompt.startswith('Suggest'):
            findings = json.loads(prompt.split('\n', 1)[1])['findings']
            return validator({'recommendations':[{'publication':findings[0]['num'], 'element_ids':list(json.loads(prompt.split('\n', 1)[1])['elements']),
                                                  'points':['Verify the filter arrangement in the claims.',
                                                            'Document confirmed differences.']}]})
        data=json.loads(prompt.split('\n',1)[1])
        return validator({'results':[response(p) for p in (seed, sibling)
                                     if any(row['num'] == p.id for row in data['patents'])]})
    with (
        patch.object(a, 'ask', ask),
        patch.object(a, 'plan', side_effect=[queries, []]),
        patch.object(a, 'search_page', return_value=epo.Page([seed], 1, 1)),
        patch.object(a, 'family_publications', return_value=[seed, sibling]) as family,
        patch('graph.searching.enrich_epo_full_text') as full,
    ):
        events=list(run_graph_pipeline('water filter and UV emitter',None))
    full.assert_not_called()
    family.assert_called_once()
    terminal=events[-1]
    assert terminal['type']=='result'
    assert terminal['data']['patents'][0]['risk']=='High'
    assert terminal['data']['coverage']['analyzed_abstracts']==2
    assert terminal['data']['recommendations'][0].splitlines()[-1] == 'Document confirmed differences.'
    assert terminal['data']['recommendations'][-1] == (
        'The report shows 2 publications identified as related to your search.'
    )
    assert terminal['data']['coverage']['returned_families']==1
    assert {patent['num'] for patent in terminal['data']['patents']} == {seed.id, sibling.id}
    metadata = report_store.runs[-1]['metadata']
    assert metadata['analysis_basis']=='abstract'
    assert metadata['family_retrieval_audit'][0]['added'] == [sibling.id]
