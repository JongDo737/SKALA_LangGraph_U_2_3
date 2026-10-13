"""사용자 실행 로그에서 발견한 문제의 회귀 테스트:
PDF 다운로드가 연결 끊김으로 실패한 기업(재무 0/14)이 목표 수 자리를 차지해 평가 기업이 1곳만 남았던 문제."""
import io
import urllib.error

import pytest

import agents.dart as dart
from orchestrator import rules
from orchestrator.settings import Settings
from orchestrator.store import Store


# ── 1) PDF 다운로드 재시도 ─────────────────────────────────────────────────────────────
class _Resp:
    def __init__(self, data):
        self._d = data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._d


def test_pdf_download_retries_connection_reset(monkeypatch, tmp_path):
    calls = {"n": 0}

    def flaky(req, timeout=0):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise urllib.error.URLError(ConnectionResetError(54, "Connection reset by peer"))
        return _Resp(b"%PDF-1.4 fake")

    monkeypatch.setattr(dart.urllib.request, "urlopen", flaky)
    monkeypatch.setattr(dart, "PDF_DOWNLOAD_BACKOFF", 0.0)
    path = dart.download_dart_pdf("https://dart.fss.or.kr/x?rcpNo=2026&dcmNo=99", "99", output_dir=str(tmp_path))
    assert calls["n"] == 3 and open(path, "rb").read().startswith(b"%PDF")


def test_pdf_download_reuses_existing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(dart, "PDF_DOWNLOAD_BACKOFF", 0.0)
    monkeypatch.setattr(dart.urllib.request, "urlopen", lambda *a, **k: _Resp(b"%PDF-1.4 first"))
    first = dart.download_dart_pdf("https://dart.fss.or.kr/x?rcpNo=2026&dcmNo=99", "99", output_dir=str(tmp_path))

    def must_not_be_called(*a, **k):
        raise AssertionError("이미 받은 파일을 다시 내려받음")

    monkeypatch.setattr(dart.urllib.request, "urlopen", must_not_be_called)
    assert dart.download_dart_pdf("https://dart.fss.or.kr/x?rcpNo=2026&dcmNo=99", "99", output_dir=str(tmp_path)) == first


def test_pdf_download_gives_up_with_error(monkeypatch, tmp_path):
    monkeypatch.setattr(dart, "PDF_DOWNLOAD_BACKOFF", 0.0)
    monkeypatch.setattr(dart, "PDF_DOWNLOAD_RETRIES", 2)

    def always(req, timeout=0):
        raise urllib.error.URLError("reset")

    monkeypatch.setattr(dart.urllib.request, "urlopen", always)
    with pytest.raises(urllib.error.URLError):
        dart.download_dart_pdf("https://dart.fss.or.kr/x?rcpNo=2026&dcmNo=98", "98", output_dir=str(tmp_path))


# ── 2) 후보 선택: 재무가 있는 기업 우선 ────────────────────────────────────────────────────
def _base(cid, filled):
    keys = ["자산총계", "부채총계", "자본총계", "매출액", "영업이익"]
    return {
        "id": cid, "name": f"기업{cid}", "company_name": f"기업{cid}",
        "financial_summary": {k: ("1,000" if filled else None) for k in keys},
        "financials": [],
    }


def test_has_financial_data():
    assert rules.has_financial_data(_base("a", True))
    assert not rules.has_financial_data(_base("b", False))
    assert rules.has_financial_data({"financial_summary": {}, "financials": [{"year": "2024", "매출액": "5"}]})
    assert not rules.has_financial_data({"financial_summary": {}, "financials": [{"year": "2024", "매출액": None}]})


def test_startup_screen_prefers_candidates_with_financial_data(monkeypatch, tmp_path):
    """로그 재현: 앞의 4곳은 PDF 다운로드 실패로 재무 없음 → 목표 수만큼 자르면 재무 있는 후보가 밀려났다."""
    from orchestrator.nodes.startup_screen import startup_screen

    bases = [_base(f"x{i}", False) for i in range(4)] + [_base(f"ok{i}", True) for i in range(6)]
    store = Store(tmp_path, "t")
    state = {
        "run_id": "t", "output_dir": str(tmp_path),
        "settings": Settings.from_dict({**Settings().to_dict(), "target_company_count": 5}).to_dict(),
        "candidate_ids": [b["id"] for b in bases],
        "companies": {b["id"]: {"name": b["name"], "base_ref": store.put("base", b["id"], b)} for b in bases},
    }

    class Stub:
        def startup_screen(self, companies):
            return companies, []  # 전원 통과

    monkeypatch.setattr("orchestrator.common.get_services", lambda: Stub())
    out = startup_screen(state)
    assert len(out["company_ids"]) == 5
    assert all(cid.startswith("ok") for cid in out["company_ids"])  # 재무 없는 x* 는 뒤로 밀림


# ── 3) 재무 부족은 재계획하지 않음 ─────────────────────────────────────────────────────────
def test_financial_coverage_failure_does_not_consume_replans():
    from orchestrator.nodes.record_failure import record_failure

    base = {"run_id": "t", "settings": Settings().to_dict(), "replan_count": 0, "stage": "sufficiency", "excluded": {}}
    only_fin = record_failure({**base, "failures": [
        {"kind": "financial", "company_id": "c1", "code": "fin_coverage_low"},
    ]})
    assert only_fin["retry_possible"] is False  # 다시 계산해도 같은 결과 → 바로 fallback(제외)
    mixed = record_failure({**base, "failures": [
        {"kind": "financial", "company_id": "c1", "code": "fin_coverage_low"},
        {"kind": "competitor", "company_id": "c2", "code": "competitor_ungrounded"},
    ]})
    assert mixed["retry_possible"] is True


def test_planner_skips_hopeless_financial_and_excludes_empty_companies(tmp_path):
    from orchestrator.nodes.planner import planner

    store = Store(tmp_path, "t")
    empty, full = _base("e", False), _base("f", True)
    state = {
        "run_id": "t", "output_dir": str(tmp_path), "settings": Settings().to_dict(), "replan_count": 0,
        "company_ids": ["e", "f"],
        "companies": {c["id"]: {"name": c["name"], "profile_ref": store.put("profile", c["id"], c)} for c in (empty, full)},
    }
    out = planner(state)
    assert "e" in out["excluded"] and all(t["company_id"] == "f" for t in out["subtasks"])  # 재무 0인 기업은 워커 비용 전에 제외

    replan = planner({**state, "replan_count": 0, "plan_requested": True, "company_ids": ["f"], "failures": [
        {"kind": "financial", "company_id": "f", "code": "fin_coverage_low", "reason": "x"},
        {"kind": "competitor", "company_id": "f", "code": "competitor_ungrounded", "reason": "y"},
    ]})
    assert {t["kind"] for t in replan["subtasks"]} == {"competitor"}  # financial 은 재계획에서 제외


def test_async_calls_share_one_event_loop():
    """DART·스크리닝을 두 번 호출해도 같은 이벤트 루프를 써서 'Event loop is closed' 가 나지 않는다 (보충 검색 실제 실행에서 발생)."""
    import asyncio

    from orchestrator.services import run_async

    async def loop_id():
        return id(asyncio.get_running_loop())

    async def boom():
        raise ValueError("그대로 전달")

    assert run_async(loop_id()) == run_async(loop_id())
    with pytest.raises(ValueError, match="그대로 전달"):
        run_async(boom())


def test_startup_screen_ranks_by_computable_coverage(tmp_path, monkeypatch):
    """재무 키가 많아도 계산 가능 지표가 기준 미만이면 뒤로 밀려, 평가 가능한 기업이 목표 수를 채운다."""
    from orchestrator.nodes.startup_screen import startup_screen

    bases = [{"id": f"c{i}", "name": f"기업{i}", "financial_summary": {"revenue": 1}} for i in range(7)]
    coverage = {"c0": 0.33, "c1": 0.33, "c2": 0.9, "c3": 0.5, "c4": 0.8, "c5": 0.7, "c6": 0.6}
    monkeypatch.setattr("orchestrator.nodes.startup_screen.fin_coverage", lambda b: coverage[b["id"]])
    store = Store(tmp_path, "t")
    state = {
        "run_id": "t", "output_dir": str(tmp_path),
        "settings": Settings.from_dict({**Settings().to_dict(), "target_company_count": 5}).to_dict(),
        "candidate_ids": [b["id"] for b in bases],
        "companies": {b["id"]: {"name": b["name"], "base_ref": store.put("base", b["id"], b)} for b in bases},
    }

    class Stub:
        def startup_screen(self, companies):
            return companies, []

    monkeypatch.setattr("orchestrator.common.get_services", lambda: Stub())
    out = startup_screen(state)
    assert out["company_ids"] == ["c2", "c3", "c4", "c5", "c6"]  # 계산 가능 비율이 낮은 c0, c1 은 제외 대상 순위
    assert rules.fin_coverage({"name": "x"}) == 0.0  # 계산 불가 데이터도 예외 없이 순위 뒤로
