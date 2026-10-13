"""[노드] 결과 병합·작업 완료 확인. (기능: 계획한 서브태스크의 결과가 모두 도착했는지 집계)

결과 자체의 병합은 State 리듀서(results)가 하고, 이 노드는 완료 여부와 병렬 처리 효과(워커 소요 합계 대비 실제 경과)를 기록합니다.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from orchestrator.common import log


def merge_results(state: dict[str, Any]) -> dict[str, Any]:
    planned = state.get("subtasks") or []
    results = state.get("results") or {}
    done = [t["id"] for t in planned if (results.get(t["id"]) or {}).get("round") == t["round"]]
    missing = [t["id"] for t in planned if t["id"] not in done]

    busy = sum(float((results.get(i) or {}).get("duration_s") or 0) for i in done)
    wall = 0.0
    if state.get("plan_started_at"):
        started = datetime.fromisoformat(state["plan_started_at"])
        wall = (datetime.now(timezone.utc) - started).total_seconds()
    speed = (
        f" · 워커 소요 합계 {busy:.0f}초 → 실제 경과 {wall:.0f}초 (병렬 {busy / wall:.1f}배)"
        if planned and wall >= 1.0 and busy > 0
        else ""
    )
    return {
        "merge_status": {"planned": len(planned), "done": len(done), "missing": missing, "worker_seconds": round(busy, 1), "wall_seconds": round(wall, 1)},
        "decision_log": [
            log(
                state,
                "merge_results",
                "merged",
                f"계획 {len(planned)}개 중 {len(done)}개 도착, 누락 {len(missing)}개{speed}",
            )
        ],
    }
