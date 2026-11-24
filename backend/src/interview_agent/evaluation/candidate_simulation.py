"""Deterministic, stateful candidate simulator for the evaluation harness (S00C).

The simulator is an evaluation-side actor: it answers the interview API with the text a
synthetic candidate would say. It is **not** part of the product and never decides product
state. Its only inputs are the *public* question payload the API returns, its own rule list and
the hidden truth units belonging to that synthetic case. Scores and oracle expectations are not
inputs: an answer may be deliberately wrong only when a rule says so.

Design rules that matter for the tests:

- **Deterministic.** Same case, same prefix, same rules -> same branch and same text. There is no
  random choice; synonym tables would be a separate experiment.
- **Nothing leaks by accident.** A rule may only reveal facts it lists, and the rendered text is
  checked against every other hidden fact's statement. A leak is a `simulator_failure`, never a
  quiet success.
- **State stays with the case.** A state produced for one `case_id` cannot be fed to another.
- **Unmatched is honest.** A question without a rule is `unmatched` with a reason; it is not an
  empty answer and not a guessed one.
"""
from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import Field

from interview_agent.contracts.common import Contract

BEHAVIORS = ("FULL", "PARTIAL", "VAGUE", "OFF_TOPIC", "INCORRECT", "EVASIVE")
KINDS = ("base", "probe", "retest", "project")
FACT_SLOT = re.compile(r"\{fact:([a-z0-9-]{1,80})\}")


class CandidateFact(Contract):
    """One hidden truth unit. `statement` is the sentence the candidate may say when allowed."""

    id: str = Field(pattern=r"^[a-z0-9-]{1,80}$")
    statement: Annotated[str, Field(min_length=1, max_length=400)]
    # Words that only appear in this statement; used to catch an accidental leak in other text.
    markers: Annotated[list[str], Field(max_length=6)] = []


class BehaviorRule(Contract):
    """One ordered rule. Matching is exact first, then topic/kind; the first match wins."""

    branch: str = Field(pattern=r"^[a-z0-9-]{1,40}$")
    # Usage counters are per rule, not per branch: two questions may share one behavior.
    rule_id: str | None = Field(default=None, pattern=r"^[a-z0-9-]{1,40}$")
    behavior: Literal[BEHAVIORS]
    text: Annotated[str, Field(min_length=1, max_length=1500)]
    question_ids: Annotated[list[str], Field(max_length=6)] = []
    topic_ids: Annotated[list[str], Field(max_length=6)] = []
    kinds: Annotated[list[Literal[KINDS]], Field(max_length=4)] = []
    # State guards: only usable after / never after these facts were revealed.
    requires_revealed: Annotated[list[str], Field(max_length=6)] = []
    forbids_revealed: Annotated[list[str], Field(max_length=6)] = []
    # Facts this answer is allowed to disclose, and facts it asserts (must be in `allowed`).
    reveals: Annotated[list[str], Field(max_length=6)] = []
    claims: Annotated[list[str], Field(max_length=6)] = []
    max_uses: Annotated[int, Field(ge=1, le=12)] | None = None


class CandidateProfile(Contract):
    """The truth side of one synthetic case: what the candidate knows and may admit."""

    case_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,80}$")  # session case ids look like IS01
    facts: Annotated[list[CandidateFact], Field(min_length=1, max_length=24)]
    # Facts the candidate may use in any answer (their own background); hidden ones are not here.
    allowed_fact_ids: Annotated[list[str], Field(min_length=1, max_length=24)]
    rules: Annotated[list[BehaviorRule], Field(min_length=1, max_length=16)]


class CandidateState(Contract):
    case_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,80}$")
    answered: Annotated[int, Field(ge=0)] = 0
    revealed_fact_ids: Annotated[list[str], Field(max_length=24)] = []
    used_branches: Annotated[list[str], Field(max_length=32)] = []
    used_rule_ids: Annotated[list[str], Field(max_length=32)] = []
    asked_topic_ids: Annotated[list[str], Field(max_length=32)] = []


class CandidateReply(Contract):
    status: Literal["answered", "unmatched", "simulator_failure"]
    branch: str | None = None
    behavior: str | None = None
    text: str = ""
    allowed_fact_ids: Annotated[list[str], Field(max_length=24)] = []
    revealed_fact_ids: Annotated[list[str], Field(max_length=24)] = []
    reason: str | None = None


def parse_question(payload):
    """Normalise the public question into only what a candidate could actually know.

    Missing pieces are absent, not guessed: `unmatched` later explains what was missing.
    """
    if not isinstance(payload, dict):
        return None
    topic_id, kind = payload.get("topic_id"), payload.get("kind")
    question_id = payload.get("question_id")
    if not isinstance(topic_id, str) or kind not in KINDS:
        return None
    intent = {"topic_id": topic_id, "kind": kind}
    if isinstance(question_id, str) and question_id:
        intent["question_id"] = question_id
    if isinstance(payload.get("parent_question_id"), str):
        intent["parent_question_id"] = payload["parent_question_id"]
    return intent


class CandidateSimulator:
    """Rules + truth -> deterministic answers, with validation on both sides."""

    def __init__(self, profile: CandidateProfile):
        self.profile = profile
        self.facts = {fact.id: fact for fact in profile.facts}
        self.allowed = set(profile.allowed_fact_ids)
        if len(self.facts) != len(profile.facts):
            raise ValueError("candidate_fact_duplicate")
        unknown = self.allowed - set(self.facts)
        if unknown:
            raise ValueError("candidate_allowed_unknown_fact")
        for rule in profile.rules:
            for fact_id in rule.reveals + rule.claims + rule.requires_revealed + rule.forbids_revealed:
                if fact_id not in self.facts:
                    raise ValueError("candidate_rule_unknown_fact")
        self._reject_ambiguous_exact_rules()

    def _reject_ambiguous_exact_rules(self):
        seen = {}
        for rule in self.profile.rules:
            for question_id in rule.question_ids:
                if question_id in seen:
                    raise ValueError("candidate_rule_ambiguous")
                seen[question_id] = rule.branch

    def begin(self, case_id=None):
        """A fresh state for this case. A state from another case is never reused."""
        case_id = case_id or self.profile.case_id
        if case_id != self.profile.case_id:
            raise ValueError("candidate_state_foreign_case")
        return CandidateState(case_id=self.profile.case_id)

    def next_answer(self, payload, state: CandidateState):
        if state.case_id != self.profile.case_id:
            raise ValueError("candidate_state_foreign_case")
        intent = parse_question(payload)
        if intent is None:
            return CandidateReply(status="unmatched", reason="question_not_parsed",
                                  allowed_fact_ids=sorted(self.allowed))
        rule = self._pick(intent, state)
        if rule is None:
            return CandidateReply(status="unmatched", reason="no_matching_rule",
                                  allowed_fact_ids=sorted(self.allowed))
        problem = self._check(rule, state)
        if problem is not None:
            return CandidateReply(status="simulator_failure", reason=problem,
                                  branch=rule.branch, behavior=rule.behavior,
                                  allowed_fact_ids=sorted(self.allowed))
        text = FACT_SLOT.sub(lambda match: self.facts[match.group(1)].statement, rule.text)
        leak = self._leaked(text, rule, state)
        if leak is not None:
            return CandidateReply(status="simulator_failure", reason=f"unrevealed_fact:{leak}",
                                  branch=rule.branch, behavior=rule.behavior,
                                  allowed_fact_ids=sorted(self.allowed))
        self._advance(rule, intent, state)
        return CandidateReply(status="answered", branch=rule.branch, behavior=rule.behavior,
                              text=text, allowed_fact_ids=sorted(self.allowed),
                              revealed_fact_ids=list(rule.reveals))

    def _pick(self, intent, state):
        """Exact question id first, then topic+kind, then topic. State guards are hard filters."""
        candidates = [rule for rule in self.profile.rules if self._guard_ok(rule, state)]
        for rule in candidates:
            if intent.get("question_id") in rule.question_ids:
                return rule
        for rule in candidates:
            if (rule.topic_ids and intent["topic_id"] in rule.topic_ids
                    and rule.kinds and intent["kind"] in rule.kinds):
                return rule
        for rule in candidates:
            if rule.topic_ids and intent["topic_id"] in rule.topic_ids and not rule.kinds:
                return rule
        return None

    def _guard_ok(self, rule, state):
        if any(fact_id not in state.revealed_fact_ids for fact_id in rule.requires_revealed):
            return False
        if any(fact_id in state.revealed_fact_ids for fact_id in rule.forbids_revealed):
            return False
        if rule.max_uses is not None and state.used_rule_ids.count(self._key(rule)) >= rule.max_uses:
            return False
        return True

    @staticmethod
    def _key(rule):
        return rule.rule_id or rule.branch

    def _check(self, rule, state):
        for fact_id in rule.claims:
            if fact_id not in self.allowed and fact_id not in state.revealed_fact_ids \
                    and fact_id not in rule.reveals:
                return f"claim_not_allowed:{fact_id}"
        for fact_id in rule.reveals:
            if fact_id not in self.allowed and fact_id not in self.facts:
                return f"reveal_not_allowed:{fact_id}"
        for slot in FACT_SLOT.findall(rule.text):
            if slot not in rule.reveals and slot not in self.allowed:
                return f"slot_not_allowed:{slot}"
        return None

    def _leaked(self, text, rule, state):
        """A hidden statement may not appear unless this rule mentions it (or it is public)."""
        permitted = set(rule.reveals) | set(rule.claims) | self.allowed | set(state.revealed_fact_ids)
        for fact_id, fact in self.facts.items():
            if fact_id in permitted:
                continue
            if fact.statement in text or any(marker and marker in text for marker in fact.markers):
                return fact_id
        return None

    def _advance(self, rule, intent, state):
        state.answered += 1
        for fact_id in rule.reveals:
            if fact_id not in state.revealed_fact_ids:
                state.revealed_fact_ids.append(fact_id)
        state.used_branches.append(rule.branch)
        state.used_rule_ids.append(self._key(rule))
        state.asked_topic_ids.append(intent["topic_id"])


def simulate(profile, payloads, case_id=None):
    """Replay a fixed prefix: returns the replies in order (used by prefix/switch tests)."""
    simulator = CandidateSimulator(profile)
    state = simulator.begin(case_id)
    return simulator, state, [simulator.next_answer(payload, state) for payload in payloads]
