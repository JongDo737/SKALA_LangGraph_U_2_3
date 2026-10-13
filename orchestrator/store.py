"""대용량 페이로드 저장소.

State(체크포인트)에는 ref 만 넣고, 기업 프로필·워커 결과 원본·보고서 본문은 여기에 파일로 둡니다.
경로: {output_dir}/runs/{run_id}/{namespace}/{key}.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


def _safe(key: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", str(key))


class Store:
    def __init__(self, output_dir: str | Path, run_id: str) -> None:
        self.base = Path(output_dir)
        self.run_id = run_id
        self.root = self.base / "runs" / run_id

    def put(self, namespace: str, key: str, obj: Any) -> str:
        path = self.root / _safe(namespace) / f"{_safe(key)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        return str(path.relative_to(self.base))

    def get(self, ref: str) -> Any:
        return json.loads((self.base / ref).read_text(encoding="utf-8"))

    def write_text(self, name: str, text: str) -> Path:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path
