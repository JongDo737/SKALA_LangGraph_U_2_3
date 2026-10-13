"""워커 노드 공통 실행기.

각 워커 노드는 자기 기능 함수(do) 하나만 정의하고, 이 실행기가
컨텍스트 구성 → 프로필 로드 → 실행 → 저장 → 요약 → 실패 격리를 맡습니다.
워커가 예외를 던져도 그래프가 죽지 않고 status="error" 결과로 돌아와
검증 단계에서 계속/재시도/제외 정책이 적용됩니다. (Error-aware: errors / error_count / last_error 에 ts 와 함께 기록)
"""

from __future__ import annotations

import time
from typing import Any, Callable

from orchestrator import rules
from orchestrator.common import get_ctx, log
from orchestrator.observability import now_ts


def _brief(kind: str, summary: dict[str, Any]) -> str:
    """워커 결과를 한 줄로 요약해 진행 로그에 보여 줍니다."""
    if kind == "financial":
        cov = summary.get("coverage")
        return f"재무 VBM {summary.get('conclusion')} (지표 {cov:.0%})" if isinstance(cov, (int, float)) else f"재무 VBM {summary.get('conclusion')}"
    if kind == "market_rag":
        return f"시장 자료 {summary.get('status')}, 출처 {summary.get('n_sources')}건"
    if kind == "market_score":
        return f"시장성 {summary.get('score')}점 (질문 {summary.get('n_scored')}/{summary.get('n_questions')}, 근거 도메인 {summary.get('n_domains')}개)"
    if kind == "competitor":
        return f"경쟁사 {summary.get('found')}곳 (타사 출처 확인 {summary.get('grounded')}곳)"
    return ""


def execute(
    task_input: dict[str, Any],
    do: Callable[[Any, dict[str, Any], dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    subtask = task_input["subtask"]
    kind, cid = subtask["kind"], subtask["company_id"]
    state_like = {**task_input, "replan_count": subtask["round"]}
    ctx = get_ctx(state_like)

    agent = f"{kind}_worker"
    result: dict[str, Any] = {
        "subtask_id": subtask["id"],
        "company_id": cid,
        "kind": kind,
        "round": subtask["round"],
        "agent": agent,
        "worker_id": task_input.get("worker_id", ""),  # 에이전트이름_timestamp
    }
    started = time.monotonic()
    try:
        profile = ctx.store.get(task_input["profile_ref"])
        payload = do(ctx, profile, subtask)
        ref = ctx.store.put(f"results/{kind}", f"{cid}_r{subtask['round']}", payload)
        result.update(
            status="ok",
            duration_s=round(time.monotonic() - started, 2),
            ts=now_ts(),
            ref=ref,
            summary=rules.SUMMARIZERS[kind](payload, profile),
        )
        entry = log(
            state_like,
            f"worker_{kind}",
            "done",
            f"{profile.get('name', cid)}: {_brief(kind, result['summary'])}"[:160],
            company_id=cid,
            plan_reason=subtask["reason"],
            duration_s=result["duration_s"],
            worker_id=result["worker_id"],
            summary=result["summary"],
        )
        return {"results": {subtask["id"]: result}, "decision_log": [entry]}
    except Exception as error:  # noqa: BLE001 - 워커 실패는 결과로 격리합니다
        ts = now_ts()
        info = {"type": type(error).__name__, "message": str(error)}
        result.update(status="error", ts=ts, error=info, duration_s=round(time.monotonic() - started, 2))
        entry = log(
            state_like,
            f"worker_{kind}",
            "error",
            f"{info['type']}: {info['message']}"[:200],
            company_id=cid,
            worker_id=result["worker_id"],
            duration_s=result["duration_s"],
        )
        return {
            "results": {subtask["id"]: result},
            "errors": [{"agent": agent, "company_id": cid, "error": info, "ts": ts}],
            "error_count": 1,
            "last_error": {"node": f"worker_{kind}", "company_id": cid, **info, "ts": ts},
            "decision_log": [entry],
        }
