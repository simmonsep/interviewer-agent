"""Operator-attested review overlay; export never fabricates human approval."""
import hashlib,json
from pathlib import Path
from typing import Annotated,Literal
from pydantic import Field,model_validator
from interview_agent.contracts.common import Contract

def item_hash(value):return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

class Review(Contract):
    task_id:str
    suite_version:str
    task_sha256:str
    oracle_sha256:str
    decision:Literal['pending','approve','reject']='pending'
    reviewer:Annotated[str,Field(max_length=80)]=''
    notes:Annotated[str,Field(max_length=1200)]=''
    source:Literal['operator_attested']='operator_attested'

    @model_validator(mode='after')
    def attestation(self):
        if self.decision!='pending' and (not self.reviewer.strip() or not self.notes.strip()):raise ValueError('reviewer_and_reason_required')
        return self

def export_reviews(directory,manifest,tasks,oracles):
    root=Path(directory);root.mkdir(parents=True,exist_ok=False);rows=[];sections=['# 任务与rubric人工审核包','',
        '本包由程序导出，所有状态pending。请由操作者逐条审核公共输入、初始状态、允许结果与rubric；填写reviewer/decision/notes。程序不能证明填写者是人，记录仅为operator_attested。',
        '审核任务正确性不等于已审核模型输出；不要把本包提交为真实用户或模型效果证据。','']
    for t in tasks:
        o=oracles[t.id];row=Review(task_id=t.id,suite_version=manifest['version'],task_sha256=item_hash(t.model_dump(mode='json')),oracle_sha256=item_hash(o.model_dump(mode='json')))
        rows.append(row.model_dump(mode='json'))
        sections.extend([f'## {t.id} / {t.split} / {t.family}','', '```json',json.dumps({'public_input':t.input.model_dump(mode='json'),'proposed_oracle':o.model_dump(mode='json')},ensure_ascii=False,indent=2),'```','',
            '检查：结果是否合理且允许多条合理路径；是否把未知当冲突/成功；是否需额外引用或辅助标记；分组是否可能泄漏。',''])
    (root/'reviews.jsonl').write_bytes(('\n'.join(json.dumps(r,ensure_ascii=False) for r in rows)+'\n').encode())
    (root/'review-guide.md').write_bytes(('\n'.join(sections)+'\n').encode())
    return {'exported':len(rows),'approved':0,'human_identity_verified':False}

def validate_reviews(path,manifest,tasks,oracles,*,selected_ids=None):
    rows=[Review.model_validate_json(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]
    by_id={t.id:t for t in tasks}
    if len({r.task_id for r in rows})!=len(rows):raise ValueError('duplicate_review')
    for r in rows:
        if r.task_id not in by_id or r.suite_version!=manifest['version']:raise ValueError('review_scope_mismatch')
        if r.task_sha256!=item_hash(by_id[r.task_id].model_dump(mode='json')) or r.oracle_sha256!=item_hash(oracles[r.task_id].model_dump(mode='json')):raise ValueError('review_content_changed')
    scope=set(by_id) if selected_ids is None else set(selected_ids)
    if not scope.issubset(by_id):raise ValueError('review_selected_scope_mismatch')
    selected=[r for r in rows if r.task_id in scope]
    return {'selected_total':len(scope),'reviewed_items':len(selected),'missing_items':len(scope)-len(selected),
        'outside_selected_items':len(rows)-len(selected),
        'decisions':{d:sum(r.decision==d for r in selected) for d in ('pending','approve','reject')},
        'human_identity_verified':False,'source':'operator_attested','output_semantic_review':'not_performed_by_this_overlay'}
