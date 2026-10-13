"""Orchestrator-Workers 기반 AI 스타트업 투자 평가 실행 스크립트.

    python app.py                      # 실제 DART·검색·LLM 호출 (.env 필요)
    python app.py --company-count 3    # 평가할 기업 수

기존 순차 파이프라인은 app_legacy.py 로 보존했습니다.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")  # 키는 환경변수로만 사용합니다. 출력·저장하지 않습니다.

from orchestrator.graph import build_graph  # noqa: E402
import orchestrator.observability as observability  # noqa: E402
from orchestrator.observability import events_path, read_events  # noqa: E402
from orchestrator.settings import Settings  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Orchestrator-Workers 투자 평가")
    p.add_argument("--company-count", type=int, default=None)
    p.add_argument("--max-replans", type=int, default=None, help="재계획(재작업) 루프 상한 (기본 2)")
    p.add_argument("--run-id", default=None)
    p.add_argument("--output-dir", default=str(ROOT / "outputs"))
    p.add_argument("--checkpoint", choices=("memory", "sqlite", "none"), default="memory")
    p.add_argument("--resume", metavar="RUN_ID", help="sqlite 체크포인트에서 이어서 실행")
    p.add_argument("--no-trace", action="store_true", help="LangSmith 트레이싱을 끕니다")
    p.add_argument("--quiet", action="store_true", help="진행 상황 출력을 끕니다 (이벤트는 events.jsonl 에 기록)")
    return p.parse_args()


def new_run_id(output_dir: str) -> str:
    """실행 시각(YYYYMMDD_HHMMSS)을 run_id 로 씁니다. 폴더 이름만 봐도 언제 실행했는지 알 수 있고 시간순으로 정렬됩니다."""
    base = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id, n = base, 2
    while (Path(output_dir) / "runs" / run_id).exists():  # 같은 초에 두 번 실행해도 겹치지 않게
        run_id, n = f"{base}_{n}", n + 1
    return run_id


def initial_state(args: argparse.Namespace, run_id: str) -> dict:
    overrides = {}
    if args.company_count is not None:
        overrides["target_company_count"] = args.company_count
    if args.max_replans is not None:
        overrides["max_replans"] = args.max_replans
    settings = Settings.from_env()
    settings = Settings.from_dict({**settings.to_dict(), **overrides})
    return {
        "run_id": run_id,
        "output_dir": args.output_dir,
        "settings": settings.to_dict(),
        "status": "RUNNING",
        "max_steps": settings.max_steps,
        "replan_count": 0,
        "plan_requested": False,
    }


def print_summary(result: dict, output_dir: str) -> None:
    print("\n" + "=" * 72)
    print(f"run_id(trace_id): {result['run_id']}")
    print(f"상태            : {result.get('status')} ({result.get('outcome')})")
    print(
        f"재계획 횟수     : {result.get('replan_count', 0)} (상한 {result['settings']['max_replans']}), "
        f"노드 실행 {result.get('step_count', 0)}회, 오류 {result.get('error_count', 0)}건"
    )
    if result.get("topup_count"):
        print(f"보충 검색       : {result['topup_count']}회 (상한 {result['settings']['max_topups']}) — 적합 기업이 목표에 모자라 부족분만 다시 찾았습니다")
    if result.get("last_error"):
        e = result["last_error"]
        print(f"마지막 오류     : [{e.get('ts')}] {e.get('node')} {e.get('type')}: {e.get('message', '')[:100]}")
    for plan in read_events(output_dir, result["run_id"], "plan"):
        print(f"  · 회차 {plan['round']} ({plan['mode']}): 서브태스크 {plan['n_subtasks']}개 {plan['by_kind']}")
    for cid, d in result.get("decisions", {}).items():
        print(f"  · {result['companies'][cid]['name']}: {d['decision']} (시장성 {d.get('market_score')}, VBM {d.get('vbm_conclusion')})")
    for cid, reason in (result.get("excluded") or {}).items():
        print(f"  · 제외 {result['companies'].get(cid, {}).get('name', cid)}: {reason}")
    for w in result.get("warnings", []):
        print(f"  ! {w}")
    print(f"보고서          : {result.get('report_path', '생성되지 않음')}")
    print("=" * 72)


def main() -> int:
    args = parse_args()
    if args.no_trace:
        os.environ["LANGCHAIN_TRACING_V2"] = "false"
        os.environ["LANGSMITH_TRACING"] = "false"

    observability.CONSOLE = not args.quiet
    run_id = args.resume or args.run_id or new_run_id(args.output_dir)
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    if args.checkpoint == "sqlite" or args.resume:
        from langgraph.checkpoint.sqlite import SqliteSaver

        saver_cm = SqliteSaver.from_conn_string(str(Path(args.output_dir) / "checkpoints.sqlite"))
    else:
        saver_cm = nullcontext(None)
        if args.checkpoint == "memory":
            from langgraph.checkpoint.memory import MemorySaver

            saver_cm = nullcontext(MemorySaver())

    config = {
        "recursion_limit": 150,
        "run_name": "orchestrator-workers",
        "tags": ["orchestrator-workers"],
        "metadata": {"run_id": run_id},
        "configurable": {"thread_id": run_id},
    }
    with saver_cm as saver:
        graph = build_graph(saver)
        payload = None if args.resume else initial_state(args, run_id)
        if payload is not None:
            cfg = payload["settings"]
            print(
                f"\n▶ 시작 run_id={run_id} · 평가 목표 {cfg['target_company_count']}곳 · 재계획 상한 {cfg['max_replans']}회 "
                f"· 진행 상황은 아래에 실시간으로 표시됩니다",
                flush=True,
            )
        result = graph.invoke(payload, config)

    print_summary(result, args.output_dir)
    print(f"이벤트 로그(결정·계획·실패·품질): {events_path(args.output_dir, run_id)}")
    return 0 if result.get("status") == "SUCCESS" else 1


if __name__ == "__main__":
    sys.exit(main())
