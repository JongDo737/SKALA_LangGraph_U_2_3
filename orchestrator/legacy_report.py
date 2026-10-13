"""기존 보고서 생성기(agents/report_prep.py, agents/report.py)에 오케스트레이터 결과를 연결하는 어댑터.

노션 요구: "보고서 내용 구성은 지난 보고서 구성과 동일(단 최대 10장)". 지난 보고서의 구성(SUMMARY → 종합 분석 → 시장·경쟁 →
투자 판단·리스크 → REFERENCE)은 기존 생성기가 만들므로, 워커 결과를 그 생성기가 읽는 기업 레코드 형태로 되돌려 넘깁니다.
(품질 평가는 출처 연결이 명시된 마크다운 초안에 대해 수행하고, PDF 는 같은 데이터로 이 생성기가 렌더링합니다.)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.artifacts import payload_of
from orchestrator.store import Store


def legacy_company(state: dict[str, Any], store: Store, cid: str) -> dict[str, Any]:
    """워커 결과 → 기존 파이프라인의 'judge 이후' 기업 레코드."""
    from agents.investment_decision import build_company_evaluation_summary

    meta = state["companies"][cid]
    base = store.get(meta["base_ref"])
    profile = store.get(meta["profile_ref"])
    market = (payload_of(state, store, cid, "market_rag") or {}).get("market") or {}
    assessment = (payload_of(state, store, cid, "market_score") or {}).get("assessment") or {}
    financial = payload_of(state, store, cid, "financial") or {}
    research = (payload_of(state, store, cid, "competitor") or {}).get("research") or {}
    verdict = store.get(state["decisions"][cid]["ref"])
    compare = (
        store.get(state["compare"][cid]["ref"]) if cid in (state.get("compare") or {}) else {}
    )

    raw_sources = market.get("raw_sources") or []
    competitors = [c for c in research.get("competitors", []) if isinstance(c, dict)]
    legacy_market = {
        **{k: v for k, v in market.items() if k not in ("sources", "raw_sources")},
        "sources": raw_sources,
        "chunk": raw_sources,
        "subdomain": profile.get("subdomain"),
        "description": profile.get("description"),
    }
    judgement = {
        "company": profile["name"],
        "decision": verdict["decision"],
        "reason": verdict["reason"],
        "external_market_assessment": assessment,
        "external_market_score": assessment.get("score"),
        "vbm_assessment": financial.get("vbm") or {},
        "investment_reasons": verdict.get("investment_reasons", []),
        "competitor_financial_comparison": compare,
        "found_competitor_count": len(competitors),
        "requested_competitor_count": research.get("requested_competitor_count", 1),
        "competitors": competitors,
        "technical_comparison": {"status": "unavailable", "reason": "기술 비교 근거가 없습니다.", "sources": []},
    }
    company = {
        **base,
        "id": cid,
        "name": profile["name"],
        "company_name": profile["name"],
        "description": profile.get("description", ""),
        "intro": profile.get("description", ""),
        "subdomain": profile.get("subdomain", ""),
        "country": profile.get("country", "KR"),
        "region": profile.get("country", "KR"),
        "financials": profile.get("financials") or [],
        "market": legacy_market,
        "market_context": {
            "source": market.get("status"),
            "summary": market.get("summary") or {},
            "sources": raw_sources,
            "inferred": {
                "topic": profile.get("topic"),
                "description": profile.get("description"),
                "subdomain": profile.get("subdomain"),
                "country": profile.get("country"),
            },
        },
        "competition": {**research, "competitors": competitors},
        "competitor_research": {**research, "competitors": competitors},
        "judgement": judgement,
    }
    company["evaluation"] = build_company_evaluation_summary(company)
    return company


def render_legacy_pdf(
    state: dict[str, Any], store: Store, suitable_ids: list[str], path: Path, use_llm: bool
) -> tuple[Path, dict[str, Any]]:
    """적합 기업을 지난 보고서 구성의 PDF 로 렌더링하고 (경로, report_payload) 를 반환합니다."""
    from agents.report import generate_report
    from agents.report_prep import (
        build_report_md_payload,
        prepare_companies_for_report,
        sanitize_report_payload,
    )

    companies = [legacy_company(state, store, cid) for cid in suitable_ids]
    names = {cid: meta.get("name", cid) for cid, meta in state["companies"].items()}
    rejections = [
        {"name": names.get(cid, cid), "reason": d["reason"], "decision": d["decision"]}
        for cid, d in (state.get("decisions") or {}).items()
        if d["decision"] != "적합"
    ] + [{"name": names.get(cid, cid), "reason": reason, "decision": "제외"} for cid, reason in (state.get("excluded") or {}).items()]
    enriched, bundles = prepare_companies_for_report(companies, use_llm=use_llm)
    payload = build_report_md_payload(
        {
            "eligible_companies": enriched,
            "target_company_count": len(names),
            "judgement_rejections": rejections,
            "dart_fetch_requested": len(names),
        },
        bundles,
    )
    payload = sanitize_report_payload(payload)
    return generate_report(payload, path), payload
