"""[노드] 보고서 초안. (기능: 판단 결과와 근거를 SUMMARY / 기업별 평가 / REFERENCE 마크다운으로 작성)

모든 사실 서술에는 [n] 인용을 붙이고, REFERENCE에는 인용된 자료만 싣습니다.
근거가 없는 항목은 '(출처 확인 불가)'로 표시해 품질 평가가 잡아내도록 합니다. 우열·추천 표현은 쓰지 않습니다.
"""

from __future__ import annotations

import re
from typing import Any

from orchestrator import rules
from orchestrator.artifacts import payload_of, profile_of
from orchestrator.common import active_ids, get_ctx, log


class _Refs:
    """인용 번호 등록부. 같은 출처는 하나의 번호로 합칩니다."""

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []
        self._index: dict[str, int] = {}

    def add(self, title: str, url: str = "", publisher: str = "") -> int:
        key = url or title
        if key not in self._index:
            self.items.append({"title": title, "url": url, "publisher": publisher})
            self._index[key] = len(self.items)
        return self._index[key]

    def tags(self, numbers: list[int]) -> str:
        return "".join(f"[{n}]" for n in sorted(set(numbers)))


def _ext(text: Any) -> str:
    """외부 자료 텍스트의 '[2]' 같은 대괄호 숫자는 보고서의 인용 번호와 혼동되므로 제거합니다."""
    return re.sub(r"\s*\[\d+\]", "", str(text or ""))


def _pct(value: Any) -> str:
    return f"{value * 100:.1f}%" if isinstance(value, (int, float)) else "산출 불가"


def _metrics(vbm: dict[str, Any]) -> dict[str, Any]:
    return {m["name"]: m["value"] for m in vbm.get("metrics", []) if m.get("status") == "calculated"}


def _pick_score_sources(assessment: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    """채점 근거 중 저품질(유튜브·블로그)을 피하고 서로 다른 도메인을 우선 고릅니다."""

    seen: set[str] = set()
    chosen: list[dict[str, Any]] = []
    candidates = [e for q in assessment.get("questions", []) for e in q.get("evidence", []) if e.get("url")]
    for allow_lowq in (False, True):
        for e in candidates:
            d = rules.domain_of(e["url"])
            if d in seen or len(chosen) >= limit:
                continue
            if rules.is_lowquality(e["url"]) and not allow_lowq:
                continue
            seen.add(d)
            chosen.append(e)
    return chosen


def report_draft(state: dict[str, Any]) -> dict[str, Any]:
    from agents.market_scoring import MARKET_PASS_SCORE

    ctx = get_ctx(state)
    refs = _Refs()
    decisions = state.get("decisions") or {}
    names = {cid: state["companies"][cid]["name"] for cid in state.get("company_ids", [])}
    excluded = state.get("excluded") or {}
    warnings = state.get("warnings") or []

    suitable = [cid for cid in active_ids(state) if (decisions.get(cid) or {}).get("decision") == "적합"]
    rejected = [cid for cid in active_ids(state) if (decisions.get(cid) or {}).get("decision") == "부적합"]

    sections: list[str] = []
    for index, cid in enumerate(suitable, 1):
        profile = profile_of(state, ctx.store, cid)
        financial = payload_of(state, ctx.store, cid, "financial") or {}
        rag = (payload_of(state, ctx.store, cid, "market_rag") or {}).get("market") or {}
        score = (payload_of(state, ctx.store, cid, "market_score") or {}).get("assessment") or {}
        research = (payload_of(state, ctx.store, cid, "competitor") or {}).get("research") or {}
        decision = decisions[cid]
        lines = [f"### {index}. {profile['name']} — {decision['decision']}"]

        # 사업 개요 (프로필: 웹검색 출처)
        cites = [
            refs.add(s.get("title", "웹 자료"), s["url"])
            for s in [x for x in (profile.get("sources") or []) if x.get("url")][:2]
        ]
        text = _ext(profile.get("description")) or "사업 설명을 확인하지 못했습니다."
        lines.append(f"- **사업 개요**: {text} {refs.tags(cites)}" if cites else f"- **사업 개요**: {text} (출처 확인 불가)")

        # 시장 규모 (RAG/웹 요약)
        summary = rag.get("summary") or {}
        parts = [
            f"{label}: {_ext(summary[key])}"
            for label, key in (("대상 시장", "target_market"), ("시장 규모", "market_size"), ("성장성", "growth"))
            if summary.get(key)
        ]
        cites = [
            refs.add(s.get("title", "시장 자료"), s.get("url", ""), s.get("publisher", ""))
            for s in (rag.get("sources") or [])[:3]
        ]
        if parts and cites:
            lines.append(f"- **시장 규모**: {'; '.join(parts)} {refs.tags(cites)}")
        else:
            lines.append("- **시장 규모**: 시장 규모·성장성 근거를 확인하지 못했습니다. (출처 확인 불가)")

        # 시장성 점수 (웹 검색 근거 + LLM 채점)
        groups = ", ".join(
            f"{g['group']} {g['score']:g}/{g['max_score']}" for g in score.get("group_scores", []) if g.get("score") is not None
        )
        cites = [refs.add(e.get("title", "웹 자료"), e["url"]) for e in _pick_score_sources(score)]
        score_text = f"외부 시장성 {score.get('score'):g}/100점 (기준 {MARKET_PASS_SCORE}점). {groups}" if score.get("score") is not None else "외부 시장성 점수를 산출하지 못했습니다."
        lines.append(f"- **시장성 점수**: {score_text} {refs.tags(cites)}" if cites else f"- **시장성 점수**: {score_text} (출처 확인 불가)")

        # 재무 (DART)
        m = _metrics(financial.get("vbm") or {})
        dart = profile.get("dart_viewer_link") or ""
        cites = [refs.add("DART 감사보고서", dart)] if dart else []
        fin_text = (
            f"영업이익률 {_pct(m.get('operating_margin'))}, 순이익률 {_pct(m.get('net_margin'))}, "
            f"부채비율 {m['debt_to_equity']:.2f}배, ROIC–WACC {_pct(m.get('roic_minus_wacc'))}. "
            f"VBM 결론: {(financial.get('vbm') or {}).get('conclusion')}"
            if "debt_to_equity" in m
            else f"VBM 결론: {(financial.get('vbm') or {}).get('conclusion')}"
        )
        lines.append(f"- **재무**: {fin_text} {refs.tags(cites)}" if cites else f"- **재무**: {fin_text} (출처 확인 불가)")

        # 경쟁사 (참고 정보)
        competitors = [
            c for c in research.get("competitors", []) if isinstance(c, dict) and not rules.is_self_competitor(c, profile)
        ]
        if competitors:
            c = competitors[0]
            evidence = _ext(c.get("evidence")).strip().replace("\n", " ")
            if len(evidence) > 140:
                evidence = evidence[:140].rstrip() + "…"
            cites = [refs.add(f"{c.get('name')} 관련 자료", c["source_url"])] if c.get("source_url") else []
            comp_text = f"{c.get('name')} — {evidence}"
            lines.append(f"- **경쟁사**: {comp_text} {refs.tags(cites)}" if cites else f"- **경쟁사**: {comp_text} (출처 확인 불가)")
        else:
            lines.append("- **경쟁사**: 경쟁사 조사 결과를 확인하지 못했습니다. (출처 확인 불가)")

        lines.append(f"- **판단**: {decision['decision']} — {decision['reason']}")
        limits = ["시장성 점수는 웹 검색 근거를 LLM이 채점한 값이라 실행마다 달라질 수 있습니다"]
        if (financial.get("vbm") or {}).get("conclusion") != "긍정":
            limits.append("재무 기준은 완화 조건으로 통과했으며 가치 창출 근거가 충분히 강하지는 않습니다")
        if research.get("selection_method") == "openai_recommendation":
            limits.append("경쟁사는 모델이 제안한 후보로 검색 근거로 확인되지 않았습니다")
        lines.append(f"- **한계·위험**: {'; '.join(limits)}")
        sections.append("\n".join(lines))

    summary_lines = [
        "# 투자 평가 보고서",
        f"run_id(trace_id): {state['run_id']} · 결정·계획·실패 이력은 events.jsonl 참조",
        "",
        "## SUMMARY",
        f"- 평가 대상 {len(active_ids(state)) + len(excluded)}곳 중 적합 {len(suitable)}곳, 부적합 {len(rejected)}곳, 제외 {len(excluded)}곳입니다.",
    ]
    for cid in suitable + rejected:
        d = decisions[cid]
        summary_lines.append(
            f"- {names[cid]} — {d['decision']} (시장성 {d.get('market_score')}점, VBM {d.get('vbm_conclusion')})"
        )
    for cid, reason in excluded.items():
        summary_lines.append(f"- {names.get(cid, cid)} — 제외: {reason}")
    for w in warnings:
        summary_lines.append(f"- 근거 한계: {w}")

    body = ["", "## 기업별 평가", ""] + ("\n\n".join(sections).split("\n") if sections else ["적합 판정 기업이 없습니다."])
    reference = ["", "## REFERENCE"] + [
        f"[{i}] {r['title']}" + (f" — {r['url']}" if r["url"] else f" — {r['publisher'] or '내부 문서'}")
        for i, r in enumerate(refs.items, 1)
    ]
    markdown = "\n".join(summary_lines + body + reference) + "\n"
    feedback = state.get("quality_feedback") or []
    if feedback:  # 직전 품질 평가 미달 사유 중 초안이 스스로 고칠 수 있는 것(표현·인용)을 보정
        markdown = rules.repair_markdown(markdown, feedback)
    ref = ctx.store.put("report", "draft", {"markdown": markdown, "refs": refs.items})
    return {
        "report_uri": ref,
        "report": {"ref": ref, "n_refs": len(refs.items), "n_suitable": len(suitable), "chars": len(markdown)},
        "decision_log": [
            log(state, "report_draft", "drafted", f"초안 작성: 적합 {len(suitable)}곳, 인용 {len(refs.items)}건")
        ],
    }
