"""[노드] 실패 사유·필요 근거 기록. (기능: 이번 단계의 실패를 이력에 남기고 재계획 가능 여부를 정한다)

재계획 횟수가 settings.max_replans(기본 2) 미만이면 플래너에게 재계획을 요청(plan_requested)합니다.
"""

from __future__ import annotations

from typing import Any

from orchestrator.common import get_ctx, log
from orchestrator.observability import now_ts
from orchestrator.rules import REPAIRABLE_REPORT_CODES
from orchestrator.state import WORKER_KINDS

ACTIONABLE_KINDS = (*WORKER_KINDS, "profile")


def record_failure(state: dict[str, Any]) -> dict[str, Any]:
    ctx = get_ctx(state)
    stage = state.get("stage", "")
    failures = state.get("failures") or []
    excluded = state.get("excluded") or {}
    # 워커·프로필 보완이나 보고서 보정으로 고칠 수 있는 실패가 하나도 없으면 재계획은 낭비입니다.
    actionable = [
        f
        for f in failures
        if (
            f.get("kind") in ACTIONABLE_KINDS
            and f.get("company_id")
            and f["company_id"] not in excluded
            and f.get("code") != "fin_coverage_low"  # 데이터를 다시 가져올 방법이 없어 재시도해도 같은 결과
        )
        # 워커 대상은 아니지만 report_draft 가 피드백으로 보정할 수 있는 보고서 문제(표현·인용)도 Loop 대상
        or (f.get("kind") == "report" and f.get("code") in REPAIRABLE_REPORT_CODES)
    ]
    budget_left = (
        int(state.get("replan_count", 0)) < ctx.settings.max_replans
        and int(state.get("step_count", 0)) < ctx.settings.max_steps
    )
    retry_possible = budget_left and bool(actionable)
    return {
        "failure_history": [
            {"round": state.get("replan_count", 0), "stage": stage, "failures": failures, "failed_at": now_ts()}
        ],
        "failed_stage": stage,
        "retry_possible": retry_possible,
        "plan_requested": retry_possible,
        "decision_log": [
            log(
                state,
                "record_failure",
                "retry" if retry_possible else "budget_exhausted",
                f"{stage} 단계 실패 {len(failures)}건 기록. "
                + (
                    f"재계획 {state.get('replan_count', 0) + 1}/{ctx.settings.max_replans}회를 요청합니다"
                    if retry_possible
                    else (
                        f"재계획 예산({ctx.settings.max_replans}회)을 모두 사용했습니다"
                        if not budget_left
                        else "워커로 고칠 수 있는 실패가 없어 재계획하지 않습니다"
                    )
                ),
            )
        ],
    }
