# 파일 설명: [시장성 채점 에이전트] 외부 시장성 질문 10개를 웹 검색 근거로 채점해
# 100점 만점 점수와 그룹별 점수를 만듭니다.
# 원본: agents/judge.py 의 QUESTIONS / OpenAIMarketScorer / external_assessment 등 (코드는 그대로 이동)

"""시장성 채점 에이전트.

오케스트레이터는 ``run_market_scoring_agent`` 하나만 호출하면 됩니다.
"""
from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from prompts import load_prompt

try:
    from agents.compitition import EmptySearchClient, SearchClient, SerperSearchClient
    from agents.financial_vbm import number
except ImportError:  # 단독 실행 시
    from compitition import EmptySearchClient, SearchClient, SerperSearchClient
    from financial_vbm import number

# 기업당 시장 질문 검색·채점 동시성. Serper/OpenAI rate limit을 피하려고 제한합니다.
JUDGE_COMPANY_CONCURRENCY = int(os.getenv("JUDGE_COMPANY_CONCURRENCY", "2"))
JUDGE_SEARCH_CONCURRENCY = int(os.getenv("JUDGE_SEARCH_CONCURRENCY", "5"))
JUDGE_SEARCH_LIMIT = int(os.getenv("JUDGE_SEARCH_LIMIT", "3"))
JUDGE_OPENAI_TIMEOUT = int(os.getenv("JUDGE_OPENAI_TIMEOUT", "120"))


QUESTIONS = (
    (
        "market_growth",
        "해당 세부 시장은 향후 3~5년 동안 충분한 규모와 성장률을 보일 것으로 예상되는가?",
    ),
    (
        "power_demand",
        "대상 지역에서 AI 데이터센터, 전기화, 산업 수요 증가로 전력 수요가 확대되고 있는가?",
    ),
    (
        "grid_bottleneck",
        "송전·배전망, 계통연계, 변압기 또는 ESS의 구조적 병목이 해당 솔루션 수요를 만드는가?",
    ),
    (
        "structural_need",
        "해당 문제는 단기 수요 급증이 아니라 지속적인 구조적 문제인가?",
    ),
    (
        "policy_support",
        "정부의 전력망·에너지저장장치·청정전력 투자 계획 또는 규제가 시장 성장을 지원하는가?",
    ),
    (
        "regulatory_risk",
        "인허가, 안전 기준, 전력시장 제도가 신규 공급자의 시장 진입을 과도하게 제한하지 않는가?",
    ),
    (
        "customer_capex",
        "유틸리티, 하이퍼스케일러, 발전사 또는 산업 고객이 관련 설비투자를 확대하고 있는가?",
    ),
    (
        "customer_pain",
        "고객이 솔루션을 도입하지 않을 때 발생하는 비용 또는 위험이 충분히 큰가?",
    ),
    (
        "competition",
        "경쟁 강도가 차별화된 신규 사업자가 진입할 여지를 남길 만큼 적정한가?",
    ),
    (
        "entry_room",
        "해당 지역과 세부 도메인에 신규 공급자가 진입할 수 있는 실질적 시장 여지가 있는가?",
    ),
)



class OpenAIMarketScorer:
    """Scores the ten market questions from the search evidence only.

    The API key is read from OPENAI_API_KEY; it is never stored in the result.
    """

    endpoint = "https://api.openai.com/v1/responses"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-5")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is required for the openai market scorer")

    @staticmethod
    def schema() -> dict[str, Any]:
        score = {
            "type": "object",
            "additionalProperties": False,
            "required": ["score", "reason", "source_indices"],
            "properties": {
                "score": {"type": "integer", "minimum": 0, "maximum": 10},
                "reason": {"type": "string"},
                "source_indices": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 1},
                },
            },
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["scores"],
            "properties": {
                "scores": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [identifier for identifier, _ in QUESTIONS],
                    "properties": {identifier: score for identifier, _ in QUESTIONS},
                }
            },
        }

    def score(
        self, company: dict[str, Any], questions: list[dict[str, Any]]
    ) -> dict[str, dict[str, Any]]:
        evidence = [
            {
                "id": question["id"],
                "question": question["question"],
                "sources": [
                    {
                        "index": index + 1,
                        "title": source["title"],
                        "snippet": source["snippet"],
                        "url": source["url"],
                    }
                    for index, source in enumerate(question["evidence"])
                ],
            }
            for question in questions
        ]
        instructions = load_prompt("judge_market_score")
        payload = {
            "model": self.model,
            "store": False,
            "instructions": instructions,
            "input": json.dumps(
                {
                    "company": {
                        "name": company.get("name"),
                        "description": company.get("description"),
                        "country": company.get("country") or company.get("region"),
                    },
                    "questions": evidence,
                },
                ensure_ascii=False,
            ),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "market_scores",
                    "strict": True,
                    "schema": self.schema(),
                }
            },
        }
        request = Request(
            self.endpoint,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=JUDGE_OPENAI_TIMEOUT) as response:
                result = json.loads(response.read().decode("utf-8"))
            output_text = result.get("output_text")
            if not output_text:
                # Responses API 일부 응답은 output[].content[].text 형태입니다.
                chunks: list[str] = []
                for item in result.get("output") or []:
                    for content in item.get("content") or []:
                        text = content.get("text")
                        if text:
                            chunks.append(text)
                output_text = "".join(chunks)
            return json.loads(output_text)["scores"]
        except (
            TimeoutError,
            HTTPError,
            URLError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise RuntimeError(f"OpenAI market scoring failed: {exc}") from exc


# 모든 질문은 10점 만점이며, 그룹별 두 질문의 합계는 20점이다.

GROUPS = (
    ("시장 수요·성장성", ("market_growth", "power_demand")),
    ("인프라 문제의 필요성", ("grid_bottleneck", "structural_need")),
    ("정책·규제 환경", ("policy_support", "regulatory_risk")),
    ("고객 투자·구매 필요성", ("customer_capex", "customer_pain")),
    ("경쟁·진입 가능성", ("competition", "entry_room")),
)

# 외부 시장성 적합 하한(100점 만점). 환경변수로 조정 가능.
MARKET_PASS_SCORE = int(os.getenv("JUDGE_MARKET_MIN_SCORE", "50"))


def credible(url: str) -> bool:
    return any(
        token in url.casefold()
        for token in (
            ".gov",
            ".org",
            ".edu",
            "iea.org",
            "irena.org",
            "worldbank.org",
            "sec.gov",
        )
    )


def external_score(raw_score: Any) -> tuple[float | None, str | None]:
    """Accept only explicit 0--10 scores; never infer a score from hit counts."""
    score = number(raw_score)
    if score is None:
        return None, "점수 입력 또는 근거 평가가 필요합니다."
    if not 0 <= score <= 10:
        return None, "점수는 0점 이상 10점 이하여야 합니다."
    return score, None


def grouped_market_score(questions: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {question["id"]: question for question in questions}
    groups = []
    for name, ids in GROUPS:
        missing = [
            identifier for identifier in ids if by_id[identifier]["score"] is None
        ]
        score = (
            None if missing else sum(by_id[identifier]["score"] for identifier in ids)
        )
        groups.append(
            {
                "group": name,
                "max_score": 20,
                "score": score,
                "status": "pending" if missing else "calculated",
                "missing_question_ids": missing,
            }
        )
    total = (
        None
        if any(group["score"] is None for group in groups)
        else sum(group["score"] for group in groups)
    )
    if total is None:
        decision, reason = (
            "부적합",
            "10개 외부 시장 질문의 점수가 모두 입력되지 않아 시장성 기준을 충족하지 못했습니다.",
        )
    elif total >= MARKET_PASS_SCORE:
        decision, reason = (
            "적합",
            f"외부 시장성 점수가 {MARKET_PASS_SCORE}점 이상입니다.",
        )
    else:
        decision, reason = (
            "부적합",
            f"외부 시장성 점수가 {MARKET_PASS_SCORE}점 미만입니다.",
        )
    return {
        "max_score": 100,
        "score": total,
        "groups": groups,
        "decision": decision,
        "reason": reason,
    }


def external_assessment(
    subdomain: str,
    region: str,
    company: dict[str, Any],
    client: SearchClient,
    scorer: OpenAIMarketScorer | None = None,
) -> dict[str, Any]:
    supplied_scores = company.get("external_market_scores")
    supplied_scores = supplied_scores if isinstance(supplied_scores, dict) else {}
    short_domain = re.split(r"[|/]", subdomain or "energy")[0].strip()[:40] or "energy"
    short_region = (region or "KR").strip()[:20]

    def _search_one(identifier: str, question: str) -> dict[str, Any]:
        query = re.sub(
            r"\s+", " ", f"{short_domain} {short_region} {question}"
        ).strip()[:200]
        try:
            sources = client.search(query, JUDGE_SEARCH_LIMIT)
        except Exception as error:
            print(f"  [judge] 검색 실패 ({company.get('name')}/{identifier}): {error}")
            sources = []
        raw_score = supplied_scores.get(identifier)
        score, score_error = external_score(
            raw_score.get("score") if isinstance(raw_score, dict) else raw_score
        )
        return {
            "id": identifier,
            "question": question,
            "max_score": 10,
            "score": score,
            "score_status": "calculated" if score is not None else "pending",
            "score_reason": (
                raw_score.get("reason") if isinstance(raw_score, dict) else None
            ),
            "source_indices": (
                raw_score.get("source_indices", [])
                if isinstance(raw_score, dict)
                else []
            ),
            "score_unavailable_reason": score_error,
            "search_query": query,
            "evidence": [
                {
                    "title": x.title,
                    "snippet": x.snippet,
                    "url": x.url,
                    "credibility": (
                        "institutional" if credible(x.url) else "commercial_or_news"
                    ),
                }
                for x in sources
            ],
        }

    # 질문별 웹 검색을 병렬로 수행합니다. (기업당 10회 직렬 호출이 멈춘 것처럼 보이던 원인)
    output: list[dict[str, Any]] = [None] * len(QUESTIONS)  # type: ignore[list-item]
    with ThreadPoolExecutor(
        max_workers=max(1, min(JUDGE_SEARCH_CONCURRENCY, len(QUESTIONS)))
    ) as pool:
        futures = {
            pool.submit(_search_one, identifier, question): index
            for index, (identifier, question) in enumerate(QUESTIONS)
        }
        for future in as_completed(futures):
            output[futures[future]] = future.result()

    scoring_error = None
    if scorer is not None and not supplied_scores:
        try:
            generated_scores = scorer.score(company, output)
            for question in output:
                generated = generated_scores.get(question["id"], {})
                score, score_error = external_score(generated.get("score"))
                question.update(
                    {
                        "score": score,
                        "score_status": (
                            "calculated" if score is not None else "pending"
                        ),
                        "score_reason": generated.get("reason"),
                        "source_indices": generated.get("source_indices", []),
                        "score_unavailable_reason": score_error,
                    }
                )
        except (RuntimeError, TimeoutError) as exc:
            scoring_error = str(exc)
            print(
                f"  [judge] OpenAI 채점 실패 ({company.get('name')}): {scoring_error}"
            )
    summary = grouped_market_score(output)
    return {
        "questions": output,
        "group_scores": summary["groups"],
        "max_score": summary["max_score"],
        "score": summary["score"],
        "decision": summary["decision"],
        "reason": summary["reason"],
        "scoring_method": (
            "openai"
            if scorer is not None and not supplied_scores
            else "provided_scores"
        ),
        "scoring_error": scoring_error,
    }



GROUP_REASON_TEMPLATES = {
    "시장 수요·성장성": "시장 성장성과 전력 수요 측면에서 {score}/20점을 받았습니다.",
    "인프라 문제의 필요성": "전력 인프라 병목이 구조적 수요로 이어질 가능성이 확인되어 {score}/20점을 받았습니다.",
    "정책·규제 환경": "정책 지원 및 규제 환경 측면에서 {score}/20점을 받았습니다.",
    "고객 투자·구매 필요성": "핵심 고객의 CAPEX와 솔루션 도입 필요성 측면에서 {score}/20점을 받았습니다.",
    "경쟁·진입 가능성": "경쟁 강도와 신규 사업자 진입 여지 측면에서 {score}/20점을 받았습니다.",
}



def build_search_client() -> SearchClient:
    key = (os.getenv("SERPER_API_KEY") or "").strip().strip("\"'")
    if not key or key.casefold().startswith("your_"):
        print("  [judge] SERPER_API_KEY 없음/예시값 → EmptySearchClient 사용")
        return EmptySearchClient()
    try:
        return SerperSearchClient(key)
    except ValueError as error:
        print(f"  [judge] Serper 비활성: {error}")
        return EmptySearchClient()


def build_market_scorer() -> OpenAIMarketScorer | None:
    if (
        os.getenv("OPENAI_API_KEY")
        and os.getenv("JUDGE_MARKET_SCORER", "openai") != "manual"
    ):
        try:
            return OpenAIMarketScorer(model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
        except ValueError as error:
            print(f"  [judge] OpenAI scorer 비활성: {error}")
    print("  [judge] 시장성 점수 scorer=manual (제공 점수 없으면 보류 가능)")
    return None




_UNSET = object()


def run_market_scoring_agent(
    company: dict[str, Any],
    subdomain: str,
    region: str,
    client: Any = _UNSET,
    scorer: Any = _UNSET,
) -> dict[str, Any]:
    """[에이전트 진입점] 기업 1곳의 외부 시장성 평가를 반환합니다.

    ``client``/``scorer``를 주지 않으면 환경변수 기준 기본 구현을 만듭니다.
    """

    if client is _UNSET:
        client = build_search_client()
    if scorer is _UNSET:
        scorer = build_market_scorer()
    return external_assessment(subdomain, region, company, client, scorer)
