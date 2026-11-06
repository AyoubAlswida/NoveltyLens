"""Developer-only doubles for the EPO abstract pipeline; never imported by runtime."""
import json


def response(prompt):
    if prompt.startswith('Extract independently meaningful'):
        return json.dumps({'elements':['water sterilization','membrane filtration','flow sensing']})
    if prompt.startswith('Expand '):
        data = json.loads(prompt.split('\n', 1)[1])
        if prompt.startswith('Expand the existing patent search with 2'):
            return json.dumps({'queries':[{'groups':[[phrase]],
                'rationale':'Broader mechanism recall',
                'covered_element_ids':list(data['elements'])}
                for phrase in ['filter assembly','ultraviolet vessel']]})
        return json.dumps({'queries':[]})
    if prompt.startswith('Create '):
        data=json.loads(prompt.split('\n',1)[1])
        units=list(data['elements'])
        phrases=['water sterilization','ultraviolet treatment','membrane filtration',
                 'portable water vessel','flow sensing','water treatment apparatus']
        return json.dumps({'queries':[{'groups':[[phrase]],'rationale':'Alternative mechanism terminology',
            'covered_element_ids':units} for phrase in phrases]})
    if prompt.startswith('Screen EACH'):
        data=json.loads(prompt.split('\n',1)[1])
        ids=list(data['elements'])
        rows=[]
        for p in data['patents']:
            relevant='gearbox' not in ' '.join(s['text'] for s in p['spans']).lower()
            rows.append({'num':p['num'],'relevant':relevant,'core_mechanism':False,
                'evidence':[{'span_id':p['spans'][0]['span_id'],'element_ids':ids[:1],
                    'support':'full','relationship':False}] if relevant else []})
        return json.dumps({'results':rows})
    if prompt.startswith('Suggest as many'):
        data = json.loads(prompt.split('\n', 1)[1])
        return json.dumps({'recommendations':[{
            'publication':data['findings'][0]['num'],
            'element_ids':list(data['elements']),
            'points':['Compare the disclosed mechanism.', 'Document the technical differences.'],
        }]})
    return None
