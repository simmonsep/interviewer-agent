"""An explicit scripted executor traverses the real LangGraph; no credentials or HTTP."""

from interview_agent.domain.practice import AssessmentMetadata, AssessmentOutput, build_snapshot, gate_candidate
from interview_agent.runtime.adapter import InterviewRuntimeAdapter
from interview_agent.runtime.scripted import ScriptedProvider, success_steps
from interview_agent.runtime.store import ScopedAssessmentStore
from interview_agent.contracts.operations import ASSESSMENT_WAIT_CODES


class AssessmentUnavailable(Exception):
    def __init__(self, code="model_not_enabled"):
        if code not in ASSESSMENT_WAIT_CODES:
            raise ValueError("invalid_assessment_wait_code")
        self.code = code
        super().__init__(code)


async def scripted_assessment(context, cancel):
    snapshot, run = build_snapshot(context)
    provider = ScriptedProvider(success_steps(snapshot, run))
    result = await InterviewRuntimeAdapter(ScopedAssessmentStore(snapshot), provider).run(run, cancel=cancel)
    if not result.cleanup_complete or result.envelope.termination != "candidate_ready" or result.envelope.candidate is None:
        raise ValueError("assessment_candidate_not_ready")
    return AssessmentOutput(candidate=gate_candidate(context, result.envelope.candidate),
                            metadata=AssessmentMetadata(origin="scripted_protocol", provider="pm003-scripted",
                                                        run_id=result.envelope.run_id, counts=result.envelope.counts))
