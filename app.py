# 작성자: 통합 담당자 신종민
# 파일 설명: 팀원이 구현한 각 에이전트 노드를 LangGraph로 연결하고,
# 목표 기업 수를 충족할 때까지 스타트업 탐색 단계를 반복합니다.

from __future__ import annotations

import argparse
import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from langchain_teddynote.graphs import visualize_graph
from langgraph.graph import END, START, StateGraph

from agents.dart import dart_lookup_node
from agents.rag import market_research_node
from agents.startup_screen import screen_startups_node
from config import DEFAULT_COMPANY_COUNT, DEFAULT_MAX_SEARCH_ATTEMPTS
from state import GraphState

PROJECT_ROOT = Path(__file__).resolve().parent
COMPANY_DATA_PATH = PROJECT_ROOT / "data" / "companies.json"


# ---------------------------------------------------------------------------
# 팀원별 노드 함수 연결 예정
# ---------------------------------------------------------------------------
# 각 담당자가 아래 함수명과 입출력 규격에 맞춰 구현하면 주석을 해제합니다.
#
# from agents.compitition import competition_node
# from agents.judge import judgement_node
# from agents.report import report_node
#
# 스타트업 탐색과 TIPS 데이터 수집 함수의 위치가 정해지면 함께 연결합니다.
#
# from agents.search import startup_search_node
# from agents.tips import tips_enrichment_node


def _company_names(companies: list[dict[str, Any]]) -> list[str]:
    """로그에서 기업 객체 전체 대신 기업명만 간단히 표시합니다."""

    return [str(company.get("name", "이름 없음")) for company in companies]


def _append_log(state: GraphState, message: str) -> list[str]:
    """State에 저장된 실행 로그 뒤에 현재 노드의 메시지를 추가합니다."""

    return [*state.get("execution_log", []), message]


def _print_node_result(
    node_name: str,
    *,
    received: str,
    returned: str,
) -> None:
    """각 노드가 받은 State와 반환한 값을 터미널에 표시합니다."""

    print(f"\n[노드 실행] {node_name}")
    print(f"  입력 State : {received}")
    print(f"  반환 값    : {returned}")


def _load_scenario_companies() -> list[dict[str, Any]]:
    """시나리오에서 사용할 에너지 관련 기업을 원본 JSON에서 읽습니다."""

    payload = json.loads(COMPANY_DATA_PATH.read_text(encoding="utf-8"))
    companies = payload.get("data", [])
    return [
        company
        for company in companies
        if "ENERGY" in str(company.get("industry12Codes", ""))
        or "ECO" in str(company.get("industry12Codes", ""))
    ]


def startup_search_node(state: GraphState) -> dict[str, Any]:
    """목표 수에서 부족한 만큼 새로운 기업 후보를 추가로 찾습니다."""

    # TODO: 담당자가 웹 또는 API 기반 스타트업 탐색 함수로 교체합니다.
    attempt = state.get("search_attempts", 0) + 1
    target_count = state.get("target_company_count", DEFAULT_COMPANY_COUNT)
    selected_count = len(state.get("eligible_companies", []))
    needed_count = max(target_count - selected_count, 0)

    all_companies = _load_scenario_companies()
    seen_ids = set(state.get("seen_company_ids", []))
    candidates = [
        company for company in all_companies if company.get("id") not in seen_ids
    ][:needed_count]
    updated_seen_ids = [
        *state.get("seen_company_ids", []),
        *(str(company.get("id", "")) for company in candidates),
    ]
    message = (
        f"{attempt}차 탐색: {selected_count}/{target_count}개 보유, "
        f"부족한 {needed_count}개를 추가 탐색했습니다."
    )

    _print_node_result(
        "search_startups",
        received=(
            f"누적 기업={selected_count}개, 부족 기업={needed_count}개, "
            f"search_attempts={state.get('search_attempts', 0)}"
        ),
        returned=f"추가 후보={_company_names(candidates)}",
    )
    return {
        "candidate_pool": candidates,
        "seen_company_ids": updated_seen_ids,
        "execution_log": _append_log(state, message),
    }


def tips_enrichment_node(state: GraphState) -> dict[str, Any]:
    """후보 기업에 TIPS 수집 완료 표시를 추가해 다음 노드로 전달합니다."""

    # TODO: 담당자가 TIPS 기업 정보 수집 함수로 교체합니다.
    candidates = [
        {**deepcopy(company), "scenario_tips_status": "collected"}
        for company in state.get("candidate_pool", [])
    ]
    message = f"TIPS 정보가 보강된 후보 {len(candidates)}개를 전달했습니다."

    _print_node_result(
        "enrich_tips",
        received=f"candidate_pool={_company_names(state.get('candidate_pool', []))}",
        returned="scenario_tips_status=collected",
    )
    return {
        "candidate_pool": candidates,
        "execution_log": _append_log(state, message),
    }


def competition_node(state: GraphState) -> dict[str, Any]:
    """첫 경쟁사 비교에서 일부 기업이 탈락하는 상황을 재현합니다."""

    # TODO: agents/compitition.py의 구현 함수로 교체합니다.
    attempt = state.get("competition_attempts", 0) + 1
    input_companies = list(state.get("eligible_companies", []))
    rejected_count = 1 if attempt == 1 else 0
    passed_companies = (
        input_companies[:-rejected_count] if rejected_count else input_companies
    )
    rejected_companies = input_companies[-rejected_count:] if rejected_count else []
    companies = [
        {**deepcopy(company), "scenario_competition_status": "compared"}
        for company in passed_companies
    ]
    message = (
        f"{attempt}차 경쟁사 비교: 통과 {len(companies)}개, "
        f"부적합 {len(rejected_companies)}개"
    )

    _print_node_result(
        "compare_competition",
        received=f"기업={_company_names(input_companies)}",
        returned=(
            f"통과={_company_names(companies)}, "
            f"부적합={_company_names(rejected_companies)}"
        ),
    )
    return {
        "eligible_companies": companies,
        "competition_attempts": attempt,
        "next_stage": "judge_investment",
        "execution_log": _append_log(state, message),
    }


def judgement_node(state: GraphState) -> dict[str, Any]:
    """첫 투자 판단에서 일부 기업이 탈락하는 상황을 재현합니다."""

    # TODO: agents/judge.py의 구현 함수로 교체합니다.
    attempt = state.get("judgement_attempts", 0) + 1
    input_companies = list(state.get("eligible_companies", []))
    rejected_count = 1 if attempt == 1 else 0
    passed_companies = (
        input_companies[:-rejected_count] if rejected_count else input_companies
    )
    rejected_companies = input_companies[-rejected_count:] if rejected_count else []
    companies = [
        {**deepcopy(company), "scenario_judgement": "suitable"}
        for company in passed_companies
    ]
    message = (
        f"{attempt}차 투자 판단: 적합 {len(companies)}개, "
        f"부적합 {len(rejected_companies)}개"
    )

    _print_node_result(
        "judge_investment",
        received=f"기업={_company_names(input_companies)}",
        returned=(
            f"적합={_company_names(companies)}, "
            f"부적합={_company_names(rejected_companies)}"
        ),
    )
    return {
        "eligible_companies": companies,
        "judgement_attempts": attempt,
        "next_stage": "generate_report",
        "execution_log": _append_log(state, message),
    }


def report_node(state: GraphState) -> dict[str, Any]:
    """최종 기업 목록을 보고서 노드가 받는 상황을 재현합니다."""

    # TODO: agents/report.py의 구현 함수로 교체합니다.
    companies = state.get("eligible_companies", [])
    message = f"최종 보고서에 기업 {len(companies)}개를 전달했습니다."

    _print_node_result(
        "generate_report",
        received=f"최종 기업={_company_names(companies)}",
        returned="status=completed",
    )
    return {
        "evaluated_companies": companies,
        "status": "completed",
        "report_path": "outputs/scenario_investment_report.pdf",
        "execution_log": _append_log(state, message),
    }


def selection_failed_node(state: GraphState) -> dict[str, Any]:
    """목표 기업 수를 채우지 못했을 때 실패 상태를 기록합니다."""

    message = (
        "최대 탐색 횟수 안에 목표 기업 수를 채우지 못해 종료했습니다. "
        f"현재 {len(state.get('eligible_companies', []))}개"
    )
    _print_node_result(
        "selection_failed",
        received=(
            f"탐색 횟수={state.get('search_attempts', 0)}, "
            f"기업 수={len(state.get('eligible_companies', []))}"
        ),
        returned="status=failed",
    )
    return {
        "status": "failed",
        "workflow_errors": [message],
        "execution_log": _append_log(state, message),
    }


def validate_company_count_node(state: GraphState) -> dict[str, Any]:
    """선택된 기업 수가 목표 기업 수 이상인지 검증합니다.

    기본 목표는 ``DEFAULT_COMPANY_COUNT``이며, 실행 옵션으로 변경한 경우에는
    State의 ``target_company_count``를 기준으로 검증합니다.
    """

    companies = list(state.get("eligible_companies", []))
    target_count = state.get("target_company_count", DEFAULT_COMPANY_COUNT)
    if len(companies) > target_count:
        companies = companies[:target_count]
    selected_count = len(companies)
    is_valid = selected_count >= target_count
    message = f"기업 수 검증: {selected_count}/{target_count}, 통과={is_valid}"

    _print_node_result(
        "validate_company_count",
        received=f"eligible_companies={selected_count}개",
        returned=f"company_count_is_valid={is_valid}",
    )
    return {
        "eligible_companies": companies,
        "company_count_is_valid": is_valid,
        "selected_company_count": selected_count,
        "execution_log": _append_log(state, message),
    }


def route_by_company_count(
    state: GraphState,
) -> str:
    """기업 수가 부족하면 탐색하고, 충족하면 예정된 다음 단계로 이동합니다."""

    selected_count = len(state.get("eligible_companies", []))
    target_count = state.get("target_company_count", DEFAULT_COMPANY_COUNT)

    # 목표 기업 수보다 적으면 스타트업 탐색 단계로 되돌아갑니다.
    if selected_count < target_count:
        # 데이터 부족이나 API 오류로 인한 무한 루프를 막습니다.
        if state.get("search_attempts", 0) >= state.get(
            "max_search_attempts",
            DEFAULT_MAX_SEARCH_ATTEMPTS,
        ):
            print(
                "  [조건 분기] 목표 기업 수 부족 + 최대 탐색 횟수 초과 "
                "→ selection_failed"
            )
            return "selection_failed"
        print(
            f"  [조건 분기] {selected_count}/{target_count}개로 부족 "
            "→ lookup_dart 재탐색"
        )
        return "lookup_dart"

    # 목표 기업 수를 채우면 직전 노드가 지정한 다음 단계로 진행합니다.
    next_stage = state.get("next_stage", "market_research")
    print(f"  [조건 분기] {selected_count}/{target_count}개 충족 " f"→ {next_stage}")
    return next_stage


def build_workflow():
    """빈 노드로 전체 흐름을 연결하고 실행 가능한 그래프로 컴파일합니다."""

    builder = StateGraph(GraphState)

    # -----------------------------------------------------------------------
    # 노드 등록
    # -----------------------------------------------------------------------
    # 1. 스타트업 탐색 노드
    #    웹 또는 API에서 에너지 도메인 스타트업 후보를 가져옵니다.
    builder.add_node("search_startups", startup_search_node)

    # 2. TIPS 정보 수집 노드
    #    후보 기업의 TIPS 정보와 기본 기업 정보를 State에 저장합니다.
    builder.add_node("enrich_tips", tips_enrichment_node)

    # 3. DART 검색 노드
    #    기업명을 DART에서 검색하고 조건을 통과한 기업을 누적합니다.
    builder.add_node("lookup_dart", dart_lookup_node)

    # 4. 스타트업 검증 노드
    #    일반 중소기업을 걸러내고, 필요한 경우만 웹 검색으로 Series를 확정합니다.
    builder.add_node("screen_startups", screen_startups_node)

    # 5. 기업 수 검증 노드
    #    이 노드만 통합 담당자가 구현하며 목표 기업 수와 현재 수를 비교합니다.
    builder.add_node("validate_company_count", validate_company_count_node)

    # 6. 시장성 평가 노드
    #    검증된 스타트업 JSON을 RAG 입력으로 사용합니다.
    builder.add_node("market_research", market_research_node)

    # 7. 경쟁사 비교 노드
    #    RAG 결과와 추가 웹 검색을 바탕으로 기업과 경쟁사를 비교합니다.
    builder.add_node("compare_competition", competition_node)

    # 8. 투자 판단 노드
    #    VC·PE 투자 기준에 따라 적합, 부적합 또는 검토 필요로 판단합니다.
    builder.add_node("judge_investment", judgement_node)

    # 9. 보고서 생성 노드
    #    검증된 기업별 결과를 정해진 양식의 투자 보고서로 생성합니다.
    builder.add_node("generate_report", report_node)

    # 10. 기업 선택 실패 노드
    #    최대 탐색 횟수 안에 목표 기업 수를 채우지 못했을 때 오류를 기록합니다.
    builder.add_node("selection_failed", selection_failed_node)

    # -----------------------------------------------------------------------
    # 엣지 연결
    # -----------------------------------------------------------------------
    # 시작 → 스타트업 탐색
    builder.add_edge(START, "search_startups")

    # 스타트업 탐색 → TIPS 정보 수집
    builder.add_edge("search_startups", "enrich_tips")

    # TIPS 정보 수집 → DART 검색
    builder.add_edge("enrich_tips", "lookup_dart")

    # DART 검색 → 스타트업 검증 → 기업 수 검증
    builder.add_edge("lookup_dart", "screen_startups")
    builder.add_edge("screen_startups", "validate_company_count")

    # 모든 평가 단계는 기업 수 검증 노드로 돌아옵니다.
    builder.add_edge("market_research", "validate_company_count")
    builder.add_edge("compare_competition", "validate_company_count")
    builder.add_edge("judge_investment", "validate_company_count")

    # 기업 수 검증 후 조건 분기
    # - 목표보다 적음: search_startups로 돌아가 부족한 기업을 추가 탐색
    # - 목표 충족: 직전 노드가 State의 next_stage에 지정한 단계로 이동
    # - 최대 탐색 횟수 초과: selection_failed로 이동
    builder.add_conditional_edges(
        "validate_company_count",
        route_by_company_count,
        {
            "search_startups": "search_startups",
            "lookup_dart": "lookup_dart",
            "screen_startups": "screen_startups",
            "market_research": "market_research",
            "compare_competition": "compare_competition",
            "judge_investment": "judge_investment",
            "generate_report": "generate_report",
            "selection_failed": "selection_failed",
        },
    )

    # 보고서 생성 → 정상 종료
    builder.add_edge("generate_report", END)

    # 기업 선택 실패 → 보고서 없이 종료
    builder.add_edge("selection_failed", END)

    return builder.compile()


def create_initial_state(company_count: int) -> GraphState:
    """기업 수 검증에 필요한 초기 State를 생성합니다."""

    if company_count < 1:
        raise ValueError("기업 선택 개수는 1개 이상이어야 합니다.")

    return {
        "target_company_count": company_count,
        "max_search_attempts": DEFAULT_MAX_SEARCH_ATTEMPTS,
        "search_attempts": 0,
        "market_attempts": 0,
        "competition_attempts": 0,
        "judgement_attempts": 0,
        "candidate_pool": [],
        "seen_company_ids": [],
        "eligible_companies": [],
        "dart_rejections": [],
        "dart_seen_corp_codes": [],
        "startup_rejections": [],
        "evaluated_companies": [],
        "workflow_errors": [],
        "execution_log": [],
        "next_stage": "market_research",
        "status": "initialized",
    }


def parse_args() -> argparse.Namespace:
    """CLI에서 평가할 기업 수를 입력받습니다."""

    parser = argparse.ArgumentParser(description="에너지 스타트업 투자 평가")
    parser.add_argument(
        "--company-count",
        type=int,
        default=DEFAULT_COMPANY_COUNT,
        help=f"평가할 기업 수 (기본값: {DEFAULT_COMPANY_COUNT})",
    )
    return parser.parse_args()


# 노드와 엣지가 연결된 실행 가능한 LangGraph입니다.
app = build_workflow()


def main() -> None:
    """전체 시나리오를 실행하고 State 변화와 그래프를 표시합니다."""

    args = parse_args()
    initial_state = create_initial_state(args.company_count)

    print("=" * 70)
    print(f"시나리오 시작 - 목표 기업 수: {initial_state['target_company_count']}")
    print("=" * 70)

    # dart_lookup_node가 비동기 DART API를 사용하므로 전체 그래프도 비동기로 실행합니다.
    result = asyncio.run(app.ainvoke(initial_state, config={"recursion_limit": 100}))

    print("\n" + "=" * 70)
    print("최종 State")
    print(f"  실행 상태       : {result['status']}")
    print(f"  총 탐색 횟수    : {result['search_attempts']}")
    print(f"  시장성 평가 횟수: {result['market_attempts']}")
    print(f"  경쟁사 비교 횟수: {result['competition_attempts']}")
    print(f"  투자 판단 횟수  : {result['judgement_attempts']}")
    print(f"  선택된 기업 수  : {len(result.get('eligible_companies', []))}")
    print(f"  보고서 전달 기업: {len(result.get('evaluated_companies', []))}")
    print(f"  보고서 경로     : {result.get('report_path', '생성 안 됨')}")
    print("=" * 70)

    # 노트북 예제와 같은 방식으로 연결된 노드와 엣지를 표시합니다.
    visualize_graph(app, xray=True)


if __name__ == "__main__":
    main()
