"""Claim-bound live assessment; the Graph remains candidate-only and SQL owns commit."""

import httpx

from interview_agent.billing.config import load_key
from interview_agent.billing.ledger import BillingLedger
from interview_agent.domain.practice import AssessmentMetadata, AssessmentOutput, build_snapshot, gate_candidate
from interview_agent.runtime.adapter import InterviewRuntimeAdapter
from interview_agent.runtime.store import ScopedAssessmentStore
from .assessment import AssessmentUnavailable
from interview_agent.contracts.operations import ASSESSMENT_WAIT_CODES



async def live_assessment(repository, claim, context, cancel, *, policy, authorization_id, credentials, inner=None):
    try:
        key = load_key(credentials)
    except ValueError:
        raise AssessmentUnavailable("model_credentials_unavailable") from None
    # Keep the SDK and its dependencies out of disabled/scripted worker startup.
    from interview_agent.runtime.deepseek import BudgetedTransport, DeepSeekProvider
    ledger = BillingLedger(repository.pool, policy, authorization_id)
    transport = BudgetedTransport(inner if inner is not None else httpx.AsyncHTTPTransport(retries=0), ledger, claim, context)
    provider = DeepSeekProvider(key, transport)
    snapshot, run = build_snapshot(context)
    run = run.model_copy(update={"model_profile": policy.profile})
    try:
        result = await InterviewRuntimeAdapter(ScopedAssessmentStore(snapshot), provider, cleanup_grace_s=1).run(run, cancel=cancel)
    finally:
        await provider.aclose()
    if transport.failure_code in ASSESSMENT_WAIT_CODES:
        raise AssessmentUnavailable(transport.failure_code)
    summary = await ledger.summary(context.operation_id)
    if summary["usage_status"] == "unknown":
        raise AssessmentUnavailable("model_usage_unknown")
    if not result.cleanup_complete or result.envelope.termination != "candidate_ready" or result.envelope.candidate is None:
        raise ValueError("live_assessment_candidate_not_ready")
    return AssessmentOutput(candidate=gate_candidate(context, result.envelope.candidate), metadata=AssessmentMetadata(
        origin="real_model", provider=policy.model, authorization_id=authorization_id,
        http_attempts=summary["http_attempts"], accounted_upper_nano=summary["accounted_upper_nano"],
        run_id=result.envelope.run_id, counts=result.envelope.counts))
