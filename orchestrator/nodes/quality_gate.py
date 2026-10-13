"""[노드] 보고서 품질 평가. (기능: Groundedness / 중립성 / 편향 통제를 판정하고 미달 사유를 Failure 로 만든다)

방식: Hybrid. 규칙(rules.check_report)이 형식·출처 연결·표현·다양성을 검사하고,
서비스가 LLM 심사(quality_review)를 제공하면 내용 일치·편중 판단을 추가합니다.
"""

from __future__ import annotations

from typing import Any

from orchestrator import rules
from orchestrator.common import get_ctx, log


def quality_gate(state: dict[str, Any]) -> dict[str, Any]:
    ctx = get_ctx(state)
    draft = ctx.store.get(state["report"]["ref"])
    verdict = rules.check_report(draft["markdown"], ctx.settings)

    review = getattr(ctx.services, "quality_review", None)
    if callable(review):
        extra = review(draft["markdown"], draft["refs"]) or {}
        for name, issues in extra.items():
            check = verdict["checks"].setdefault(name, {"passed": True, "issues": []})
            check["issues"].extend(issues)
            check["passed"] = not check["issues"]
        verdict["passed"] = all(c["passed"] for c in verdict["checks"].values())

    name_to_id = {meta["name"]: cid for cid, meta in state["companies"].items()}
    failures = []
    for check_name, check in verdict["checks"].items():
        for issue in check["issues"]:
            failures.append(
                {
                    "stage": "quality",
                    "company_id": name_to_id.get(issue.get("company", ""), ""),
                    "kind": issue["kind"],
                    "code": issue["code"],
                    "reason": f"[{check_name}] {issue['company'] or '보고서'} · {issue['label']}: {issue['detail']}",
                    "needed": issue.get("needed", ""),
                }
            )
    summary = {
        "passed": verdict["passed"],
        "round": state.get("replan_count", 0),
        "checks": {k: {"passed": v["passed"], "n_issues": len(v["issues"])} for k, v in verdict["checks"].items()},
    }
    return {
        "quality": summary,
        "quality_history": [summary],
        "failures": failures,
        "quality_feedback": failures,  # 통과하면 빈 목록 → 다음 초안은 보정하지 않음
        "stage": "quality",
        "decision_log": [
            log(
                state,
                "quality_gate",
                "pass" if verdict["passed"] else "fail",
                "품질 평가 통과"
                if verdict["passed"]
                else "품질 미달: " + ", ".join(f"{k}({v['n_issues']})" for k, v in summary["checks"].items() if not v["passed"]),
            )
        ],
    }
