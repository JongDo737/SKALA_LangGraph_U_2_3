"""State(요약·ref) → store(원본) 조회 도우미."""

from __future__ import annotations

from typing import Any

from orchestrator.store import Store


def result_of(state: dict[str, Any], company_id: str, kind: str) -> dict[str, Any] | None:
    """기업·종류의 최신 결과가 정상(ok)이면 반환합니다."""
    result = (state.get("results") or {}).get(f"{company_id}:{kind}")
    return result if result and result.get("status") == "ok" else None


def payload_of(state: dict[str, Any], store: Store, company_id: str, kind: str) -> dict[str, Any] | None:
    result = result_of(state, company_id, kind)
    return store.get(result["ref"]) if result else None


def profile_of(state: dict[str, Any], store: Store, company_id: str) -> dict[str, Any]:
    return store.get(state["companies"][company_id]["profile_ref"])


def summaries_of(state: dict[str, Any], company_id: str) -> dict[str, dict[str, Any]]:
    out = {}
    for kind in ("financial", "market_rag", "market_score", "competitor"):
        result = result_of(state, company_id, kind)
        if result:
            out[kind] = result.get("summary") or {}
    return out
