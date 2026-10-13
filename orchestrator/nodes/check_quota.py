"""[노드] 적합 기업 수 확인. (기능: 적합 기업이 목표 수에 못 미치면 부족분 보충 검색을 요청)

보충은 DART 검색부터 다시 하되, 이미 평가한 기업은 그대로 두고 부족한 수만큼만 새로 평가합니다.
재계획 예산과는 별개로 settings.max_topups(기본 1)회까지만 합니다.
"""

from __future__ import annotations

from typing import Any

from orchestrator.common import get_ctx, log


def check_quota(state: dict[str, Any]) -> dict[str, Any]:
    ctx = get_ctx(state)
    suitable = sum(1 for d in (state.get("decisions") or {}).values() if d.get("decision") == "적합")
    target = ctx.settings.target_company_count
    shortfall = max(0, target - suitable)
    used = int(state.get("topup_count") or 0)
    request = shortfall > 0 and used < ctx.settings.max_topups
    if request:
        reason = f"적합 {suitable}곳 < 목표 {target}곳: 부족 {shortfall}곳을 보충 검색합니다 ({used + 1}/{ctx.settings.max_topups}회)"
    elif shortfall > 0:
        reason = f"적합 {suitable}곳 < 목표 {target}곳이지만 보충 상한({ctx.settings.max_topups}회)을 모두 사용했습니다"
    else:
        reason = f"적합 {suitable}곳으로 목표 {target}곳을 채웠습니다"
    return {
        "topup_shortfall": shortfall if request else 0,
        "topup_count": used + 1 if request else used,
        "decision_log": [log(state, "check_quota", "topup" if request else "enough", reason)],
    }
