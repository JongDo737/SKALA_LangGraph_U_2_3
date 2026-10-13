"""[워커] 대상 기업 재무 계산. (기능: VBM 지표 계산과 재무 기준 통과 여부)"""

from __future__ import annotations

from typing import Any

from orchestrator.nodes._worker import execute


def _run(ctx: Any, profile: dict[str, Any], subtask: dict[str, Any]) -> dict[str, Any]:
    from agents.financial_vbm import run_financial_agent
    from config import DEFAULT_WACC

    company = {
        "name": profile["name"],
        "financials": profile.get("financials") or [profile.get("financial_summary") or {}],
        "wacc": DEFAULT_WACC,
    }
    return run_financial_agent(company, DEFAULT_WACC)


def worker_financial(task: dict[str, Any]) -> dict[str, Any]:
    return execute(task, _run)
