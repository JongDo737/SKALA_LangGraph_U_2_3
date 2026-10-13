"""[노드] 스타트업 적격 검증. (기능: 스타트업·투자 단계 필터)

보충 검색(topup_shortfall)이면 기존 평가 대상은 그대로 두고, 새로 통과한 기업을 부족한 수만큼만 더합니다.
"""

from __future__ import annotations

from typing import Any

from orchestrator.common import get_ctx, log
from orchestrator.rules import fin_coverage, has_financial_data


def startup_screen(state: dict[str, Any]) -> dict[str, Any]:
    ctx = get_ctx(state)
    shortfall = int(state.get("topup_shortfall") or 0)
    ids = state.get("candidate_ids", [])
    bases = [ctx.store.get(state["companies"][cid]["base_ref"]) for cid in ids]
    passed, rejected = ctx.services.startup_screen(bases)
    # DART 조회는 탈락을 대비해 목표 수보다 많이 가져옵니다. 통과한 기업 중 재무 데이터가 있는 후보를 먼저 뽑아
    # (PDF 다운로드 실패 등으로 재무가 비어 평가가 불가능한 기업이 자리를 차지하지 않게) 목표 수까지만 평가합니다.
    # 순위: ① 재무 지표를 충분히 계산할 수 있는 기업 ② 계산 가능 비율이 기준(min_fin_coverage) 미만인 기업 ③ 재무 데이터 없음.
    # (수집된 재무 키가 많아도 계산 가능 지표가 적으면 근거 부족으로 제외돼 자리만 차지하기 때문입니다. 같은 순위끼리는 기존 순서 유지)
    def rank(base: dict[str, Any]) -> int:
        if not has_financial_data(base):
            return 2
        return 0 if fin_coverage(base) >= ctx.settings.min_fin_coverage else 1

    want = shortfall or ctx.settings.target_company_count
    passed = sorted(passed, key=rank)[:want]

    passed_ids = []
    companies: dict[str, Any] = {}
    for base in passed:
        cid = str(base.get("id") or base.get("corp_code"))
        companies[cid] = {"base_ref": ctx.store.put("base", cid, base)}
        passed_ids.append(cid)

    rejected_names = {r.get("name") for r in rejected}
    excluded = {}
    for cid in ids:
        # 탈락 사유가 있는 기업만 제외로 기록합니다. (목표 수 초과로 평가하지 않은 기업은 기록하지 않음)
        if cid not in passed_ids and state["companies"][cid]["name"] in rejected_names:
            reason = next(
                (r.get("reason") for r in rejected if r.get("name") == state["companies"][cid]["name"]),
                "스타트업 적격 검증 탈락",
            )
            excluded[cid] = f"스타트업 검증 탈락: {reason}"

    kept = list(state.get("company_ids", [])) if shortfall else []
    return {
        "company_ids": kept + passed_ids,
        "topup_ids": passed_ids if shortfall else [],
        "topup_shortfall": 0,
        "companies": companies,
        "excluded": excluded,
        "decision_log": [
            log(
                state,
                "startup_screen",
                "screened",
                f"통과 {len(passed_ids)}곳, 탈락 {len(excluded)}곳" + (f" (보충, 기존 {len(kept)}곳 유지)" if shortfall else ""),
                passed=len(passed_ids),
                rejected=len(excluded),
            )
        ],
    }
