"""Content-bound operator review of saved outputs; no model calls or automatic approvals."""
import json, random
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4
from pydantic import Field, model_validator
from interview_agent.contracts.common import Contract
from .contracts import Attempt
from .reviews import item_hash


class OutputReview(Contract):
    sample_id: str
    attempt_sha256: str
    task_sha256: str
    oracle_sha256: str
    verdicts: Annotated[list[Literal['pending','pass','fail','unjudgeable']], Field(min_length=1)]
    reviewer: Annotated[str, Field(max_length=80)] = ''
    notes: Annotated[str, Field(max_length=1600)] = ''
    evidence_refs: list[Annotated[str, Field(min_length=1,max_length=200)]] = Field(default_factory=list)
    source: Literal['operator_attested'] = 'operator_attested'

    @model_validator(mode='after')
    def completion(self):
        if 'pending' in self.verdicts:
            if any(v!='pending' for v in self.verdicts): raise ValueError('partial_output_review')
        elif not self.reviewer.strip() or not self.notes.strip() or not self.evidence_refs:
            raise ValueError('output_review_reason_and_evidence_required')
        return self

    @property
    def decision(self):
        return next((v for v in ('pending','fail','unjudgeable') if v in self.verdicts),'pass')


def load_outputs(run_dir, manifest, tasks, oracles, *, attempt_contract=Attempt):
    run_dir=Path(run_dir)
    raw={n:json.loads((run_dir/n).read_text(encoding='utf-8')) for n in ['run.json','report.json']}
    if raw['run.json']['suite']!=manifest: raise ValueError('output_review_suite_changed')
    rows=[json.loads(x) for x in (run_dir/'attempts.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
    by_id={t.id:t for t in tasks};keys=set();bound=[]
    for row in rows:
        a=attempt_contract.model_validate(row['attempt']);key=(a.task_id,a.adapter,a.repeat)
        if key in keys or a.task_id not in by_id: raise ValueError('output_review_attempt_scope')
        keys.add(key);bound.append({'key':list(key),'attempt':a,'attempt_sha256':item_hash(row),
            'task_sha256':item_hash(by_id[a.task_id].model_dump(mode='json')),
            'oracle_sha256':item_hash(oracles[a.task_id].model_dump(mode='json'))})
    meta=raw['run.json']
    expected={(t,a,r) for t in meta['selected_ids'] for a in meta['adapters'] for r in range(1,meta['repeats']+1)}
    if keys!=expected: raise ValueError('output_review_incomplete_matrix')
    return bound,by_id,{n:item_hash(raw[n]) for n in raw}|{'attempts.jsonl':item_hash(rows)}


def export_outputs(directory,run_dir,manifest,tasks,oracles,*,attempt_contract=Attempt):
    bound,by_id,sources=load_outputs(run_dir,manifest,tasks,oracles,attempt_contract=attempt_contract)
    random.SystemRandom().shuffle(bound)
    root=Path(directory);root.mkdir(parents=True,exist_ok=False)
    mapping=[];reviews=[];guide=['# 已保存输出的逐项审核','',
        '所有评分pending，由实际审阅者填写；这不是程序或模型已完成的人工评分。',
        '填写reviews.jsonl：每条verdicts与下列rubric顺序一致，只填pass/fail/unjudgeable，另填reviewer、notes、evidence_refs（例如output.action_text）。无法判断用unjudgeable。',
        '展示顺序随机，默认不展示adapter/机器得分/费用；本地mapping.json可读，产物也可能透露流程，不能宣称密封盲审。任务/rubric审核与输出审核独立。','']
    for x in bound:
        a=x['attempt'];sid=uuid4().hex;t=by_id[a.task_id];o=oracles[a.task_id]
        hashes={k:x[k] for k in ['attempt_sha256','task_sha256','oracle_sha256']}
        mapping.append({'sample_id':sid,'key':x['key'],**hashes})
        reviews.append(OutputReview(sample_id=sid,**hashes,verdicts=['pending']*len(o.human_rubric)).model_dump(mode='json'))
        output=a.output
        shown={k:output.get(k) for k in ['decision_type','action_kind','action_text','evidence_refs','selected_evidence_assisted']}
        shown['application_evidence']=(output.get('artifacts') or {}).get('applications',{})
        if 'observed' in output:
            shown.update(observed=output['observed'], guards=output.get('guards'),
                         artifacts=output.get('artifacts'), api_requests=output.get('api_requests'))
        guide.extend([f'## {sid}','', '```json',json.dumps({'public_input':t.input.model_dump(mode='json'),
            'execution_status':a.status,'error_code':a.error_code,'output':shown,'rubric':o.human_rubric},ensure_ascii=False,indent=2),'```',''])
    (root/'mapping.json').write_bytes((json.dumps({'version':1,'suite':manifest['version'],'sources':sources,'samples':mapping},ensure_ascii=False,indent=2)+'\n').encode())
    (root/'reviews.jsonl').write_bytes(('\n'.join(json.dumps(r,ensure_ascii=False) for r in reviews)+'\n').encode())
    (root/'guide.md').write_bytes(('\n'.join(guide)+'\n').encode())
    return {'exported':len(reviews),'pending':len(reviews),'human_identity_verified':False,'domain_success_rate':None}


def validate_outputs(directory,run_dir,manifest,tasks,oracles,*,attempt_contract=Attempt):
    root=Path(directory);bound,_,sources=load_outputs(run_dir,manifest,tasks,oracles,attempt_contract=attempt_contract)
    saved=json.loads((root/'mapping.json').read_text(encoding='utf-8'))
    if saved['version']!=1 or saved['suite']!=manifest['version'] or saved['sources']!=sources:
        raise ValueError('output_review_sources_changed')
    lookup={tuple(x['key']):x for x in bound};samples={x['sample_id']:x for x in saved['samples']}
    if len(saved['samples'])!=len(samples) or len(samples)!=len(bound) or {tuple(x['key']) for x in samples.values()}!=set(lookup):raise ValueError('output_review_mapping_changed')
    rows=[OutputReview.model_validate_json(x) for x in (root/'reviews.jsonl').read_text(encoding='utf-8').splitlines() if x.strip()]
    if len(rows)!=len(samples) or len({r.sample_id for r in rows})!=len(rows):raise ValueError('output_review_missing_or_duplicate')
    groups={};counts={v:0 for v in ['pending','pass','fail','unjudgeable']}
    for r in rows:
        if r.sample_id not in samples:raise ValueError('output_review_sample_changed')
        mapping=samples[r.sample_id];original=lookup[tuple(mapping['key'])]
        if any(getattr(r,k)!=original[k] or mapping[k]!=original[k] for k in ['attempt_sha256','task_sha256','oracle_sha256']):raise ValueError('output_review_content_changed')
        if len(r.verdicts)!=len(oracles[mapping['key'][0]].human_rubric):raise ValueError('output_review_rubric_count')
        decision=r.decision;counts[decision]+=1;adapter=mapping['key'][1]
        group=groups.setdefault(adapter,{v:0 for v in counts});group[decision]+=1
    return {'items':len(rows),'decisions':counts,'by_adapter':groups,'human_identity_verified':False,
        'source':'operator_attested','task_rubric_approval':'separate','domain_success_rate':None}
