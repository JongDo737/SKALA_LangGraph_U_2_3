"""오케스트레이터 그래프 검증 (tests/fake_services.py 의 가짜 응답기 사용: API 키·네트워크 불필요).

확인하는 것
- 동적 fan-out : 재계획 회차의 서브태스크 수가 최초 계획보다 줄고, 실패한 것만 다시 계획된다
- 재작업 루프  : 검증·근거 충분성·품질 평가 실패가 같은 예산(max_replans=2)으로 재계획된다
- 종료 보장    : 어떤 시나리오에서도 replan_count 가 상한을 넘지 않고 보고서가 생성된다
- fallback     : 예산 소진 시 제외·경고로 처리하고 보고서를 확정한다
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from orchestrator.graph import build_graph  # noqa: E402
from orchestrator.observability import read_events  # noqa: E402
from tests import fake_services  # noqa: E402
from orchestrator.settings import Settings  # noqa: E402


def run(tmp_path, scenario="fixture", max_replans=2, saver=None):
    fake_services.use(scenario)
    run_id = uuid.uuid4().hex[:8]
    settings = Settings.from_dict({**Settings().to_dict(), "max_replans": max_replans})
    state = {
        "run_id": run_id,
        "output_dir": str(tmp_path),
        "settings": settings.to_dict(),
        "status": "RUNNING",
        "max_steps": settings.max_steps,
        "replan_count": 0,
        "plan_requested": False,
    }
    graph = build_graph(saver)
    config = {"recursion_limit": 150, "configurable": {"thread_id": run_id}}
    return graph.invoke(state, config), graph, config


def events(result, node=None, event=None):
    """외부 관측성 계층(events.jsonl)의 결정 로그."""
    return [
        e
        for e in read_events(result["output_dir"], result["run_id"], "decision")
        if (node is None or e["node"] == node) and (event is None or e["event"] == event)
    ]


def history(result, kind):
    return read_events(result["output_dir"], result["run_id"], kind)


# ── 동적 fan-out ─────────────────────────────────────────────
def test_replan_only_reruns_failed_subtasks(tmp_path):
    result, *_ = run(tmp_path, "fixture")
    first, second = history(result, "plan")[:2]
    assert first["mode"] == "initial" and first["n_subtasks"] == 20  # 5곳 × 4종류
    assert second["mode"] == "replan"
    assert second["n_subtasks"] < first["n_subtasks"]  # 부족한 것만 다시 계획
    assert second["by_kind"] == {"competitor": 5, "market_rag": 1}
    assert result["outcome"] == "completed" and result["status"] == "SUCCESS"
    assert result["replan_count"] == 1


def test_fanout_count_follows_plan(tmp_path):
    from orchestrator.routing import route_after_plan

    def state_with(n):
        tasks = [
            {"id": f"c{i}:financial", "company_id": f"c{i}", "kind": "financial", "round": 0, "reason": "", "directive": {}}
            for i in range(n)
        ]
        return {
            "run_id": "x",
            "subtasks": tasks,
            "companies": {f"c{i}": {"profile_ref": f"p{i}"} for i in range(n)},
        }

    assert len(route_after_plan(state_with(3))) == 3
    assert len(route_after_plan(state_with(7))) == 7
    assert route_after_plan(state_with(0)) == "merge_results"  # 재작업할 것이 없으면 결과 재사용


# ── 시나리오 ─────────────────────────────────────────────────
def test_profile_retry_goes_back_to_profile_prep(tmp_path):
    result, *_ = run(tmp_path, "profile_retry")
    assert events(result, "profile_prep", "refined"), "프로필 추가 보완 경로를 타야 합니다"
    assert result["profile_refined"]
    assert result["replan_count"] <= 2
    assert result["status"] == "SUCCESS"


def test_quality_failure_triggers_second_replan(tmp_path):
    result, *_ = run(tmp_path, "quality_loop")
    quality = history(result, "quality")
    assert quality[0]["passed"] is False and quality[-1]["passed"] is True
    assert result["replan_count"] == 2
    assert result["outcome"] == "completed" and result["status"] == "SUCCESS"
    failed = [k for k, v in quality[0]["checks"].items() if not v["passed"]]
    assert failed == ["neutrality"]


def test_exhaustion_applies_fallback_and_still_reports(tmp_path):
    result, *_ = run(tmp_path, "exhaust")
    assert result["replan_count"] == 2  # 상한을 넘지 않음
    assert result["outcome"] == "completed_with_warnings" and result["status"] == "SUCCESS"
    assert any("디케이" in name for name in [result["companies"][c]["name"] for c in result["excluded"]])
    assert result["warnings"]
    assert Path(result["report_path"]).exists()
    assert events(result, "apply_fallback")


def test_validate_failure_does_not_hide_other_insufficiency(tmp_path):
    """워커 하나가 실패해도 같은 회차에 다른 기업의 근거 부족이 함께 재계획된다."""
    result, *_ = run(tmp_path, "exhaust")
    round1 = history(result, "plan")[1]
    assert round1["by_kind"].get("market_score") == 1  # 실패한 워커
    assert round1["by_kind"].get("competitor", 0) >= 1  # 같은 회차에 근거 부족도 수집
    names = {result["companies"][c]["name"] for c in result["excluded"]}
    assert any("디케이" in n for n in names)
    assert any("동인" in w and "competitor" in w for w in result["warnings"])  # 끝까지 못 푼 경쟁사 근거는 경고


@pytest.mark.parametrize("cap", [0, 1, 2])
def test_replan_count_never_exceeds_cap(tmp_path, cap):
    result, *_ = run(tmp_path, "exhaust", max_replans=cap)
    assert result["replan_count"] <= cap
    assert Path(result["report_path"]).exists()


# ── 보고서·품질 ──────────────────────────────────────────────
def test_report_has_required_sections_and_citations(tmp_path):
    result, *_ = run(tmp_path, "fixture")
    md = (Path(result["report_path"]).with_suffix(".md")).read_text(encoding="utf-8")
    assert "## SUMMARY" in md and "## REFERENCE" in md
    assert result["quality"]["passed"] is True


def test_quality_rules_catch_problems():
    from orchestrator import rules

    s = Settings()
    md = (
        "## SUMMARY\n- 요약\n\n## 기업별 평가\n\n### 1. A사 — 적합\n"
        "- **재무**: 영업이익률 10% (출처 확인 불가)\n"
        "- **경쟁사**: B사가 업계 최고 수준이다 [1][9]\n"
        "- **한계·위험**: 없음\n\n## REFERENCE\n[1] 자료 — https://a.example/x\n[2] 안 쓰인 자료 — https://b.example/y\n"
    )
    verdict = rules.check_report(md, s)
    codes = {i["code"] for c in verdict["checks"].values() for i in c["issues"]}
    assert {"uncited_claim", "dangling_citation", "unused_reference", "neutrality_term"} <= codes
    assert verdict["passed"] is False


def test_sufficiency_rules():
    from orchestrator import rules

    s = Settings()
    ok_profile = {"n_sources": 1, "desc_chars": 40, "subdomain": "solar / PV"}
    sums = {
        "financial": {"coverage": 0.9},
        "market_rag": {"status": "rag", "n_sources": 1, "has_size": True, "has_growth": True},
        "competitor": {"found": 1, "with_url": 0, "method": "openai_recommendation"},
    }
    codes = {f["code"] for f in rules.check_company("c", ok_profile, sums, s)}
    assert codes == {"market_sources_low", "competitor_ungrounded"}


def test_self_competitor_is_not_grounded_evidence():
    """실제 실행에서 기업 자신의 사업소개 페이지가 '경쟁사'로 잡혔던 문제의 회귀 테스트."""
    from orchestrator import rules

    profile = {"name": "해오름태양광발전소 주식회사", "sources": [{"url": "http://own.example/intro"}]}
    own_page = {"name": "사업소개", "source_url": "http://own.example/intro", "evidence": "태양광 패널을 설치하여"}
    same_name = {"name": "해오름태양광발전소", "source_url": "http://x.example/a", "evidence": "x"}
    other = {"name": "한화큐셀", "source_url": "http://other.example/hq", "evidence": "태양광 제조"}
    assert rules.is_self_competitor(own_page, profile)
    # 실제 실행에서 페이지 제목의 일반 명사가 경쟁사로 잡힌 사례
    for junk in ("태양광 시공", "기술", "에너지 솔루션"):
        assert rules.is_self_competitor({"name": junk, "source_url": "http://x.example/1"}, profile), junk
    for real in ("Recurrent Energy", "한화큐셀", "한전KPS", "Sungrow"):
        assert not rules.is_self_competitor({"name": real, "source_url": "http://x.example/2"}, profile), real
    assert rules.is_self_competitor(same_name, profile)
    assert not rules.is_self_competitor(other, profile)

    summary = rules.summarize_competitor({"research": {"competitors": [own_page]}}, profile)
    assert summary["grounded"] == 0 and summary["self_ref"] == 1
    ok_profile = {"n_sources": 1, "desc_chars": 40, "subdomain": "solar"}
    failures = rules.check_company("c", ok_profile, {"competitor": summary}, Settings())
    assert [f["code"] for f in failures] == ["competitor_self_source"]


def test_report_only_failures_do_not_consume_replans():
    """워커로 고칠 수 없는 실패(보고서 문구 등)는 재계획 예산을 쓰지 않고 바로 fallback 으로 간다."""
    from orchestrator.nodes.record_failure import record_failure

    base = {
        "run_id": "t",
        "settings": Settings().to_dict(),
        "replan_count": 0,
        "stage": "quality",
        "excluded": {},
    }
    # 보고서 문제 중 report_draft 가 스스로 고칠 수 없는 것(예: 한계·위험 서술 누락 같은 구조 문제)은 재계획하지 않는다
    only_report = record_failure({**base, "failures": [{"kind": "report", "company_id": "", "code": "unknown_structure_issue"}]})
    assert only_report["retry_possible"] is False
    with_worker = record_failure({**base, "failures": [{"kind": "competitor", "company_id": "c1", "code": "x"}]})
    assert with_worker["retry_possible"] is True


def test_references_only_include_cited_sources(tmp_path):
    """프로필 출처를 많이 등록해도 인용하지 않은 출처가 REFERENCE에 남지 않는다 (실제 실행에서 발견한 버그)."""
    from orchestrator.store import Store

    result, *_ = run(tmp_path, "many_profile_sources")
    store = Store(tmp_path, result["run_id"])
    cid = result["company_ids"][0]
    profile = store.get(result["companies"][cid]["profile_ref"])
    assert len([s for s in profile["sources"] if s.get("url")]) > 2  # 시나리오가 해당 경로를 실제로 지난다
    assert result["quality"]["passed"] is True, result["quality"]
    md = (Path(result["report_path"]).with_suffix(".md")).read_text(encoding="utf-8")
    ref_lines = [l for l in md.split("## REFERENCE")[1].split("\n") if l.startswith("[")]
    cited = {int(n) for n in __import__("re").findall(r"\[(\d+)\]", md.split("## REFERENCE")[0])}
    assert len(ref_lines) == len(cited)


def test_results_reducer_newer_round_wins():
    from orchestrator.state import merge_results

    old = {"a:x": {"round": 0, "status": "error"}}
    assert merge_results(old, {"a:x": {"round": 1, "status": "ok"}})["a:x"]["status"] == "ok"
    assert merge_results({"a:x": {"round": 1, "status": "ok"}}, {"a:x": {"round": 0, "status": "error"}})["a:x"]["status"] == "ok"


# ── 재개/복구 ────────────────────────────────────────────────
def test_checkpoint_resume_state_is_available(tmp_path):
    from langgraph.checkpoint.sqlite import SqliteSaver

    with SqliteSaver.from_conn_string(str(tmp_path / "ck.sqlite")) as saver:
        result, graph, config = run(tmp_path, "fixture", saver=saver)
        snapshot = graph.get_state(config)
        assert snapshot.values["status"] == "SUCCESS"
        assert snapshot.values["run_id"] == result["run_id"]
        assert not snapshot.next  # 이어서 실행할 노드가 없음 = 정상 종료


# ── Error-aware State (SKALA 강의 'State는 실패를 고려해야 함') ────────────────────────
def test_error_aware_fields_have_timestamps(tmp_path):
    result, *_ = run(tmp_path, "exhaust")  # 디케이 시장성 워커가 계속 실패
    assert result["error_count"] >= 3  # 최초 + 재계획 2회
    last = result["last_error"]
    assert {"node", "type", "message", "ts"} <= set(last)
    assert last["node"] == "worker_market_score" and last["type"] == "WorkerFailure"
    assert all({"agent", "error", "ts"} <= set(e) for e in result["errors"])
    failed = [r for r in result["results"].values() if r["status"] == "error"]
    assert failed and all(r["ts"] and r["agent"] == "market_score_worker" for r in failed)
    assert all(r["worker_id"].startswith("market_score_") for r in failed)  # 에이전트이름_timestamp


def test_control_meta_is_in_state_but_history_is_external(tmp_path):
    result, *_ = run(tmp_path, "fixture")
    assert result["step_count"] > 0 and result["max_steps"] > 0
    assert result["status"] == "SUCCESS" and result["node_status"]["planner"] == "ok"
    # 결정 로그·계획 이력은 State 가 아니라 외부 관측성 계층에 있다 (노션 State 예시)
    for key in ("decision_log", "plan_history", "failure_history", "quality_history"):
        assert key not in result
    decisions = read_events(result["output_dir"], result["run_id"], "decision")
    assert decisions and all(e["trace_id"] == result["run_id"] and e["ts"] for e in decisions)


def test_report_body_is_not_in_state(tmp_path):
    """Synthesis 결과(보고서 본문)는 저장소에 두고 State 는 URI·요약만 가진다."""
    result, *_ = run(tmp_path, "fixture")
    assert result["report_uri"].startswith("runs/")
    assert "markdown" not in str(result["report"]) and result["report"]["chars"] > 0


def test_external_bracket_numbers_do_not_become_citations():
    """검색 요약의 '[2]' 가 보고서 인용 번호로 오인되지 않는다 (실제 실행에서 발견)."""
    from orchestrator.nodes.report_draft import _ext

    assert _ext("임직원 수. 6,608명[2] ; 자본금[12] 90억") == "임직원 수. 6,608명 ; 자본금 90억"


# ── 품질 미달 → Loop: 보고서 문구·인용 문제도 재계획 대상 ──────────────────────────────────
def test_report_wording_failures_loop_and_get_repaired():
    from orchestrator import rules
    from orchestrator.nodes.record_failure import record_failure

    md = (
        "## SUMMARY\n- 평가 대상 1곳입니다. 업계 최고 수준입니다.\n\n## 기업별 평가\n\n### 1. A사 — 적합\n"
        "- **재무**: 영업이익률 10% [1]\n- **시장 규모**: 시장 규모 100억 [2]\n- **한계·위험**: 없음\n\n"
        "## REFERENCE\n[1] 자료 — https://a.example/x\n[2] 다른 자료 — https://b.example/y\n[3] 안 쓰인 자료 — https://c.example/z\n"
    )
    verdict = rules.check_report(md, Settings())
    feedback = [
        {"kind": i["kind"], "code": i["code"], "company_id": ""}
        for c in verdict["checks"].values()
        for i in c["issues"]
    ]
    assert {f["code"] for f in feedback} >= {"neutrality_term", "unused_reference"}
    # 워커 대상이 아니어도 보정 가능한 실패면 예산이 남는 한 Loop(재계획)를 요청한다
    out = record_failure(
        {"run_id": "t", "settings": Settings().to_dict(), "replan_count": 0, "stage": "quality", "excluded": {}, "failures": feedback}
    )
    assert out["retry_possible"] is True
    # 다음 초안에서 보정되어 재평가를 통과한다
    fixed = rules.repair_markdown(md, feedback)
    assert rules.check_report(fixed, Settings())["passed"] is True
    assert "최고" not in fixed and "[3]" not in fixed


def test_bool_env_parsing(monkeypatch):
    monkeypatch.setenv("ORCH_REQUIRE_COMPETITOR_URL", "false")
    monkeypatch.setenv("ORCH_REPORT_USE_LLM", "true")
    s = Settings.from_env()
    assert s.require_competitor_url is False and s.report_use_llm is True


# ── 재개/복구: 중간 노드가 죽어도 체크포인트에서 이어서 실행 ─────────────────────────────────
def test_resume_from_checkpoint_after_node_crash(tmp_path, monkeypatch):
    from langgraph.checkpoint.sqlite import SqliteSaver

    import orchestrator.graph as graph_module

    calls = {"n": 0}
    original = graph_module.NODES["report_draft"]

    def flaky_report_draft(state):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated crash")
        return original(state)

    monkeypatch.setitem(graph_module.NODES, "report_draft", flaky_report_draft)
    run_id = uuid.uuid4().hex[:8]
    settings = Settings().to_dict()
    state = {
        "run_id": run_id, "output_dir": str(tmp_path),
        "settings": settings, "status": "RUNNING", "max_steps": settings["max_steps"], "replan_count": 0, "plan_requested": False,
    }
    config = {"recursion_limit": 150, "configurable": {"thread_id": run_id}}
    with SqliteSaver.from_conn_string(str(tmp_path / "ck.sqlite")) as saver:
        graph = graph_module.build_graph(saver)
        with pytest.raises(RuntimeError, match="simulated crash"):
            graph.invoke(state, config)
        crashed = graph.get_state(config)
        assert crashed.next == ("report_draft",)  # 죽은 노드부터 이어서 실행할 수 있다
        assert crashed.values["replan_count"] == 1 and crashed.values["results"]  # 재개에 필요한 상태가 남아 있다

        result = graph.invoke(None, config)  # 재개
    assert result["status"] == "SUCCESS" and calls["n"] == 2
    decisions = read_events(str(tmp_path), run_id, "decision")
    # 이미 끝난 노드는 다시 실행하지 않는다
    assert len([e for e in decisions if e["node"] == "dart_lookup"]) == 1
    assert len([e for e in decisions if e["node"] == "worker_financial"]) == 5
    crash_events = read_events(str(tmp_path), run_id, "node_exception")
    assert crash_events and crash_events[0]["node"] == "report_draft" and crash_events[0]["ts"]


# ── 진행 상황 출력 (실행 중 무엇을 하는지 화면에 보인다) ────────────────────────────────────
def test_progress_is_printed_while_running(tmp_path, capsys):
    run(tmp_path, "fixture")
    out = capsys.readouterr().out
    assert "planner" in out and "서브태스크 20개" in out
    assert "(1/20)" in out and "(20/20)" in out  # 워커 진행도
    assert "시장성" in out and "경쟁사" in out  # 워커 결과 요약
    assert "check_sufficiency" in out and "record_failure" in out and "finalize_report" in out


def test_progress_output_can_be_silenced(tmp_path, capsys, monkeypatch):
    import orchestrator.observability as obs

    monkeypatch.setattr(obs, "CONSOLE", False)
    run(tmp_path, "fixture")
    assert "planner" not in capsys.readouterr().out
    assert read_events(str(tmp_path), "x") == []  # 다른 run_id 는 비어 있음 (파일 기록은 별도로 유지)


# ── 병렬 처리 ────────────────────────────────────────────────────────────────────────────
def test_workers_report_duration_and_round_summary(tmp_path):
    result, *_ = run(tmp_path, "fixture")
    workers = [e for e in events(result, event="done") if e["node"].startswith("worker_")]
    assert workers and all(e["duration_s"] >= 0 for e in workers)
    merged = events(result, "merge_results")[0]
    assert "계획 20개 중 20개 도착" in merged["reason"]


def test_only_vector_search_is_serialized():
    """RAG: 벡터 검색(search_rag)만 한 번에 하나, 그 밖의 단계(LLM·웹 보완)는 병렬로 돈다."""
    import threading
    import time

    from orchestrator.services import _serialize_search

    state = {"search_now": 0, "search_max": 0, "other_now": 0, "other_max": 0}
    lock = threading.Lock()

    class Agent:
        def search_rag(self):
            with lock:
                state["search_now"] += 1
                state["search_max"] = max(state["search_max"], state["search_now"])
            time.sleep(0.03)
            with lock:
                state["search_now"] -= 1

        def slow_llm_step(self):
            with lock:
                state["other_now"] += 1
                state["other_max"] = max(state["other_max"], state["other_now"])
            time.sleep(0.05)
            with lock:
                state["other_now"] -= 1

    agent = _serialize_search(Agent())

    def worker():
        agent.search_rag()
        agent.slow_llm_step()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert state["search_max"] == 1  # 벡터 검색은 동시에 하나만 (멈춤 방지)
    assert state["other_max"] > 1  # 나머지는 병렬


# ── 경쟁사 재작업: 1차 후보를 확인만 한다 (실행 로그에서 2차에 한진해운이 나온 문제) ─────────────────
def test_competitor_replan_carries_previous_candidates(tmp_path):
    result, *_ = run(tmp_path, "fixture")
    r1 = [t for t in result["subtasks"] if t["kind"] == "competitor"]  # 마지막 회차(재계획) 서브태스크
    assert r1, "재계획에 경쟁사 서브태스크가 있어야 한다"
    for task in r1:
        assert task["directive"].get("candidates"), task  # 1차 후보 이름이 지시에 실려 있다
        assert all(c["name"] for c in task["directive"]["candidates"])


def test_services_verify_previous_candidates_without_reproposing(monkeypatch):
    """후보가 지시에 있으면 모델에게 다시 제안받지 않고 그 회사를 검색으로 확인한다. 채용 사이트는 근거가 아니다."""
    import agents.compitition as comp
    import agents.market_scoring as scoring
    from orchestrator.services import Services

    def must_not_propose(*a, **k):
        raise AssertionError("재작업에서 후보를 새로 제안받음")

    class Hit:
        def __init__(self, title, snippet, url):
            self.title, self.snippet, self.url = title, snippet, url

    class Client:
        def search(self, query, limit):
            return [
                Hit("한화에너지 채용", "한화에너지 신입 공고", "https://www.saramin.co.kr/x"),
                Hit("한화에너지 - 회사 소개", "한화에너지는 발전·에너지 사업을 하는 기업", "https://www.hanwha.com/energy"),
            ]

    monkeypatch.setattr(comp, "run", must_not_propose)
    monkeypatch.setattr(scoring, "build_search_client", lambda: Client())
    profile = {"name": "테스트기업", "sources": [], "subdomain": "solar", "country": "KR", "financials": []}
    out = Services().competitor(
        profile, 1, {"provider": "web", "require_source_url": True, "candidates": [{"name": "주식회사 한화에너지", "evidence": "x"}]}
    )["research"]
    c = out["competitors"][0]
    assert c["name"] == "주식회사 한화에너지"  # 이름이 바뀌지 않는다
    assert c["source_url"] == "https://www.hanwha.com/energy"  # 채용 사이트가 아니라 회사 소개 출처


# ── 중립성 미달: 워커를 다시 돌리지 않고 문장만 정리 (실행 로그에서 2차 재계획이 결과를 악화시킨 문제) ───────────
def test_neutrality_failure_is_repaired_without_rerunning_workers(tmp_path):
    result, *_ = run(tmp_path, "quality_loop")
    plans = history(result, "plan")
    assert plans[-1]["n_subtasks"] == 0  # 마지막 재계획에서 워커를 다시 돌리지 않았다
    assert result["quality"]["passed"] is True and result["outcome"] == "completed"
    md = (Path(result["report_path"]).with_suffix(".md")).read_text(encoding="utf-8")
    assert "최고" not in md


def test_repair_keeps_citations_when_removing_banned_sentence():
    from orchestrator import rules

    md = (
        "## SUMMARY\n- 요약입니다.\n\n## 기업별 평가\n\n### 1. A사 — 적합\n"
        "- **경쟁사**: B사 — 태양광 모듈을 제조한다. 업계 최고 수준의 기술력을 보유한다. [1]\n"
        "- **시장 규모**: 시장 규모 100억 [2]\n- **한계·위험**: 없음\n\n"
        "## REFERENCE\n[1] B사 소개 — https://b.example/x\n[2] 시장 자료 — https://c.example/y\n"
    )
    verdict = rules.check_report(md, Settings())
    feedback = [{"kind": i["kind"], "code": i["code"], "company_id": ""} for c in verdict["checks"].values() for i in c["issues"]]
    assert any(f["code"] == "neutrality_term" for f in feedback)
    fixed = rules.repair_markdown(md, feedback)
    assert "최고" not in fixed and "[1]" in fixed  # 문장은 지우되 출처 인용은 남긴다
    assert rules.check_report(fixed, Settings())["checks"]["groundedness"]["passed"] is True


def test_repair_placeholder_does_not_trip_neutrality_check():
    """문장이 전부 제거돼 대체 문구가 들어가도 그 문구가 금지 표현을 포함해 다시 걸리면 안 된다 (실제 실행 a8f1187e57)."""
    from orchestrator import rules

    assert not any(t in rules.REMOVED_NOTE for t in rules.BANNED_TERMS)
    md = (
        "## SUMMARY\n- 요약입니다.\n\n## 기업별 평가\n\n### 1. A사 — 적합\n"
        "- **사업 개요**: 업계 최고 수준의 태양광 발전 사업자이다. [1]\n"
        "- **한계·위험**: 없음\n\n## REFERENCE\n[1] A사 소개 — https://a.example/x\n"
    )
    feedback = [{"kind": "profile", "code": "neutrality_term", "company_id": ""}]
    fixed = rules.repair_markdown(md, feedback)
    assert rules.check_report(fixed, Settings())["checks"]["neutrality"]["passed"] is True


def test_replanned_competitor_is_always_verified(monkeypatch):
    """어떤 지시로 재작업하든(예: 후보를 새로 제안받는 경우) 검색 확인을 건너뛰지 않는다."""
    import agents.compitition as comp
    import agents.market_scoring as scoring
    from orchestrator.services import Services

    class Hit:
        def __init__(self, t, s, u):
            self.title, self.snippet, self.url = t, s, u

    class Client:
        def search(self, q, n):
            return [Hit("테스트회사 소개", "테스트회사는 에너지 기업", "https://t.example/about")]

    monkeypatch.setattr(comp, "run", lambda payload, client: {"research_results": [{"competitors": [{"name": "테스트회사", "evidence": "x"}]}]})
    monkeypatch.setattr(scoring, "build_search_client", lambda: Client())
    profile = {"name": "기업", "sources": [], "subdomain": "solar", "country": "KR", "financials": []}
    out = Services().competitor(profile, 2, {"repropose": True})["research"]  # provider/require_source_url 지시 없음
    assert out["competitors"][0].get("source_url") == "https://t.example/about"


def test_planner_does_not_refine_profile_or_rerun_workers_for_wording(tmp_path):
    """사업 개요(프로필) 쪽 중립성 미달도 프로필 재조사·워커 재실행 없이 문장 정리로 처리한다."""
    from orchestrator.nodes.planner import planner
    from orchestrator.store import Store

    store = Store(tmp_path, "t")
    prof = {"id": "c", "name": "기업", "financial_summary": {"자산총계": "1"}, "financials": [], "sources": []}
    state = {
        "run_id": "t", "output_dir": str(tmp_path), "settings": Settings().to_dict(), "replan_count": 0, "plan_requested": True,
        "company_ids": ["c"], "companies": {"c": {"name": "기업", "profile_ref": store.put("profile", "c", prof)}},
        "failures": [{"stage": "quality", "kind": "profile", "company_id": "c", "code": "neutrality_term", "reason": "x"}],
    }
    out = planner(state)
    assert out["subtasks"] == [] and out["profile_targets"] == []


# ── 적합 기업 부족분 보충 (check_quota → DART 검색부터 다시, 최대 1회) ─────────────────
def _suitable(result):
    return [cid for cid, d in result["decisions"].items() if d["decision"] == "적합"]


def test_topup_fills_only_the_shortfall_and_keeps_earlier_companies(tmp_path):
    result, _, _ = run(tmp_path, "topup")
    topup_events = events(result, "check_quota", "topup")
    assert len(topup_events) == 1 and "부족 2곳" in topup_events[0]["reason"]
    assert result["topup_count"] == 1
    # 검색부터 다시 하되 새로 찾은 기업은 부족한 2곳뿐이고, 기존 5곳은 그대로 남는다
    second_lookup = events(result, "dart_lookup")[1]
    assert second_lookup["n"] == 2 and "기존 5곳 제외" in second_lookup["reason"]
    assert len(result["company_ids"]) == 7
    assert len(_suitable(result)) == 5
    # 보충 계획에는 새 기업의 워커만 들어 있고, 기존 기업을 다시 돌리지 않는다
    plan = [p for p in history(result, "plan") if p["mode"] == "topup"][0]
    assert plan["n_subtasks"] == 2 * 4
    workers = [e for e in events(result) if e["node"].startswith("worker_") and e.get("company_id") in ("x1", "x2")]
    assert {e["company_id"] for e in workers} == {"x1", "x2"}
    assert result["report_path"] and result["status"] == "SUCCESS"


def test_topup_happens_at_most_once(tmp_path):
    result, _, _ = run(tmp_path, "topup_still_short")
    assert len(events(result, "check_quota", "topup")) == 1
    assert len(events(result, "dart_lookup")) == 2  # 처음 + 보충 1회
    assert events(result, "check_quota", "enough")  # 두 번째 확인에서는 상한 소진으로 보고서 단계로 진행
    assert len(_suitable(result)) < 5 and result["report_path"]  # 그래도 모자라면 있는 만큼으로 보고서를 낸다


def test_topup_with_no_new_candidates_still_finishes(tmp_path):
    result, _, _ = run(tmp_path, "topup_none_found")
    assert result["topup_count"] == 1
    assert [p["mode"] for p in history(result, "plan")].count("topup") == 0
    assert result["report_path"] and result["status"] == "SUCCESS"


def test_no_topup_when_target_is_met(tmp_path):
    result, _, _ = run(tmp_path, "fixture")
    assert len(_suitable(result)) == 5
    assert not events(result, "check_quota", "topup") and not result.get("topup_count")


def test_run_id_is_a_readable_timestamp_and_never_collides(tmp_path):
    import re

    from app import new_run_id

    first = new_run_id(str(tmp_path))
    assert re.fullmatch(r"\d{8}_\d{6}", first)  # 예: 20261013_170938
    (tmp_path / "runs" / first).mkdir(parents=True)
    second = new_run_id(str(tmp_path))  # 같은 초에 한 번 더 실행해도 폴더가 겹치지 않는다
    assert second != first and second.startswith(first)
