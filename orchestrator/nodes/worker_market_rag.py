"""[워커] 시장 자료 조사(RAG, 부족하면 웹). (기능: 시장 규모·성장성·수요 근거 수집)"""

from __future__ import annotations

from typing import Any

from orchestrator.nodes._worker import execute


def _run(ctx: Any, profile: dict[str, Any], subtask: dict[str, Any]) -> dict[str, Any]:
    return ctx.services.market_rag(profile, subtask["round"], subtask["directive"])


def worker_market_rag(task: dict[str, Any]) -> dict[str, Any]:
    return execute(task, _run)
