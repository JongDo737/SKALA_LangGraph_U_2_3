# 작성자: 통합 담당자 신종민
# 파일 설명: rag.py는 담당자가 구현하므로 여기에서는 건드리지 않습니다.
# DART(+스크리닝) State를 RAG 개발자가 남긴 enriched 출력 형태에 가깝게
# 변환해 경쟁사 비교(compitition) 에이전트로 넘깁니다.
#
# RAG 담당 예상 흐름(참고):
# - 기업별 분야 문구로 Semantic Chunking RAG 검색
# - 거리 > 0.4 이면 Research Nester(웹) 1건 대체
# - 평가/점수는 하지 않고 시장 정보만 부착
# - 입력 필드: name/description/intro 등 → *_enriched.json 형태
#
# 현재 프로젝트는 JSON 파일이 아니라 GraphState.eligible_companies로
# 기업을 전달하므로, 동일 계약을 State 위에서 맞춥니다.

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any

from state import GraphState

# RAG 담당자가 쓰던 거리 기준. 실제 임베딩이 붙기 전에는 bridge 상태만 표시합니다.
RAG_DISTANCE_THRESHOLD = 0.4


def _company_name(company: dict[str, Any]) -> str:
    return str(company.get("company_name") or company.get("name") or "이름 없음")


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _tips_intro(company: dict[str, Any]) -> str:
    tips = company.get("tips") if isinstance(company.get("tips"), dict) else {}
    for key in ("intro", "description", "summary", "과제명", "title"):
        text = _clean(tips.get(key) or company.get(key))
        if text:
            return text
    return ""


def _screening_blurb(company: dict[str, Any]) -> str:
    screening = company.get("screening") if isinstance(company.get("screening"), dict) else {}
    parts = [
        _clean(screening.get("reason")),
        _clean(company.get("estimated_investment_stage")),
        _clean(company.get("legal_class")),
    ]
    return " / ".join(part for part in parts if part)


def build_description(company: dict[str, Any]) -> str:
    """경쟁사·판단 에이전트가 요구하는 description을 State에서 구성합니다."""

    chunks = [
        _tips_intro(company),
        _screening_blurb(company),
        _clean(company.get("address")),
    ]
    financial = company.get("financial_summary") if isinstance(company.get("financial_summary"), dict) else {}
    if financial.get("매출액"):
        chunks.append(f"매출액 {financial.get('매출액')}")
    if financial.get("자산총계"):
        chunks.append(f"자산총계 {financial.get('자산총계')}")
    text = " | ".join(part for part in chunks if part)
    return text or f"{_company_name(company)} 에너지·인프라 관련 기업"


def build_query_candidates(company: dict[str, Any]) -> list[str]:
    """RAG 다중 질의 전략을 흉내 낸 후보 질의입니다. 실제 검색은 rag.py 담당."""

    name = _company_name(company)
    intro = _tips_intro(company) or build_description(company)
    short = re.split(r"[|/]|관련", intro)[0].strip()[:40]
    stage = _clean(company.get("estimated_investment_stage"))
    queries = [
        f"{name} {intro}".strip(),
        f"{name} {short}".strip(),
        f"{name} 에너지 인프라 {stage}".strip(),
    ]
    # 중복·공백 제거
    unique: list[str] = []
    seen: set[str] = set()
    for query in queries:
        key = re.sub(r"\s+", " ", query).strip().casefold()
        if key and key not in seen:
            seen.add(key)
            unique.append(re.sub(r"\s+", " ", query).strip())
    return unique


def infer_subdomain(company: dict[str, Any]) -> str:
    text = " ".join(
        [
            _company_name(company),
            build_description(company),
            _clean(company.get("industry_code")),
        ]
    ).casefold()
    mapping = [
        (("배터리", "battery", "ess"), "battery / ESS"),
        (("수소", "hydrogen"), "hydrogen energy"),
        (("태양광", "솔라", "solar", "pv"), "solar / PV"),
        (("전력", "그리드", "송전", "배전", "grid"), "grid / power infrastructure"),
        (("충전", "ev"), "EV charging"),
        (("풍력", "wind"), "wind energy"),
    ]
    for keywords, label in mapping:
        if any(keyword in text for keyword in keywords):
            return label
    return "energy infrastructure"


def financials_as_statements(company: dict[str, Any]) -> list[dict[str, Any]]:
    """judge VBM이 기대하는 financials 리스트로 변환합니다."""

    financials = company.get("financials")
    if isinstance(financials, list):
        rows = [row for row in financials if isinstance(row, dict) and any(
            value not in {None, "", "-"} for key, value in row.items() if key != "year"
        )]
        if rows:
            return rows

    summary = company.get("financial_summary")
    if isinstance(summary, dict) and any(summary.values()):
        row = dict(summary)
        row.setdefault("year", company.get("rcept_dt", "")[:4] or None)
        return [row]
    return []


def build_market_attachment(company: dict[str, Any]) -> dict[str, Any]:
    """RAG *_enriched.json에 가까운 market 필드를 State에 붙입니다.

    실제 Chroma/BGE-M3 검색은 rag.py 담당자가 채웁니다.
    여기 bridge는 입력 계약을 맞추고, 검색 전 상태를 명시합니다.
    """

    queries = build_query_candidates(company)
    description = build_description(company)
    intro = _tips_intro(company)
    return {
        "status": "pending_rag",  # rag | web | none | pending_rag
        "source": "state_bridge",
        "distance_threshold": RAG_DISTANCE_THRESHOLD,
        "distance": None,
        "query": queries[0] if queries else _company_name(company),
        "query_candidates": queries,
        "chunk": None,
        "web_fallback": None,
        "description": description,
        "intro": intro or None,
        "subdomain": infer_subdomain(company),
        "needs_web_search": True,
        "note": (
            "rag.py 미연결 상태입니다. DART·스크리닝 State로 description/subdomain을 "
            "구성했고, 경쟁사 비교 에이전트 입력 계약을 맞췄습니다."
        ),
    }


def enrich_company_for_competition(company: dict[str, Any]) -> dict[str, Any]:
    """1(DART)+스크리닝 State → 2(RAG 예상 enrich) → 3(경쟁사) 입력 형태로 변환."""

    enriched = deepcopy(company)
    name = _company_name(company)
    market = build_market_attachment(company)
    enriched["name"] = name
    enriched["company_name"] = enriched.get("company_name") or name
    enriched["description"] = market["description"]
    enriched["intro"] = market.get("intro") or enriched.get("intro")
    enriched["subdomain"] = market["subdomain"]
    enriched["country"] = enriched.get("country") or "KR"
    enriched["region"] = enriched.get("region") or "KR"
    enriched["financials"] = financials_as_statements(company)
    enriched["market"] = market
    return enriched


def market_bridge_node(state: GraphState) -> dict[str, Any]:
    """rag.py를 대체하지 않는 State 브리지 노드입니다."""

    attempt = int(state.get("market_attempts", 0)) + 1
    input_companies = list(state.get("eligible_companies", []))
    enriched = [enrich_company_for_competition(company) for company in input_companies]

    print("\n[작업] 시장성 조사 (브리지)")
    print(
        f"  입력 State : DART 기업="
        f"{[_company_name(company) for company in input_companies]}"
    )
    print(
        "  반환 값    : "
        + ", ".join(
            f"{company.get('name')}/"
            f"{(company.get('market') or {}).get('subdomain')}/"
            f"{(company.get('market') or {}).get('status')}"
            for company in enriched
        )
    )
    print("\n" + "=" * 70)
    print(f"[RAG 계약 브리지] competition 입력 enrich ({len(enriched)}개)")
    print("=" * 70)
    for index, company in enumerate(enriched, 1):
        handoff = {
            "name": company.get("name"),
            "description": company.get("description"),
            "subdomain": company.get("subdomain"),
            "country": company.get("country"),
            "financials": company.get("financials"),
            "market": company.get("market"),
            "estimated_investment_stage": company.get("estimated_investment_stage"),
            "dart_viewer_link": company.get("dart_viewer_link"),
        }
        print(f"\n--- [{index}] {company.get('name')} ---")
        print(json.dumps(handoff, ensure_ascii=False, indent=2, default=str))
    print("\n" + "=" * 70 + "\n")

    message = (
        f"{attempt}차 시장성 브리지: State {len(enriched)}개 기업을 "
        "RAG enriched 계약으로 변환해 경쟁사 비교로 전달"
    )
    return {
        "eligible_companies": enriched,
        "market_attempts": attempt,
        "next_stage": "compare_competition",
        "execution_log": [*state.get("execution_log", []), message],
    }
