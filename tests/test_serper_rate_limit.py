"""Serper 초당 5회 제한(429) 대응 확인: 요청 간격 제한 + 429 재시도."""
import io
import json
import threading
import time
from urllib.error import HTTPError

import agents.compitition as comp


class _Resp:
    def __init__(self, body: dict):
        self._b = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._b


def _client(monkeypatch):
    monkeypatch.setattr(comp, "SERPER_RETRY_BACKOFF", 0.0)
    return comp.SerperSearchClient("test-key")


def test_429_is_retried_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(request, timeout=0):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b'{"message":"Rate limit"}'))
        return _Resp({"organic": [{"title": "t", "snippet": "s", "link": "http://x.example"}]})

    monkeypatch.setattr(comp, "urlopen", fake_urlopen)
    monkeypatch.setattr(comp, "SERPER_MIN_INTERVAL", 0.0)
    results = _client(monkeypatch).search("q", 3)
    assert calls["n"] == 3 and len(results) == 1 and results[0].url == "http://x.example"


def test_gives_up_after_max_retries_without_crashing(monkeypatch):
    def always_429(request, timeout=0):
        raise HTTPError("u", 429, "Too Many Requests", {}, io.BytesIO(b"{}"))

    monkeypatch.setattr(comp, "urlopen", always_429)
    monkeypatch.setattr(comp, "SERPER_MIN_INTERVAL", 0.0)
    monkeypatch.setattr(comp, "SERPER_MAX_RETRIES", 3)
    assert _client(monkeypatch).search("q", 3) == []


def test_request_starts_are_spaced_across_threads(monkeypatch):
    """여러 워커 스레드가 동시에 검색해도 요청 시작 간격이 최소 간격 이상이다."""
    starts = []
    lock = threading.Lock()

    def fake_urlopen(request, timeout=0):
        with lock:
            starts.append(time.monotonic())
        return _Resp({"organic": []})

    monkeypatch.setattr(comp, "urlopen", fake_urlopen)
    monkeypatch.setattr(comp, "SERPER_MIN_INTERVAL", 0.05)
    client = _client(monkeypatch)
    threads = [threading.Thread(target=client.search, args=(f"q{i}", 1)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(starts) == 12 and min(gaps) >= 0.045  # 초당 20회 → 5 req/s 한도를 안정적으로 지키는 간격
