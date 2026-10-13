"""관측성 계층: 결정 로그·계획 이력·실패 이력은 State 가 아니라 여기(외부 이벤트 로그)에 남깁니다.

- 노션 State 예시: "결정 로그는 State 가 아니라 관측성 계층으로 … {trace_id, node, decision, reason, ts}"
- SKALA 강의: State 에는 제어·재개에 필요한 것만 두고(status, step_count, error_count, last_error …),
  이력은 외부에 둡니다. 상관 키는 run_id(= trace_id)이며 LangSmith metadata 와 같은 값입니다.

노드 함수는 기존처럼 ``decision_log`` 등을 반환하면 되고, ``observed`` 데코레이터가 그것을 State 에 싣지 않고
{output_dir}/runs/{run_id}/events.jsonl 에 추가합니다. (State 에는 step_count·node_status 만 반영)
"""

from __future__ import annotations

import functools
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

_lock = threading.Lock()

# 진행 상황을 화면에도 출력합니다 (events.jsonl 에는 항상 기록). app.py --quiet 로 끌 수 있습니다.
CONSOLE = True
_progress = {"total": 0, "done": 0}  # 현재 회차 워커 진행도 (병렬 워커 완료 순서대로 증가)

# 노드가 반환해도 State 에 싣지 않고 외부로 보내는 키 → 이벤트 type
EXTERNAL_KEYS = {
    "decision_log": "decision",
    "plan_history": "plan",
    "failure_history": "failure",
    "quality_history": "quality",
}


def now_ts() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def events_path(output_dir: str | Path, run_id: str) -> Path:
    return Path(output_dir) / "runs" / run_id / "events.jsonl"


def _local_time(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")
    except ValueError:
        return ts[11:19]


def _print_progress(record: dict[str, Any]) -> None:
    """이벤트를 사람이 읽는 한 줄로 화면에 출력합니다. (호출자가 _lock 을 잡고 있어야 함)"""
    kind = record.get("type")
    if kind == "plan":
        _progress["total"], _progress["done"] = int(record.get("n_subtasks", 0)), 0
        return
    if kind not in ("decision", "node_exception"):
        return
    clock = _local_time(record["ts"])
    if kind == "node_exception":
        err = record.get("error", {})
        print(f"[{clock}] !! {record.get('node')} 오류: {err.get('type')}: {err.get('message')}", flush=True)
        return
    node, event = record.get("node", ""), record.get("event", "")
    reason = record.get("reason", "")
    if node.startswith("worker_") and event in ("done", "error"):
        _progress["done"] += 1
        mark = "✓" if event == "done" else "✗"
        took = f" [{record['duration_s']:.1f}s]" if record.get("duration_s") is not None else ""
        print(f"[{clock}]   ({_progress['done']}/{_progress['total']}) {mark} {node:<20} {reason}{took}", flush=True)
    else:
        label = f"R{record.get('round', 0)}"
        took = f" [{record['node_s']:.1f}s]" if (record.get("node_s") or 0) >= 1.0 else ""  # 오래 걸린 단계가 어디인지 보이게
        print(f"[{clock}] {label} {node:<20} {reason}{took}", flush=True)


def emit(output_dir: str | Path, run_id: str, event: dict[str, Any]) -> None:
    path = events_path(output_dir, run_id)
    record = {"trace_id": run_id, "ts": now_ts(), **event}
    with _lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if CONSOLE:
            _print_progress(record)


def read_events(output_dir: str | Path, run_id: str, type: str | None = None) -> list[dict[str, Any]]:
    path = events_path(output_dir, run_id)
    if not path.exists():
        return []
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [e for e in events if type is None or e.get("type") == type]


def observed(node_name: str) -> Callable:
    """노드 래퍼: 외부 이벤트 기록 + 제어 메타(step_count, node_status) 반영 + 예외 기록."""

    def decorate(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(state: dict[str, Any]) -> dict[str, Any]:
            output_dir = state.get("output_dir") or "outputs"
            run_id = state.get("run_id", "unknown")
            started = time.monotonic()
            try:
                out = dict(fn(state) or {})
            except Exception as error:  # noqa: BLE001 - 기록 후 그대로 다시 던집니다 (체크포인트로 재개 가능)
                emit(
                    output_dir,
                    run_id,
                    {
                        "type": "node_exception",
                        "node": node_name,
                        "error": {"type": type(error).__name__, "message": str(error)},
                    },
                )
                raise
            elapsed = round(time.monotonic() - started, 2)
            for key, event_type in EXTERNAL_KEYS.items():
                for entry in out.pop(key, None) or []:
                    if event_type == "decision" and not node_name.startswith("worker_"):
                        entry = {"node_s": elapsed, **entry}  # 노드 실행 소요 시간 (워커는 duration_s 를 따로 가짐)
                    emit(output_dir, run_id, {"type": event_type, **entry})
            out["step_count"] = out.get("step_count", 0) + 1
            out["node_status"] = {node_name: "error" if out.get("last_error") else "ok"}
            return out

        return wrapper

    return decorate
