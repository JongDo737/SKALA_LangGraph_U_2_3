# 작성자: 권태현
# 파일 설명: 사전에 정의한 투자 판단 기준을 적용하여
# 10개 기업의 투자 적합 여부를 판단하는 에이전트를 구현합니다.
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
JUDGE_OPENAI_TIMEOUT = int(os.getenv("JUDGE_OPENAI_TIMEOUT", "45"))



QUESTIONS = (
    ("market_growth", "해당 세부 시장은 향후 3~5년 동안 충분한 규모와 성장률을 보일 것으로 예상되는가?"),
    ("power_demand", "대상 지역에서 AI 데이터센터, 전기화, 산업 수요 증가로 전력 수요가 확대되고 있는가?"),
    ("grid_bottleneck", "송전·배전망, 계통연계, 변압기 또는 ESS의 구조적 병목이 해당 솔루션 수요를 만드는가?"),
    ("structural_need", "해당 문제는 단기 수요 급증이 아니라 지속적인 구조적 문제인가?"),
    ("policy_support", "정부의 전력망·에너지저장장치·청정전력 투자 계획 또는 규제가 시장 성장을 지원하는가?"),
    ("regulatory_risk", "인허가, 안전 기준, 전력시장 제도가 신규 공급자의 시장 진입을 과도하게 제한하지 않는가?"),
    ("customer_capex", "유틸리티, 하이퍼스케일러, 발전사 또는 산업 고객이 관련 설비투자를 확대하고 있는가?"),
    ("customer_pain", "고객이 솔루션을 도입하지 않을 때 발생하는 비용 또는 위험이 충분히 큰가?"),
    ("competition", "경쟁 강도가 차별화된 신규 사업자가 진입할 여지를 남길 만큼 적정한가?"),
    ("entry_room", "해당 지역과 세부 도메인에 신규 공급자가 진입할 수 있는 실질적 시장 여지가 있는가?"),
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
            "type": "object", "additionalProperties": False,
            "required": ["score", "reason", "source_indices"],
            "properties": {
                "score": {"type": "integer", "minimum": 0, "maximum": 10},
                "reason": {"type": "string"},
                "source_indices": {"type": "array", "items": {"type": "integer", "minimum": 1}},
            },
        }
        return {
            "type": "object", "additionalProperties": False, "required": ["scores"],
            "properties": {
                "scores": {
                    "type": "object", "additionalProperties": False,
                    "required": [identifier for identifier, _ in QUESTIONS],
                    "properties": {identifier: score for identifier, _ in QUESTIONS},
                }
            },
        }

    def score(self, company: dict[str, Any], questions: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        evidence = [{
            "id": question["id"], "question": question["question"],
            "sources": [{"index": index + 1, "title": source["title"], "snippet": source["snippet"], "url": source["url"]}
                        for index, source in enumerate(question["evidence"])]
        } for question in questions]
        instructions = load_prompt("judge_market_score")
        payload = {
            "model": self.model,
            "store": False,
            "instructions": instructions,
            "input": json.dumps({"company": {"name": company.get("name"), "description": company.get("description"), "country": company.get("country") or company.get("region")}, "questions": evidence}, ensure_ascii=False),
            "text": {"format": {"type": "json_schema", "name": "market_scores", "strict": True, "schema": self.schema()}},
        }
        request = Request(self.endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}, method="POST")
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
        except (HTTPError, URLError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"OpenAI market scoring failed: {exc}") from exc

# 모든 질문은 10점 만점이며, 그룹별 두 질문의 합계는 20점이다.
GROUPS = (
    ("시장 수요·성장성", ("market_growth", "power_demand")),
    ("인프라 문제의 필요성", ("grid_bottleneck", "structural_need")),
    ("정책·규제 환경", ("policy_support", "regulatory_risk")),
    ("고객 투자·구매 필요성", ("customer_capex", "customer_pain")),
    ("경쟁·진입 가능성", ("competition", "entry_room")),
)

# DART-style Korean keys are primary; normalized English keys are also allowed.
KEYS = {
    "assets": ("자산총계", "total_assets"), "liabilities": ("부채총계", "total_liabilities"),
    "equity": ("자본총계", "total_equity"), "revenue": ("매출액", "revenue"),
    "operating_profit": ("영업이익", "operating_income"), "net_income": ("당기순이익", "net_income"),
    "tax_expense": ("법인세비용", "income_tax_expense"), "cash": ("현금및현금성자산", "cash_and_cash_equivalents"),
    "debt": ("이자부부채", "이자부채", "interest_bearing_debt"), "interest_expense": ("이자비용", "interest_expense"),
    "operating_cf": ("영업활동현금흐름", "operating_cash_flow"), "capex": ("CAPEX", "설비투자", "유형자산의취득", "capital_expenditure"),
    "current_assets": ("유동자산", "current_assets"), "current_liabilities": ("유동부채", "current_liabilities"),
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
    return next((result for key in KEYS[name] if (result := number(row.get(key))) is not None), None)


def divide(top: float | None, bottom: float | None) -> float | None:
    return None if top is None or bottom in (None, 0) else top / bottom


def metric(name: str, value: float | None, formula: str, missing: str, unit: str = "ratio") -> dict[str, Any]:
    return {"name": name, "value": value, "unit": unit if value is not None else None, "formula": formula,
            "status": "calculated" if value is not None else "unavailable",
            "unavailable_reason": None if value is not None else missing}


def records(company: dict[str, Any]) -> list[dict[str, Any]]:
    rows = company.get("financials", company.get("financial_statements", [company]))
    if not isinstance(rows, list):
        return []
    valid = [row for row in rows if isinstance(row, dict)]
    return sorted(valid, key=lambda row: number(row.get("year") or row.get("사업연도")) or float("inf"))


def cagr(first: float | None, last: float | None, years: int) -> float | None:
    return None if first is None or last is None or first <= 0 or last < 0 or years <= 0 else (last / first) ** (1 / years) - 1


def vbm_analysis(company: dict[str, Any], default_wacc: Any) -> dict[str, Any]:
    statements = records(company)
    if not statements:
        return {"status": "unavailable", "reason": "financials must be a list of statement objects", "metrics": []}
    current, prior = statements[-1], statements[-2] if len(statements) > 1 else None
    revenue, op, net = (field(current, key) for key in ("revenue", "operating_profit", "net_income"))
    assets, liabilities, equity = (field(current, key) for key in ("assets", "liabilities", "equity"))
    items = [
        metric("operating_margin", divide(op, revenue), "operating_profit / revenue", "매출액과 영업이익이 필요합니다."),
        metric("net_margin", divide(net, revenue), "net_income / revenue", "매출액과 당기순이익이 필요합니다."),
        metric("debt_to_equity", divide(liabilities, equity), "total_liabilities / total_equity", "부채총계와 자본총계가 필요합니다."),
        metric("current_ratio", divide(field(current, "current_assets"), field(current, "current_liabilities")), "current_assets / current_liabilities", "유동자산과 유동부채가 필요합니다."),
        metric("interest_coverage", divide(op, field(current, "interest_expense")), "operating_profit / interest_expense", "영업이익과 이자비용이 필요합니다."),
    ]
    if prior:
        old_assets, old_equity = field(prior, "assets"), field(prior, "equity")
        avg_assets = (assets + old_assets) / 2 if assets is not None and old_assets is not None else None
        avg_equity = (equity + old_equity) / 2 if equity is not None and old_equity is not None else None
        items += [
            metric("roa", divide(net, avg_assets), "net_income / average_total_assets", "당기순이익과 2개년 자산총계가 필요합니다."),
            metric("roe", divide(net, avg_equity), "net_income / average_total_equity", "당기순이익과 2개년 자본총계가 필요합니다."),
            metric("revenue_cagr", cagr(field(statements[0], "revenue"), revenue, len(statements) - 1), "(latest_revenue / first_revenue)^(1/years)-1", "2개년 이상 양(+)의 매출액이 필요합니다."),
            metric("operating_profit_cagr", cagr(field(statements[0], "operating_profit"), op, len(statements) - 1), "(latest_operating_profit / first_operating_profit)^(1/years)-1", "2개년 이상 양(+)의 영업이익이 필요합니다."),
        ]
    else:
        items += [metric("roa", None, "net_income / average_total_assets", "전년도 자산총계가 필요합니다."), metric("roe", None, "net_income / average_total_equity", "전년도 자본총계가 필요합니다."), metric("revenue_cagr", None, "CAGR", "2개년 이상 매출액이 필요합니다."), metric("operating_profit_cagr", None, "CAGR", "2개년 이상 영업이익이 필요합니다.")]
    tax, debt, cash = field(current, "tax_expense"), field(current, "debt"), field(current, "cash")
    pretax = net + tax if net is not None and tax is not None else None
    tax_rate = divide(tax, pretax)
    invested_capital = debt + equity - cash if None not in (debt, equity, cash) else None
    nopat = op * (1 - tax_rate) if op is not None and tax_rate is not None else None
    roic = divide(nopat, invested_capital)
    items.append(metric("roic", roic, "NOPAT / (interest_bearing_debt + equity - cash)", "법인세비용, 이자부채, 현금, 자본 및 영업이익이 필요합니다."))
    wacc = number(company.get("wacc", default_wacc))
    items.append(metric("roic_minus_wacc", roic - wacc if roic is not None and wacc is not None else None, "ROIC - WACC", "계산 가능한 ROIC와 WACC 가정이 필요합니다."))
    ocf, capex = field(current, "operating_cf"), field(current, "capex")
    items.append(metric("free_cash_flow", ocf - abs(capex) if ocf is not None and capex is not None else None, "operating_cash_flow - abs(CAPEX)", "영업활동현금흐름과 CAPEX가 필요합니다.", "currency"))
    values = {item["name"]: item["value"] for item in items if item["status"] == "calculated"}
    positive = sum((values.get("operating_margin", 0) > 0, values.get("net_margin", 0) > 0, values.get("free_cash_flow", 0) > 0, values.get("roic_minus_wacc", 0) > 0, values.get("revenue_cagr", 0) > 0))
    coverage = len(values) / len(items)
    if coverage < .4:
        conclusion, reason = "보류", "VBM 핵심 지표의 입력 데이터가 부족합니다."
    elif values.get("roic_minus_wacc") is not None and values["roic_minus_wacc"] <= 0:
        conclusion, reason = "주의", "ROIC가 WACC를 초과하지 않아 자본비용 이상의 가치 창출이 확인되지 않습니다."
    elif positive >= 3:
        conclusion, reason = "긍정", "계산 가능한 수익성·현금창출력·성장성 지표 중 다수가 양호합니다."
    else:
        conclusion, reason = "주의", "계산 가능한 VBM 지표에서 가치 창출 근거가 충분하지 않습니다."
    return {"status": "available", "statement_years": [row.get("year", row.get("사업연도")) for row in statements], "metrics": items, "data_coverage": coverage, "wacc": wacc, "conclusion": conclusion, "conclusion_reason": reason}


def credible(url: str) -> bool:
    return any(token in url.casefold() for token in (".gov", ".org", ".edu", "iea.org", "irena.org", "worldbank.org", "sec.gov"))


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
        missing = [identifier for identifier in ids if by_id[identifier]["score"] is None]
        score = None if missing else sum(by_id[identifier]["score"] for identifier in ids)
        groups.append({"group": name, "max_score": 20, "score": score, "status": "pending" if missing else "calculated", "missing_question_ids": missing})
    total = None if any(group["score"] is None for group in groups) else sum(group["score"] for group in groups)
    if total is None:
        decision, reason = "보류", "10개 외부 시장 질문의 점수가 모두 입력되어야 최종 시장성 점수를 계산할 수 있습니다."
    elif total >= 70:
        decision, reason = "적합", "외부 시장성 점수가 70점 이상입니다."
    elif total >= 50:
        decision, reason = "보류", "외부 시장성 점수가 50~69점입니다."
    else:
        decision, reason = "부적합", "외부 시장성 점수가 50점 미만입니다."
    return {"max_score": 100, "score": total, "groups": groups, "decision": decision, "reason": reason}


def external_assessment(subdomain: str, region: str, company: dict[str, Any], client: SearchClient, scorer: OpenAIMarketScorer | None = None) -> dict[str, Any]:
    supplied_scores = company.get("external_market_scores")
    supplied_scores = supplied_scores if isinstance(supplied_scores, dict) else {}
    short_domain = re.split(r"[|/]", subdomain or "energy")[0].strip()[:40] or "energy"
    short_region = (region or "KR").strip()[:20]

    def _search_one(identifier: str, question: str) -> dict[str, Any]:
        query = re.sub(r"\s+", " ", f"{short_domain} {short_region} {question}").strip()[:200]
        try:
            sources = client.search(query, JUDGE_SEARCH_LIMIT)
        except Exception as error:
            print(f"  [judge] 검색 실패 ({company.get('name')}/{identifier}): {error}")
            sources = []
        raw_score = supplied_scores.get(identifier)
        score, score_error = external_score(raw_score.get("score") if isinstance(raw_score, dict) else raw_score)
        return {
            "id": identifier,
            "question": question,
            "max_score": 10,
            "score": score,
            "score_status": "calculated" if score is not None else "pending",
            "score_reason": raw_score.get("reason") if isinstance(raw_score, dict) else None,
            "source_indices": raw_score.get("source_indices", []) if isinstance(raw_score, dict) else [],
            "score_unavailable_reason": score_error,
            "search_query": query,
            "evidence": [
                {
                    "title": x.title,
                    "snippet": x.snippet,
                    "url": x.url,
                    "credibility": "institutional" if credible(x.url) else "commercial_or_news",
                }
                for x in sources
            ],
        }

    # 질문별 웹 검색을 병렬로 수행합니다. (기업당 10회 직렬 호출이 멈춘 것처럼 보이던 원인)
    output: list[dict[str, Any]] = [None] * len(QUESTIONS)  # type: ignore[list-item]
    with ThreadPoolExecutor(max_workers=max(1, min(JUDGE_SEARCH_CONCURRENCY, len(QUESTIONS)))) as pool:
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
                question.update({
                    "score": score,
                    "score_status": "calculated" if score is not None else "pending",
                    "score_reason": generated.get("reason"),
                    "source_indices": generated.get("source_indices", []),
                    "score_unavailable_reason": score_error,
                })
        except RuntimeError as exc:
            scoring_error = str(exc)
            print(f"  [judge] OpenAI 채점 실패 ({company.get('name')}): {scoring_error}")
    summary = grouped_market_score(output)
    return {
        "questions": output,
        "group_scores": summary["groups"],
        "max_score": summary["max_score"],
        "score": summary["score"],
        "decision": summary["decision"],
        "reason": summary["reason"],
        "scoring_method": "openai" if scorer is not None and not supplied_scores else "provided_scores",
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
            reasons.append(GROUP_REASON_TEMPLATES[group["group"]].format(score=f"{group['score']:g}"))
    metrics = {entry["name"]: entry["value"] for entry in vbm.get("metrics", []) if entry["status"] == "calculated"}
    roic, wacc, gap = metrics.get("roic"), vbm.get("wacc"), metrics.get("roic_minus_wacc")
    if roic is not None and wacc is not None and gap is not None and gap > 0:
        reasons.append(f"ROIC {percent(roic)}가 WACC {percent(wacc)}를 {gap * 100:.1f}%p 초과했습니다.")
    if metrics.get("operating_margin") is not None and metrics["operating_margin"] > 0:
        reasons.append(f"영업이익률이 {percent(metrics['operating_margin'])}로 양(+)의 수익성을 보였습니다.")
    if metrics.get("free_cash_flow") is not None and metrics["free_cash_flow"] > 0:
        reasons.append("잉여현금흐름(FCF)이 양(+)으로 현금창출력이 확인되었습니다.")
    if metrics.get("revenue_cagr") is not None and metrics["revenue_cagr"] > 0:
        reasons.append(f"매출 CAGR이 {percent(metrics['revenue_cagr'])}로 성장 추세가 확인되었습니다.")
    return reasons


def judge_company(item: dict[str, Any], client: SearchClient, wacc: Any, scorer: OpenAIMarketScorer | None = None) -> dict[str, Any]:
    company = item.get("company", {})
    name = company.get("name") or "이름 없음"
    print(f"  [judge] 심사 시작: {name}")
    errors = [field for field in ("name", "description") if not company.get(field)]
    vbm = vbm_analysis(company, wacc)
    requested = int(item.get("requested_competitor_count") or 0)
    found = int(item.get("found_competitor_count") or len(item.get("competitors") or []))
    complete = found >= requested
    competitors = item.get("competitors") if isinstance(item.get("competitors"), list) else []
    competitor_names = [
        str((row or {}).get("name") or "").strip()
        for row in competitors
        if isinstance(row, dict) and (row.get("name") or "").strip()
    ]
    print(
        f"  [judge] {name} 경쟁사 근거: {found}/{requested} "
        f"complete={complete}, names={competitor_names[:5]}"
    )
    market = external_assessment(
        item.get("subdomain", "energy infrastructure"),
        company.get("country") or company.get("region") or "",
        company,
        client,
        scorer,
    )
    if errors:
        reason = "필수 기업 정보가 누락되었습니다: " + ", ".join(errors)
    elif market["decision"] == "적합" and vbm.get("conclusion") == "긍정" and complete:
        reason = "외부 시장성과 VBM 판단이 모두 긍정적입니다."
    elif market["score"] is None:
        reason = "외부 시장 질문의 점수가 완성되면 종합 투자판단을 할 수 있습니다."
    else:
        reason = "재무 또는 경쟁·외부시장 근거를 보완한 뒤 종합 판단해야 합니다."
    decision = (
        "적합"
        if market["decision"] == "적합" and vbm.get("conclusion") == "긍정" and complete and not errors
        else "보류"
    )
    reasons = investment_reasons(market, vbm) if decision == "적합" else []
    print(
        f"  [judge] 심사 완료: {name} → {decision} "
        f"(시장={market.get('decision')}, score={market.get('score')}, "
        f"VBM={vbm.get('conclusion')}/{vbm.get('conclusion_reason')}, "
        f"경쟁사={found}/{requested})"
    )
    return {
        "company": company.get("name"),
        "internal_validation": "failed" if errors else "passed",
        "validation_errors": errors,
        "competitor_research_complete": complete,
        "found_competitor_count": found,
        "requested_competitor_count": requested,
        "competitors": competitors,
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


def run(payload: dict[str, Any], client: SearchClient, scorer: OpenAIMarketScorer | None = None) -> dict[str, Any]:
    items = list(payload.get("research_results", []))
    if not items:
        return {"validation": payload.get("validation", {}), "question_count": len(QUESTIONS), "results": []}

    workers = max(1, min(JUDGE_COMPANY_CONCURRENCY, len(items)))
    print(f"  [judge] {len(items)}개 기업 심사 (동시 {workers}개, 질문검색 병렬)")
    results: list[dict[str, Any] | None] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(judge_company, item, client, payload.get("wacc"), scorer): index
            for index, item in enumerate(items)
        }
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return {
        "validation": payload.get("validation", {}),
        "question_count": len(QUESTIONS),
        "results": [item for item in results if item is not None],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="-", help="competitor-research JSON path, or - for stdin")
    parser.add_argument("--search-provider", choices=("serper", "empty"), default="empty")
    parser.add_argument("--market-scorer", choices=("manual", "openai"), default="manual")
    parser.add_argument("--openai-model", default=None, help="OpenAI model; defaults to OPENAI_MODEL or gpt-5")
    args = parser.parse_args()
    raw = sys.stdin.read() if args.input == "-" else open(args.input, encoding="utf-8").read()
    client: SearchClient = SerperSearchClient() if args.search_provider == "serper" else EmptySearchClient()
    scorer = OpenAIMarketScorer(model=args.openai_model) if args.market_scorer == "openai" else None
    print(json.dumps(run(json.loads(raw), client, scorer), ensure_ascii=False, indent=2))


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
    if os.getenv("OPENAI_API_KEY") and os.getenv("JUDGE_MARKET_SCORER", "openai") != "manual":
        try:
            return OpenAIMarketScorer(
                model=os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            )
        except ValueError as error:
            print(f"  [judge] OpenAI scorer 비활성: {error}")
    print("  [judge] 시장성 점수 scorer=manual (제공 점수 없으면 보류 가능)")
    return None


def judgement_node(state: dict[str, Any]) -> dict[str, Any]:
    """경쟁사 조사 결과를 바탕으로 적합/보류를 판단합니다.

    이미 ``judgement.decision == 적합``인 기업은 재심사하지 않고 유지합니다.
    적합·보류 모두 ``eligible_companies``에 남겨 보고서 단계로 넘깁니다.
    (보류라고 해서 DART 재탐색 루프에 넣지 않습니다.)
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
        judgement = company.get("judgement") if isinstance(company.get("judgement"), dict) else {}
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
            competitors = patched.get("competitors") if isinstance(patched.get("competitors"), list) else []
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
                "description": company.get("description") or "",
                "country": company.get("country") or "KR",
                "financials": _financials_of(company),
                "wacc": company.get("wacc") or default_wacc,
            },
            "subdomain": company.get("subdomain") or "energy infrastructure",
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
        name_key = str(company.get("name") or company.get("company_name") or "").casefold()
        from_company = _research_item(company)
        from_payload = payload_by_name.get(name_key)
        if from_payload and not from_company.get("competitors"):
            merged = deepcopy(from_payload)
            merged_company = dict(merged.get("company") or {})
            financials = _financials_of(merged_company) or _financials_of(company)
            if financials:
                merged_company["financials"] = financials
            merged_company["description"] = (
                merged_company.get("description")
                or company.get("description")
                or ""
            )
            merged_company["wacc"] = (
                merged_company.get("wacc") or company.get("wacc") or default_wacc
            )
            merged["company"] = merged_company
            competitors = merged.get("competitors") if isinstance(merged.get("competitors"), list) else []
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
    print("\n[노드 실행] judge_investment (agents/judge.py)")
    print(
        f"  입력 State : 기업="
        f"{[c.get('name') or c.get('company_name') for c in input_companies]} "
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
    result = (
        run(payload, client, scorer)
        if patched_results
        else {"validation": payload["validation"], "question_count": len(QUESTIONS), "results": []}
    )

    newly_suitable: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    judged_all: list[dict[str, Any]] = []
    decision_by_name = {
        str(item.get("company") or "").casefold(): item for item in result.get("results", [])
    }
    for company in pending:
        name = str(company.get("name") or company.get("company_name") or "")
        decision_row = decision_by_name.get(name.casefold())
        updated = deepcopy(company)
        updated["judgement"] = decision_row or {
            "decision": "보류",
            "reason": "판단 결과 없음",
        }
        judged_all.append(updated)
        if decision_row and decision_row.get("decision") == "적합":
            newly_suitable.append(updated)
        else:
            rejected.append(updated)
            reason = str(
                (decision_row or {}).get("reason")
                or updated["judgement"].get("reason")
                or "투자 판단 보류"
            )
            prior_rejections.append({"name": name, "reason": reason})

    # 보류 기업도 걸러내지 않고 보고서 후보로 유지합니다.
    # (적합/보류 라벨은 company.judgement에 남기고, 리포트가 hold/recommend로 정리합니다.)
    kept = [*already_suitable, *judged_all]
    message = (
        f"{attempt}차 투자 판단: 적합 {len(already_suitable) + len(newly_suitable)}개, "
        f"보류 {len(rejected)}개 → 보고서 후보 {len(kept)}개 유지"
    )
    print(
        f"  반환 값    : 보고서 후보="
        f"{[c.get('name') or c.get('company_name') for c in kept]} "
        f"(적합={[c.get('name') or c.get('company_name') for c in already_suitable + newly_suitable]}, "
        f"보류={[c.get('name') or c.get('company_name') for c in rejected]})"
    )
    for row in result.get("results", []):
        print(
            f"  · {row.get('company')}: decision={row.get('decision')} / "
            f"{row.get('reason')}"
        )
    for company in already_suitable:
        print(
            f"  · {company.get('name') or company.get('company_name')}: "
            "decision=적합 / 이전 판단 유지"
        )

    return {
        "eligible_companies": kept,
        "judgement_attempts": attempt,
        "judgement_payload": result,
        "judgement_rejections": prior_rejections,
        "dart_seen_corp_codes": list(seen_corp_codes),
        "wacc": default_wacc,
        "next_stage": "generate_report",
        "execution_log": [*state.get("execution_log", []), message],
    }

