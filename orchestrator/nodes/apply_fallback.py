"""[노드] 재작업 예산 소진 후 fallback 적용. (기능: 미해결 실패를 종류별 정책(제외/계속)으로 처리)

노션 요구사항 '일부 worker 작업 실패 시 계속/재시도/제외 중 하나를 정해야 함'의 '제외'와 '계속'을 담당합니다. (재시도는 플래너 재계획)
- 제외: 판단에 반드시 필요한 근거(재무, 시장성 점수)를 못 얻은 기업은 평가에서 뺍니다.
- 계속: 서술용·참고용 근거(프로필, 시장 자료, 경쟁사)는 '근거 한계'로 보고서에 표기하고 진행합니다.
- 품질 평가 미달: 보고서는 내되 경고를 남깁니다.
"""

from __future__ import annotations

from typing import Any

from orchestrator.common import log

POLICY = {
    "financial": "exclude",
    "market_score": "exclude",
    "market_rag": "continue",
    "competitor": "continue",
    "profile": "continue",
    "report": "continue",
}


def apply_fallback(state: dict[str, Any]) -> dict[str, Any]:
    stage = state.get("failed_stage", "")
    failures = state.get("failures") or []
    names = {cid: meta.get("name", cid) for cid, meta in (state.get("companies") or {}).items()}
    excluded: dict[str, str] = {}
    warnings: list[str] = []

    for failure in failures:
        cid, kind = failure.get("company_id", ""), failure.get("kind", "")
        label = f"{names.get(cid, cid) or '보고서'} / {kind} ({failure.get('code')}): {failure.get('reason')}"
        if stage != "quality" and POLICY.get(kind) == "exclude" and cid:
            excluded[cid] = f"근거 부족으로 제외 ({kind}): {failure.get('reason')}"
        else:
            warnings.append(("품질 평가 미달 — " if stage == "quality" else "근거 부족 — ") + label)

    return {
        "excluded": excluded,
        "warnings": warnings,
        "failures": [],
        "decision_log": [
            log(
                state,
                "apply_fallback",
                "fallback",
                f"{stage} 단계 미해결 {len(failures)}건: 제외 {len(excluded)}곳, 경고 {len(warnings)}건",
                excluded=list(excluded),
            )
        ],
    }
