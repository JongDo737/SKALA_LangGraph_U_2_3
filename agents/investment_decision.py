# 파일 설명: [투자 판단 에이전트] 시장성 점수와 재무(VBM) 결과를 받아 적합/부적합을 결정하고
# 투자 근거 문장과 평가 요약을 만듭니다.
# 원본: agents/judge.py 의 judge_company 판정부(decide_investment로 분리), investment_reasons, build_* 요약

"""투자 판단 에이전트."""
from __future__ import annotations

from typing import Any

try:
    from agents.financial_vbm import conditional_vbm_acceptable
    from agents.market_scoring import GROUP_REASON_TEMPLATES, MARKET_PASS_SCORE
except ImportError:  # 단독 실행 시
    from financial_vbm import conditional_vbm_acceptable
    from market_scoring import GROUP_REASON_TEMPLATES, MARKET_PASS_SCORE

def percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def investment_reasons(market: dict[str, Any], vbm: dict[str, Any]) -> list[str]:
    """Return concise positive evidence for a final `적합` decision."""
    reasons = []
    for group in market["group_scores"]:
        # 20점 중 14점 이상인 그룹만 투자 강점으로 설명한다.
        if group["score"] is not None and group["score"] >= 14:
            reasons.append(
                GROUP_REASON_TEMPLATES[group["group"]].format(
                    score=f"{group['score']:g}"
                )
            )
    metrics = {
        entry["name"]: entry["value"]
        for entry in vbm.get("metrics", [])
        if entry["status"] == "calculated"
    }
    roic, wacc, gap = (
        metrics.get("roic"),
        vbm.get("wacc"),
        metrics.get("roic_minus_wacc"),
    )
    if roic is not None and wacc is not None and gap is not None and gap > 0:
        reasons.append(
            f"ROIC {percent(roic)}가 WACC {percent(wacc)}를 {gap * 100:.1f}%p 초과했습니다."
        )
    if metrics.get("operating_margin") is not None and metrics["operating_margin"] > 0:
        reasons.append(
            f"영업이익률이 {percent(metrics['operating_margin'])}로 양(+)의 수익성을 보였습니다."
        )
    if metrics.get("roe") is not None and metrics["roe"] > 0:
        reasons.append(f"ROE가 {percent(metrics['roe'])}로 양(+)의 자본수익률을 보였습니다.")
    if metrics.get("roa") is not None and metrics["roa"] > 0:
        reasons.append(f"ROA가 {percent(metrics['roa'])}로 양(+)의 자산수익률을 보였습니다.")
    if (
        metrics.get("interest_coverage") is not None
        and metrics["interest_coverage"] > 1
    ):
        reasons.append(
            f"이자보상배율이 {metrics['interest_coverage']:.2f}배로 이자 부담을 감당할 수준입니다."
        )
    if metrics.get("current_ratio") is not None and metrics["current_ratio"] >= 1:
        reasons.append(
            f"유동비율이 {metrics['current_ratio']:.2f}배로 단기 지급능력이 양호합니다."
        )
    if metrics.get("free_cash_flow") is not None and metrics["free_cash_flow"] > 0:
        reasons.append("잉여현금흐름(FCF)이 양(+)으로 현금창출력이 확인되었습니다.")
    if metrics.get("revenue_cagr") is not None and metrics["revenue_cagr"] > 0:
        reasons.append(
            f"매출 CAGR이 {percent(metrics['revenue_cagr'])}로 성장 추세가 확인되었습니다."
        )
    return reasons



def _company_display_name(company: dict[str, Any]) -> str:
    return str(company.get("name") or company.get("company_name") or "이름 없음")


def _competitor_rows(company: dict[str, Any], judgement: dict[str, Any]) -> list[dict[str, Any]]:
    """웹 검색 경쟁사 목록을 보고서/요약용으로 짧게 정리합니다."""

    research = company.get("competitor_research") or company.get("competition") or {}
    raw = judgement.get("competitors")
    if not isinstance(raw, list) or not raw:
        raw = research.get("competitors") if isinstance(research, dict) else []
    rows: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return rows
    for item in raw[:5]:
        if not isinstance(item, dict):
            continue
        verification = (
            item.get("identity_verification")
            if isinstance(item.get("identity_verification"), dict)
            else {}
        )
        evidence_rows = verification.get("verification_sources") or []
        if not isinstance(evidence_rows, list):
            evidence_rows = []
        rows.append(
            {
                "name": item.get("name") or "",
                "evidence": item.get("evidence") or "",
                "source_url": item.get("source_url"),
                "search_query": item.get("search_query"),
                "competitor_data": item.get("competitor_data")
                if isinstance(item.get("competitor_data"), dict)
                else {},
                "identity_verification": verification,
                "identity_evidence": evidence_rows,
            }
        )
    return rows



def build_company_evaluation_summary(company: dict[str, Any]) -> dict[str, Any]:
    """기업 1건의 경쟁사·judge 평가를 report 직전 JSON 항목으로 압축합니다."""

    judgement = (
        company.get("judgement") if isinstance(company.get("judgement"), dict) else {}
    )
    research = company.get("competitor_research") or company.get("competition") or {}
    if not isinstance(research, dict):
        research = {}
    market = (
        judgement.get("external_market_assessment")
        if isinstance(judgement.get("external_market_assessment"), dict)
        else {}
    )
    vbm = (
        judgement.get("vbm_assessment")
        if isinstance(judgement.get("vbm_assessment"), dict)
        else {}
    )
    competitors = _competitor_rows(company, judgement)
    return {
        "company_name": _company_display_name(company),
        "corp_code": company.get("corp_code") or company.get("id"),
        "estimated_investment_stage": company.get("estimated_investment_stage")
        or (company.get("screening") or {}).get("investment_stage"),
        "subdomain": company.get("subdomain")
        or research.get("subdomain")
        or (company.get("market") or {}).get("subdomain"),
        "decision": judgement.get("decision") or "부적합",
        "reason": judgement.get("reason") or "",
        "external_market_score": judgement.get("external_market_score"),
        "external_market_decision": market.get("decision"),
        "investment_reasons": list(judgement.get("investment_reasons") or [])[:5],
        "vbm": {
            "conclusion": vbm.get("conclusion"),
            "conclusion_reason": vbm.get("conclusion_reason") or vbm.get("summary"),
            "roic": (vbm.get("roic") or {}).get("value")
            if isinstance(vbm.get("roic"), dict)
            else vbm.get("roic"),
            "wacc": vbm.get("wacc"),
        },
        "competitor_research": {
            "status": research.get("status"),
            "found_competitor_count": judgement.get("found_competitor_count")
            if judgement.get("found_competitor_count") is not None
            else research.get("found_competitor_count"),
            "requested_competitor_count": judgement.get("requested_competitor_count")
            if judgement.get("requested_competitor_count") is not None
            else research.get("requested_competitor_count"),
            "selection_method": research.get("selection_method"),
            "competitors": competitors,
        },
        "technical_comparison": judgement.get("technical_comparison")
        or company.get("technical_comparison")
        or research.get("technical_comparison"),
        "competitor_financial_comparison": judgement.get(
            "competitor_financial_comparison"
        )
        or company.get("competitor_financial_comparison"),
    }


def build_final_suitable_companies(
    companies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """적합 판정 기업만 요약 리스트로 반환합니다. report/LLM 요약 직전 핸드오프용."""

    suitable: list[dict[str, Any]] = []
    for company in companies:
        summary = build_company_evaluation_summary(company)
        if summary.get("decision") == "적합":
            suitable.append(summary)
    return suitable


def build_company_evaluation_list(
    companies: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """적합·부적합을 포함한 개별 평가 JSON 리스트."""

    return [build_company_evaluation_summary(company) for company in companies]




def decide_investment(
    market: dict[str, Any], vbm: dict[str, Any], errors: list[str]
) -> tuple[str, str]:
    """시장성·VBM·필수정보 검사 결과로 (decision, reason)을 정합니다. (judge_company에서 분리)"""

    eligible = (
        market["score"] is not None
        and market["score"] >= MARKET_PASS_SCORE
        and conditional_vbm_acceptable(vbm)
        and not errors
    )
    if errors:
        reason = "필수 기업 정보가 누락되었습니다: " + ", ".join(errors)
        decision = "부적합"
    elif eligible:
        reason = (
            f"외부 시장성({MARKET_PASS_SCORE}점 이상)과 VBM 투자 기준을 충족합니다. "
            "경쟁사 정보는 보고서의 참고 자료로 제공합니다."
        )
        decision = "적합"
    elif market["score"] is None:
        reason = (
            "외부 시장 질문의 점수가 완성되지 않아 투자 기준을 충족하지 못했습니다."
        )
        decision = "부적합"
    else:
        reason = "시장성 또는 재무 투자 기준에 미달합니다."
        decision = "부적합"
    return decision, reason


def run_investment_decision_agent(
    market: dict[str, Any], vbm: dict[str, Any], errors: list[str] | None = None
) -> dict[str, Any]:
    """[에이전트 진입점] 기업 1곳의 투자 판단 결과를 반환합니다."""

    decision, reason = decide_investment(market, vbm, list(errors or []))
    return {
        "decision": decision,
        "reason": reason,
        "investment_reasons": investment_reasons(market, vbm) if decision == "적합" else [],
    }
