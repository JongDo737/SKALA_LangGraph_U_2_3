"""그래프 조립. (팀 다이어그램: LLM Orchestrator → 병렬 워커 → 병합 → 검증 → 근거 충분성 → 판단 → 보고서 → 품질 평가 → 재계획)

START → dart_lookup → startup_screen → profile_prep → planner
planner ─(Send, 동적 fan-out)→ worker_* → merge_results → validate_results → check_sufficiency
   검증 실패·근거 부족(한 번에 수집) / 품질 미달 → record_failure → [예산 남음] planner (재계획)
                                                          [소진]     apply_fallback → (제외·경고 후 진행)
   모두 통과 → competitor_compare → investment_decision → check_quota ─(적합 < 목표, 1회)→ dart_lookup (부족분만 보충)
                                                          └(충분)→ report_draft → quality_gate → finalize_report → END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from orchestrator.nodes.apply_fallback import apply_fallback
from orchestrator.observability import observed
from orchestrator.nodes.check_quota import check_quota
from orchestrator.nodes.check_sufficiency import check_sufficiency
from orchestrator.nodes.competitor_compare import competitor_compare
from orchestrator.nodes.dart_lookup import dart_lookup
from orchestrator.nodes.finalize_report import finalize_report
from orchestrator.nodes.investment_decision import investment_decision
from orchestrator.nodes.merge_results import merge_results
from orchestrator.nodes.planner import planner
from orchestrator.nodes.profile_prep import profile_prep
from orchestrator.nodes.quality_gate import quality_gate
from orchestrator.nodes.record_failure import record_failure
from orchestrator.nodes.report_draft import report_draft
from orchestrator.nodes.save_unresolved import save_unresolved
from orchestrator.nodes.startup_screen import startup_screen
from orchestrator.nodes.validate_results import validate_results
from orchestrator.nodes.worker_competitor import worker_competitor
from orchestrator.nodes.worker_financial import worker_financial
from orchestrator.nodes.worker_market_rag import worker_market_rag
from orchestrator.nodes.worker_market_score import worker_market_score
from orchestrator.routing import (
    route_after_fallback,
    route_after_plan,
    route_after_quota,
    route_after_quality,
    route_after_screen,
    route_after_sufficiency,
    route_budget,
)
from orchestrator.state import OrchestratorState

NODES = {
    "dart_lookup": dart_lookup,
    "startup_screen": startup_screen,
    "profile_prep": profile_prep,
    "planner": planner,
    "worker_financial": worker_financial,
    "worker_market_rag": worker_market_rag,
    "worker_market_score": worker_market_score,
    "worker_competitor": worker_competitor,
    "merge_results": merge_results,
    "validate_results": validate_results,
    "check_sufficiency": check_sufficiency,
    "competitor_compare": competitor_compare,
    "investment_decision": investment_decision,
    "check_quota": check_quota,
    "report_draft": report_draft,
    "quality_gate": quality_gate,
    "record_failure": record_failure,
    "apply_fallback": apply_fallback,
    "finalize_report": finalize_report,
    "save_unresolved": save_unresolved,
}
WORKERS = ("worker_financial", "worker_market_rag", "worker_market_score", "worker_competitor")


def build_graph(checkpointer=None):
    g = StateGraph(OrchestratorState)
    for name, fn in NODES.items():
        g.add_node(name, observed(name)(fn))  # 외부 이벤트 로그 + step_count/node_status

    g.add_edge(START, "dart_lookup")
    g.add_edge("dart_lookup", "startup_screen")
    g.add_conditional_edges(
        "startup_screen", route_after_screen, ["profile_prep", "save_unresolved", "report_draft"]
    )
    g.add_edge("profile_prep", "planner")
    g.add_conditional_edges("planner", route_after_plan, ["profile_prep", "merge_results", *WORKERS])
    for worker in WORKERS:
        g.add_edge(worker, "merge_results")
    g.add_edge("merge_results", "validate_results")
    g.add_edge("validate_results", "check_sufficiency")  # 검증 실패도 근거 충분성과 함께 한 번에 모읍니다
    g.add_conditional_edges("check_sufficiency", route_after_sufficiency, ["record_failure", "competitor_compare"])
    g.add_edge("competitor_compare", "investment_decision")
    g.add_edge("investment_decision", "check_quota")
    g.add_conditional_edges("check_quota", route_after_quota, ["dart_lookup", "report_draft"])
    g.add_edge("report_draft", "quality_gate")
    g.add_conditional_edges("quality_gate", route_after_quality, ["finalize_report", "record_failure"])
    g.add_conditional_edges("record_failure", route_budget, ["planner", "apply_fallback"])
    g.add_conditional_edges(
        "apply_fallback", route_after_fallback, ["competitor_compare", "finalize_report", "save_unresolved"]
    )
    g.add_edge("finalize_report", END)
    g.add_edge("save_unresolved", END)
    return g.compile(checkpointer=checkpointer)
