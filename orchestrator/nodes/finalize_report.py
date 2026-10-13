"""[노드] 보고서 확정. (기능: 최종 보고서를 파일(PDF·마크다운)로 저장하고 종료 상태를 기록)

PDF 는 지난 보고서 구성(SUMMARY → 종합 분석 → 시장·경쟁 → 투자 판단·리스크 → REFERENCE)으로 기존 생성기를 통해 만들고,
생성기가 실패하면 단순 렌더러로 대체하며 경고를 남깁니다. 마크다운은 품질 평가를 통과한 초안 원본입니다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.common import get_ctx, log
from orchestrator.legacy_report import render_legacy_pdf
from orchestrator.report_render import render_markdown_pdf


def finalize_report(state: dict[str, Any]) -> dict[str, Any]:
    ctx = get_ctx(state)
    draft = ctx.store.get(state["report"]["ref"])
    markdown = draft["markdown"]
    warnings: list[str] = []
    quality = state.get("quality") or {}

    out_dir = Path(state.get("output_dir") or "outputs")
    md_path = ctx.store.write_text("investment_report.md", markdown)
    (out_dir / "investment_report.md").write_text(markdown, encoding="utf-8")

    suitable = [cid for cid, d in (state.get("decisions") or {}).items() if d["decision"] == "적합" and cid not in (state.get("excluded") or {})]
    pdf_path = ctx.store.root / "investment_report.pdf"
    renderer = "legacy(지난 보고서 구성)"
    try:
        _, payload = render_legacy_pdf(state, ctx.store, suitable, pdf_path, ctx.settings.report_use_llm)
        ctx.store.put("report", "legacy_payload", payload)
    except Exception as error:  # noqa: BLE001 - 생성기 실패가 보고서 전체를 막지 않게 대체합니다
        renderer = "simple(대체 렌더러)"
        warnings.append(f"기존 보고서 생성기 실패로 단순 PDF 로 대체: {type(error).__name__}: {error}")
        render_markdown_pdf(markdown, pdf_path)
    (out_dir / "investment_report.pdf").write_bytes(pdf_path.read_bytes())

    all_warnings = [*(state.get("warnings") or []), *warnings]
    outcome = "completed" if quality.get("passed") and not all_warnings else "completed_with_warnings"
    return {
        "status": "SUCCESS",
        "outcome": outcome,
        "report_path": str(pdf_path),
        "warnings": warnings,
        "decision_log": [
            log(
                state,
                "finalize_report",
                outcome,
                f"보고서 확정: {md_path.name}, {pdf_path.name} [{renderer}] (경고 {len(all_warnings)}건, 재계획 {state.get('replan_count', 0)}회)",
            )
        ],
    }
