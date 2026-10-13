"""[노드] 공통 기업 프로필 준비(웹검색). (기능: 사업 설명·분야·출처 확정)

처음에는 모든 기업을, 플래너가 '프로필 추가 보완'을 요청하면 지정된 기업만 다시 만듭니다.
여기서 확정한 분야(topic/subdomain)를 이후 모든 워커가 그대로 쓰므로, 재실행해도 검색 질의가 바뀌지 않습니다.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from orchestrator import rules
from orchestrator.common import active_ids, get_ctx, log


def profile_prep(state: dict[str, Any]) -> dict[str, Any]:
    ctx = get_ctx(state)
    targets = list(state.get("profile_targets") or [])
    refining = bool(targets)
    if not refining:
        # 보충 검색으로 들어온 기업이 있으면 그 기업만 만듭니다. (기존 기업의 프로필은 그대로)
        targets = [cid for cid in state.get("topup_ids") or [] if cid in active_ids(state)] or active_ids(state)
    round_no = ctx.round

    def build(cid: str) -> tuple[str, dict[str, Any]]:
        base = ctx.store.get(state["companies"][cid]["base_ref"])
        directive = {"why": "프로필 보완"} if refining else {}
        return cid, ctx.services.profile(base, round_no, directive)

    with ThreadPoolExecutor(max_workers=max(1, len(targets))) as pool:  # 기업 수만큼 한 번에
        built = list(pool.map(build, targets))

    companies: dict[str, Any] = {}
    for cid, profile in built:
        companies[cid] = {
            "profile_ref": ctx.store.put("profile", cid, profile),
            "profile_summary": rules.summarize_profile(profile),
        }
    update: dict[str, Any] = {
        "companies": companies,
        "profile_targets": [],
        "decision_log": [
            log(
                state,
                "profile_prep",
                "refined" if refining else "prepared",
                f"프로필 {len(built)}곳 {'보완' if refining else '준비'} (웹검색)",
                company_ids=targets,
            )
        ],
    }
    if refining:
        update["profile_refined"] = {cid: round_no for cid in targets}
    return update
