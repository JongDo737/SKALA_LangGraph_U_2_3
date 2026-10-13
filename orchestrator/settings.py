"""조정 계층의 모든 임계값·상한을 한 곳에서 관리합니다. 환경변수 ORCH_* 로 덮어쓸 수 있습니다."""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, fields


def _env(name: str, default, cast):
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    if isinstance(default, bool):  # bool("false") 는 True 이므로 별도 파싱
        return raw.strip().lower() in ("1", "true", "yes", "on")
    return cast(raw)


@dataclass(frozen=True)
class Settings:
    # ── 종료 보장 ──────────────────────────────────────────────
    # 재계획(재작업) 루프 상한. 검증·근거 충분성·품질 평가 어느 단계에서 실패하든 같은 예산을 씁니다.
    max_replans: int = 2
    # 노드 실행 횟수 가드(step_count). 넘으면 더 이상 재계획하지 않고 fallback 으로 마무리합니다.
    max_steps: int = 300

    # 적합 기업이 목표 수에 못 미칠 때 DART 검색부터 다시 해서 부족한 만큼만 채우는 보충 횟수 상한 (재계획 예산과 별개)
    max_topups: int = 1

    # ── 대상 ──────────────────────────────────────────────────
    target_company_count: int = 5
    # DART 후보를 목표 수의 몇 배로 조회할지 (자산 500억 초과 등 탈락 대비). 기존 파이프라인(config.DART_FETCH_MULTIPLIER)과 같은 4배.
    # 클수록 PDF 파싱·스크리닝 시간이 늘어납니다.
    dart_fetch_multiplier: int = 4

    # ── 근거 충분성 기준 ────────────────────────────────────────
    min_profile_sources: int = 1  # 프로필(웹검색) 출처 수
    min_profile_description_chars: int = 20
    min_fin_coverage: float = 0.4  # VBM 지표 중 계산 가능한 비율 (judge의 '보류' 기준과 동일)
    min_market_sources: int = 2  # 시장 조사(RAG/웹) 출처 수
    min_score_domains: int = 8  # 시장성 채점 근거의 서로 다른 도메인 수
    max_lowquality_share: float = 0.5  # 유튜브·블로그 등 근거 비중 상한
    min_competitors: int = 1
    require_competitor_url: bool = True  # 경쟁사는 검색으로 확인된 출처(URL)가 있어야 함

    # ── 보고서 ────────────────────────────────────────────────
    # 기존 보고서 생성기(agents/report_prep.py)의 LLM 서술 보강 사용 여부.
    # 끄면 수집한 근거만으로 서술해 출처 추적성(Groundedness)이 높고, 켜면 서술은 풍부하지만 근거 없는 문장이 섞일 수 있습니다.
    report_use_llm: bool = False

    # ── 보고서 품질 평가 기준 ───────────────────────────────────
    min_domains_per_company: int = 2  # 기업별 인용 출처 도메인 수 (편향 통제)
    max_top_domain_share: float = 0.7  # 보고서 전체에서 한 도메인이 차지할 수 있는 인용 비중
    max_cited_lowquality_share: float = 0.5

    @classmethod
    def from_env(cls) -> "Settings":
        values = {}
        for f in fields(cls):
            values[f.name] = _env(f"ORCH_{f.name.upper()}", f.default, type(f.default))
        return cls(**values)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
