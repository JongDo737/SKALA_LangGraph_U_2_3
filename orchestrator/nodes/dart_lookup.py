"""[노드] DART 공시에서 후보 기업의 원본 레코드를 수집합니다. (기능: 후보 수집)

처음에는 평가 목표 수만큼, 적합 기업이 모자라 보충을 요청받으면(topup_shortfall) 부족한 수만큼만 찾습니다.
보충 때는 앞서 본 기업(평가했든 탈락했든)을 다시 가져오지 않도록 제외합니다.
"""

from __future__ import annotations

from typing import Any

from orchestrator.common import get_ctx, log


def dart_lookup(state: dict[str, Any]) -> dict[str, Any]:
    ctx = get_ctx(state)
    shortfall = int(state.get("topup_shortfall") or 0)
    known = set((state.get("companies") or {}).keys()) if shortfall else set()
    bases = ctx.services.dart_lookup(
        limit=shortfall or ctx.settings.target_company_count,
        fetch_multiplier=ctx.settings.dart_fetch_multiplier,
        exclude=known,
    )
    ids: list[str] = []
    companies: dict[str, Any] = {}
    for base in bases:
        cid = str(base.get("id") or base.get("corp_code"))
        if cid in known:
            continue
        ref = ctx.store.put("base", cid, base)
        ids.append(cid)
        companies[cid] = {"name": base.get("name") or base.get("company_name"), "base_ref": ref}
    what = f"보충 후보 {len(ids)}곳 수집 (부족 {shortfall}곳, 기존 {len(known)}곳 제외)" if shortfall else f"후보 {len(ids)}곳 수집"
    return {
        "candidate_ids": ids,
        "companies": companies,
        "decision_log": [log(state, "dart_lookup", "collected", what, n=len(ids))],
    }
