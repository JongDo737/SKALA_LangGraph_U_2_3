"""조건부 엣지 함수. 노드가 아니라 State 를 읽어 다음 목적지만 정합니다. (State 변경 없음)"""

from __future__ import annotations

from typing import Any

from langgraph.types import Send

from orchestrator.common import active_ids
from orchestrator.observability import now_ts
from orchestrator.state import WORKER_KINDS

WORKER_NODE = {kind: f"worker_{kind}" for kind in WORKER_KINDS}


def route_after_screen(state: dict[str, Any]) -> str:
    if state.get("topup_count"):
        # 보충 검색: 새로 통과한 기업이 있으면 그 기업만 처리하고, 없으면 기존 결과로 보고서를 씁니다.
        return "profile_prep" if state.get("topup_ids") else "report_draft"
    return "profile_prep" if active_ids(state) else "save_unresolved"


def route_after_quota(state: dict[str, Any]) -> str:
    return "dart_lookup" if state.get("topup_shortfall") else "report_draft"


def route_after_plan(state: dict[str, Any]):
    """계획 이후에 워커가 정해집니다 (동적 fan-out): 서브태스크 수만큼 Send 합니다."""

    if state.get("profile_targets"):
        return "profile_prep"  # 프로필 추가 보완 후 다시 계획
    tasks = state.get("subtasks") or []
    if not tasks:
        return "merge_results"  # 재작업할 것이 없으면 유효한 결과를 그대로 재사용
    base = {
        key: state.get(key)
        for key in ("run_id", "output_dir", "settings")
    }
    stamp = now_ts().replace(":", "").replace("-", "").replace(".", "")
    return [
        Send(
            WORKER_NODE[task["kind"]],
            {
                **base,
                # WorkerState: 워커 한 건의 실행 상태 (에이전트이름_timestamp 로 식별)
                "worker_id": f"{task['kind']}_{task['company_id']}_{stamp}",
                "subtask": task,
                "profile_ref": state["companies"][task["company_id"]]["profile_ref"],
                "status": "RUNNING",
                "step_count": 0,
                "max_steps": 1,
                "error_count": 0,
                "last_error": None,
            },
        )
        for task in tasks
    ]


def route_after_sufficiency(state: dict[str, Any]) -> str:
    """검증 실패와 근거 부족이 합쳐진 failures 가 있으면 실패 기록, 없으면 판단으로 진행."""
    return "record_failure" if state.get("failures") else "competitor_compare"


def route_after_quality(state: dict[str, Any]) -> str:
    return "finalize_report" if (state.get("quality") or {}).get("passed") else "record_failure"


def route_budget(state: dict[str, Any]) -> str:
    """재작업 예산이 남았는가?"""
    return "planner" if state.get("retry_possible") else "apply_fallback"


def route_after_fallback(state: dict[str, Any]) -> str:
    if state.get("failed_stage") == "quality":
        return "finalize_report"  # 품질 미달이지만 예산 소진: 경고를 남기고 보고서 확정
    return "competitor_compare" if active_ids(state) else "save_unresolved"
