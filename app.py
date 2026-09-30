# 작성자: 통합 담당자 신종민
# 파일 설명: 팀원이 구현한 각 에이전트 노드를 LangGraph로 연결하고,
# 목표 기업 수를 충족할 때까지 DART 탐색을 반복합니다.

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

from langchain_teddynote.graphs import visualize_graph
from langgraph.graph import END, START, StateGraph

from agents.compitition import competition_node
from agents.dart import dart_lookup_node, print_companies_for_rag
from agents.embed import ensure_vector_db
from agents.judge import judgement_node
from agents.market_bridge import enrich_company_for_competition, market_bridge_node
from agents.report import build_report_payload, generate_report
from agents.startup_screen import screen_startups_node
from config import DEFAULT_COMPANY_COUNT, DEFAULT_MAX_SEARCH_ATTEMPTS, DEFAULT_WACC
from state import GraphState

# rag.py: GraphState(dict)를 받아 eligible_companies에 market 필드를 붙여 반환
try:
    from agents.rag import market_research_node as _rag_market_research_node
except ImportError:
    _rag_market_research_node = None

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "outputs" / "logs"
OUTPUT_DIR = PROJECT_ROOT / "outputs"

# 터미널 로그용 한글 작업명 (내부 노드 id → 표시명)
NODE_LABELS: dict[str, str] = {
    "lookup_dart": "DART 공시·재무 탐색",
    "screen_startups": "스타트업 적격 검증",
    "validate_company_count": "목표 기업 수 확인",
    "market_research": "시장성 조사 (RAG)",
    "compare_competition": "경쟁사 조사",
    "judge_investment": "투자 적합 판단",
    "generate_report": "투자 보고서 생성",
    "selection_failed": "기업 선정 실패 종료",
}


def _node_label(node_name: str) -> str:
    return NODE_LABELS.get(node_name, node_name)


def _print_node_result(
    node_name: str,
    *,
    received: str,
    returned: str,
) -> None:
    """각 노드가 받은 State와 반환한 값을 터미널에 표시합니다."""

    print(f"\n[작업] {_node_label(node_name)}")
    print(f"  입력 : {received}")
    print(f"  결과 : {returned}")


def save_final_companies(state: GraphState, *, tag: str = "judge") -> Path:
    """judge 이후 최종 선정 기업 State를 outputs/ 에 JSON으로 저장합니다."""

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    companies = list(state.get("eligible_companies") or [])
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    payload = {
        "saved_at": datetime.now().isoformat(timespec="seconds"),
        "tag": tag,
        "target_company_count": state.get("target_company_count"),
        "company_count": len(companies),
        "wacc": state.get("wacc"),
        "eligible_companies": companies,
        "judgement_rejections": list(state.get("judgement_rejections") or []),
    }
    path = OUTPUT_DIR / f"final_companies_{stamp}.json"
    latest = OUTPUT_DIR / "final_companies_latest.json"
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    path.write_text(text, encoding="utf-8")
    latest.write_text(text, encoding="utf-8")
    print(f"  [저장] 최종 선정 기업 {len(companies)}개 → {path}")
    print(f"  [저장] 최신본 → {latest}")
    return path


class _TeeStream:
    """터미널과 로그 파일에 동시에 출력합니다."""

    def __init__(self, primary: TextIO, secondary: TextIO) -> None:
        self.primary = primary
        self.secondary = secondary

    def write(self, data: str) -> int:
        self.primary.write(data)
        self.secondary.write(data)
        self.primary.flush()
        self.secondary.flush()
        return len(data)

    def flush(self) -> None:
        self.primary.flush()
        self.secondary.flush()

    def isatty(self) -> bool:
        return bool(getattr(self.primary, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self.primary.fileno()


def setup_print_logging(log_dir: Path | None = None) -> Path:
    """모든 print/stdout/stderr를 ``outputs/logs/run_*.log``에도 남깁니다."""

    target_dir = log_dir or LOG_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = target_dir / f"run_{stamp}.log"
    log_file = log_path.open("a", encoding="utf-8")

    sys.stdout = _TeeStream(sys.__stdout__, log_file)  # type: ignore[assignment]
    sys.stderr = _TeeStream(sys.__stderr__, log_file)  # type: ignore[assignment]
    print(f"[log] 실행 로그 파일: {log_path}")
    return log_path


# ---------------------------------------------------------------------------
# 팀원별 노드 함수 연결
# ---------------------------------------------------------------------------
# 경쟁사·투자판단: agents/compitition.py, agents/judge.py
# 시장성(RAG 계약): agents/market_bridge.py (rag.py는 담당자 영역)
# 최종 보고서: agents/report.py + docs/report.md


def _company_names(companies: list[dict[str, Any]]) -> list[str]:
    """로그에서 기업 객체 전체 대신 기업명만 간단히 표시합니다."""

    return [
        str(company.get("company_name") or company.get("name") or "이름 없음")
        for company in companies
    ]


def _append_log(state: GraphState, message: str) -> list[str]:
    """State에 저장된 실행 로그 뒤에 현재 노드의 메시지를 추가합니다."""

    return [*state.get("execution_log", []), message]


def _company_ready_for_competition(company: dict[str, Any]) -> bool:
    """rag가 description·subdomain·market을 채웠는지 확인합니다."""

    market = company.get("market") if isinstance(company.get("market"), dict) else {}
    has_description = bool(
        company.get("description")
        or market.get("description")
        or company.get("intro")
    )
    has_subdomain = bool(company.get("subdomain") or market.get("subdomain"))
    has_market = bool(market) and market.get("status") in {"rag", "web", "none"}
    return has_description and has_subdomain and has_market


def market_research_node(state: GraphState) -> dict[str, Any]:
    """DART(+스크리닝) 결과에 시장 정보를 붙여 경쟁사 비교로 넘깁니다.

    1순위: agents/rag.py (Chroma RAG + 웹 fallback)
    2순위: market_bridge (rag 실패·필드 누락 시만 보강)
    반환은 JSON 파일이 아니라 GraphState partial update(dict)입니다.
    """

    companies = list(state.get("eligible_companies", []))
    print_companies_for_rag(
        companies,
        title="market_research 입력 = DART(+스크리닝) 원본",
    )

    if callable(_rag_market_research_node):
        try:
            result = _rag_market_research_node(state)
            enriched = list(result.get("eligible_companies") or [])
            # rag가 채운 필드는 유지하고, 누락된 기업만 bridge로 보강
            finalized: list[dict[str, Any]] = []
            bridged_count = 0
            for company in enriched:
                if _company_ready_for_competition(company):
                    finalized.append(company)
                else:
                    finalized.append(enrich_company_for_competition(company))
                    bridged_count += 1

            message = (
                f"시장성 평가: rag {len(enriched) - bridged_count}개, "
                f"bridge 보강 {bridged_count}개"
            )
            print(f"  [시장성 조사] {message}")
            return {
                **result,
                "eligible_companies": finalized,
                "market_attempts": result.get(
                    "market_attempts",
                    int(state.get("market_attempts", 0)) + 1,
                ),
                "next_stage": "compare_competition",
                "execution_log": [
                    *list(result.get("execution_log") or state.get("execution_log") or []),
                    message,
                ],
            }
        except Exception as error:
            print(f"  [시장성 조사] rag 실패 → bridge 사용: {error}")

    return market_bridge_node(state)


def report_node(state: GraphState) -> dict[str, Any]:
    """docs/report.md 계약으로 State를 변환한 뒤 PDF 보고서를 생성합니다."""

    companies = list(state.get("eligible_companies", []))
    # judge 이후 최종 선정 기업 데이터를 outputs/ 에 먼저 저장 (PDF 실패와 무관)
    try:
        save_final_companies(state, tag="after_judge")
    except Exception as error:
        print(f"  [저장 실패] 최종 기업 JSON 저장 오류: {error}")

    report_payload = build_report_payload(state)
    output_path = PROJECT_ROOT / "outputs" / "investment_report.pdf"
    try:
        path = generate_report(report_payload, output_path)
        status = "completed"
        message = (
            f"최종 보고서 생성 완료: 기업 {len(companies)}개 → {path}"
        )
        returned = f"report_path={path}"
    except Exception as error:
        path = output_path
        status = "failed"
        message = f"최종 보고서 생성 실패: {error}"
        returned = f"error={error}"
        print(f"  [보고서] {message}")

    _print_node_result(
        "generate_report",
        received=f"최종 기업={_company_names(companies)}",
        returned=returned,
    )
    return {
        "evaluated_companies": companies,
        "report_payload": report_payload,
        "status": status,
        "report_path": str(path),
        "execution_log": _append_log(state, message),
        "workflow_errors": [message] if status == "failed" else [],
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
        received=f"현재 후보 기업={selected_count}개 / 목표={target_count}개",
        returned=f"목표 충족={is_valid}",
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

    # 목표 기업 수보다 적으면 DART 탐색으로 되돌아갑니다.
    if selected_count < target_count:
        # 데이터 부족이나 API 오류로 인한 무한 루프를 막습니다.
        if state.get("search_attempts", 0) >= state.get(
            "max_search_attempts",
            DEFAULT_MAX_SEARCH_ATTEMPTS,
        ):
            print(
                "  [다음 단계] 목표 기업 수 부족 + 최대 탐색 횟수 초과 "
                f"→ {_node_label('selection_failed')}"
            )
            return "selection_failed"
        print(
            f"  [다음 단계] {selected_count}/{target_count}개로 부족 "
            f"→ {_node_label('lookup_dart')} 재탐색"
        )
        return "lookup_dart"

    # 목표 기업 수를 채우면 직전 노드가 지정한 다음 단계로 진행합니다.
    next_stage = state.get("next_stage", "market_research")
    print(
        f"  [다음 단계] {selected_count}/{target_count}개 충족 "
        f"→ {_node_label(next_stage)}"
    )
    return next_stage


def build_workflow():
    """DART부터 시작하는 전체 흐름을 연결하고 실행 가능한 그래프로 컴파일합니다."""

    builder = StateGraph(GraphState)

    # -----------------------------------------------------------------------
    # 노드 등록
    # -----------------------------------------------------------------------
    # 1. DART 검색 노드
    builder.add_node("lookup_dart", dart_lookup_node)

    # 2. 스타트업 검증 노드
    builder.add_node("screen_startups", screen_startups_node)

    # 3. 기업 수 검증 노드
    builder.add_node("validate_company_count", validate_company_count_node)

    # 4. 시장성 평가 노드
    builder.add_node("market_research", market_research_node)

    # 5. 경쟁사 비교 노드
    builder.add_node("compare_competition", competition_node)

    # 6. 투자 판단 노드
    builder.add_node("judge_investment", judgement_node)

    # 7. 보고서 생성 노드
    builder.add_node("generate_report", report_node)

    # 8. 기업 선택 실패 노드
    builder.add_node("selection_failed", selection_failed_node)

    # -----------------------------------------------------------------------
    # 엣지 연결
    # -----------------------------------------------------------------------
    # 시작 → DART 검색 → 스타트업 검증 → 기업 수 검증
    builder.add_edge(START, "lookup_dart")
    builder.add_edge("lookup_dart", "screen_startups")
    builder.add_edge("screen_startups", "validate_company_count")

    # 모든 평가 단계는 기업 수 검증 노드로 돌아옵니다.
    builder.add_edge("market_research", "validate_company_count")
    builder.add_edge("compare_competition", "validate_company_count")
    builder.add_edge("judge_investment", "validate_company_count")

    # 기업 수 검증 후 조건 분기
    # - 목표보다 적음: lookup_dart로 돌아가 부족한 기업을 추가 탐색
    # - 목표 충족: 직전 노드가 State의 next_stage에 지정한 단계로 이동
    # - 최대 탐색 횟수 초과: selection_failed로 이동
    builder.add_conditional_edges(
        "validate_company_count",
        route_by_company_count,
        {
            "lookup_dart": "lookup_dart",
            "screen_startups": "screen_startups",
            "market_research": "market_research",
            "compare_competition": "compare_competition",
            "judge_investment": "judge_investment",
            "generate_report": "generate_report",
            "selection_failed": "selection_failed",
        },
    )

    builder.add_edge("generate_report", END)
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
        "judgement_rejections": [],
        "evaluated_companies": [],
        "workflow_errors": [],
        "execution_log": [],
        "wacc": DEFAULT_WACC,
        "next_stage": "market_research",
        "status": "initialized",
    }


def build_report_fixture_companies(count: int) -> list[dict[str, Any]]:
    """judge 직후와 같은 형태의 더미 기업 State를 만듭니다. (report 단독 테스트용)"""

    samples = [
        ("한화솔라파워", "solar / PV", "적합", 72),
        ("코리아로드태양광", "solar / PV", "보류", 65),
        ("유니드에너지", "energy infrastructure", "보류", 63),
        ("한뉴딜에너지", "energy infrastructure", "보류", 61),
        ("지평선에너지", "energy infrastructure", "보류", 64),
    ]
    companies: list[dict[str, Any]] = []
    for index in range(count):
        name, subdomain, decision, score = samples[index % len(samples)]
        if count > len(samples):
            name = f"{name}-{index + 1}"
        companies.append(
            {
                "id": f"fixture-{index + 1:03d}",
                "corp_code": f"0000000{index + 1}",
                "name": name,
                "company_name": name,
                "description": f"{name} 에너지·인프라 사업 개요 (report-only fixture)",
                "subdomain": subdomain,
                "country": "KR",
                "estimated_investment_stage": "자산추정 Series A~B",
                "dart_viewer_link": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo=fixture{index + 1}",
                "financial_summary": {
                    "자산총계": "8,930,578,215",
                    "부채총계": "8,037,857,307",
                    "자본총계": "892,720,908",
                    "매출액": "1,189,911,112",
                    "영업이익": "408,935,217",
                    "당기순이익": "-17,077,150",
                    "현금및현금성자산": "788,523,040",
                    "이자부부채": "7,952,000,000",
                    "이자비용": "426,742,639",
                    "영업활동현금흐름": "417,435,473",
                    "CAPEX": "-8,333,032",
                    "유동자산": "896,314,426",
                    "유동부채": "333,857,307",
                },
                "financials": [
                    {
                        "year": "2018",
                        "자산총계": "9,111,895,258",
                        "매출액": "1,181,859,977",
                        "영업이익": "464,371,845",
                        "당기순이익": "56,071,981",
                        "자본총계": "909,798,058",
                        "이자부부채": "8,116,000,000",
                        "현금및현금성자산": "543,420,599",
                        "영업활동현금흐름": "-3,987,771,524",
                    },
                    {
                        "year": "2019",
                        "자산총계": "8,930,578,215",
                        "매출액": "1,189,911,112",
                        "영업이익": "408,935,217",
                        "당기순이익": "-17,077,150",
                        "자본총계": "892,720,908",
                        "이자부부채": "7,952,000,000",
                        "현금및현금성자산": "788,523,040",
                        "영업활동현금흐름": "417,435,473",
                        "CAPEX": "-8,333,032",
                    },
                ],
                "market": {
                    "status": "fixture",
                    "subdomain": subdomain,
                    "description": f"{name} 관련 {subdomain} 시장 fixture 설명",
                },
                "competition": {
                    "status": "complete",
                    "found_competitor_count": 2,
                    "requested_competitor_count": 2,
                    "competitors": [
                        {
                            "name": f"{name} 경쟁사A",
                            "evidence": "동일 도메인 사업 운영",
                            "source_url": "https://example.com/comp-a",
                        },
                        {
                            "name": f"{name} 경쟁사B",
                            "evidence": "태양광·에너지 인프라 경쟁",
                            "source_url": "https://example.com/comp-b",
                        },
                    ],
                },
                "competitor_research": {
                    "status": "complete",
                    "found_competitor_count": 2,
                    "requested_competitor_count": 2,
                    "competitors": [
                        {
                            "name": f"{name} 경쟁사A",
                            "evidence": "동일 도메인 사업 운영",
                            "source_url": "https://example.com/comp-a",
                        },
                        {
                            "name": f"{name} 경쟁사B",
                            "evidence": "태양광·에너지 인프라 경쟁",
                            "source_url": "https://example.com/comp-b",
                        },
                    ],
                },
                "judgement": {
                    "company": name,
                    "decision": decision,
                    "reason": (
                        "외부 시장성과 VBM 판단이 모두 긍정적입니다."
                        if decision == "적합"
                        else "재무 또는 경쟁·외부시장 근거를 보완한 뒤 종합 판단해야 합니다."
                    ),
                    "external_market_score": score,
                    "investment_reasons": (
                        [f"외부 시장성 점수 {score}점", "경쟁사 조사 완료"]
                        if decision == "적합"
                        else []
                    ),
                    "vbm_assessment": {
                        "conclusion": "긍정" if decision == "적합" else "주의",
                        "conclusion_reason": (
                            "계산 가능한 수익성·현금창출력·성장성 지표 중 다수가 양호합니다."
                            if decision == "적합"
                            else "계산 가능한 VBM 지표에서 가치 창출 근거가 충분하지 않습니다."
                        ),
                    },
                    "external_market_assessment": {
                        "scores": {
                            "market_growth": {
                                "score": min(score // 10, 10),
                                "reason": "fixture 시장 성장 근거",
                            }
                        }
                    },
                },
            }
        )
    return companies


def create_report_only_state(
    company_count: int,
    *,
    input_path: Path | None = None,
) -> GraphState:
    """judge까지 끝난 것과 같은 State로 report_node만 테스트합니다."""

    state = create_initial_state(company_count)
    if input_path is not None:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
        if isinstance(payload.get("eligible_companies"), list):
            companies = payload["eligible_companies"]
        elif isinstance(payload, list):
            companies = payload
        else:
            raise ValueError(
                "JSON에 eligible_companies 배열이 있거나, 기업 배열 자체여야 합니다."
            )
        state["eligible_companies"] = companies[:company_count] or companies
        if payload.get("wacc") is not None:
            state["wacc"] = payload["wacc"]
        if payload.get("judgement_payload"):
            state["judgement_payload"] = payload["judgement_payload"]
        if payload.get("competition_payload"):
            state["competition_payload"] = payload["competition_payload"]
    else:
        state["eligible_companies"] = build_report_fixture_companies(company_count)

    state["search_attempts"] = 1
    state["market_attempts"] = 1
    state["competition_attempts"] = 1
    state["judgement_attempts"] = 1
    state["next_stage"] = "generate_report"
    state["status"] = "evaluating"
    state["execution_log"] = [
        "report-only 모드: judge 이전 단계를 건너뛰고 보고서만 생성합니다."
    ]
    return state


def parse_args() -> argparse.Namespace:
    """CLI에서 평가할 기업 수를 입력받습니다."""

    parser = argparse.ArgumentParser(description="에너지 스타트업 투자 평가")
    parser.add_argument(
        "--company-count",
        type=int,
        default=DEFAULT_COMPANY_COUNT,
        help=f"평가할 기업 수 (기본값: {DEFAULT_COMPANY_COUNT})",
    )
    parser.add_argument(
        "--report-only",
        action="store_true",
        help="DART~judge를 건너뛰고 최종 State 형식 fixture로 report만 생성합니다.",
    )
    parser.add_argument(
        "--report-input",
        type=Path,
        default=None,
        help="report-only 모드에서 사용할 JSON State 경로 (eligible_companies 포함)",
    )
    parser.add_argument(
        "--rebuild-embed",
        action="store_true",
        help="기존 chroma_db를 지우고 data/for_embed PDF를 다시 임베딩합니다.",
    )
    return parser.parse_args()


# 노드와 엣지가 연결된 실행 가능한 LangGraph입니다.
app = build_workflow()


def main() -> None:
    """전체 시나리오를 실행하고 State 변화와 그래프를 표시합니다."""

    log_path = setup_print_logging()
    args = parse_args()

    if args.report_only:
        state = create_report_only_state(
            args.company_count,
            input_path=args.report_input,
        )
        print("=" * 70)
        print("report-only 모드 - judge 건너뛰고 보고서만 생성")
        print(f"기업 수           : {len(state.get('eligible_companies', []))}")
        print(f"입력 JSON         : {args.report_input or '(내장 fixture)'}")
        print(f"로그 파일         : {log_path}")
        print("=" * 70)
        result = report_node(state)
        print("\n" + "=" * 70)
        print("최종 State (report-only)")
        print(f"  실행 상태       : {result.get('status')}")
        print(f"  선택된 기업 수  : {len(state.get('eligible_companies', []))}")
        print(f"  보고서 전달 기업: {len(result.get('evaluated_companies', []))}")
        print(f"  보고서 경로     : {result.get('report_path', '생성 안 됨')}")
        print(f"  로그 파일       : {log_path}")
        print("=" * 70)
        return

    initial_state = create_initial_state(args.company_count)

    print("=" * 70)
    print(f"시나리오 시작 - 목표 기업 수: {initial_state['target_company_count']}")
    print(f"로그 파일         : {log_path}")
    print("=" * 70)

    # 시장성 RAG용 벡터DB는 앱 시작 시 한 번만 준비 (있으면 재사용)
    try:
        ensure_vector_db(
            force_rebuild=bool(getattr(args, "rebuild_embed", False)),
            force_download=bool(getattr(args, "rebuild_embed", False)),
        )
    except Exception as error:
        print(f"[embed] 벡터DB 준비 실패 (rag는 bridge로 대체될 수 있음): {error}")

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
    print(f"  로그 파일       : {log_path}")
    print("=" * 70)

    # 노트북 예제와 같은 방식으로 연결된 노드와 엣지를 표시합니다.
    visualize_graph(app, xray=True)


if __name__ == "__main__":
    main()
