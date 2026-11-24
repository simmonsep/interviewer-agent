"""Frozen baseline for the context A/B: the model view as it was before S02A.

Copied verbatim from `backend/src/interview_agent/interviews/agent.py` at revision
`b1c657a^` (the parent of S02A). It is not imported by the product; the only caller is
`backend/tests/test_interview_context_ab.py`, which runs it and the current
`compile_interview_context` on the same frozen snapshot.

Why a copy instead of a runtime switch: the product has one view per version, and adding a
`strategy` parameter would change production behaviour to make a measurement possible. The
baseline is frozen here with its source revision so the comparison stays reproducible after
the old code leaves HEAD.
"""
from .contracts import question_coverage  # noqa: F401  (used by the frozen function below)

def scope_view(scope):
    """Bound delivered history; full frozen scope remains in SQL and the commit gate.

    F07Z-93: the bounds are sized against the provider's 32 KiB request budget. Facts and the
    current answer are deliberately NOT trimmed - they are the evidence the model must judge.
    Everything else is loose context and is shortened in a fixed order, with every cut recorded
    in `model_view_truncation` so the loss is visible instead of silent.
    """
    view={k:scope[k] for k in ('policy','facts','current_question','answer','assisted_topics')}
    view['prior_written_results']=[{k:o[k] for k in ('id','session_id','source_id','goal_version','topic_id','outcome','qualification','policy')}
                                  for o in scope.get('prior_written_results',[])]
    for item,original in zip(view['prior_written_results'],scope.get('prior_written_results',[])):
        if original.get('comparison'):item['comparison']=original['comparison']
        if original.get('application'):item['application']=original['application']
    if scope.get('material_alignment'):view['material_alignment']=scope['material_alignment']
    if scope.get('question_banks'):view['question_banks']=scope['question_banks']
    truncated=[];target=dict(scope['target'])
    for key,limit in (('text',300),('requirements',300),('title',120)):
        if isinstance(target.get(key),str) and len(target[key])>limit:
            target[key]=target[key][:limit];truncated.append('target.'+key)
    view['target']=target;history=[]
    view['application']=scope.get('application')
    for q in scope['question_history'][-6:]:
        copied=dict(q)
        if len(copied['text'])>50:copied['text']=copied['text'][:50];truncated.append('question_history.'+q['id']+'.text')
        history.append(copied)
    if len(scope['question_history'])>6:truncated.append('question_history.latest_6_only')
    memory=[]
    for o in scope['prior_observations'][-6:]:
        copied=dict(o)
        for key,limit in (('summary',90),('answer_quote',60)):
            if isinstance(copied.get(key),str) and len(copied[key])>limit:
                copied[key]=copied[key][:limit];truncated.append('prior_observations.'+copied['id']+'.'+key)
        memory.append(copied)
    if len(scope['prior_observations'])>6:truncated.append('prior_observations.latest_6_only')
    # The truncation record itself is part of the payload; keep it bounded but still readable.
    if len(truncated)>10:truncated=truncated[:10]+['truncated_more']
    view.update(question_coverage=question_coverage(scope),question_history=history,prior_observations=memory,model_view_truncation=truncated,
                original_answer_and_confirmed_statements_truncated=False)
    return view
