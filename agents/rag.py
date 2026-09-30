# 작성자: 손민재
# 파일 설명: 친환경, 에너지, 핵융합 도메인의 시장조사 PDF를 학습(임베딩)한 벡터DB로,
# 스타트업 탐색 에이전트가 넘긴 기업들에 시장 정보를 붙여 다음 단계(경쟁사 비교)로 넘깁니다.
# 벡터DB에 관련 자료가 없으면 웹 검색(Research Nester)으로 보완합니다. (점수·평가는 하지 않습니다.)
#
# ---------------------------------------------------------------------------
# 요약
# ---------------------------------------------------------------------------
# - 호출: market_research_node(state)  ← app.py(LangGraph)가 GraphState(dict)를 넘김. JSON 파일을 읽지 않음.
# - 입력: state["eligible_companies"] (list[dict]), state["market_attempts"], state["execution_log"]
# - 처리(기업별 병렬):
#     1) 분야 정보가 없으면 회사명·업종명(KSIC 조회)·주소로 LLM 이 사업 분야를 추정
#     2) 벡터DB(BGE-M3, Semantic Chunking)에서 가장 가까운 시장 자료 검색 (cosine 거리 0.4 이내)
#     3) 문서 분야(sub_domain)가 기업 분야와 다른 산업이면 제외 → 웹 검색(Research Nester)으로 대체
#     4) 자료를 LLM 으로 시장 규모 / 성장성 / 수요 근거로 정리 (원문은 붙이지 않고 출처만 남김)
# - 출력: 기업마다 description / subdomain / country / market 을 붙여 eligible_companies 로 반환
#     + market_attempts+1, next_stage="compare_competition", execution_log 추가
# - 필요: OPENAI_API_KEY (환경변수 또는 .env), 사전에 embed.py 로 벡터DB(chroma_db) 생성
# ---------------------------------------------------------------------------

"""LangGraph 노드: 시장성 평가 (RAG).

app.py 가 GraphState(dict)를 넘겨 market_research_node(state) 를 호출한다. (JSON 파일을 읽지 않는다.)
state["eligible_companies"] 의 기업마다 아래를 붙여서 반환한다.
  - description : 기업 설명 (입력에 없으면 회사명·업종명 등으로 추정)
  - subdomain   : 영문 소분류 라벨 (예: "solar / PV")
  - country     : 국가 코드 (예: "KR")
  - market      : 시장 정보 (status / distance / chunk / description / subdomain + 정리된 summary)
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from typing import Any

try:  # 프로젝트 루트에서 app.py 실행
    from agents.market_agent import MarketAgent, get_name, get_topic
except ImportError:  # agents/ 폴더에서 단독 실행
    from market_agent import MarketAgent, get_name, get_topic

_agent: MarketAgent | None = None
_agent_lock = threading.Lock()


def get_agent():
    """임베딩 모델 로딩이 무거우므로 한 번만 만들어 재사용한다. (market_attempts 재시도 때도 재사용)"""
    global _agent

    with _agent_lock:
        if _agent is None:
            _agent = MarketAgent()

    return _agent


def market_description(summary):
    """정리된 summary 를 다음 에이전트가 읽기 쉬운 한 문단으로 합친다. (LLM 호출 없음)"""
    if not summary:
        return ""

    parts = [
        summary.get("target_market"),
        f"시장 규모: {summary['market_size']}" if summary.get("market_size") else None,
        f"성장성: {summary['growth']}" if summary.get("growth") else None,
        (
            f"수요 근거: {summary['demand_evidence']}"
            if summary.get("demand_evidence")
            else None
        ),
    ]

    return ". ".join(part for part in parts if part)


def to_state_company(enriched: dict[str, Any]) -> dict[str, Any]:
    """MarketAgent 결과(market_context 포함)를 competition/judge용 State 기업 dict로 바꾼다.

    JSON 파일이 아니라 GraphState.eligible_companies 원소(dict)로 반환한다.
    """
    company = {key: value for key, value in enriched.items() if key != "market_context"}
    context = enriched.get("market_context") or {}
    inferred = context.get("inferred") or {}
    summary = context.get("summary") or {}

    name = get_name(company)
    topic = get_topic(company) or inferred.get("topic") or ""
    subdomain = (
        company.get("subdomain")
        or inferred.get("subdomain")
        or topic
        or "energy infrastructure"
    )
    address = company.get("address") or ""
    country = (
        company.get("country")
        or inferred.get("country")
        or ("KR" if re.search(r"[가-힣]", address) else "KR")
    )

    # description 은 competition REQUIRED — 추정 실패해도 비우지 않는다
    description = (
        company.get("description")
        or company.get("intro")
        or inferred.get("description")
        or market_description(summary)
        or f"{name} (사업 분야 정보 부족)"
    )

    sources = context.get("sources") or []
    distances = [
        source["distance"]
        for source in sources
        if isinstance(source, dict) and "distance" in source
    ]

    market = {
        "status": context.get("source") or "none",  # "rag" | "web" | "none"
        "distance": min(distances) if distances else None,
        "chunk": sources,
        "description": market_description(summary) or description,
        "subdomain": subdomain,
        "summary": summary,
        "topic": inferred.get("topic") or topic,
        "topic_inferred": bool(inferred),
        "topic_basis": inferred.get("basis"),
        "demand": summary.get("demand_evidence"),
        "sources": sources,
    }

    if context.get("rag_rejected"):
        market["rag_rejected"] = context["rag_rejected"]
    if context.get("error"):
        market["error"] = context["error"]

    # financials 가 없으면 financial_summary 1건으로 보완 (judge VBM용)
    financials = company.get("financials")
    if not (isinstance(financials, list) and financials):
        summary_fin = company.get("financial_summary")
        if isinstance(summary_fin, dict) and any(summary_fin.values()):
            row = dict(summary_fin)
            row.setdefault("year", str(company.get("rcept_dt") or "")[:4] or None)
            financials = [row]
        else:
            financials = []

    return {
        **company,
        "id": company.get("id") or company.get("corp_code"),
        "name": company.get("name") or company.get("company_name") or name,
        "company_name": company.get("company_name") or company.get("name") or name,
        "description": description,
        "intro": company.get("intro") or inferred.get("description") or description,
        "subdomain": subdomain,
        "country": country,
        "region": company.get("region") or country,
        "financials": financials,
        "market": market,
        # RAG 원본 구조도 State에 유지 (디버깅·report 참고용)
        "market_context": context,
    }


def market_research_node(state: dict[str, Any]) -> dict[str, Any]:
    """app.py → LangGraph 가 넘기는 GraphState(dict)를 받아 기업별 시장 정보를 붙여 돌려준다."""
    companies: list[dict[str, Any]] = list(state.get("eligible_companies") or [])
    market_attempts = int(state.get("market_attempts") or 0)
    execution_log = list(state.get("execution_log") or [])

    print("\n[작업] 시장성 조사 (RAG)")
    print(
        f"  입력 기업 수 : {len(companies)} / 목표={state.get('target_company_count')}"
    )

    enriched = [to_state_company(company) for company in get_agent().run(companies)]

    counts = Counter(company["market"]["status"] for company in enriched)
    message = (
        f"시장성 RAG 완료: {len(enriched)}개 "
        f"(RAG {counts['rag']} / 웹 {counts['web']} / 정보 없음 {counts['none']})"
    )
    print(f"  {message}")

    return {
        "eligible_companies": enriched,
        "market_attempts": market_attempts + 1,
        "next_stage": "compare_competition",
        "execution_log": [*execution_log, message],
    }
