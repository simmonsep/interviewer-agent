import hashlib,json
from pathlib import Path
from .contracts import Task,Oracle

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read_jsonl(path,contract):
    return [contract.model_validate_json(line) for line in Path(path).read_text(encoding='utf-8').splitlines() if line.strip()]

def load_suite(directory):
    root=Path(directory);manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    for name in ('tasks.jsonl','oracles.jsonl'):
        if sha(root/name)!=manifest['sha256'][name]:raise ValueError('suite_checksum_mismatch')
    if sha(root/'baseline-spec.json')!=manifest['baseline_spec_sha256']:raise ValueError('baseline_checksum_mismatch')
    tasks=read_jsonl(root/'tasks.jsonl',Task);oracles=read_jsonl(root/'oracles.jsonl',Oracle)
    if len({t.id for t in tasks})!=len(tasks) or len({o.task_id for o in oracles})!=len(oracles):raise ValueError('duplicate_task_or_oracle')
    if {t.id for t in tasks}!={o.task_id for o in oracles}:raise ValueError('task_oracle_pair_missing')
    families={}
    for t in tasks:
        if t.family in families and families[t.family]!=t.split:raise ValueError('family_crosses_split')
        families[t.family]=t.split
    counts={s:sum(t.split==s for t in tasks) for s in ('development','reserved_candidate')}
    if len(tasks)!=manifest['tasks'] or counts!=manifest['splits']:raise ValueError('suite_count_mismatch')
    return manifest,tasks,{o.task_id:o for o in oracles}
