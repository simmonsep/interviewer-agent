"""S00G: the claim guard, and the gap it closes.

The first test is the reproduction: a rewrite item whose citation resolves and whose source is in
scope passes `validate_result` today even when the sentence claims something the source never
said. The second measures the guard on a labelled corpus so recall and false positives stay
visible instead of being asserted from memory.
"""
import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from interview_agent.evaluation import material_guard as evaluation_guard
from interview_agent.materials.claim_guard import (ALL_FAMILIES, REVIEWED_FAMILIES, claims, codes,
                                                   scan)
from interview_agent.materials.contracts import validate_result

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "benchmarks" / "material-claims-v0.1"


def load_corpus():
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    body = (CORPUS / "cases.jsonl").read_bytes()
    assert hashlib.sha256(body).hexdigest() == manifest["sha256"]["cases.jsonl"]
    return manifest, [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]


def test_the_corpus_keeps_the_measured_baseline():
    manifest, cases = load_corpus()
    assert manifest["labels"] == {"violation": 6, "faithful": 7}
    violations = [case for case in cases if case["label"] == "violation"]
    faithful = [case for case in cases if case["label"] == "faithful"]
    caught = [case for case in violations
              if sorted({hit["code"] for hit in claims(case["rewrite"], case["source"])})
              == sorted(case["expected"])]
    flagged = [case for case in faithful if claims(case["rewrite"], case["source"])]
    assert len(caught) == len(violations), [c["case_id"] for c in violations if c not in caught]
    assert flagged == [], [c["case_id"] for c in flagged]
    assert {case["family"] for case in cases} <= set(ALL_FAMILIES)


def test_every_hit_carries_a_family_code_and_reason():
    hits = claims("参与订单缓存改造，保障系统稳定运行，日常巡检", "参与订单缓存改造")
    assert hits
    assert all({"family", "code", "term", "why"} <= set(hit) for hit in hits)
    assert {hit["family"] for hit in hits} <= set(REVIEWED_FAMILIES)


def test_the_activity_family_stays_out_of_the_reviewed_set():
    """重构→改造 must not block a draft; the family stays available for evaluation only."""
    assert "activity" not in REVIEWED_FAMILIES
    assert codes("完成导出链路的改造工作", "负责报表导出模块的重构", ("activity",)) == [
        "unsupported_activity"]
    assert claims("完成导出链路的改造工作", "负责报表导出模块的重构") == []


def test_the_evaluation_entry_point_uses_the_same_rules():
    text, source = "承接订单相关事件的流程处理", "参与订单事件处理"
    assert evaluation_guard.codes(text, source) == codes(text, source)
    assert evaluation_guard.scan(text, source) == scan(text, source)


def test_a_valid_citation_still_passes_validation_today():
    """The reproduction: structure passes, content does not follow from the source.

    This is the gap S00G closes; it is written as an assertion on *current* behaviour so the
    next step (making the claim guard act on it) flips exactly this test.
    """
    source_id = str(uuid4())
    scope = {"mode": "draft", "draft_style": "rewrite", "goal_version": 1, "material_revision": 1,
             "target": {"kind": "direction", "direction": "Java 后端"}, "facts": [],
             "sources": [{"id": source_id, "name": "简历.md", "content_sha256": "x",
                          "text": "参与订单服务的缓存改造，负责缓存与数据库一致性"}]}
    payload = {"action": "draft",
               "rewrite_items": [{"text": "参与订单缓存改造，保障系统稳定运行",
                                  "source_ids": [source_id], "change_note": "整理"}]}
    result = validate_result(scope, payload)
    assert result.rewrite_items[0].text.endswith("保障系统稳定运行")
    hits = claims(result.rewrite_items[0].text, scope["sources"][0]["text"])
    assert [hit["code"] for hit in hits] == ["unsupported_outcome"]


# ----------------------------------------------------------------- S00Gb: per-item product behaviour

def _scope(text):
    from interview_agent.materials.segments import scope_segments
    source_id = str(uuid4())
    scope = {"mode": "draft", "draft_style": "rewrite", "goal_version": 1, "material_revision": 1,
             "target": {"kind": "direction", "direction": "Java 后端"}, "facts": [],
             "sources": [{"id": source_id, "name": "简历.md", "content_sha256": "x", "text": text}]}
    return scope, source_id, [seg for seg in scope_segments(scope) if not seg["heading"]]


def _item(text, source_id, segment):
    from interview_agent.materials.contracts import RewriteItem
    return RewriteItem(text=text, source_ids=[source_id], change_note="整理",
                       evidence_segments=[segment])


def test_ground_items_moves_the_unsupported_claim_and_keeps_the_rest():
    from interview_agent.materials.claim_guard import ground_items

    scope, source_id, segments = _scope("工作经历\n2019.03 某公司\n负责缓存改造\n技能\n- Python\n")
    work = next(seg["id"] for seg in segments if seg["text"].startswith("负责缓存改造"))
    faithful = _item("负责缓存改造", source_id, work)
    exaggerated = _item("负责缓存改造，保障系统稳定运行", source_id, work)

    kept, moved, findings = ground_items([faithful, exaggerated], scope)
    assert [item.text for item in kept] == ["负责缓存改造"]
    assert moved == ["负责缓存改造，保障系统稳定运行"]
    assert findings[0]["code"] == "unsupported_outcome"
    assert findings[0]["source_ids"] == [source_id]
    assert findings[0]["evidence_segments"] == [work]


def test_every_item_flagged_is_a_failed_proposal_not_a_blank_resume():
    from interview_agent.materials.claim_guard import ground_items

    scope, source_id, segments = _scope("工作经历\n2019.03 某公司\n负责缓存改造\n")
    work = next(seg["id"] for seg in segments if seg["text"].startswith("负责缓存改造"))
    with pytest.raises(ValueError, match="claims_not_grounded"):
        ground_items([_item("负责缓存改造，保障系统稳定运行", source_id, work)], scope)


def test_an_item_with_no_resolvable_source_is_not_silently_dropped():
    """Without a source there is nothing to compare against; the guard stays out of the way."""
    from interview_agent.materials.claim_guard import ground_items

    scope, source_id, segments = _scope("工作经历\n2019.03 某公司\n负责缓存改造\n")
    from interview_agent.materials.contracts import RewriteItem
    orphan = RewriteItem(text="负责缓存改造，保障系统稳定运行", source_ids=[str(uuid4())],
                         change_note="整理")
    kept, moved, findings = ground_items([orphan], scope)
    assert kept == [orphan] and moved == [] and findings == []


def test_the_agent_moves_a_bad_bullet_into_suggestions_end_to_end():
    """S00Gb wiring: the model-generated draft keeps only grounded bullets."""
    import asyncio

    from interview_agent.materials.agent import run_material_agent
    from interview_agent.materials.segments import scope_segments
    from interview_agent.runtime.types import AssistantResponse, ToolCall

    scope, source_id, _ = _scope(
        "工作经历\n2019.03 某公司\n负责缓存改造\n参与订单服务的缓存改造\n技能\n- Python\n")
    segments = [seg for seg in scope_segments(scope) if not seg["heading"]]
    pick = lambda prefix: next(seg["id"] for seg in segments if seg["text"].startswith(prefix))
    dated, work, second = pick("2019.03"), pick("负责缓存改造"), pick("参与订单服务的缓存改造")
    skill = pick("- Python")

    def submit(experience, items, skills=()):
        return {"items": [{"text": text_, "source_ids": [source_id], "change_note": "整理",
                           "evidence_segments": [seg]} for text_, seg in items],
                "education": [], "experience": experience, "projects": [],
                "skills": list(skills), "suggestions": []}

    class Mixed:
        def __init__(self):
            self.n = 0

        async def complete(self, messages, specs):
            self.n += 1
            items = [("负责缓存改造", work),
                     ("参与订单缓存改造，保障系统稳定运行", second)]
            if self.n == 1:
                name, args = "read_material_scope", {}
            elif self.n == 2:
                name, args = "read_target", {}
            elif self.n == 3:
                name, args = "read_source", {"source_id": source_id}
            elif self.n == 4:
                name, args = "submit_rewrite", submit([dated], items)
            else:
                name, args = "submit_rewrite", submit([dated], items, [skill])
            return AssistantResponse(tool_calls=[ToolCall(id=str(self.n), name=name, arguments=args)])

    result = asyncio.run(run_material_agent(scope, Mixed(), asyncio.Event()))
    assert [item.text for item in result.rewrite_items] == ["负责缓存改造"]
    assert any("保障系统稳定运行" in suggestion for suggestion in result.suggestions)
    assert result.structured_sections["experience"] == [dated]
    assert [finding["code"] for finding in result.grounding_findings] == ["unsupported_outcome"]
    assert result.structured_sections["skills"] == [skill]
