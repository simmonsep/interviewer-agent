"""Live planner wiring: LangChain agent + our own budgeted transport (S03b).

The agent's chat model is the *same* `ChatDeepSeek` instance the interview role uses, whose httpx
client is our `BudgetedTransport`; every planning request therefore goes through the ledger
before a byte leaves the process. There is no second SDK client and no direct provider call here.
"""
from __future__ import annotations

import json
from pathlib import Path

from langchain_core.messages import HumanMessage

from interview_agent.billing.config import load_key
from interview_agent.billing.ledger import BillingUnavailable
from interview_agent.runtime.deepseek import BudgetedTransport, DeepSeekProvider

from .billing import MAX_PREPARATION_REQUESTS, PreparationLedger
from .planning import (PLANNER_VERSION, SYSTEM_PROMPT, PlanningGuard, build_planner,
                       normalize_plan)


class PlanningFailed(Exception):
    """Stable failure code for a planning attempt that produced nothing usable."""

    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail or {}
        super().__init__(code)


def _plan_object(text):
    """Pull one JSON object out of the last message, tolerating a fenced code block.

    `structured=False` asks for a bare JSON object, but the model sometimes wraps it in
    ```json ... ``` anyway. Rejecting that costs a whole paid retry, so the fence is
    stripped locally instead of being reported as `planning_no_plan`.
    """
    if not isinstance(text, str):
        return None
    value = text.strip()
    if value.startswith("```"):
        body = value.split("\n", 1)[1] if "\n" in value else ""
        value = body.rsplit("```", 1)[0].strip()
    start = value.find("{")
    if start < 0:
        return None
    try:
        parsed, _end = json.JSONDecoder().raw_decode(value[start:])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract(state, guard):
    """Prefer the framework's structured response; fall back to a JSON object in the last text."""
    value = state.get("structured_response")
    if value is not None:
        return value
    for message in reversed(state.get("messages") or []):
        parsed = _plan_object(getattr(message, "content", None))
        if parsed is not None:
            return parsed
    raise PlanningFailed("planning_no_plan", {"model_calls": guard.model_calls,
                                              "rejections": list(guard.rejections)})


def _invalid_fields(error):
    """Field paths of a rejected plan. Paths only: the values may hold user material."""
    fields = []
    for item in getattr(error, "errors", lambda: [])():
        location = ".".join(str(part) for part in (item.get("loc") or ()))
        if location:
            fields.append(location)
    return fields[:6]


class LivePlanner:
    def __init__(self, pool, repository, claim, policy, authorization, credentials,
                 *, inner_factory=None, tracer=None, reference_lookup=None, experience_lookup=None):
        self.pool, self.repository, self.claim = pool, repository, claim
        self.policy, self.authorization, self.credentials = policy, authorization, credentials
        self.inner_factory, self.tracer = inner_factory, tracer
        self.reference_lookup = reference_lookup
        self.experience_lookup = experience_lookup
        self.provider = None

    def _key(self):
        if self.credentials == "vault":
            from interview_agent.desktop import key_vault
            raw = key_vault.load()
            if not raw:
                raise BillingUnavailable("model_credentials_unavailable")
            return raw
        try:
            # `--credentials` arrives as a string from argparse; `load_key` wants a Path and
            # raises AttributeError on a str (that was the live run's `planning_crashed`).
            return load_key(Path(self.credentials)).get_secret_value()
        except (ValueError, TypeError, AttributeError):
            raise BillingUnavailable("model_credentials_unavailable") from None

    async def plan(self, frozen):
        import httpx

        from .mcp_client import McpCatalogClient, McpCatalogError

        key = self._key()
        ledger = PreparationLedger(self.pool, self.policy, self.authorization, self.repository)
        transport = BudgetedTransport(self.inner_factory() if self.inner_factory
                                      else httpx.AsyncHTTPTransport(retries=0),
                                      ledger, self.claim, frozen, tracer=self.tracer)
        self.provider = DeepSeekProvider(key, transport, system_prompt=SYSTEM_PROMPT, tracer=self.tracer)
        # The frozen role is the only role the catalog may be asked about. A snapshot without a
        # role (unit-level ledger probes) keeps the old in-process topic list, not a silent MCP
        # spawn; the real preparation row always carries one.
        catalog = McpCatalogClient(frozen['role']) if frozen.get('role') else None
        try:
            if catalog is not None:
                try:
                    await catalog.__aenter__()
                    # Preflight: validate the catalog payload here, before the first paid
                    # request. A wrong version, bad schema or broken response then costs
                    # 0 model calls instead of burning the turn budget and failing after.
                    await catalog.list_topics()
                except McpCatalogError as error:
                    raise PlanningFailed('mcp_catalog_unavailable',
                                         {'mcp_code': error.code}) from None
            # `structured=False`: this endpoint rejects the `response_format` field (HTTP 400).
            # `max_model_calls` is the same fuse the ledger enforces, so the last permitted
            # request is forced to answer instead of being refused (S03c3).
            references = (self.reference_lookup if frozen.get('question_basis')
                          and frozen.get('role') == 'ai-application' else None)
            experiences = self.experience_lookup if frozen.get('question_basis') else None

            async def read_experiences(scope, max_chars):
                return await experiences.lookup(scope, lambda: self.repository.admit_experience_search(self.claim),
                                                max_chars=max_chars)

            agent, guard = build_planner(
                self.provider.model, frozen, structured=False,
                middleware=PlanningGuard(max_model_calls=MAX_PREPARATION_REQUESTS),
                catalog=catalog, reference_lookup=references,
                read_experiences=read_experiences if experiences is not None else None)
            state = await agent.ainvoke({"messages": [HumanMessage(content="按当前准备范围给出计划。")]})
            if guard.catalog_failures:
                # The tool reported a stable MCP code; do not turn a broken catalog into a plan
                # that silently lacks topics.
                raise PlanningFailed('mcp_catalog_unavailable',
                                     {'mcp_code': guard.catalog_failures[0]})
            proposed = _extract(state, guard)
            try:
                plan = normalize_plan(proposed, frozen)
            except ValueError as error:
                # A wrong shape is a planning outcome ("the model answered, but not the
                # contract"), not a crash: keep a stable code and the field paths only.
                raise PlanningFailed("planning_invalid_plan",
                                     {"fields": _invalid_fields(error)}) from None
            plan._reference_lookup = references
            plan._experience_lookup = experiences
            self._record(plan, guard)
            return plan
        finally:
            if catalog is not None:
                await catalog.aclose()
            await self.provider.aclose()

    def _record(self, plan, guard):
        if self.tracer is None:
            return
        self.tracer.record('planning.decided', stage='planning', provider='real_model',
                           round_index=guard.model_calls, tool_count=len(guard.rejections),
                           topic_id=(plan.focus_topic_ids[0] if plan.focus_topic_ids else None),
                           evidence_ids=list(plan.evidence_ids)[:6])

    async def aclose(self):
        if self.provider is not None:
            await self.provider.aclose()


def plan_from(planner_result, frozen):
    """Server-side gate + deterministic envelope. Runs for every planner, fake or real."""
    from .graph import build_plan
    from .planning import normalize_plan

    references = getattr(planner_result, '_reference_lookup', None)
    experiences = getattr(planner_result, '_experience_lookup', None)
    planner_result = normalize_plan(planner_result, frozen)
    base = build_plan(frozen)
    generated = []
    for item in planner_result.generated_questions:
        question = item.model_dump(mode='json', exclude={'source_id', 'source_quote', 'experience_id', 'experience_quote'})
        citation = references.citation(item.source_id, item.source_quote) if references else None
        if citation is not None:
            question['public_reference'] = citation
        if experiences:
            inspiration = experiences.citation(item.experience_id, item.experience_quote, item.target_quote)
            if inspiration:
                question['experience_reference'] = inspiration
        generated.append(question)
    return {**base, 'version': PLANNER_VERSION, 'planner': 'langchain-agent',
            'focus_topic_ids': list(planner_result.focus_topic_ids),
            'evidence_ids': list(planner_result.evidence_ids),
            'resume_item_indexes': (list(planner_result.resume_item_indexes)
                                    or list(frozen.get('resume_item_indexes') or [])),
            'rationale': planner_result.rationale,
            **({'public_reference_status': references.status,
                'public_reference_count': sum('public_reference' in item for item in generated)}
               if references is not None else {}),
            **({'experience_search_status': experiences.status,
                'experience_search_provider': experiences.service.provider,
                'experience_question_count': sum('experience_reference' in item for item in generated)}
               if experiences is not None else {}),
            **({'generated_questions': generated, 'generated_question_count': len(generated)}
               if frozen.get('question_basis') else {})}
