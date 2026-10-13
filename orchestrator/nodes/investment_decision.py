"""[노드] 투자 판단. (기능: 시장성 점수 + 재무(VBM) → 적합/부적합 결정)

판정 규칙은 agents/investment_decision.py (기존 judge.py 에서 분리한 코드)를 그대로 씁니다.
"""

from __future__ import annotations

from typing import Any

from orchestrator.artifacts import payload_of, profile_of
from orchestrator.common import active_ids, get_ctx, log


def investment_decision(state: dict[str, Any]) -> dict[str, Any]:
    from agents.investment_decision import run_investment_decision_agent

    ctx = get_ctx(state)
    decisions: dict[str, Any] = {}
    for cid in active_ids(state):
        financial = payload_of(state, ctx.store, cid, "financial")
        score = payload_of(state, ctx.store, cid, "market_score")
        if not financial or not score:
            continue
        profile = profile_of(state, ctx.store, cid)
        errors = [f for f in ("name", "description") if not profile.get(f)]
        market = score["assessment"]
        verdict = run_investment_decision_agent(market, financial["vbm"], errors)
        verdict.update(
            company_id=cid,
            name=profile["name"],
            market_score=market.get("score"),
            vbm_conclusion=financial["vbm"].get("conclusion"),
        )
        verdict["ref"] = ctx.store.put("decision", cid, verdict)
        decisions[cid] = {
            "decision": verdict["decision"],
            "reason": verdict["reason"],
            "market_score": verdict["market_score"],
            "vbm_conclusion": verdict["vbm_conclusion"],
            "ref": verdict["ref"],
        }
    suitable = sum(1 for d in decisions.values() if d["decision"] == "적합")
    return {
        "decisions": decisions,
        "decision_log": [
            log(state, "investment_decision", "decided", f"적합 {suitable}곳 / 부적합 {len(decisions) - suitable}곳")
        ],
    }
