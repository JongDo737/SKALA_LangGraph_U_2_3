"""보고서 마크다운 → PDF (reportlab, 프로젝트에 포함된 Pretendard 폰트 사용)."""

from __future__ import annotations

import re
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"


def _register_fonts() -> tuple[str, str]:
    try:
        pdfmetrics.registerFont(TTFont("Pretendard", str(FONT_DIR / "Pretendard-Regular.ttf")))
        pdfmetrics.registerFont(TTFont("Pretendard-Bold", str(FONT_DIR / "Pretendard-SemiBold.ttf")))
        return "Pretendard", "Pretendard-Bold"
    except Exception:  # noqa: BLE001 - 폰트가 없으면 기본 폰트로 (한글은 깨질 수 있음)
        return "Helvetica", "Helvetica-Bold"


def _inline(text: str) -> str:
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escape(text))


def render_markdown_pdf(markdown: str, path: Path) -> Path:
    regular, bold = _register_fonts()
    base = ParagraphStyle("base", fontName=regular, fontSize=9.5, leading=14, alignment=TA_LEFT)
    h1 = ParagraphStyle("h1", parent=base, fontName=bold, fontSize=18, leading=24, spaceAfter=6)
    h2 = ParagraphStyle("h2", parent=base, fontName=bold, fontSize=13, leading=18, spaceBefore=10, spaceAfter=4)
    h3 = ParagraphStyle("h3", parent=base, fontName=bold, fontSize=11, leading=16, spaceBefore=8, spaceAfter=2)
    bullet = ParagraphStyle("bullet", parent=base, leftIndent=10, bulletIndent=0)

    story = []
    for line in markdown.split("\n"):
        if not line.strip():
            story.append(Spacer(1, 3))
        elif line.startswith("# "):
            story.append(Paragraph(_inline(line[2:]), h1))
        elif line.startswith("## "):
            story.append(Paragraph(_inline(line[3:]), h2))
        elif line.startswith("### "):
            story.append(Paragraph(_inline(line[4:]), h3))
        elif line.startswith("- "):
            story.append(Paragraph(_inline(line[2:]), bullet, bulletText="•"))
        else:
            story.append(Paragraph(_inline(line), base))
    path.parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(
        str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
        title="투자 평가 보고서",
    ).build(story)
    return path
