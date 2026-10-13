"""외부 서비스 창구: 기존 agents/ 를 감싸 실제 DART·Serper·OpenAI·ChromaDB 를 호출합니다.

주의: 이 파일은 API 키·네트워크가 필요해서 개발 환경에서 실행 검증을 하지 못했습니다.
기존 agents 의 공개 함수 시그니처에 맞춰 얇게 감싼 코드이며, 처음 실행할 때 오류가 나면
이 파일만 고치면 되도록 외부 호출을 전부 여기에 모았습니다. (그래프 흐름은 tests/ 의 가짜 응답기로 검증)
무거운 의존성(sentence-transformers, chromadb 등)은 메서드 안에서 지연 import 합니다.
"""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

_market_agent = None
_market_agent_lock = threading.Lock()

# DART 수집·스타트업 스크리닝은 async 에이전트입니다. 호출마다 asyncio.run 으로 이벤트 루프를 새로 만들면,
# 첫 호출에서 만들어져 캐시된 HTTP 클라이언트(openai/httpx)가 닫힌 루프에 묶여 두 번째 호출이 'Event loop is closed' 로 실패합니다.
# (부족분 보충 검색에서 실제로 기업 1곳이 이 오류로 잘못 탈락했습니다.) 그래서 루프 하나를 백그라운드에 두고 계속 재사용합니다.
_loop = None
_loop_lock = threading.Lock()


def run_async(coro):
    """코루틴을 프로세스 전체에서 공유하는 이벤트 루프에서 실행하고 결과를 기다립니다. (예외는 그대로 전달)"""
    global _loop
    with _loop_lock:
        if _loop is None or _loop.is_closed():
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, name="orchestrator-async", daemon=True).start()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result()
# 임베딩 모델(torch)과 ChromaDB 클라이언트는 하나를 공유하며 동시 호출에 안전하지 않습니다.
# (락이 없으면 두 워커가 동시에 검색하다 멈추는 것을 실제 실행에서 확인했습니다.)
# 안전이 필요한 것은 '벡터 검색(임베딩 + Chroma 조회)' 뿐이라 그 부분만 직렬화하고, 오래 걸리는 LLM 요약·웹 보완 검색은
# 기업별 워커가 병렬로 진행합니다.
_search_lock = threading.Lock()


def _serialize_search(agent):
    """agent.search_rag 만 한 번에 하나씩 실행하도록 감쌉니다. (나머지 단계는 병렬)"""
    original = agent.search_rag

    def locked(*args, **kwargs):
        with _search_lock:
            return original(*args, **kwargs)

    agent.search_rag = locked
    return agent


def _get_market_agent():
    """임베딩 모델 로딩이 무거우므로 한 번만 만들어 재사용합니다."""
    global _market_agent
    with _market_agent_lock:
        if _market_agent is None:
            from agents.market_agent import MarketAgent

            try:  # chroma_db 가 없는 새 환경에서는 data/for_embed 의 PDF 로 벡터 DB 를 먼저 만듭니다
                from agents.embed import ensure_vector_db

                ensure_vector_db()
            except Exception as error:  # noqa: BLE001
                print(f"[embed] 벡터DB 준비 실패(기존 DB 사용을 시도합니다): {error}")
            _market_agent = _serialize_search(MarketAgent())
    return _market_agent


class Services:

    # ── 입력 수집 ─────────────────────────────────────────────
    def dart_lookup(
        self, limit: int, fetch_multiplier: int = 4, exclude: set[str] | None = None
    ) -> list[dict[str, Any]]:
        from agents.dart import search_energy_startups_dart_only

        results = run_async(
            search_energy_startups_dart_only(
                limit=limit * max(1, fetch_multiplier), exclude_corp_codes=set(exclude or ())
            )
        )
        out = []
        for company in results:
            corp_code = str(company.get("corp_code", ""))
            if not corp_code:
                continue
            out.append(
                {
                    **company,
                    "id": corp_code,
                    "name": company.get("company_name", ""),
                    "established_at": company.get("established_date", ""),
                }
            )
        return out

    def startup_screen(self, companies: list[dict[str, Any]]):
        from agents.startup_screen import screen_startups_node

        result = run_async(
            screen_startups_node(
                {"eligible_companies": companies, "target_company_count": len(companies)}
            )
        )
        return result.get("eligible_companies", []), result.get("startup_rejections", [])

    # ── 공통 기업 프로필: 웹검색 + 검색 결과에서만 추출 ────────────
    def profile(self, base: dict[str, Any], round_no: int, directive: dict[str, Any]):
        from pydantic import BaseModel, Field

        from agents.market_scoring import build_search_client

        name = base.get("company_name") or base.get("name") or ""
        client = build_search_client()
        query = f"{name} 사업 소개 주요 제품 서비스"
        if directive.get("why"):
            query += " 회사 개요"
        try:
            hits = client.search(query, 5)
        except Exception:
            hits = []
        sources = [{"title": h.title, "url": h.url, "snippet": h.snippet} for h in hits]

        class ProfileGuess(BaseModel):
            description: str = Field(description="검색 결과에 근거한 사업 설명 1~2문장. 근거 없으면 빈 문자열")
            topic: str = Field(description="시장 자료 검색용 한국어 사업 분야 문구(15단어 이내)")
            subdomain: str = Field(description="영문 소분류 라벨 (예: solar / PV)")
            country: str = Field(default="KR")

        description = topic = subdomain = ""
        country = "KR"
        if sources and os.getenv("OPENAI_API_KEY"):
            from openai import OpenAI

            snippets = "\n".join(
                f"[{i}] {s['title']}: {s['snippet']}" for i, s in enumerate(sources, 1)
            )
            response = OpenAI().chat.completions.parse(
                model=os.getenv("ORCH_PROFILE_MODEL", "gpt-4o-mini"),
                temperature=0,
                messages=[
                    {
                        "role": "system",
                        "content": "검색 결과 스니펫에 있는 내용만 사용해 기업의 사업 설명과 분야를 정리한다. 근거가 없으면 비워 둔다.",
                    },
                    {"role": "user", "content": f"기업명: {name}\n업종코드: {base.get('industry_code')}\n{snippets}"},
                ],
                response_format=ProfileGuess,
            )
            guess = response.choices[0].message.parsed
            description, topic, subdomain, country = (
                guess.description,
                guess.topic,
                guess.subdomain,
                guess.country or "KR",
            )
        financials = base.get("financials") or (
            [base["financial_summary"]] if base.get("financial_summary") else []
        )
        dart_link = base.get("dart_viewer_link", "")
        if dart_link:
            sources.append({"title": "DART 감사보고서", "url": dart_link, "snippet": ""})
        return {
            "id": base["id"],
            "name": name,
            "description": description,
            "topic": topic,
            "subdomain": subdomain,
            "country": country,
            "sources": [s for s in sources if s.get("url")],
            "method": "web_search",
            "financials": financials,
            "financial_summary": base.get("financial_summary", {}),
            "dart_viewer_link": dart_link,
            "estimated_investment_stage": base.get("estimated_investment_stage", ""),
        }

    # ── 워커용 ────────────────────────────────────────────────
    def market_rag(self, profile: dict[str, Any], round_no: int, directive: dict[str, Any]):
        from agents.rag import to_state_company

        agent = _get_market_agent()
        # 프로필이 확정한 분야를 sector 로 고정 → 재실행해도 검색 질의가 바뀌지 않습니다.
        company = {
            "name": profile["name"],
            "company_name": profile["name"],
            "sector": profile.get("topic") or profile.get("subdomain"),
        }
        if directive.get("add_web_source"):
            work, inferred = agent.prepare(company)
            context = agent.process(company, work, inferred, [])["market_context"]
            enriched = {**company, "market_context": context}
        else:
            enriched = agent.run([company])[0]
        market = to_state_company(enriched)["market"]
        market["raw_sources"] = list(market.get("sources") or [])  # 기존 보고서 생성기용 원본 형식
        sources = []
        for s in market.get("sources") or []:
            if s.get("url"):
                sources.append({"title": s.get("title", ""), "url": s["url"], "kind": "web"})
            else:
                sources.append(
                    {
                        "title": f"{s.get('source', '문서')} p.{s.get('page', '?')}",
                        "url": "",
                        "kind": "rag",
                        "publisher": "내부 문서풀",
                    }
                )
        market["sources"] = sources
        return {"market": market}

    def market_score(self, profile: dict[str, Any], round_no: int, directive: dict[str, Any]):
        from agents.market_scoring import run_market_scoring_agent

        company = {
            "name": profile["name"],
            "description": profile.get("description"),
            "country": profile.get("country", "KR"),
        }
        assessment = run_market_scoring_agent(
            company, profile.get("subdomain") or "energy infrastructure", profile.get("country", "KR")
        )
        return {"assessment": assessment}

    def competitor(self, profile: dict[str, Any], round_no: int, directive: dict[str, Any]):
        """경쟁사 조사.

        1차: 모델이 동일 산업 경쟁사를 제안합니다(빠르지만 검색 근거가 없음).
        재작업(directive.provider == "web"): 제안된 회사를 검색해 **그 회사를 실제로 다루는 외부 출처**를 확인하고
        출처 URL·검색 요약을 붙입니다. 확인하지 못하면 근거 없는 채로 두어 충분성·품질 평가가 그대로 드러냅니다.
        (기존 웹검색 경로는 페이지 제목에서 이름을 뽑아 '보도자료', '홍보센터' 같은 분류명이 경쟁사로 잡혀 쓰지 않습니다)
        """
        from agents.compitition import run
        from agents.market_scoring import build_search_client

        payload = {
            "domain": profile.get("subdomain") or "energy infrastructure",
            "wacc": 0.10,
            "competitors_per_company": 1,
            "companies": [
                {
                    "name": profile["name"],
                    "description": profile.get("description") or profile.get("topic") or "",
                    "subdomain": profile.get("subdomain") or "energy infrastructure",
                    "country": profile.get("country", "KR"),
                    "financials": profile.get("financials") or [],
                    "wacc": 0.10,
                }
            ],
        }
        candidates = directive.get("candidates")
        if candidates:  # 재작업: 1차 후보를 새로 제안받지 않고 그대로 검색 확인만 한다
            research = {
                "company": {"name": profile["name"]},
                "requested_competitor_count": 1,
                "competitors": [dict(c) for c in candidates],
                "selection_method": "openai_recommendation",
            }
        else:
            research = (run(payload, None).get("research_results") or [{}])[0]
        if round_no >= 1 or directive.get("provider") == "web" or directive.get("require_source_url"):
            client = build_search_client()
            own_urls = {s.get("url") for s in profile.get("sources") or []}
            research["competitors"] = [
                _verify_with_search(c, client, own_urls) for c in research.get("competitors") or []
            ]
            research["selection_method"] = "openai_recommendation+search_verification"
        return {"research": research}


def _norm(text: str) -> str:
    import re

    return re.sub(r"주식회사|\(주\)|㈜|co\.?,?\s*ltd\.?|inc\.?|corp\.?|\s|[()\[\]]", "", (text or "").casefold())


def _verify_with_search(competitor: dict[str, Any], client: Any, own_urls: set) -> dict[str, Any]:
    """제안된 경쟁사 이름이 제목·요약에 나오는 외부 출처(저품질·자사 자료 제외)를 찾아 붙입니다."""
    from orchestrator.rules import is_lowquality

    # 채용·구인 사이트는 '그 회사가 존재한다'는 근거일 뿐 경쟁 관계를 보여 주지 못하므로 출처로 쓰지 않습니다.
    job_boards = ("incruit.com", "saramin.co.kr", "jobkorea.co.kr", "jobplanet.co.kr", "catch.co.kr", "wanted.co.kr", "linkedin.com")

    name = competitor.get("name") or ""
    core = _norm(name)
    if len(core) < 2:
        return competitor
    for query in (f"{name} 기업 소개", f"{name} company"):
        try:
            hits = client.search(query, 6)
        except Exception:  # noqa: BLE001 - 검색 실패는 '근거 없음'으로 남깁니다
            hits = []
        for hit in hits:
            if hit.url in own_urls or is_lowquality(hit.url) or any(d in hit.url for d in job_boards):
                continue
            if core in _norm(f"{hit.title} {hit.snippet}"):
                return {
                    **competitor,
                    "source_url": hit.url,
                    "evidence": (hit.snippet or competitor.get("evidence") or "")[:300],
                    "model_evidence": competitor.get("evidence"),
                    "verification": "search_verified",
                }
    return competitor
