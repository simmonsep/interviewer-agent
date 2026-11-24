"""S00R: the JD -> confirmed-fact ranking is deterministic, bounded and honest about misses."""
import hashlib
import json
from pathlib import Path

from interview_agent.materials.alignment_ranking import rank, terms

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "benchmarks" / "material-align-v0.1"


def load():
    manifest = json.loads((CORPUS / "manifest.json").read_text(encoding="utf-8"))
    body = (CORPUS / "cases.jsonl").read_bytes()
    assert hashlib.sha256(body).hexdigest() == manifest["sha256"]["cases.jsonl"]
    return [json.loads(line) for line in body.decode("utf-8").splitlines() if line.strip()]


def test_the_corpus_keeps_the_measured_order():
    cases = load()
    assert {case["shape"] for case in cases} == {"mixed_cn_en", "same_named_projects",
                                                 "old_version", "no_evidence"}
    for case in cases:
        got = [row["id"] for row in rank(case["jd_text"], case["facts"])]
        assert got == case["expected_top"], (case["case_id"], got)


def test_ties_break_by_id_so_the_order_never_drifts():
    facts = [{"id": "b", "quote": "缓存"}, {"id": "a", "quote": "缓存"}]
    first = rank("缓存", facts)
    second = rank("缓存", facts)
    assert [row["id"] for row in first] == ["a", "b"]
    assert first == second


def test_an_english_jd_matches_a_chinese_fact_through_the_alias_table():
    facts = [{"id": "f", "quote": "用线程池处理并发请求。"}]
    assert rank("concurrency and threading", facts)[0]["id"] == "f"
    assert "concurrency" in terms("并发")


def test_no_jd_or_no_facts_is_an_empty_ranking_not_a_guess():
    assert rank("", [{"id": "a", "quote": "x"}]) == []
    assert rank("需要 Kubernetes", []) == []


def test_the_limit_bounds_the_candidate_set():
    facts = [{"id": f"f{index}", "quote": "缓存与数据库"} for index in range(10)]
    assert len(rank("缓存数据库", facts, limit=3)) == 3


# ------------------------------------------------------------- S00Rb: wired into the align tool

def test_citing_a_fact_outside_the_delivered_set_is_refused():
    """A JD with no lexical overlap delivers nothing; citing a confirmed fact is then refused."""
    import asyncio

    import pytest

    from interview_agent.materials.agent import MaterialAgentFailure, run_material_agent
    from test_material_agent import Calls

    fact_id = "00000000-0000-0000-0000-00000000000a"
    scope = {"mode": "align", "goal_version": 1, "material_revision": 0,
             "target": {"kind": "user_jd", "text": "需要 Kubernetes 与 Go 经验。"},
             "sources": [], "facts": [{"id": fact_id, "quote": "实现了事务回滚。",
                                       "source_id": "00000000-0000-0000-0000-0000000000ff",
                                       "kind": "contribution"}]}
    assert rank(scope["target"]["text"], scope["facts"]) == []  # nothing is delivered
    requirement = {"requirements": [{"requirement_quote": scope["target"]["text"],
                                     "evidence_status": "evidence_present",
                                     "fact_ids": [fact_id], "question": "", "topic_ids": []}]}
    provider = Calls([("read_material_scope", {}), ("read_target", {}),
                      ("read_confirmed_facts", {}), ("submit_alignment", requirement)])
    with pytest.raises(MaterialAgentFailure):
        asyncio.run(run_material_agent(scope, provider, asyncio.Event()))
    assert any("fact_not_delivered" in error for error in provider.errors)

