"""Deterministic preparation graph: load_inputs -> plan -> review -> start_interview.

`review` is a side-effect-free waiting node: it interrupts with the plan and, on resume, only
carries the user's decision forward. The decision of record is the one the authenticated API
wrote to SQL; `start_interview` re-reads it instead of trusting the resume payload.

S03 replaces `plan` with the planning Agent. Everything else here is meant to stay.
"""
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from interview_agent import observability
from interview_agent.interviews.contracts import CreateInterview
from interview_agent.interviews.jd_coverage import include_generated
from interview_agent.interviews.repository import InterviewRepository

PLAN_VERSION = 'prep-plan-v1'


def build_plan(frozen):
    """Deterministic v1 plan: what was frozen, what approving will do. No model call."""
    return {
        'version': PLAN_VERSION,
        'role': frozen['role'],
        'mode': frozen['mode'],
        'profile': frozen.get('profile'),
        'max_questions': frozen['max_questions'],
        'duration_seconds': frozen['duration_seconds'],
        'target': frozen['target'],
        'draft': frozen['draft'],
        'resume_item_indexes': frozen['resume_item_indexes'],
        'use_resume': frozen.get('use_resume', True),
        'selection': frozen.get('selection'),
        'frozen': {'goal_version': frozen['goal_version'], 'material_revision': frozen['material_revision']},
        'steps': ['load_inputs', 'plan', 'review', 'start_interview'],
        'on_approve': 'opens exactly one interview session; approving twice returns the same session',
        'on_reject': 'nothing is created; the preparation ends as REJECTED',
        **({'generated_question_count': 0} if frozen.get('question_basis') else {}),
        **({'selected_bank_count': len(frozen['question_banks'])} if frozen.get('question_banks') else {}),
    }


class State(TypedDict, total=False):
    prep_id: str
    frozen: dict
    plan: dict
    decision: dict
    session_id: str | None


def build(pool, repo, claim, tracer=None, planner=None):
    """Compile the graph for one attempt; DB/ownership objects are injected, not global.

    `planner` is optional: without it the `plan` node stays the deterministic v1 plan (0 yuan).
    With it the node runs the LangChain planning agent and the *server* still decides what the
    plan may contain (`normalize_plan`), so a model can never widen its own scope.
    """
    tracer = tracer if tracer is not None else observability.current()
    prep_id = claim.message.operation_id
    interviews = InterviewRepository(pool, tracer=tracer)

    async def load_inputs(state):
        row = await repo.context(claim)
        frozen = await repo.frozen_inputs(row)
        await repo.freeze(claim, frozen)
        return {'frozen': frozen}

    async def plan(state):
        frozen = state['frozen']
        coverage = frozen.get('jd_coverage') or []
        wiki = frozen.get('wiki_question')
        document = build_plan(frozen)
        if planner is not None and (not wiki or any('wiki' not in row['candidates'].values() for row in coverage)):
            from .planner import PlanningFailed, plan_from
            try:
                document = plan_from(await planner.plan(frozen), frozen)
            except PlanningFailed as error:
                if not coverage or error.code not in ('planning_no_plan', 'planning_invalid_plan'):
                    raise
                document['generation_fallback'] = error.code
        elif wiki:
            document['planner'] = 'wiki-reuse'
        if coverage:
            document['jd_coverage'] = include_generated(
                coverage, document.get('generated_questions', []), frozen['target']['kind'])
        if wiki:
            document['wiki_question_count'] = 1
            document['generated_questions'] = [wiki, *document.get('generated_questions', [])]
        return {'plan': document}

    async def review(state):
        return {'decision': interrupt({'plan': state['plan']})}

    async def start_interview(state):
        with tracer.span('preparation.start_session', stage='preparation', session_id=prep_id):
            await repo.approved_decision(claim)
            row = await repo.context(claim)
            frozen = row['frozen'] or {}
            plan = row['plan'] or {}
            banks = frozen.get('question_banks', [])
            allow_projects = frozen.get('allow_project_questions')
            if allow_projects is None and banks and row.get('profile') == 'full':
                allow_projects = True
            value = CreateInterview(
                expected_version=row['goal_version'], role=row['role'], mode=row['mode'],
                profile=row.get('profile'),
                max_questions=row['max_questions'], duration_seconds=row['duration_seconds'],
                draft_id=str(row['draft_id']) if row['draft_id'] else None,
                use_resume=bool(row['use_resume']),
                # A planner may narrow which frozen draft items to use; the server has already
                # checked the indexes against the frozen list in `normalize_plan`.
                resume_item_indexes=list((state.get('plan') or {}).get('resume_item_indexes')
                                         or row['resume_item_indexes']),
                question_banks=banks, allow_project_questions=allow_projects)
            # Stable key: a retry after a crash returns the same session instead of a second one.
            generated = plan.get('generated_questions') or []
            session_id, _created = await interviews.create_for_owner(
                claim.user_id, str(row['goal_id']), f'prep-{prep_id}', value,
                generated_questions=generated,
                generated_target_sha256=(frozen['question_basis']['target_sha256']
                                         if generated or plan.get('jd_coverage') else None),
                jd_coverage=plan.get('jd_coverage') or [])
            return {'session_id': session_id}

    graph = StateGraph(State)
    graph.add_node('load_inputs', load_inputs)
    graph.add_node('plan', plan)
    graph.add_node('review', review)
    graph.add_node('start_interview', start_interview)
    graph.add_edge(START, 'load_inputs')
    graph.add_edge('load_inputs', 'plan')
    graph.add_edge('plan', 'review')
    graph.add_edge('review', 'start_interview')
    graph.add_edge('start_interview', END)
    return graph
