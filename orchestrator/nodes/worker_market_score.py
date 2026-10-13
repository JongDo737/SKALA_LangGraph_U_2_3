"""[워커] 외부 시장성 채점. (기능: 시장 질문 10개를 웹 근거로 채점해 100점 점수 산출)"""

from __future__ import annotations

from typing import Any

from orchestrator.nodes._worker import execute


def _run(ctx: Any, profile: dict[str, Any], subtask: dict[str, Any]) -> dict[str, Any]:
    return ctx.services.market_score(profile, subtask["round"], subtask["directive"])


def worker_market_score(task: dict[str, Any]) -> dict[str, Any]:
    return execute(task, _run)
