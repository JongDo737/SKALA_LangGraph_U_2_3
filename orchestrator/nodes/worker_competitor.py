"""[워커] 경쟁사 조사. (기능: 동일 산업 경쟁사와 근거 수집)"""

from __future__ import annotations

from typing import Any

from orchestrator.nodes._worker import execute


def _run(ctx: Any, profile: dict[str, Any], subtask: dict[str, Any]) -> dict[str, Any]:
    return ctx.services.competitor(profile, subtask["round"], subtask["directive"])


def worker_competitor(task: dict[str, Any]) -> dict[str, Any]:
    return execute(task, _run)
