"""[노드] 오케스트레이터 플래너. (기능: 작업 계획·재계획 — 서브태스크 목록을 State에 구조화해 저장)

- 최초 계획: 기업마다 데이터 조건에 맞는 서브태스크를 만듭니다. (재무 데이터가 없으면 계획 단계에서 제외)
- 재계획  : 직전 단계의 실패 사유를 읽고, 실패·부족한 (기업, 종류)만 다시 계획합니다.
            이미 유효한 결과는 다시 돌리지 않고 재사용합니다. 프로필이 약한 기업은 '프로필 추가 보완'이 먼저입니다.
결정은 규칙 기반이라 같은 State 에서는 같은 계획이 나옵니다(재현성). 계획과 사유는 plan_history/decision_log 에 남습니다.
"""

from __future__ import annotations

from typing import Any

from orchestrator.artifacts import payload_of
from orchestrator.common import active_ids, get_ctx, log
from orchestrator.observability import now_ts
from orchestrator.rules import directive_for, has_financial_data
from orchestrator.state import WORKER_KINDS

DEPENDS_ON_PROFILE = ("market_rag", "market_score", "competitor")


def _task(cid: str, kind: str, round_no: int, reason: str, directive: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"{cid}:{kind}",
        "company_id": cid,
        "kind": kind,
        "round": round_no,
        "reason": reason,
        "directive": directive,
    }


def _competitor_directive(state: dict[str, Any], store: Any, cid: str) -> dict[str, Any]:
    """경쟁사 재작업은 1차 후보를 새로 제안받지 않고 그대로 검색으로 확인합니다.

    (2차에서 후보를 다시 제안받으면 이름이 바뀌어 더 부적절한 회사가 나온 사례가 있었습니다.)
    이미 확인을 시도했는데도 근거가 없으면 한 번은 새 후보를 제안받도록 허용합니다(repropose).
    """
    previous = (payload_of(state, store, cid, "competitor") or {}).get("research") or {}
    candidates = [
        {"name": c.get("name"), "evidence": (c.get("evidence") or "")[:300]}
        for c in previous.get("competitors") or []
        if isinstance(c, dict) and c.get("name")
    ]
    if candidates and "search_verification" not in (previous.get("selection_method") or ""):
        return {"candidates": candidates}
    return {"repropose": True}


def planner(state: dict[str, Any]) -> dict[str, Any]:
    ctx = get_ctx(state)
    requested = bool(state.get("plan_requested"))
    round_no = int(state.get("replan_count", 0)) + (1 if requested else 0)
    excluded = state.get("excluded") or {}
    new_excluded: dict[str, str] = {}
    tasks: dict[str, dict[str, Any]] = {}
    profile_targets: list[str] = []

    topup_ids = [cid for cid in state.get("topup_ids") or [] if cid in active_ids(state)]
    if topup_ids or round_no == 0:
        # 최초 계획이거나, 부족분 보충으로 새로 들어온 기업의 첫 계획입니다. (기존 기업의 결과는 건드리지 않음)
        for cid in topup_ids or active_ids(state):
            profile = ctx.store.get(state["companies"][cid]["profile_ref"])
            if not has_financial_data(profile):
                new_excluded[cid] = "재무 데이터 없음 (계획 단계에서 제외)"
                continue
            for kind in WORKER_KINDS:
                tasks[f"{cid}:{kind}"] = _task(cid, kind, round_no, "보충 기업 초기 계획" if topup_ids else "초기 계획", {})
        mode = "topup" if topup_ids else "initial"
    else:
        refined = state.get("profile_refined") or {}
        by_company: dict[str, dict[str, dict[str, Any]]] = {}
        for failure in state.get("failures") or []:
            cid, kind = failure.get("company_id"), failure.get("kind")
            # 우열·추천 표현은 어느 관점(프로필·경쟁사 등)에서 나왔든 근거를 다시 조사할 문제가 아니라 문장 정리 문제입니다.
            # 워커·프로필 재조사로 멀쩡한 결과까지 바뀌지 않도록 다음 report_draft 가 해당 문장만 정리합니다(quality_feedback).
            if failure.get("code") == "neutrality_term":
                continue
            if cid and cid not in excluded and kind:
                by_company.setdefault(cid, {})[kind] = failure
        for cid, kinds in by_company.items():
            if "profile" in kinds and refined.get(cid) != round_no:
                profile_targets.append(cid)
                continue
            if "profile" in kinds:
                for kind in DEPENDS_ON_PROFILE:
                    tasks[f"{cid}:{kind}"] = _task(
                        cid, kind, round_no, "프로필 보완 후 재실행", {"round": round_no}
                    )
            for kind, failure in kinds.items():
                # 재무 지표 부족은 같은 데이터로 다시 계산해도 달라지지 않으므로 재계획하지 않습니다(소진 시 제외 처리)
                if failure.get("code") == "fin_coverage_low":
                    continue
                if kind in WORKER_KINDS:
                    directive = directive_for(failure, round_no)
                    if kind == "competitor":
                        directive.update(_competitor_directive(state, ctx.store, cid))
                    tasks[f"{cid}:{kind}"] = _task(cid, kind, round_no, failure.get("reason", "재작업"), directive)
        if profile_targets:
            tasks = {}  # 프로필 보완이 먼저입니다. 보완 후 같은 회차로 다시 계획합니다.
        mode = "replan"

    subtasks = list(tasks.values())
    by_kind: dict[str, int] = {}
    for t in subtasks:
        by_kind[t["kind"]] = by_kind.get(t["kind"], 0) + 1
    reason = (
        f"{mode} 회차 {round_no}: 서브태스크 {len(subtasks)}개 {by_kind}"
        + (f", 프로필 보완 대상 {len(profile_targets)}곳" if profile_targets else "")
    )
    return {
        "plan": reason,
        "plan_started_at": now_ts(),
        "subtasks": subtasks,
        "profile_targets": profile_targets,
        "topup_ids": [],
        "replan_count": round_no,
        "plan_requested": False,
        "excluded": new_excluded,
        "plan_history": [
            {
                "round": round_no,
                "mode": mode,
                "n_subtasks": len(subtasks),
                "by_kind": by_kind,
                "profile_targets": profile_targets,
            }
        ],
        "decision_log": [log({**state, "replan_count": round_no}, "planner", "plan", reason)],
    }
