"""오케스트레이터 State 스키마 (Layered State: OrchestratorState + WorkerState).

설계 근거
- SKALA 강의 'State 설계': State 는 데이터 컨테이너가 아니라 Read/Write Contract 이며 실패를 고려해야 한다(Error-aware).
  Orchestrator-Workers 는 상위(OrchestratorState)와 워커(WorkerState)를 층으로 나누고, 워커가 같은 키에 동시에 쓰므로
  유실을 막는 커스텀 리듀서가 필수다. results 는 취합까지만, 최종 결과(보고서)는 외부 저장소에 두고 State 는 URI 만 가진다.
- 노션 State 예시: 제어 흐름 + 재개에 필요한 것만 State 에 두고, 결정 로그·트레이스 본문은 관측성 계층(observability.py)으로 분리한다.

노션 'State Schema' 7항목
1. 제어 vs 페이로드 분리 : State 에는 계획·상태·요약·ref/URI 만. 프로필·워커 결과 원본·보고서 본문은 store 에 저장.
2. 관측성 위치       : 결정 로그·계획·실패·품질 이력은 외부 이벤트 로그(events.jsonl)로 분리. State 에는 error-aware 최소 필드만.
3. 지속성 비용       : 체크포인트에 실리는 State 는 요약 크기. 이력성 데이터는 State 에 쌓지 않아 무한 증식하지 않음.
4. 상관             : run_id(= trace_id)를 State·저장 경로·이벤트 로그·LangSmith metadata 에 공통으로 사용.
5. 재개/복구         : status·step_count·node_status·last_error·재계획 횟수·results ref 가 State 에 있고,
                      SQLite 체크포인터 + thread_id=run_id 로 이어서 실행.
6. 동시 처리         : 워커가 동시에 쓰는 results/errors/error_count/last_error/node_status/step_count 에 리듀서 지정.
7. 종료 보장         : replan_count ≤ settings.max_replans(기본 2), step_count ≤ max_steps 를 record_failure 가 확인,
                      recursion_limit 은 최후 안전장치.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from typing_extensions import TypedDict

# 워커 종류: 서브태스크의 kind. 노드 하나가 기능 하나(= kind 하나)만 맡습니다.
WORKER_KINDS = ("financial", "market_rag", "market_score", "competitor")

# status: 상위 State 의 종료 상태 (SKALA: SUCCESS | FAILED | INTERRUPTED)
RUNNING, SUCCESS, FAILED, INTERRUPTED = "RUNNING", "SUCCESS", "FAILED", "INTERRUPTED"


class Subtask(TypedDict):
    """플래너가 State 에 저장하는 구조화된 서브태스크."""

    id: str  # f"{company_id}:{kind}"
    company_id: str
    kind: str
    round: int  # 0 = 최초 계획, 1.. = 재계획 회차
    reason: str  # 이 태스크를 계획한 이유
    directive: dict[str, Any]  # 워커에게 주는 지시 (예: 웹 검색 전환)


class Result(TypedDict, total=False):
    """워커 결과 한 건. SKALA 형식 {agent, output, status, ts} + 요약·참조."""

    subtask_id: str
    company_id: str
    kind: str
    round: int
    agent: str  # 예: "market_rag_worker"
    worker_id: str  # 에이전트이름_timestamp
    status: str  # "ok" | "error"
    ts: str
    duration_s: float  # 워커 실행 소요 시간(초)
    summary: dict[str, Any]  # 충분성 판정에 쓰는 작은 요약
    ref: str  # output: 원본 payload 위치 (store)
    error: dict[str, Any]


class LastError(TypedDict, total=False):
    """SKALA: last_error {node, type, message, ts}"""

    node: str
    type: str
    message: str
    ts: str
    company_id: str


class ErrorRecord(TypedDict, total=False):
    """SKALA: errors 항목 {agent, error, ts}"""

    agent: str
    company_id: str
    error: dict[str, Any]
    ts: str


class Failure(TypedDict, total=False):
    stage: str  # "validate" | "sufficiency" | "quality"
    company_id: str  # 보고서 전체 문제면 ""
    kind: str  # 어떤 워커/요소의 문제인지 ("profile", "report" 포함)
    code: str
    reason: str
    needed: str  # 보완에 필요한 근거


class WorkerState(TypedDict, total=False):
    """Send 로 각 워커에 전달되는 개별 상태 (Layered State 의 하위 층).

    worker_id 는 '에이전트이름_timestamp' 로 워커 실행 한 건을 식별합니다.
    워커 결과는 results/errors/error_count/last_error 로 상위 State 에 올라갑니다.
    """

    worker_id: str
    # 상위에서 물려받는 문맥 (읽기 전용)
    run_id: str
    output_dir: str
    settings: dict[str, Any]
    # 워커 실행 과제
    subtask: Subtask
    profile_ref: str
    # 워커 내부 실행 상태
    status: str  # RUNNING | SUCCESS | FAILED
    step_count: int
    max_steps: int
    error_count: int
    last_error: LastError | None


# ── 리듀서 ───────────────────────────────────────────────────
def merge_results(old: dict | None, new: dict | None) -> dict:
    """워커 결과를 subtask id 기준으로 병합합니다. 더 최신 회차의 결과가 이전 결과를 대체합니다."""

    merged = dict(old or {})
    for key, value in (new or {}).items():
        previous = merged.get(key)
        if previous is None or value.get("round", 0) >= previous.get("round", 0):
            merged[key] = value
    return merged


def merge_latest(old: dict | None, new: dict | None) -> dict:
    """키별 최신 값 우선."""

    return {**(old or {}), **(new or {})}


def merge_nested(old: dict | None, new: dict | None) -> dict:
    """기업별 메타처럼 {id: {필드…}} 구조를 id 안쪽 필드 단위로 병합합니다. (노드가 일부 필드만 갱신해도 나머지 유지)"""

    merged = {k: dict(v) for k, v in (old or {}).items()}
    for key, value in (new or {}).items():
        merged[key] = {**merged.get(key, {}), **value}
    return merged


def merge_first(old: dict | None, new: dict | None) -> dict:
    """키별 먼저 기록된 값 유지 (제외 사유는 처음 것을 보존)."""

    merged = dict(new or {})
    merged.update(old or {})
    return merged


def latest_error(old: dict | None, new: dict | None) -> dict | None:
    """last_error: 병렬 워커가 동시에 써도 ts 가 더 나중인 오류를 남깁니다."""

    if not old:
        return new
    if not new:
        return old
    return new if new.get("ts", "") >= old.get("ts", "") else old


class OrchestratorState(TypedDict, total=False):
    # ── 식별·설정 (상관 키) ───────────────────────────────────
    run_id: str  # = trace_id
    output_dir: str
    settings: dict[str, Any]

    # ── 전체 흐름 제어 ────────────────────────────────────────
    plan: str  # 현재 회차 계획 요약
    plan_started_at: str  # 현재 회차 워커 실행 시작 시각 (병렬 효과 계산용)
    status: str  # RUNNING | SUCCESS | FAILED | INTERRUPTED
    outcome: str  # completed | completed_with_warnings | failed_unresolved
    step_count: Annotated[int, operator.add]  # 종료 가드
    max_steps: int
    node_status: Annotated[dict, merge_latest]  # 재개·재시도 판단용 노드별 최소 상태
    stage: str  # 마지막으로 실패 판정을 낸 단계
    failed_stage: str
    replan_count: int
    plan_requested: bool
    retry_possible: bool
    profile_targets: list[str]
    topup_count: int  # 부족분 보충 검색을 한 횟수 (settings.max_topups 이하)
    topup_shortfall: int  # 보충이 요청된 부족 수 (dart_lookup → startup_screen 이 읽고 소모)
    topup_ids: list[str]  # 이번 보충으로 새로 들어온 기업 (profile_prep·planner 가 이 기업만 처리)

    # ── Error-aware (SKALA) ───────────────────────────────────
    error_count: Annotated[int, operator.add]
    last_error: Annotated[LastError | None, latest_error]
    errors: Annotated[list, operator.add]  # {agent, error, ts}

    # ── 대상 기업 색인 (작은 메타만) ──────────────────────────
    candidate_ids: list[str]  # 이번 DART 수집에서 나온 후보 (스크리닝 입력)
    company_ids: list[str]  # 평가 대상 누적 (보충 시 기존 기업 + 새 기업)
    companies: Annotated[dict, merge_nested]  # id -> {name, base_ref, profile_ref, profile_summary}
    excluded: Annotated[dict, merge_first]  # id -> 제외 사유
    profile_refined: Annotated[dict, merge_latest]  # id -> 프로필을 보완한 회차

    # ── 계획·결과 ─────────────────────────────────────────────
    subtasks: list[Subtask]  # 현재 회차 계획 (회차마다 대체)
    results: Annotated[dict, merge_results]  # 워커 동시 쓰기 → 커스텀 리듀서 (취합까지만, 종합은 별도 노드)
    merge_status: dict[str, Any]

    # ── 검증 ──────────────────────────────────────────────────
    failures: list[Failure]  # 현재 단계의 실패 (단계마다 대체)
    warnings: Annotated[list, operator.add]

    # ── 종합 (본문은 store, State 는 참조·요약) ────────────────
    compare: dict[str, Any]
    decisions: dict[str, Any]
    report: dict[str, Any]
    report_uri: str
    quality: dict[str, Any]
    quality_feedback: list  # 직전 품질 평가의 미달 사유 (다음 report_draft 가 문구·인용을 보정할 때 읽음)
    report_path: str
