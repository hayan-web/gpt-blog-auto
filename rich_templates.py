# -*- coding: utf-8 -*-
"""
rich_templates.py
- 박스/카드 제거, 일반 기사형 마크업
- 섹션 순서: 1) 내부광고 2) 요약 3) 버튼 4) 본문1(짧게) 5) 썸네일(주석만) 6) 버튼 7) 내부광고 8) 본문2(상세)
- 버튼은 전달된 HTML을 그대로 사용하고, 외곽에 'rt-center' 래퍼로만 중앙 정렬
"""

from __future__ import annotations
from typing import Dict, Optional

def _center_wrap(html: str) -> str:
    # 버튼 앵커는 그대로 두고, 바깥 래퍼만 텍스트 정렬로 중앙 배치
    return f'<div class="rt-center" style="text-align:center;margin:16px 0">{html}</div>'

def _h2(txt: str) -> str:
    return f"<h2>{txt}</h2>"

def _h3(txt: str) -> str:
    return f"<h3>{txt}</h3>"

def build_affiliate_content(
    product: Dict,
    button_html: str,
    disclosure_text: Optional[str] = None,
    ad_shortcode: Optional[str] = None,
) -> str:
    title = (product.get("title") or "추천 제품").strip()
    summary = (product.get("summary") or f"{title}를 선택할 때 성능·관리·비용의 균형을 빠르게 점검할 수 있도록 핵심만 정리했습니다.").strip()

    # 1) 내부광고
    sec1 = ad_shortcode or ""

    # 2) 요약 (일반 기사 스타일)
    sec2 = (
        _h2("요약")
        + f"<p>{summary}</p>"
        + "<ul>"
          "<li>대표 포인트: 핵심 기능</li>"
          "<li>서브 포인트: 관리 난도</li>"
          "<li>추가 포인트: 가격대</li>"
          "</ul>"
    )

    # 3) 버튼 (중앙 래퍼만 적용)
    sec3 = _center_wrap(button_html)

    # 4) 본문1 (짧게)
    sec4 = (
        _h2("핵심 한 단락")
        + "<p>첫 인상은 ‘필요한 기능이 깔끔히 모여 있다’입니다. 과한 옵션보다 "
          "자주 쓰는 요소를 빠르게 꺼내 쓸 수 있는지가 중요합니다. 초기 세팅은 간단해야 하며, "
          "가정/사무 환경 어디에 두어도 동선이 깨지지 않는 크기인지 살펴보면 선택이 쉬워집니다.</p>"
    )

    # 5) 썸네일(추후) — 현재는 자리표시 주석만
    sec5 = "<!-- thumbnail: later -->"

    # 6) 버튼(반복, 중앙)
    sec6 = _center_wrap(button_html)

    # 7) 내부광고(반복)
    sec7 = ad_shortcode or ""

    # 8) 본문2(상세) — 표/소제목 포함, 박스 없음
    table = (
        "<table>"
        "<thead><tr><th>항목</th><th>확인 포인트</th><th>비고</th></tr></thead>"
        "<tbody>"
        "<tr><td>성능</td><td>공간/목적 대비 충분한지</td><td>과투자 방지</td></tr>"
        "<tr><td>관리</td><td>세척·보관·소모품</td><td>난도/주기</td></tr>"
        "<tr><td>비용</td><td>구매가 + 유지비</td><td>시즌 특가</td></tr>"
        "</tbody></table>"
    )

    pros = product.get("pros") or ["간편한 접근성", "빠른 사용 준비", "확장성"]
    cons = product.get("cons") or ["상위급 대비 성능 격차", "소모품/배터리 주기"]

    sec8 = (
        _h2("선택 기준 3가지")
        + table
        + _h2("상세 리뷰")
        + "<p>성능은 카탈로그 수치보다 체감이 중요합니다. 동일 스펙이라도 공간 크기와 바닥 재질, "
          "환기 정도에 따라 만족도가 갈라집니다. 활용 범위를 구체화해 ‘주 사용 시나리오’를 먼저 적어보고, "
          "거기에 꼭 맞는 기능만 우선순위를 매기면 시행착오를 크게 줄일 수 있습니다.</p>"
        + _h3("장점")
        + "<ul>" + "".join(f"<li>{p}</li>" for p in pros) + "</ul>"
        + _h3("단점")
        + "<ul>" + "".join(f"<li>{c}</li>" for c in cons) + "</ul>"
        + _h3("추천 사용 시나리오")
        + "<ul>"
          "<li>좁은 공간에서 빠른 정리/세팅이 필요한 경우</li>"
          "<li>가족과 공유하며 누구나 쉽게 써야 하는 경우</li>"
          "<li>소음·관리 난도를 일정 수준 이하로 유지해야 하는 경우</li>"
          "</ul>"
        + _h3("구매 전 체크리스트")
        + "<ul>"
          "<li>내 공간/용도 명확화</li>"
          "<li>관리 주기(세척·교체) 확인</li>"
          "<li>총비용(구매+유지) 계산</li>"
          "</ul>"
        + _h3("FAQ")
        + "<p><b>Q.</b> 사양이 높을수록 좋은가요?<br>"
          "<b>A.</b> 목적 대비 과사양이면 비용·관리 부담만 커질 수 있습니다. "
          "핵심 사용 장면에 맞춘 균형이 핵심입니다.</p>"
    )

    # 전체 조립 (박스/카드 없이 플랫)
    parts = [sec1, sec2, sec3, sec4, sec5, sec6, sec7, sec8]
    return "\n\n".join([p for p in parts if p])
