"""프롬프트 파일 로더입니다. prompts/ 아래 텍스트를 읽어 에이전트에 주입합니다."""

from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    """``prompts/{name}`` 파일을 읽어 반환합니다. 확장자가 없으면 ``.txt``를 붙입니다."""

    filename = name if "." in Path(name).name else f"{name}.txt"
    path = PROMPTS_DIR / filename
    if not path.is_file():
        raise FileNotFoundError(f"프롬프트 파일을 찾을 수 없습니다: {path}")
    return path.read_text(encoding="utf-8").strip()


def format_prompt(prompt_name: str, **kwargs: object) -> str:
    """프롬프트를 읽고 ``str.format``으로 변수를 채웁니다."""

    return load_prompt(prompt_name).format(**kwargs)
