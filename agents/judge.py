# 작성자: 권태현
# 파일 설명: 사전에 정의한 투자 판단 기준을 적용하여
# 기업의 투자 적합 여부를 판단하는 에이전트를 구현합니다.
#
# LangGraph 연결: judgement_node(state) → 적합 기업만 eligible_companies에 유지

"""VBM-based investment-screening agent for energy-infrastructure companies.

ROI needs actual investment cash flows. Before investment, this module uses
financial statements to evaluate value-creation potential instead.
"""
# [분리 안내] 이 파일은 기능별 에이전트로 나뉘었고, 기존 import 호환용 진입점만 남깁니다.
#   agents/financial_vbm.py       재무 계산 (VBM)
#   agents/market_scoring.py      외부 시장성 채점
#   agents/competitor_compare.py  경쟁사 재무 비교
#   agents/investment_decision.py 투자 판단·평가 요약
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from agents.compitition import EmptySearchClient, SearchClient, SerperSearchClient  # noqa: F401
from agents.competitor_compare import COMPARISON_METRICS, compare_competitor_vbm  # noqa: F401
from agents.financial_vbm import (  # noqa: F401
    KEYS,
    cagr,
    conditional_vbm_acceptable,
    divide,
    field,
    metric,
    number,
    records,
    vbm_analysis,
)
from agents.investment_decision import (  # noqa: F401
    _company_display_name,
    _competitor_rows,
    build_company_evaluation_list,
    build_company_evaluation_summary,
    build_final_suitable_companies,
    decide_investment,
    investment_reasons,
    percent,
)
from agents.market_scoring import (  # noqa: F401
    GROUP_REASON_TEMPLATES,
    GROUPS,
    JUDGE_COMPANY_CONCURRENCY,
    JUDGE_OPENAI_TIMEOUT,
    JUDGE_SEARCH_CONCURRENCY,
    JUDGE_SEARCH_LIMIT,
    MARKET_PASS_SCORE,
    QUESTIONS,
    OpenAIMarketScorer,
    build_market_scorer,
    build_search_client,
    credible,
    external_assessment,
    external_score,
    grouped_market_score,
)


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
    decision, reason = decide_investment(market, vbm, errors)
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

