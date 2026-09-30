# 작성자: 권태현
# 파일 설명: 사전에 정의한 투자 판단 기준을 적용하여
# 기업의 투자 적합 여부를 판단하는 에이전트를 구현합니다.
#
# LangGraph 연결: judgement_node(state) → 적합 기업만 eligible_companies에 유지

"""VBM-based investment-screening agent for energy-infrastructure companies.

ROI needs actual investment cash flows. Before investment, this module uses
financial statements to evaluate value-creation potential instead.
"""
from __future__ import annotations

import argparse
import json
import math
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
except ImportError:  # python agents/judge.py 단독 실행 시
    from compitition import EmptySearchClient, SearchClient, SerperSearchClient


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

# DART-style Korean keys are primary; normalized English keys are also allowed.
KEYS = {
    "assets": ("자산총계", "total_assets"),
    "liabilities": ("부채총계", "total_liabilities"),
    "equity": ("자본총계", "total_equity"),
    "revenue": ("매출액", "revenue"),
    "operating_profit": ("영업이익", "operating_income"),
    "net_income": ("당기순이익", "net_income"),
    "tax_expense": ("법인세비용", "income_tax_expense"),
    "cash": ("현금및현금성자산", "cash_and_cash_equivalents"),
    "debt": ("이자부부채", "이자부채", "interest_bearing_debt"),
    "interest_expense": ("이자비용", "interest_expense"),
    "operating_cf": ("영업활동현금흐름", "operating_cash_flow"),
    "capex": ("CAPEX", "설비투자", "유형자산의취득", "capital_expenditure"),
    "current_assets": ("유동자산", "current_assets"),
    "current_liabilities": ("유동부채", "current_liabilities"),
}


def number(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw) if math.isfinite(raw) else None
    if isinstance(raw, str):
        text = raw.strip()
        if not text or text.lower() in {"null", "none", "-", "n/a"}:
            return None
        # 회계 음수 표기: (1,234) → -1234
        negative = False
        if text.startswith("(") and text.endswith(")"):
            negative = True
            text = text[1:-1]
        text = text.replace(",", "").replace(" ", "")
        try:
            value = float(text)
            if negative:
                value = -abs(value)
            return value if math.isfinite(value) else None
        except ValueError:
            pass
    return None


def field(row: dict[str, Any], name: str) -> float | None:
    return next(
        (result for key in KEYS[name] if (result := number(row.get(key))) is not None),
        None,
    )


def divide(top: float | None, bottom: float | None) -> float | None:
    return None if top is None or bottom in (None, 0) else top / bottom


def metric(
    name: str, value: float | None, formula: str, missing: str, unit: str = "ratio"
) -> dict[str, Any]:
    return {
        "name": name,
        "value": value,
        "unit": unit if value is not None else None,
        "formula": formula,
        "status": "calculated" if value is not None else "unavailable",
        "unavailable_reason": None if value is not None else missing,
    }


def records(company: dict[str, Any]) -> list[dict[str, Any]]:
    rows = company.get("financials", company.get("financial_statements", [company]))
    if not isinstance(rows, list):
        return []
    valid = [row for row in rows if isinstance(row, dict)]
    return sorted(
        valid,
        key=lambda row: number(row.get("year") or row.get("사업연도")) or float("inf"),
    )


def cagr(first: float | None, last: float | None, years: int) -> float | None:
    return (
        None
        if first is None or last is None or first <= 0 or last < 0 or years <= 0
        else (last / first) ** (1 / years) - 1
    )


def vbm_analysis(company: dict[str, Any], default_wacc: Any) -> dict[str, Any]:
    statements = records(company)
    if not statements:
        return {
            "status": "unavailable",
            "reason": "financials must be a list of statement objects",
            "metrics": [],
        }
    current, prior = statements[-1], statements[-2] if len(statements) > 1 else None
    revenue, op, net = (
        field(current, key) for key in ("revenue", "operating_profit", "net_income")
    )
    assets, liabilities, equity = (
        field(current, key) for key in ("assets", "liabilities", "equity")
    )
    items = [
        metric(
            "operating_margin",
            divide(op, revenue),
            "operating_profit / revenue",
            "매출액과 영업이익이 필요합니다.",
        ),
        metric(
            "net_margin",
            divide(net, revenue),
            "net_income / revenue",
            "매출액과 당기순이익이 필요합니다.",
        ),
        metric(
            "debt_to_equity",
            divide(liabilities, equity),
            "total_liabilities / total_equity",
            "부채총계와 자본총계가 필요합니다.",
        ),
        metric(
            "current_ratio",
            divide(
                field(current, "current_assets"), field(current, "current_liabilities")
            ),
            "current_assets / current_liabilities",
            "유동자산과 유동부채가 필요합니다.",
        ),
        metric(
            "interest_coverage",
            divide(op, field(current, "interest_expense")),
            "operating_profit / interest_expense",
            "영업이익과 이자비용이 필요합니다.",
        ),
    ]
    if prior:
        old_assets, old_equity = field(prior, "assets"), field(prior, "equity")
        avg_assets = (
            (assets + old_assets) / 2
            if assets is not None and old_assets is not None
            else None
        )
        avg_equity = (
            (equity + old_equity) / 2
            if equity is not None and old_equity is not None
            else None
        )
        items += [
            metric(
                "roa",
                divide(net, avg_assets),
                "net_income / average_total_assets",
                "당기순이익과 2개년 자산총계가 필요합니다.",
            ),
            metric(
                "roe",
                divide(net, avg_equity),
                "net_income / average_total_equity",
                "당기순이익과 2개년 자본총계가 필요합니다.",
            ),
            metric(
                "revenue_cagr",
                cagr(field(statements[0], "revenue"), revenue, len(statements) - 1),
                "(latest_revenue / first_revenue)^(1/years)-1",
                "2개년 이상 양(+)의 매출액이 필요합니다.",
            ),
            metric(
                "operating_profit_cagr",
                cagr(field(statements[0], "operating_profit"), op, len(statements) - 1),
                "(latest_operating_profit / first_operating_profit)^(1/years)-1",
                "2개년 이상 양(+)의 영업이익이 필요합니다.",
            ),
        ]
    else:
        items += [
            metric(
                "roa",
                None,
                "net_income / average_total_assets",
                "전년도 자산총계가 필요합니다.",
            ),
            metric(
                "roe",
                None,
                "net_income / average_total_equity",
                "전년도 자본총계가 필요합니다.",
            ),
            metric("revenue_cagr", None, "CAGR", "2개년 이상 매출액이 필요합니다."),
            metric(
                "operating_profit_cagr",
                None,
                "CAGR",
                "2개년 이상 영업이익이 필요합니다.",
            ),
        ]
    tax, debt, cash = (
        field(current, "tax_expense"),
        field(current, "debt"),
        field(current, "cash"),
    )
    pretax = net + tax if net is not None and tax is not None else None
    tax_rate = divide(tax, pretax)
    invested_capital = (
        debt + equity - cash if None not in (debt, equity, cash) else None
    )
    nopat = op * (1 - tax_rate) if op is not None and tax_rate is not None else None
    roic = divide(nopat, invested_capital)
    items.append(
        metric(
            "roic",
            roic,
            "NOPAT / (interest_bearing_debt + equity - cash)",
            "법인세비용, 이자부채, 현금, 자본 및 영업이익이 필요합니다.",
        )
    )
    wacc = number(company.get("wacc", default_wacc))
    items.append(
        metric(
            "roic_minus_wacc",
            roic - wacc if roic is not None and wacc is not None else None,
            "ROIC - WACC",
            "계산 가능한 ROIC와 WACC 가정이 필요합니다.",
        )
    )
    ocf, capex = field(current, "operating_cf"), field(current, "capex")
    items.append(
        metric(
            "free_cash_flow",
            ocf - abs(capex) if ocf is not None and capex is not None else None,
            "operating_cash_flow - abs(CAPEX)",
            "영업활동현금흐름과 CAPEX가 필요합니다.",
            "currency",
        )
    )
    values = {
        item["name"]: item["value"] for item in items if item["status"] == "calculated"
    }

    # 공시에 자주 있는 지표(ROE/ROA/유동비율/이자보상)까지 양호 신호로 인정합니다.
    # 양호 3개 이상이면 긍정. ROIC≤WACC만으로 즉시 주의 처리하지 않습니다.
    positive_flags = {
        "operating_margin": values.get("operating_margin") is not None
        and values["operating_margin"] > 0,
        "net_margin": values.get("net_margin") is not None and values["net_margin"] > 0,
        "free_cash_flow": values.get("free_cash_flow") is not None
        and values["free_cash_flow"] > 0,
        "roic_minus_wacc": values.get("roic_minus_wacc") is not None
        and values["roic_minus_wacc"] > 0,
        "revenue_cagr": values.get("revenue_cagr") is not None
        and values["revenue_cagr"] > 0,
        "roe": values.get("roe") is not None and values["roe"] > 0,
        "roa": values.get("roa") is not None and values["roa"] > 0,
        "interest_coverage": values.get("interest_coverage") is not None
        and values["interest_coverage"] > 1,
        "current_ratio": values.get("current_ratio") is not None
        and values["current_ratio"] >= 1,
        "debt_to_equity": values.get("debt_to_equity") is not None
        and 0 < values["debt_to_equity"] <= 5,
        "operating_profit_cagr": values.get("operating_profit_cagr") is not None
        and values["operating_profit_cagr"] > 0,
    }
    positive = sum(positive_flags.values())
    positive_names = [name for name, ok in positive_flags.items() if ok]
    coverage = len(values) / len(items)
    if coverage < 0.4:
        conclusion, reason = "보류", "VBM 핵심 지표의 입력 데이터가 부족합니다."
    elif positive >= 3:
        conclusion, reason = (
            "긍정",
            f"계산 가능한 수익성·안정성·성장성 지표 중 {positive}개가 양호합니다.",
        )
    elif values.get("roic_minus_wacc") is not None and values["roic_minus_wacc"] <= 0:
        conclusion, reason = (
            "주의",
            "양호 지표가 부족하고 ROIC가 WACC를 초과하지 않아 가치 창출 근거가 약합니다.",
        )
    else:
        conclusion, reason = (
            "주의",
            "계산 가능한 VBM 지표에서 가치 창출 근거가 충분하지 않습니다.",
        )
    return {
        "status": "available",
        "statement_years": [row.get("year", row.get("사업연도")) for row in statements],
        "metrics": items,
        "data_coverage": coverage,
        "wacc": wacc,
        "positive_signal_count": positive,
        "positive_signals": positive_names,
        "conclusion": conclusion,
        "conclusion_reason": reason,
    }


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


COMPARISON_METRICS = {
    "operating_margin": ("영업이익률", "higher"),
    "net_margin": ("순이익률", "higher"),
    "debt_to_equity": ("부채비율", "lower"),
    "current_ratio": ("유동비율", "higher"),
    "interest_coverage": ("이자보상배율", "higher"),
    "roa": ("ROA", "higher"),
    "roe": ("ROE", "higher"),
    "revenue_cagr": ("매출 CAGR", "higher"),
    "operating_profit_cagr": ("영업이익 CAGR", "higher"),
    "roic_minus_wacc": ("ROIC-WACC", "higher"),
}


def compare_competitor_vbm(
    target_vbm: dict[str, Any], competitor: dict[str, Any] | None, wacc: Any
) -> dict[str, Any]:
    """Compare like-for-like VBM ratios with a DART-verified competitor."""
    dart = competitor.get("dart") if isinstance(competitor, dict) else None
    if not isinstance(dart, dict) or not isinstance(dart.get("financials"), list):
        return {
            "status": "unavailable",
            "reason": "DART 재무제표가 있는 경쟁사 1개를 확보하지 못했습니다.",
            "competitor": None,
            "conclusion": "비교불가",
            "metrics": [],
        }
    competitor_company = {
        "name": dart.get("name") or competitor.get("name"),
        "financials": dart["financials"],
        "wacc": wacc,
    }
    competitor_vbm = vbm_analysis(competitor_company, wacc)
    target_values = {
        item["name"]: item["value"]
        for item in target_vbm.get("metrics", [])
        if item.get("status") == "calculated"
    }
    competitor_values = {
        item["name"]: item["value"]
        for item in competitor_vbm.get("metrics", [])
        if item.get("status") == "calculated"
    }
    rows: list[dict[str, Any]] = []
    wins = losses = 0
    for metric_id, (label, direction) in COMPARISON_METRICS.items():
        target_value, competitor_value = target_values.get(
            metric_id
        ), competitor_values.get(metric_id)
        if target_value is None or competitor_value is None:
            continue
        # Ratio differences below 1%p are treated as practically similar.
        if abs(target_value - competitor_value) < 0.01:
            result = "유사"
        elif (target_value > competitor_value) == (direction == "higher"):
            result = "우위"
            wins += 1
        else:
            result = "열위"
            losses += 1
        rows.append(
            {
                "metric": metric_id,
                "label": label,
                "target_value": target_value,
                "competitor_value": competitor_value,
                "direction": direction,
                "result": result,
            }
        )
    if len(rows) < 3:
        conclusion, status, reason = (
            "비교불가",
            "unavailable",
            "공통으로 계산 가능한 VBM 지표가 3개 미만입니다.",
        )
    elif wins >= losses + 2:
        conclusion, status, reason = (
            "우위",
            "complete",
            "공통 VBM 지표에서 대상 기업의 우위 항목이 더 많습니다.",
        )
    elif losses >= wins + 2:
        conclusion, status, reason = (
            "열위",
            "complete",
            "공통 VBM 지표에서 대상 기업의 열위 항목이 더 많습니다.",
        )
    else:
        conclusion, status, reason = (
            "유사",
            "complete",
            "공통 VBM 지표에서 뚜렷한 우위 또는 열위가 확인되지 않습니다.",
        )
    return {
        "status": status,
        "reason": reason,
        "competitor": {
            "name": competitor_company["name"],
            "corp_code": dart.get("corp_code"),
            "dart_viewer_link": dart.get("dart_viewer_link"),
            "statement_years": competitor_vbm.get("statement_years", []),
            "financial_source": dart.get("financial_source"),
        },
        "competitor_vbm_assessment": competitor_vbm,
        "conclusion": conclusion,
        "wins": wins,
        "losses": losses,
        "metrics": rows,
    }


def conditional_vbm_acceptable(vbm: dict[str, Any]) -> bool:
    """VBM 적합 여부를 판정합니다.

    - 긍정: 통과
    - 주의: 영업이익률 또는 순이익률이 양수면 통과
      (ROIC≤WACC만으로 탈락시키지 않음 — 완화 기준)
    - 보류/그 외: 탈락
    """

    if vbm.get("conclusion") == "긍정":
        return True
    if vbm.get("conclusion") != "주의":
        return False
    values = {
        entry.get("name"): entry.get("value")
        for entry in vbm.get("metrics", [])
        if entry.get("status") == "calculated"
    }
    operating_margin = values.get("operating_margin")
    net_margin = values.get("net_margin")
    if operating_margin is not None and operating_margin > 0:
        return True
    if net_margin is not None and net_margin > 0:
        return True
    # 마진을 계산하지 못했더라도 ROIC>WACC이면 통과
    spread = values.get("roic_minus_wacc")
    return spread is not None and spread > 0


def judge_company(
    item: dict[str, Any],
    client: SearchClient,
    wacc: Any,
    scorer: OpenAIMarketScorer | None = None,
) -> dict[str, Any]:
    company = item.get("company", {})
    name = company.get("name") or "이름 없음"
    print(f"  [judge] 심사 시작: {name}")
    errors = [field for field in ("name", "description") if not company.get(field)]
    vbm = vbm_analysis(company, wacc)
    requested = int(item.get("requested_competitor_count") or 0)
    found = int(
        item.get("found_competitor_count") or len(item.get("competitors") or [])
    )
    complete = found >= requested
    competitors = (
        item.get("competitors") if isinstance(item.get("competitors"), list) else []
    )
    competitor_names = [
        str((row or {}).get("name") or "").strip()
        for row in competitors
        if isinstance(row, dict) and (row.get("name") or "").strip()
    ]
    print(
        f"  [judge] {name} 경쟁사 근거: {found}/{requested} "
        f"complete={complete}, names={competitor_names[:5]}"
    )
    comparison = compare_competitor_vbm(
        vbm, competitors[0] if competitors else None, wacc
    )
    comparison_complete = comparison["status"] == "complete"
    technical = (
        item.get("technical_comparison")
        if isinstance(item.get("technical_comparison"), dict)
        else {}
    )
    technical_complete = technical.get("status") == "evidence_collected"
    # DART 재무 비교는 있으면 열위 여부를 추가 검토하되, 공시 재무가 없다는
    # 이유만으로 웹 기반 경쟁사 비교를 실패 처리하지 않는다.
    market = external_assessment(
        item.get("subdomain", "energy infrastructure"),
        company.get("country") or company.get("region") or "",
        company,
        client,
        scorer,
    )
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
    reasons = investment_reasons(market, vbm) if decision == "적합" else []
    print(
        f"  [judge] 심사 완료: {name} → {decision} "
        f"(시장={market.get('decision')}, score={market.get('score')}, "
        f"VBM={vbm.get('conclusion')}/{vbm.get('conclusion_reason')}, "
        f"경쟁사={found}/{requested}, 비교={comparison.get('conclusion')})"
    )
    return {
        "company": company.get("name"),
        "internal_validation": "failed" if errors else "passed",
        "validation_errors": errors,
        "competitor_research_complete": complete,
        "found_competitor_count": found,
        "requested_competitor_count": requested,
        "competitors": competitors,
        "technical_comparison": technical
        or {
            "status": "unavailable",
            "reason": "기술 비교 근거가 없습니다.",
            "sources": [],
        },
        "competitor_financial_comparison": comparison,
        "competitor_comparison_complete": comparison_complete,
        "competitor_reference_only": True,
        "vbm_assessment": vbm,
        "external_market_assessment": market,
        "external_market_score": market["score"],
        "decision": decision,
        "reason": reason,
        "investment_reasons": reasons,
        "roi": {
            "status": "not_evaluated",
            "reason": "실제 투자금·지분·회수 현금흐름이 없어 ROI를 산출하지 않습니다.",
        },
    }


def run(
    payload: dict[str, Any],
    client: SearchClient,
    scorer: OpenAIMarketScorer | None = None,
) -> dict[str, Any]:
    items = list(payload.get("research_results", []))
    if not items:
        return {
            "validation": payload.get("validation", {}),
            "question_count": len(QUESTIONS),
            "results": [],
        }

    workers = max(1, min(JUDGE_COMPANY_CONCURRENCY, len(items)))
    print(f"  [judge] {len(items)}개 기업 심사 (동시 {workers}개, 질문검색 병렬)")
    results: list[dict[str, Any] | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(judge_company, item, client, payload.get("wacc"), scorer): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as error:
                company = items[index].get("company") or {}
                name = company.get("name") or f"기업{index + 1}"
                print(f"  [judge] 심사 실패 ({name}): {error} → 이 기업만 건너뜁니다.")
    return {
        "validation": payload.get("validation", {}),
        "question_count": len(QUESTIONS),
        "results": [item for item in results if item is not None],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input", default="-", help="competitor-research JSON path, or - for stdin"
    )
    parser.add_argument(
        "--search-provider", choices=("serper", "empty"), default="empty"
    )
    parser.add_argument(
        "--market-scorer", choices=("manual", "openai"), default="manual"
    )
    parser.add_argument(
        "--openai-model",
        default=None,
        help="OpenAI model; defaults to OPENAI_MODEL or gpt-5",
    )
    args = parser.parse_args()
    raw = (
        sys.stdin.read()
        if args.input == "-"
        else open(args.input, encoding="utf-8").read()
    )
    client: SearchClient = (
        SerperSearchClient()
        if args.search_provider == "serper"
        else EmptySearchClient()
    )
    scorer = (
        OpenAIMarketScorer(model=args.openai_model)
        if args.market_scorer == "openai"
        else None
    )
    print(
        json.dumps(run(json.loads(raw), client, scorer), ensure_ascii=False, indent=2)
    )


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------------------
# LangGraph 노드
# ---------------------------------------------------------------------------
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


def judgement_node(state: dict[str, Any]) -> dict[str, Any]:
    """경쟁사 조사 결과를 바탕으로 적합/부적합을 판단합니다.

    이미 ``judgement.decision == 적합``인 기업은 재심사하지 않고 유지합니다.
    적합 기업만 ``eligible_companies``에 남겨 보고서 단계로 넘깁니다.
    부적합 결과는 진단용 상태에만 보존합니다.
    report 직전 핸드오프용으로 ``final_suitable_companies``(적합 요약 리스트)와
    ``company_evaluations``(전체 평가 리스트)를 함께 반환합니다.
    """

    from copy import deepcopy

    try:
        from config import DEFAULT_WACC
    except ImportError:
        DEFAULT_WACC = 0.10

    attempt = int(state.get("judgement_attempts", 0)) + 1
    input_companies = list(state.get("eligible_companies", []))
    competition_payload = state.get("competition_payload") or {}
    prior_rejections = list(state.get("judgement_rejections", []))
    seen_corp_codes = set(state.get("dart_seen_corp_codes", []))
    default_wacc = state.get("wacc")
    if default_wacc is None:
        default_wacc = DEFAULT_WACC

    already_suitable: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for company in input_companies:
        judgement = (
            company.get("judgement")
            if isinstance(company.get("judgement"), dict)
            else {}
        )
        if judgement.get("decision") == "적합":
            already_suitable.append(deepcopy(company))
        else:
            pending.append(company)

    def _financials_of(company: dict[str, Any]) -> list[dict[str, Any]]:
        financials = company.get("financials")
        if isinstance(financials, list) and financials:
            return [row for row in financials if isinstance(row, dict)]
        summary = company.get("financial_summary")
        if isinstance(summary, dict) and any(summary.values()):
            row = dict(summary)
            row.setdefault("year", str(company.get("rcept_dt") or "")[:4] or None)
            return [row]
        return []

    def _research_item(company: dict[str, Any]) -> dict[str, Any]:
        item = company.get("competitor_research") or company.get("competition")
        market_context = (
            company.get("market_context")
            if isinstance(company.get("market_context"), dict)
            else {}
        )
        inferred = (
            market_context.get("inferred")
            if isinstance(market_context.get("inferred"), dict)
            else {}
        )
        if isinstance(item, dict) and (
            item.get("competitors") is not None
            or item.get("found_competitor_count") is not None
            or item.get("company")
        ):
            patched = deepcopy(item)
            company_payload = dict(patched.get("company") or {})
            company_payload["name"] = (
                company_payload.get("name")
                or company.get("name")
                or company.get("company_name")
            )
            company_payload["description"] = (
                company_payload.get("description")
                or company.get("description")
                or (company.get("market") or {}).get("description")
                or inferred.get("description")
                or ""
            )
            company_payload["country"] = (
                company_payload.get("country") or company.get("country") or "KR"
            )
            financials = _financials_of(company_payload) or _financials_of(company)
            if financials:
                company_payload["financials"] = financials
            company_payload["wacc"] = (
                company_payload.get("wacc") or company.get("wacc") or default_wacc
            )
            competitors = (
                patched.get("competitors")
                if isinstance(patched.get("competitors"), list)
                else []
            )
            found = int(patched.get("found_competitor_count") or len(competitors))
            requested = int(
                patched.get("requested_competitor_count")
                if patched.get("requested_competitor_count") is not None
                else max(len(input_companies), 1)
            )
            patched.update(
                {
                    "company": company_payload,
                    "subdomain": patched.get("subdomain")
                    or company.get("subdomain")
                    or inferred.get("subdomain")
                    or "energy infrastructure",
                    "competitors": competitors,
                    "found_competitor_count": found,
                    "requested_competitor_count": requested,
                }
            )
            return patched
        competitors = []
        return {
            "company": {
                "name": company.get("name") or company.get("company_name"),
                "description": company.get("description")
                or inferred.get("description")
                or "",
                "country": company.get("country") or inferred.get("country") or "KR",
                "financials": _financials_of(company),
                "wacc": company.get("wacc") or default_wacc,
            },
            "subdomain": company.get("subdomain")
            or inferred.get("subdomain")
            or "energy infrastructure",
            "requested_competitor_count": max(len(input_companies), 1),
            "found_competitor_count": 0,
            "competitors": competitors,
            "status": "missing",
        }

    # 기업별 competitor_research를 우선하고, 없으면 competition_payload를 보조로 씁니다.
    payload_by_name = {
        str((item.get("company") or {}).get("name") or "").casefold(): item
        for item in (competition_payload.get("research_results") or [])
        if isinstance(item, dict)
    }
    patched_results = []
    for company in pending:
        name_key = str(
            company.get("name") or company.get("company_name") or ""
        ).casefold()
        from_company = _research_item(company)
        from_payload = payload_by_name.get(name_key)
        if from_payload and not from_company.get("competitors"):
            merged = deepcopy(from_payload)
            merged_company = dict(merged.get("company") or {})
            financials = _financials_of(merged_company) or _financials_of(company)
            if financials:
                merged_company["financials"] = financials
            merged_company["description"] = (
                merged_company.get("description") or company.get("description") or ""
            )
            merged_company["wacc"] = (
                merged_company.get("wacc") or company.get("wacc") or default_wacc
            )
            merged["company"] = merged_company
            competitors = (
                merged.get("competitors")
                if isinstance(merged.get("competitors"), list)
                else []
            )
            merged["found_competitor_count"] = int(
                merged.get("found_competitor_count") or len(competitors)
            )
            patched_results.append(merged)
        else:
            patched_results.append(from_company)

    payload = {
        "validation": competition_payload.get("validation", {}),
        "wacc": default_wacc,
        "research_results": patched_results,
    }
    print("\n[작업] 투자 적합 판단")
    print(
        f"  입력 State : 기업="
        f"{[_company_display_name(c) for c in input_companies]} "
        f"(재심사={len(pending)}개, 기존 적합={len(already_suitable)}개, wacc={default_wacc})"
    )
    for item in patched_results:
        company = item.get("company") or {}
        competitors = item.get("competitors") or []
        print(
            f"  [judge] 입력 경쟁사 패키지: {company.get('name')} "
            f"{item.get('found_competitor_count')}/{item.get('requested_competitor_count')} "
            f"financials={len(company.get('financials') or [])}년, "
            f"competitors={[c.get('name') for c in competitors[:5] if isinstance(c, dict)]}"
        )
    client = build_search_client()
    scorer = build_market_scorer()
    try:
        result = (
            run(payload, client, scorer)
            if patched_results
            else {
                "validation": payload["validation"],
                "question_count": len(QUESTIONS),
                "results": [],
            }
        )
    except Exception as error:
        print(
            f"  [judge] 일괄 심사 중단: {error} "
            "→ 지금까지 적합 판정된 기업만으로 보고서를 이어갑니다."
        )
        result = {
            "validation": payload["validation"],
            "question_count": len(QUESTIONS),
            "results": [],
        }

    newly_suitable: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    judged_all: list[dict[str, Any]] = []
    decision_by_name = {
        str(item.get("company") or "").casefold(): item
        for item in result.get("results", [])
    }
    for company in pending:
        name = _company_display_name(company)
        decision_row = decision_by_name.get(name.casefold())
        updated = deepcopy(company)
        updated["judgement"] = decision_row or {
            "decision": "부적합",
            "reason": "판단 결과 없음",
        }
        if decision_row and isinstance(
            decision_row.get("competitor_financial_comparison"), dict
        ):
            updated["competitor_financial_comparison"] = decision_row[
                "competitor_financial_comparison"
            ]
        if decision_row and isinstance(decision_row.get("technical_comparison"), dict):
            updated["technical_comparison"] = decision_row["technical_comparison"]
        judged_all.append(updated)
        if decision_row and decision_row.get("decision") == "적합":
            newly_suitable.append(updated)
        else:
            rejected.append(updated)
            prior_rejections.append(
                {
                    "name": name,
                    "reason": str(
                        (decision_row or {}).get("reason")
                        or updated["judgement"].get("reason")
                        or "투자 기준 미달"
                    ),
                    "decision": (decision_row or {}).get("decision") or "부적합",
                    "external_market_score": (decision_row or {}).get(
                        "external_market_score"
                    ),
                }
            )

    # 최종 응답·보고서에는 적합 기업만 전달한다. 부적합 결과는 진단용
    # judgement_rejections / company_evaluations에 남긴다.
    kept = [*already_suitable, *newly_suitable]
    # 원본 기업 필드는 유지하고 evaluation 요약만 붙입니다.
    for company in kept:
        company["evaluation"] = build_company_evaluation_summary(company)
    company_evaluations = build_company_evaluation_list([*judged_all, *already_suitable])
    # already_suitable이 judged_all에 없으면 뒤에 붙였으므로 이름 기준 중복 제거
    seen_eval: set[str] = set()
    deduped_evaluations: list[dict[str, Any]] = []
    for row in company_evaluations:
        key = str(row.get("company_name") or "").casefold()
        if key in seen_eval:
            continue
        seen_eval.add(key)
        deduped_evaluations.append(row)
    company_evaluations = deduped_evaluations
    final_suitable = build_final_suitable_companies(kept)

    message = (
        f"{attempt}차 투자 판단: 적합 {len(final_suitable)}개, "
        f"부적합 {len(rejected)}개 제외 → 보고서 후보 {len(kept)}개"
    )
    print(
        f"  반환 값    : 보고서 후보="
        f"{[_company_display_name(c) for c in kept]} "
        f"(적합={[_company_display_name(c) for c in already_suitable + newly_suitable]}, "
        f"부적합={[_company_display_name(c) for c in rejected]})"
    )
    print("\n[최종 적합 기업 리스트] report/LLM 요약 직전 핸드오프")
    if not final_suitable:
        print("  (적합 기업 없음)")
    for index, row in enumerate(final_suitable, 1):
        comps = (row.get("competitor_research") or {}).get("competitors") or []
        print(
            f"  [{index}] {row.get('company_name')} | "
            f"decision={row.get('decision')} | "
            f"score={row.get('external_market_score')} | "
            f"VBM={(row.get('vbm') or {}).get('conclusion')} | "
            f"경쟁사={len(comps)}개"
        )
        for reason in (row.get("investment_reasons") or [])[:2]:
            print(f"       · {reason}")
    for row in result.get("results", []):
        print(
            f"  · {row.get('company')}: decision={row.get('decision')} / "
            f"{row.get('reason')}"
        )
    for company in already_suitable:
        print(
            f"  · {_company_display_name(company)}: "
            "decision=적합 / 이전 판단 유지"
        )

    return {
        "eligible_companies": kept,
        "final_suitable_companies": final_suitable,
        "company_evaluations": company_evaluations,
        "judgement_attempts": attempt,
        "judgement_payload": result,
        "judgement_rejections": prior_rejections,
        "dart_seen_corp_codes": list(seen_corp_codes),
        "wacc": default_wacc,
        "next_stage": "generate_report",
        "execution_log": [*state.get("execution_log", []), message],
    }
