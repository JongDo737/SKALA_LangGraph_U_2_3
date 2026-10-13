"""[노드] 완료 결과 검증. (기능: 이번 회차 워커 결과가 존재하고 형식이 유효한지 확인)

내용의 충분성은 다음 노드(check_sufficiency)가 봅니다.
"""

from __future__ import annotations

from typing import Any

from orchestrator import rules
from orchestrator.common import get_ctx, log


def _fail(task: dict[str, Any], code: str, reason: str) -> dict[str, Any]:
    return {
        "stage": "validate",
        "company_id": task["company_id"],
        "kind": task["kind"],
        "code": code,
        "reason": reason,
        "needed": "워커 결과",
    }


def validate_results(state: dict[str, Any]) -> dict[str, Any]:
    ctx = get_ctx(state)
    results = state.get("results") or {}
    failures: list[dict[str, Any]] = []
    for task in state.get("subtasks") or []:
        result = results.get(task["id"])
        if result is None or result.get("round") != task["round"]:
            failures.append(_fail(task, "missing", "워커 결과가 도착하지 않았습니다"))
        elif result.get("status") != "ok":
            failures.append(_fail(task, "worker_error", result.get("error", "워커 오류")))
        else:
            issues = rules.validate_payload(task["kind"], ctx.store.get(result["ref"]))
            if issues:
                failures.append(_fail(task, "invalid_payload", "; ".join(issues)))
    return {
        "failures": failures,
        "stage": "validate",
        "decision_log": [
            log(
                state,
                "validate_results",
                "invalid" if failures else "valid",
                f"검증 실패 {len(failures)}건" if failures else "모든 결과가 유효합니다",
                n_failures=len(failures),
            )
        ],
    }
