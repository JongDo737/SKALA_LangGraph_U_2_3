"""테스트 전용 가짜 외부 서비스 (제품 코드 아님).

저장된 실제 실행 결과(tests/fixtures/final_companies_sample.json)를 돌려주어, 실제로는 원할 때 재현할 수 없는
상황(재계획 소진, 품질 미달 Loop, 워커 실패, 노드 중단 후 재개)을 pytest 로 검증하는 데만 씁니다.
회차(round)에 따라 응답이 달라져 재계획이 결과를 개선하는지 확인할 수 있습니다.
가짜로 만든 출처는 URL 이 https://test.invalid/ 로 시작하고 제목에 [test-sim] 이 붙습니다.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

# 원본 DART 기업 레코드에서 '파생 결과'로 간주해 제거할 키 (에이전트가 만들어 붙이는 값)
DERIVED_KEYS = {
    "market",
    "market_context",
    "competition",
    "competitor_research",
    "competitor_financial_comparison",
    "technical_comparison",
    "judgement",
    "evaluation",
    "report_source",
    "report_enrichment",
    "report_market_sources",
    "description",
    "intro",
    "subdomain",
    "country",
    "region",
}


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "final_companies_sample.json"

SCENARIOS: dict[str, dict[str, Any]] = {
    # 저장된 데이터를 그대로 재생: 시장 출처 1건 기업, 출처 없는 경쟁사 때문에 자연스럽게 재계획이 일어난다
    "fixture": {},
    # 프로필 웹검색 출처가 비어서 '프로필 추가 보완' 경로를 타는 경우
    "profile_retry": {"profile_no_source": ["유진솔라"]},
    # 프로필 출처가 많아도 REFERENCE에는 인용된 자료만 남는지 (회귀 테스트용)
    "many_profile_sources": {"profile_extra_sources": 4},
    # 1차 재계획 후 경쟁사 서술에 우열 표현이 들어와 품질 평가에서 2차 재계획을 타는 경우
    "quality_loop": {"competitor_biased_once": True},
    # 재작업해도 해결되지 않아 재계획 상한(2회)에서 fallback(제외·경고)으로 종료되는 경우
    "exhaust": {"always_fail": [["디케이", "market_score"]], "never_ground": ["동인"]},
    # 적합이 목표(5곳)에 못 미쳐 DART 검색부터 다시 해서 부족분만 보충하는 경우 (처음 5곳 중 2곳이 시장성 미달)
    "topup": {"low_score": ["유진", "동인"], "extra_companies": 3},
    # 보충해도 새 기업까지 시장성 미달이라 1회 보충으로 끝나는 경우 (보충은 최대 1회)
    "topup_still_short": {"low_score": ["유진", "동인", "보충"], "extra_companies": 3},
    # 보충 검색을 해도 새로 통과한 기업이 없는 경우
    "topup_none_found": {"low_score": ["유진", "동인"], "extra_companies": 0},
}

ACTIVE: "FakeServices | None" = None


def use(name: str) -> "FakeServices":
    """이후 그래프 실행이 사용할 시나리오를 고른다."""
    global ACTIVE
    ACTIVE = FakeServices(FIXTURE, SCENARIOS[name])
    return ACTIVE


class WorkerFailure(RuntimeError):
    """시나리오가 의도한 워커 실패."""


def _match(name: str, patterns: list[str]) -> bool:
    return any(p in name for p in patterns)


class FakeServices:

    def __init__(self, fixture_path: Path, scenario: dict[str, Any]) -> None:
        data = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
        self.companies = {c["id"]: c for c in data["eligible_companies"]}
        self.scenario = scenario or {}
        # 보충 검색 시나리오: 같은 데이터를 복제한 가상의 새 기업 (id x1.., 이름에 '보충')
        originals = list(self.companies.values())
        for i in range(int(self.scenario.get("extra_companies", 0))):
            clone = copy.deepcopy(originals[i % len(originals)])
            clone["id"], clone["name"] = f"x{i + 1}", f"보충기업{i + 1}호"
            self.companies[clone["id"]] = clone

    # ── helpers ───────────────────────────────────────────────
    def _src(self, company_id: str) -> dict[str, Any]:
        return self.companies[company_id]

    def _maybe_fail(self, name: str, kind: str, round_no: int) -> None:
        for pattern, k in self.scenario.get("always_fail", []):
            if k == kind and pattern in name:
                raise WorkerFailure(f"[test-sim] {name} {kind} 워커가 계속 실패합니다")
        for pattern, k in self.scenario.get("fail_once", []):
            if k == kind and pattern in name and round_no == 0:
                raise WorkerFailure(f"[test-sim] {name} {kind} 워커가 1회 실패합니다")

    # ── 입력 수집 ─────────────────────────────────────────────
    def dart_lookup(self, limit: int, fetch_multiplier: int = 1, exclude: set | None = None) -> list[dict[str, Any]]:
        out = []
        fresh = [c for c in self.companies.values() if c["id"] not in (exclude or set())]
        for company in fresh[:limit]:
            out.append(
                {k: copy.deepcopy(v) for k, v in company.items() if k not in DERIVED_KEYS}
            )
        return out

    def startup_screen(self, companies: list[dict[str, Any]]):
        passed = []
        for company in companies:
            screening = self._src(company["id"]).get("screening") or {}
            if screening.get("is_startup", True):
                passed.append({**company, "screening": screening})
        return passed, []

    # ── 공통 기업 프로필 (웹검색 시뮬레이션) ─────────────────────
    def profile(self, base: dict[str, Any], round_no: int, directive: dict[str, Any]):
        src = self._src(base["id"])
        name = src.get("name", "")
        sources: list[dict[str, str]] = []
        no_source = round_no == 0 and _match(name, self.scenario.get("profile_no_source", []))
        if not no_source:
            if src.get("dart_viewer_link"):
                sources.append({"title": "DART 감사보고서", "url": src["dart_viewer_link"]})
            home = re.search(r"https?://[^\s)\]]+", str(src.get("homepage_url") or ""))
            if home:
                sources.append({"title": "기업 홈페이지", "url": home.group(0)})
        for i in range(int(self.scenario.get("profile_extra_sources", 0))):
            sources.append(
                {"title": f"[test-sim] 추가 프로필 출처 {i + 1}", "url": f"https://test.invalid/profile/{base['id']}/{i + 1}"}
            )
        return {
            "id": base["id"],
            "name": name,
            "description": src.get("description", ""),
            "topic": src.get("description", ""),
            "subdomain": src.get("subdomain", ""),
            "country": src.get("country", "KR"),
            "sources": sources,
            "method": "web_search(test-fixture)",
            # 재무는 DART 원본을 그대로 씁니다
            "financials": src.get("financials", []),
            "financial_summary": src.get("financial_summary", {}),
            "dart_viewer_link": src.get("dart_viewer_link", ""),
            "estimated_investment_stage": src.get("estimated_investment_stage", ""),
        }

    # ── 워커용 ────────────────────────────────────────────────
    def market_rag(self, profile: dict[str, Any], round_no: int, directive: dict[str, Any]):
        name = profile["name"]
        self._maybe_fail(name, "market_rag", round_no)
        market = copy.deepcopy(self._src(profile["id"]).get("market", {}))
        market["raw_sources"] = copy.deepcopy(market.get("sources") or [])  # 기존 보고서 생성기용 원본 형식
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
        want = int(directive.get("min_sources", 0)) if directive.get("add_web_source") else 0
        while len(sources) < want:
            i = len(sources) + 1
            sources.append(
                {
                    "title": f"[test-sim] 보충 시장 자료 {i}",
                    "url": f"https://test.invalid/market/{profile['id']}/{i}",
                    "kind": "web",
                }
            )
        market["sources"] = sources
        return {"market": market}

    def market_score(self, profile: dict[str, Any], round_no: int, directive: dict[str, Any]):
        name = profile["name"]
        self._maybe_fail(name, "market_score", round_no)
        judgement = self._src(profile["id"]).get("judgement") or {}
        assessment = copy.deepcopy(judgement.get("external_market_assessment", {}))
        if _match(name, self.scenario.get("low_score", [])):
            assessment["score"] = 10  # 시장성 기준(50점) 미달 → 부적합
        return {"assessment": assessment}

    def competitor(self, profile: dict[str, Any], round_no: int, directive: dict[str, Any]):
        name = profile["name"]
        self._maybe_fail(name, "competitor", round_no)
        research = copy.deepcopy(self._src(profile["id"]).get("competitor_research") or {})
        competitors = [c for c in research.get("competitors", []) if isinstance(c, dict)]
        grounded = (directive.get("provider") == "web" or round_no >= 1) and not _match(
            name, self.scenario.get("never_ground", [])
        )
        for i, comp in enumerate(competitors, 1):
            if grounded:
                comp["source_url"] = f"https://test.invalid/competitor/{profile['id']}/{i}"
            if (
                self.scenario.get("competitor_biased_once")
                and round_no == 1
                and not directive.get("neutral_tone")
            ):
                comp["evidence"] = (comp.get("evidence") or "") + " 업계 최고 수준의 기술력을 보유합니다."
        research["competitors"] = competitors
        research["selection_method"] = (
            "web_search(test-sim)" if grounded else "openai_recommendation"
        )
        return {"research": research}
