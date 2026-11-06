"""Live recall benchmark. Each case spends real EPO/LLM/embedding quota.

Run from backend after migrations. Cases are a JSON array with at least ten
unique descriptions and expected_publications (known relevant publication ids).
This measures recall@100 against labels, not exhaustive patent-search recall.
"""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    cases=json.loads(args.cases.read_text(encoding='utf-8'))
    if not isinstance(cases,list) or len(cases)<10:
        raise ValueError('Supply at least ten independently labelled invention cases')
    descriptions=set()
    for case in cases:
        description=case['description']
        labels=case['expected_publications']
        if not isinstance(description,str) or not description.strip() or description in descriptions:
            raise ValueError('Case descriptions must be nonempty and unique')
        descriptions.add(description)
        if not isinstance(labels,list) or not labels or any(not isinstance(p,str) or not p.strip() for p in labels):
            raise ValueError('Each case needs real, known relevant publication labels')
    from graph.pipeline import run_graph_pipeline
    findings=[]
    for i,case in enumerate(cases,1):
        started=time.monotonic()
        terminal=None
        for event in run_graph_pipeline(case['description'],None):
            if event['type']=='result':
                terminal=event['data']
        if terminal is None:
            raise RuntimeError('Search did not complete; no benchmark success may be reported')
        expected={p.strip().upper() for p in case['expected_publications']}
        returned={p['num'].strip().upper() for p in terminal['patents']}
        findings.append({'case':i,'seconds':time.monotonic()-started,
            'expected':sorted(expected),'found_in_top_100':sorted(expected & returned),
            'recall_at_100':len(expected & returned)/len(expected), 'coverage':terminal['coverage']})
        print(f'Completed labelled case {i}/{len(cases)}')
    args.output.write_text(json.dumps({'status':'completed','case_count':len(cases),
        'mean_recall_at_100':sum(r['recall_at_100'] for r in findings)/len(findings),
        'cases':findings},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':
    main()
