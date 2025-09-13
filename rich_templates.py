# -*- coding: utf-8 -*-
"""
rich_templates.py — 가벼운 기사형 템플릿
- 박스 레이아웃 제거, 자연스러운 h2/h3 스타일
- 버튼은 부모 래퍼 안에서 항상 중앙 정렬
- 섹션 순서(요청안): 내부광고 → 요약 → 버튼 → 본문1(짧게) → 썸네일(옵션)
                 → 버튼 → 내부광고 → 본문2(나머지)
"""

from __future__ import annotations
from typing import Dict, Optional

def _css_block() -> str:
    # .rt-* 네임스페이스: 테마와 충돌 방지
    return """
<style>
.rt { line-height:1.75; letter-spacing:-.01em }
.rt h2{font-size:1.6em;margin:1.2em 0 .4em;font-weight:800;letter-spacing:-.02em}
.rt h3{font-size:1.15em;margin:1.0em 0 .35em;font-weight:700}
.rt .rt-meta{font-size:.925em;color:#64748b;margin:.25em 0 1.0em}
.rt ul{padding-left:1.2em}
.rt .rt-cta{margin:16px 0; text-align:center}
.rt .rt-cta .rt-btn, .rt .aff-cta a, .rt .rt-btn-wrap a{
  display:inline-block; padding:12px 18px; border-radius:10px;
  background:#111; color:#fff; text-decoration:none; font-weight:700;
}
.rt .rt-btn-wrap{justify-content:center !important; gap:12px}
.rt .aff-cta{display:block; text-align:center}
.rt figure.rt-thumb{margin:18px auto; text-align:center}
.rt figure.rt-thumb img{max-width:100%; height:auto; border-radius:12px}
.rt table.rt-table{width:100%; border-collapse:collapse; margin:.5em 0 1em}
.rt table.rt-table th, .rt table.rt-table td{border:1px solid #e5e7eb; padding:.6em .7em}
.rt .rt-kicker{background:#f8fafc; border-left:3px solid #94a3b8; padding:.9em 1em; border-radius:.6rem}
</style>
""".strip()

def _esc(s: Optional[str]) -> str:
    import html
    return html.escape((s or "").strip())

def build_affiliate_content(
    product: Dict,
    button_html: str,
    disclosure_text: Optional[str] = None,
    ad_shortcode: Optional[str] = None,
    thumb_url: Optional[str] = None,
) -> str:
    k = _esc(product.get("title") or "추천 제품")
    disc = _esc(disclosure_text) if disclosure_text else ""
    btn = button_html or ""
    ad = (ad_shortcode or "").strip()

    # 본문1: 짧은 인트로(요약과 분리)
    intro = (
        f"<p>{k}는 ‘필수 기능을 중심으로’ 쓰면 체감 효용이 확 올라갑니다. "
        f"과도한 옵션보다 자주 쓰는 장면에서 필요한 기능을 먼저 고르는 게 핵심이에요.</p>"
    )

    # 본문2: 나머지(자연스러운 서술 + 표/목록)
    body_2 = f"""
<h3>선택 기준 3가지</h3>
<table class="rt-table">
  <thead><tr><th>항목</th><th>확인 포인트</th><th>메모</th></tr></thead>
  <tbody>
    <tr><td>성능</td><td>공간/목적 대비 충분한지</td><td>과투자 방지</td></tr>
    <tr><td>관리</td><td>세척·보관·소모품 주기</td><td>난도/시간</td></tr>
    <tr><td>비용</td><td>구매가 + 유지비</td><td>시즌 특가</td></tr>
  </tbody>
</table>

<h3>상세 리뷰</h3>
<p>{k}의 첫 인상은 ‘필요한 기능을 알찬 구성으로 담았다’는 점입니다. 
초기 세팅이 단순해 가족과 함께 쓰기에도 적합하고, 관리 주기가 명확해 습관화가 쉽습니다. 
자주 쓰는 장면을 2~3개 정한 뒤 거기에 꼭 맞는 기능부터 활성화하면 만족도가 높아요.</p>

<h3>장점</h3>
<ul>
  <li>편의성: 빠른 접근과 직관적 조작</li>
  <li>시간 절약: 꾸준한 사용을 돕는 기본 성능</li>
  <li>확장성: 필요할 때 보조 기능을 단계적으로 추가</li>
</ul>

<h3>단점</h3>
<ul>
  <li>공간/소음/전력 등 환경 제약이 있을 수 있음</li>
  <li>옵션 추가에 따른 관리 난도 상승</li>
</ul>

<h3>추천 사용 시나리오</h3>
<ul>
  <li>짧은 시간에 결과를 내야 할 때 기본 기능만 집중 사용</li>
  <li>주말 정리/리셋 데이에 보조 기능을 묶음으로 실행</li>
</ul>

<h3>구매 체크리스트</h3>
<ul>
  <li>내 환경(공간·소음·예산) 먼저 정의</li>
  <li>필수 기능 → 보조 기능 순서로 결정</li>
  <li>유지관리 주기를 캘린더에 기록</li>
</ul>

<h3>FAQ</h3>
<p><b>Q.</b> 사양이 높을수록 좋은가요?<br>
<b>A.</b> 목적 대비 과사양은 비용·관리 부담이 큽니다. 내 환경에 맞는 균형이 핵심입니다.</p>
""".strip()

    thumb = f"""<figure class="rt-thumb"><img src="{_esc(thumb_url)}" alt="{k} 썸네일"></figure>""" if thumb_url else ""

    parts = [
        _css_block(),
        '<div class="rt">',
        (f'<p class="rt-meta">{disc}</p>' if disc else ''),
        (ad if ad else ''),
        '<h2>요약</h2>',
        f'<div class="rt-kicker"><ul>'
        f'<li>대표 포인트: 핵심 기능</li>'
        f'<li>서브 포인트: 관리 난도</li>'
        f'<li>추가 포인트: 가격대</li>'
        f'</ul></div>',
        f'<div class="rt-cta">{btn}</div>',
        '<h2>한 눈에 보기</h2>',
        intro,
        thumb,
        f'<div class="rt-cta">{btn}</div>',
        (ad if ad else ''),
        body_2,
        '</div>',
    ]
    return "\n".join(p for p in parts if p)
