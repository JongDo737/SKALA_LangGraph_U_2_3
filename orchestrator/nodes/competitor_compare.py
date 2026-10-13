"""[노드] 경쟁사 재무 비교. (기능: 대상 기업과 DART 재무가 있는 경쟁사의 VBM 지표 비교 — 참고용)"""

from __future__ import annotations

from typing import Any

from orchestrator.artifacts import payload_of
from orchestrator.common import active_ids, get_ctx, log


def competitor_compare(state: dict[str, Any]) -> dict[str, Any]:
    from agents.competitor_compare import run_competitor_compare_agent
    from config import DEFAULT_WACC

    ctx = get_ctx(state)
    compare: dict[str, Any] = {}
    for cid in active_ids(state):
        financial = payload_of(state, ctx.store, cid, "financial")
        research = payload_of(state, ctx.store, cid, "competitor")
        if not financial:
            continue
        competitors = ((research or {}).get("research") or {}).get("competitors") or []
        result = run_competitor_compare_agent(
            financial["vbm"], competitors[0] if competitors else None, DEFAULT_WACC
        )
        ref = ctx.store.put("compare", cid, result)
        compare[cid] = {"conclusion": result.get("conclusion"), "status": result.get("status"), "ref": ref}
    return {
        "compare": compare,
        "decision_log": [
            log(state, "competitor_compare", "compared", f"{len(compare)}곳 경쟁사 재무 비교 (참고용)")
        ],
    }
