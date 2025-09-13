# -*- coding: utf-8 -*-
"""
rich_templates.py
고품질 쿠팡 글 본문 템플릿 전용 모듈.
- 버튼 스타일/위치는 절대 건드리지 않음: affiliate_post.py에서 생성한 button_html 그대로 삽입
- 제품 딕셔너리(이름/특징/장단점/사양 등)가 비어도 자연스러운 폴백 문장 생성
- CSS 클래스는 rt-* 접두사로 격리하여 기존 테마/버튼과 충돌 방지
- min_chars(공백 제외) 이상을 보장
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple
import html, json, re

def _esc(s: Optional[str]) -> str:
    return html.escape((s or "").strip())

def _li(items: List[str]) -> str:
    xs = [i for i in (items or []) if i and str(i).strip()]
    if not xs:
        return ""
    return "<ul>\n" + "\n".join(f"  <li>{_esc(i)}</li>" for i in xs) + "\n</ul>"

def _table(rows: List[List[str]], cls: str = "rt-table") -> str:
    if not rows:
        return ""
    body = "\n".join("  <tr>" + "".join(f"<td>{_esc(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table class='{cls}'>\n{body}\n</table>"

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
.rt-note{background:#eef6ff;border-color:#bfdbfe}
</style>
""".strip()

def _first_n(items: List[str], n: int, fallback: List[str]) -> List[str]:
    xs = [i for i in (items or []) if i and str(i).strip()]
    if len(xs) >= n:
        return xs[:n]
    xs += fallback[: max(0, n - len(xs))]
    return xs[:n]

def _chars_no_space(s: str) -> int:
    return len(re.sub(r"\s+", "", s or ""))

def _pad_paras(title: str) -> List[str]:
    # 의미 있는 일반 조언 문단(SEO에 도움되는 설명형 텍스트)
    return [
        f"{title}를 고를 때는 가용 예산보다 활용 맥락을 먼저 정리하는 게 좋아요. 집의 구조, 전원 배치, 소음 허용치, 보관 동선 같은 현실 제약을 정리하면 스펙 비교가 훨씬 쉬워집니다.",
        "요즘 제품은 기본 성능 자체가 상향 평준화되었기 때문에, 극단적인 최고 성능보다 '나에게 충분한 수준'이 무엇인지 정의하는 과정이 체감 만족도를 크게 좌우합니다.",
        "사후 관리 비용도 꼭 체크하세요. 소모품 단가와 교체 주기, 세척 난도, A/S 접근성은 장기 비용과 사용 빈도에 영향을 줍니다.",
        "리뷰를 볼 때는 단순 별점 평균보다 불만 사례의 패턴을 보세요. 동일한 단점이 반복된다면 사용 환경에 따라 체감이 커질 수 있습니다.",
    ]

def _faq_ldjson(faqs: List[Tuple[str,str]]) -> str:
    if not faqs:
        return ""
    nodes = [{"@type": "Question", "name": q, "acceptedAnswer": {"@type": "Answer", "text": a}} for q,a in faqs]
    data = {"@context":"https://schema.org","@type":"FAQPage","mainEntity":nodes}
    return f"<script type='application/ld+json'>{json.dumps(data, ensure_ascii=False)}</script>"

def build_affiliate_content(
    product: Dict,
    button_html: str,
    disclosure_text: Optional[str] = None,
    min_chars: int = 1500,
    with_faq_ldjson: bool = True,
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
    title = (product.get("title") or "추천 제품").strip()
    features = product.get("features") or []
    pros = product.get("pros") or []
    cons = product.get("cons") or []
    tips = product.get("tips") or []
    criteria = product.get("criteria") or []
    specs = product.get("specs") or []
    faqs = product.get("faqs") or []
    summary = (product.get("summary") or "핵심만 간단히 정리했어요. 선택 기준→팁→장단점 순서로 보여드립니다.").strip()

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

    # (선택) 상단 고지 – affiliate_post에서 이미 출력하면 중복 피하려고 옵션
    if disclosure_text:
        parts.append(f'<div class="rt-section rt-small rt-note">{_esc(disclosure_text)}</div>')

    # 1) 한 눈에 보기 + (버튼 상단 고정 삽입)
    parts.append('<div class="rt-section">')
    parts.append(f'<div class="rt-kicker">한 눈에 보기</div>')
    parts.append(f'<div class="rt-small">{_esc(summary)}</div>')
    parts.append(_li([
        f"대표 포인트: {features[0]}",
        f"서브 포인트: {features[1]}",
        f"추가 포인트: {features[2]}",
    ]))
    parts.append('</div>')
    if button_html:
        parts.append(button_html)  # 버튼 HTML/위치 원형 유지

    # 2) 선택 기준 표
    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">선택 기준 3가지</h2>')
    parts.append(_table(criteria))
    parts.append('</div>')

    # 3) 상세 리뷰(서술형 문단 확장)
    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">상세 리뷰</h2>')
    paras = [
        f"{title}의 첫 인상은 '필요한 기능을 깔끔하게 모았다'는 점이에요. 과한 옵션보다 자주 쓰는 요소를 빠르게 꺼내 쓸 수 있고, 초기 세팅 동선이 단순해서 가족과 함께 공유하기에도 적합합니다.",
        f"성능은 카탈로그 수치보다 체감이 중요합니다. 동일 스펙이라도 공간 크기와 바닥 재질, 환기 정도에 따라 만족도가 달라져요. {title}는 일상 범위에서 요구되는 기본기를 충실히 제공해 '충분함'을 만드는 타입입니다.",
        "가격은 단발 지출이 아니라 기간 대비 가치로 보는 편이 좋아요. 유지비와 소모품 주기를 합산해보면 상위 라인업과의 체감 격차가 줄어드는 구간이 생기는데, 이 지점에서 합리성을 체감하기 쉽습니다."
    ]
    parts.append("<p>" + "</p><p>".join(_esc(p) for p in paras) + "</p>")
    parts.append('</div>')

    # 4) 장점/단점
    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">장점</h2>')
    parts.append(_li(pros))
    parts.append('</div>')

    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">단점</h2>')
    parts.append(_li(cons))
    parts.append('</div>')

    # 5) 이렇게 쓰면 좋아요 (Tip)
    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">이렇게 쓰면 좋아요</h2>')
    parts.append(_li(tips))
    parts.append('</div>')

    # 6) 추천 사용 시나리오
    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">추천 사용 시나리오</h2>')
    parts.append(_li([
        "원룸·소형 공간에서 전력/소음 민감할 때",
        "주말 집중 청소보다 평일 짧은 루틴을 원할 때",
        "초보자·가족 공동 사용 등 간단한 조작을 선호할 때",
    ]))
    parts.append('</div>')

    # 7) 구매 전 체크리스트
    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">구매 전 체크리스트</h2>')
    parts.append(_li([
        "전원/콘센트 동선과 보관 위치 미리 정하기",
        "소모품 단가/교체 주기 확인하기",
        "A/S 거점/택배 수거 정책 확인하기",
    ]))
    parts.append('</div>')

    # 8) 관리/보관 팁
    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">관리/보관 팁</h2>')
    parts.append(_li([
        "사용 후 물기 제거 및 통풍 건조로 수명 연장",
        "부품은 월 1회 기본 점검(이물질/마모 확인)",
        "시즌 종료 시 케이블/배터리 상태 점검 후 보관",
    ]))
    parts.append('</div>')

    # 9) 문제 해결(Q&A 요약)
    faq_pairs = faqs or [
        ("소음이 커진 느낌이에요.", "먼저 필터/브러시 이물질을 점검하세요. 마모가 누적되면 교체 주기를 당기면 효과적입니다."),
        ("유지비가 걱정돼요.", "소모품 단가와 주기를 미리 계산하세요. 정기 세일 시점에 묶음 구매하면 부담이 줄어듭니다."),
    ]
    parts.append('<div class="rt-section">')
    parts.append('<h2 class="rt-h2">문제 해결</h2>')
    for q, a in faq_pairs[:6]:
        parts.append(f"<p><strong>Q.</strong> {_esc(q)}<br/><span class='rt-small'><strong>A.</strong> {_esc(a)}</span></p>")
    parts.append('</div>')

    # 10) 사양 표 (있을 때만)
    if specs:
        parts.append('<div class="rt-section">')
        parts.append('<h2 class="rt-h2">주요 사양</h2>')
        parts.append(_table(specs))
        parts.append('</div>')

    # 하단 CTA 한 번 더 (그대로)
    if button_html:
        parts.append(button_html)

    # FAQPage 구조화 데이터(선택)
    if with_faq_ldjson and faq_pairs:
        parts.append(_faq_ldjson([(q, a) for q, a in faq_pairs[:6]]))

    parts.append('</div>')  # .rt-wrap
    body = "\n".join(parts)

    # === 길이 보정(공백 제외) ===
    if _chars_no_space(body) < min_chars:
        extra_section = ['<div class="rt-section">', '<h2 class="rt-h2">알아두면 좋은 선택 팁</h2>']
        extra_section.append("<p>" + "</p><p>".join(_esc(p) for p in _pad_paras(title)) + "</p>")
        extra_section.append("</div>")
        body += "\n" + "\n".join(extra_section)

    # 그래도 부족하면 안전하게 일반 조언 단락을 반복 추가(2회 한정)
    tries = 0
    while _chars_no_space(body) < min_chars and tries < 2:
        body += "\n<p class='rt-small'>" + _esc(_pad_paras(title)[tries % 4]) + "</p>"
        tries += 1

    return body
