"""[노드] 필수 근거 충분성 검사. (기능: 판단·보고서에 필요한 근거가 기업별로 충분한지 규칙으로 판정)

직전 노드(validate_results)의 실패와 함께 한 번에 모아서 내보냅니다. 워커 하나가 실패해도
다른 기업의 근거 부족을 같은 회차에 발견해, 재계획 한 번에 고칠 수 있는 것을 모두 고치게 하기 위해서입니다.
(실패한 결과가 있는 종류는 요약이 없으므로 중복으로 판정하지 않습니다.)
"""

from __future__ import annotations

from typing import Any

from orchestrator import rules
from orchestrator.artifacts import summaries_of
from orchestrator.common import active_ids, get_ctx, log


def check_sufficiency(state: dict[str, Any]) -> dict[str, Any]:
    ctx = get_ctx(state)
    validate_failures = list(state.get("failures") or []) if state.get("stage") == "validate" else []
    sufficiency_failures: list[dict[str, Any]] = []
    for cid in active_ids(state):
        sufficiency_failures += rules.check_company(
            cid,
            state["companies"][cid].get("profile_summary") or {},
            summaries_of(state, cid),
            ctx.settings,
        )
    failures = validate_failures + sufficiency_failures
    stage = (
        "validate+sufficiency"
        if validate_failures and sufficiency_failures
        else "validate"
        if validate_failures
        else "sufficiency"
    )
    names = {cid: meta.get("name", cid) for cid, meta in state["companies"].items()}
    reason = (
        f"검증 실패 {len(validate_failures)}건 + 근거 부족 {len(sufficiency_failures)}건: "
        + ", ".join(f"{names.get(f['company_id'], f['company_id'])}/{f['kind']}({f['code']})" for f in failures)
        if failures
        else "모든 기업의 필수 근거가 충분합니다"
    )
    return {
        "failures": failures,
        "stage": stage,
        "decision_log": [
            log(state, "check_sufficiency", "insufficient" if failures else "sufficient", reason, n_failures=len(failures))
        ],
    }
