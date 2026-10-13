"""[노드] 실패 사유·미해결 항목 저장. (기능: 보고서를 만들 수 없을 때 이유를 파일로 남기고 종료)"""

from __future__ import annotations

from typing import Any

from orchestrator.common import get_ctx, log


def save_unresolved(state: dict[str, Any]) -> dict[str, Any]:
    ctx = get_ctx(state)
    payload = {
        "run_id": state["run_id"],
        "excluded": state.get("excluded") or {},
        "warnings": state.get("warnings") or [],
        "failure_history": state.get("failure_history") or [],
        "replan_count": state.get("replan_count", 0),
    }
    ref = ctx.store.put("unresolved", "summary", payload)
    return {
        "status": "FAILED",
        "outcome": "failed_unresolved",
        "decision_log": [
            log(state, "save_unresolved", "failed", f"평가 가능한 기업이 없어 보고서를 만들지 못했습니다. 미해결 항목 저장: {ref}")
        ],
    }
