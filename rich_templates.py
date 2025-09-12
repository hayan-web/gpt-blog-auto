# -*- coding: utf-8 -*-
"""
rich_templates.py
고품질 쿠팡 글 본문 템플릿 전용 모듈.
- 절대 버튼 스타일/위치는 건드리지 않음: affiliate_post.py에서 생성한 button_html 그대로 삽입
- 제품 딕셔너리(이름/특징/장단점/사양 등)가 비어도 자연스러운 폴백 문장 생성
- CSS 클래스는 rt-* 접두사로 격리하여 기존 테마/버튼과 충돌 방지
"""

from __future__ import annotations
from typing import Dict, List, Optional
import html

def _esc(s: Optional[str]) -> str:
    return html.escape((s or "").strip())

def _li(items: List[str]) -> str:
    items = [i for i in (items or []) if i and i.strip()]
    if not items:
        return ""
    return "<ul>\n" + "\n".join(f"  <li>{_esc(i)}</li>" for i in items) + "\n</ul>"

def _table(rows: List[List[str]]) -> str:
    if not rows: 
        return ""
    body = "\n".join(
        "  <tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>"
        for r in rows
    )
    return f"<table class='rt-table'>\n{body}\n</table>"

def _css() -> str:
    return """
<style>
.rt-wrap{line-height:1.8}
.rt-section{margin:18px 0;padding:16px;border:1px solid #e5e7eb;border-radius:12px;background:#fafafa}
.rt-h2{margin:0 0 8px;font-size:1.2rem;color:#334155}
.rt-small{font-size:.95rem;color:#64748b}
.rt-hr{border:0;border-top:1px solid #e5e7eb;margin:18px 0}
.rt-table{width:100%;border-collapse:collapse;margin:12px 0}
.rt-table td{border:1px solid #e5e7eb;padding:10px;vertical-align:top}
.rt-kicker{display:inline-block;font-weight:800;color:#0ea5e9;margin-bottom:6px}
</style>
""".strip()

def _first_n(items: List[str], n: int, fallback: List[str]) -> List[str]:
    xs = [i for i in (items or []) if i and i.strip()]
    if len(xs) >= n:
        return xs[:n]
    xs += fallback[: max(0, n - len(xs))]
    return xs[:n]

def build_affiliate_content(
    product: Dict,
    button_html: str,
    disclosure_text: Optional[str] = None,
) -> str:
    """
    product 예시:
    {
      "title": "저전력 물걸레 청소기",
      "features": ["저전력", "간편 관리", "합리적 가격"],
      "pros": ["접근성 좋음", "유지비 부담 적음"],
      "cons": ["상위급 대비 성능 한계", "소모품 주기 필요"],
      "tips": ["가볍게 시작 후 필요 시 업그레이드"],
      "criteria": [["성능","공간/목적 대비 충분한지","과투자 방지"],
                   ["관리","세척·보관·소모품","난도/주기"],
                   ["비용","구매가+유지비","시즌 특가"]],
      "specs": [["소비전력","120W"],["무게","2.3kg"],["모드","물걸레·건식"]],
      "faqs": [["배터리 교체 주기?","일반적으로 2~3년, 사용량에 따라 다릅니다."]],
      "summary": "핵심만 간단히 정리했어요. 선택 기준→팁→장단점 순서로 보여드립니다."
    }
    """
    title = product.get("title") or "추천 제품"
    features = product.get("features") or []
    pros = product.get("pros") or []
    cons = product.get("cons") or []
    tips = product.get("tips") or []
    criteria = product.get("criteria") or []
    specs = product.get("specs") or []
    faqs = product.get("faqs") or []
    summary = product.get("summary") or "핵심만 간단히 정리했어요. 선택 기준→팁→장단점 순서로 보여드립니다."

    # 폴백 데이터
    features_fb = ["핵심 기능", "관리 난도", "가격대"]
    pros_fb = ["간편한 접근성", "부담 없는 유지비", "상황별 확장성"]
    cons_fb = ["상위급 대비 성능 격차", "소모품/배터리 주기"]
    tips_fb = ["가볍게 시작하고 필요하면 업그레이드", "사용 환경에 맞는 모드만 먼저 활용"]
    criteria_fb = [
        ["성능", "공간/목적 대비 충분한지", "과투자 방지"],
        ["관리", "세척·보관·소모품", "난도/주기"],
        ["비용", "구매가 + 유지비", "시즌 특가"],
    ]

    features = _first_n(features, 3, features_fb)
    pros = pros or pros_fb
    cons = cons or cons_fb
    tips = tips or tips_fb
    criteria = criteria or criteria_fb

    parts: List[str] = []
    parts.append(_css())
    parts.append('<div class="rt-wrap">')

    # (선택) 상단 고지 – affiliate_post에서 이미 출력하면 중복 피하려고 옵션으로만
    if disclosure_text:
        parts.append(
            f'<div class="rt-section rt-small">{_esc(disclosure_text)}</div>'
        )

    # 1) 한 눈에 보기 + (버튼 상단 고정 삽입) — 버튼 HTML 원형 그대로
    parts.append('<div class="rt-section">')
    parts.append(f'<div class="rt-kicker">한 눈에 보기</div>')
    parts.append(f'<div class="rt-small">{_esc(summary)}</div>')
    parts.append(_li([f"대표 포인트: {features[0]}", f"서브 포인트: {features[1]}", f"추가 포인트: {features[2]}"]))
    parts.append('</div>')
    if button_html:
        parts.append(button_html)  # 스타일/위치 변경 없음

    # 2) 선택 기준 표
    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">선택 기준 3가지</h2>')
    parts.append(_table(criteria))
    parts.append('</div>')

    # 3) 장점/단점
    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">장점</h2>')
    parts.append(_li(pros))
    parts.append('</div>')

    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">단점</h2>')
    parts.append(_li(cons))
    parts.append('</div>')

    # 4) 이렇게 쓰면 좋아요 (Tip)
    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">이렇게 쓰면 좋아요</h2>')
    parts.append(_li(tips))
    parts.append('</div>')

    # 5) 사양 표 (있을 때만)
    if specs:
        parts.append('<div class="rt-section">')
        parts.append('<h2 class="rt-h2">주요 사양</h2>')
        parts.append(_table(specs))
        parts.append('</div>')

    # 6) FAQ (있을 때만)
    if faqs:
        parts.append('<div class="rt-section">')
        parts.append('<h2 class="rt-h2">FAQ</h2>')
        for q, a in faqs:
            parts.append(f"<p><strong>Q.</strong> {_esc(q)}<br/><span class='rt-small'><strong>A.</strong> {_esc(a)}</span></p>")
        parts.append('</div>')

    # 하단 CTA 한 번 더 (그대로)
    if button_html:
        parts.append(button_html)

    parts.append('</div>')  # .rt-wrap
    return "\n".join(parts)
