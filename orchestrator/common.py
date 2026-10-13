"""노드 공통 도우미: 컨텍스트(설정·저장소·외부 서비스), 결정 로그 항목."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from orchestrator.services import Services
from orchestrator.settings import Settings
from orchestrator.store import Store


@dataclass
class Ctx:
    state: dict[str, Any]
    settings: Settings
    store: Store
    services: Any

    @property
    def round(self) -> int:
        return int(self.state.get("replan_count", 0))


def get_services() -> Services:
    """외부 서비스 창구. (테스트는 이 함수를 가짜 응답기로 대체한다)"""
    return Services()


def get_ctx(state: dict[str, Any]) -> Ctx:
    settings = Settings.from_dict(state.get("settings") or {})
    store = Store(state.get("output_dir") or "outputs", state["run_id"])
    return Ctx(state=state, settings=settings, store=store, services=get_services())


def log(state: dict[str, Any], node: str, event: str, reason: str = "", **extra: Any) -> dict[str, Any]:
    """결정 로그 한 줄. run_id 와 회차를 항상 포함해 외부 트레이스와 연결합니다."""

    return {
        "run_id": state.get("run_id"),
        "round": state.get("replan_count", 0),
        "node": node,
        "event": event,
        "reason": reason,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **extra,
    }


def active_ids(state: dict[str, Any]) -> list[str]:
    excluded = state.get("excluded") or {}
    return [cid for cid in state.get("company_ids", []) if cid not in excluded]
