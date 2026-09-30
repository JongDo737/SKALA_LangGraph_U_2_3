"""그래프 전체에서 공유하는 상태와 기업 데이터 계약입니다."""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal

from typing_extensions import NotRequired, TypedDict


class Evidence(TypedDict, total=False):
    """기업 정보의 출처와 검증 상태를 필드 단위로 보관합니다."""

    field: str
    value: Any
    source: str
    source_url: str
    retrieved_at: str
    match_method: str
    confidence: float
    verification_status: Literal["verified", "unverified", "conflict"]


class CompanyRecord(TypedDict, total=False):
    """각 에이전트가 단계적으로 보강하는 단일 기업 객체입니다."""

    id: str
    name: str
    ceo: str
    homepage_url: str
    established_at: str
    industries: list[str]
    intro: str
    tips: dict[str, Any]
    dart: dict[str, Any]
    company_name: str
    corp_code: str
    established_date: str
    legal_class: str
    estimated_investment_stage: str
    asset_estimated_stage: str
    stage_source: str
    financial_summary: dict[str, Any]
    dart_viewer_link: str
    screening: dict[str, Any]
    market: dict[str, Any]
    competition: dict[str, Any]
    competitor_research: dict[str, Any]
    judgement: dict[str, Any]
    description: str
    subdomain: str
    country: str
    financials: list[dict[str, Any]]
    evidence: list[Evidence]
    validation_errors: list[str]


class GraphState(TypedDict, total=False):
    """메인 그래프의 실행 상태입니다."""

    # 모든 노드가 이 값을 읽으므로 기업 수를 한 곳에서 제어할 수 있습니다.
    target_company_count: int
    candidate_batch_size: int
    max_search_attempts: int
    search_attempts: int
    market_attempts: int
    competition_attempts: int
    judgement_attempts: int
    domain_keywords: list[str]
    allow_unverified_dart: bool
    company_count_is_valid: bool
    selected_company_count: int
    next_stage: str

    candidate_pool: list[CompanyRecord]
    seen_company_ids: list[str]
    eligible_companies: list[CompanyRecord]
    dart_rejections: list[dict[str, str]]
    dart_seen_corp_codes: list[str]
    startup_rejections: list[dict[str, str]]
    judgement_rejections: list[dict[str, str]]
    competition_payload: dict[str, Any]
    judgement_payload: dict[str, Any]
    # judge 직후·report 직전 핸드오프: 적합 기업 요약 / 전체 평가 리스트
    final_suitable_companies: list[dict[str, Any]]
    company_evaluations: list[dict[str, Any]]
    dart_fetch_requested: NotRequired[int]
    report_payload: dict[str, Any]
    report_prep_status: NotRequired[str]
    wacc: float

    # Send로 병렬 실행된 기업별 결과를 operator.add 리듀서로 합칩니다.
    evaluated_companies: Annotated[list[CompanyRecord], operator.add]
    workflow_errors: Annotated[list[str], operator.add]
    execution_log: list[str]

    report_path: NotRequired[str]
    status: Literal[
        "initialized",
        "searching",
        "evaluating",
        "completed",
        "failed",
    ]


class CompanyTaskState(TypedDict, total=False):
    """기업 한 곳을 시장성→경쟁사→투자 판단 순서로 처리하는 워커 상태입니다."""

    company: CompanyRecord
    domain_keywords: list[str]
    target_company_count: int
    max_stage_retries: int
    market_attempts: int
    competition_attempts: int
    judgement_attempts: int
    worker_errors: list[str]

    # 서브그래프의 최종 결과를 메인 그래프의 리듀서 채널로 반환합니다.
    evaluated_companies: list[CompanyRecord]


class CompanyTaskOutput(TypedDict):
    """병렬 워커가 메인 그래프로 반환할 수 있는 필드를 제한합니다."""

    evaluated_companies: list[CompanyRecord]
