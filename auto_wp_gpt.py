# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상글 2건(10시/17시 KST) 예약 발행
- 기사형 섹션: 요약 → 하이라이트 → 배운 점 → 실행 계획 → 지표/체크리스트 → 회고
- 내부 광고: 상단(AD_SHORTCODE), 중간/하단(AD_INSERT_MIDDLE, 없으면 상단과 동일)
- 소제목/본문에 얇은 CSS(.dx 네임스페이스), 박스 감싸기 없음
- 1500자 보강: 자연스러운 섹션으로만 보강, 중복 금지, 최대 3블록
"""

from __future__ import annotations
import os, json, re, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, Dict
import requests
from dotenv import load_dotenv

load_dotenv()

# ===== ENV =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
VERIFY_TLS=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"

POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()
DEFAULT_CATEGORY=(os.getenv("DEFAULT_CATEGORY") or "정보").strip() or "정보"

# 내부 광고(숏코드 등)
AD_SHORTCODE=(os.getenv("AD_SHORTCODE") or "").strip()            # 상단
AD_INSERT_MIDDLE=(os.getenv("AD_INSERT_MIDDLE") or "").strip()    # 중간/하단 (없으면 상단 재사용)

REQ_HEADERS={
    "User-Agent": os.getenv("USER_AGENT") or "gpt-blog-auto/diary-2.1",
    "Accept":"application/json",
    "Content-Type":"application/json; charset=utf-8"
}

# ===== 스타일 (박스 X, 소제목만 강조) =====
def _css()->str:
    return """
<style>
.dx { line-height:1.85; letter-spacing:-.01em }
.dx h2{font-size:1.6em;margin:1.15em 0 .5em;font-weight:800;letter-spacing:-.02em}
.dx h3{font-size:1.15em;margin:1.0em 0 .4em;font-weight:700}
.dx p{margin:.6em 0}
.dx ul{padding-left:1.2em;margin:.4em 0}
.dx .lead{font-size:1.05em;font-weight:500}
.dx .muted{color:#64748b}
</style>
""".strip()

# ===== 유틸 =====
def _esc(s: Optional[str])->str:
    return html.escape((s or "").strip())

def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")

def _nchars(x: str) -> int:
    return len(re.sub(r"\s+","",_strip_tags(x)))

def _ensure_min_chars(body: str, min_chars: int = 1500) -> str:
    """자연스러운 보강만 추가(최대 3블록, 중복 금지)."""
    if _nchars(body) >= min_chars:
        return body
    fillers = [
        "<h3>작은 한 걸음</h3><p>완벽보다 빈도가 중요합니다. 측정 가능한 지표를 한 줄로 정리하고, 다음 행동을 캘린더에 바로 배치하세요.</p>",
        "<h3>되돌아보기</h3><p>오늘의 결정이 1주 후에도 같은 결정을 돕는가를 확인합니다. 재사용 가능한 문장을 한 줄 남기면 다음 실행이 쉬워집니다.</p>",
        "<h3>방해요소 차단</h3><p>내일 아침 30분만 환경을 단순화해보세요. 알림/탭/물건을 치우고 핵심 도구만 남기면 집중이 쉬워집니다.</p>",
        "<h3>루틴 설계</h3><p>시작 트리거→행동→보상 흐름으로 기록하면 재현성이 올라갑니다. 주 3회 유지 가능한 강도로 설계하세요.</p>",
        "<h3>협업 메모</h3><p>요청 사항은 ‘배경-기대결과-마감’ 3줄로 정리합니다. 다음에 나 스스로에게 보낼 메모라고 생각하면 간결해집니다.</p>",
    ]
    used = set()
    buf = body
    for add in fillers:
        if _nchars(buf) >= min_chars: break
        if add not in buf and add not in used:
            buf += "\n" + add
            used.add(add)
            if len(used) >= 3:  # 최대 3블록
                break
    return buf

# ===== WordPress =====
def _ensure_term(kind:str, name:str)->int:
    r=requests.get(
        f"{WP_URL}/wp-json/wp/v2/{kind}",
        params={"search":name,"per_page":50,"context":"edit"},
        auth=(WP_USER,WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=15, headers=REQ_HEADERS
    )
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name:
            return int(it["id"])
    r=requests.post(
        f"{WP_URL}/wp-json/wp/v2/{kind}",
        json={"name":name},
        auth=(WP_USER,WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=15, headers=REQ_HEADERS
    )
    r.raise_for_status()
    return int(r.json()["id"])

def _post_wp(title:str, content:str, when_gmt:str, category:str)->dict:
    cat_id=_ensure_term("categories", category or DEFAULT_CATEGORY)
    payload={
        "title": title,
        "content": content,
        "status": POST_STATUS,
        "categories": [cat_id],
        "comment_status": "closed",
        "ping_status": "closed",
        "date_gmt": when_gmt
    }
    r=requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        json=payload,
        auth=(WP_USER,WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=20, headers=REQ_HEADERS
    )
    r.raise_for_status()
    return r.json()

# ===== 시간대 =====
def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

def _slot_to_utc(hour:int)->str:
    now=_now_kst()
    tgt=now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if tgt <= now:
        tgt += timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ===== 본문 빌드 =====
def _build_diary_html(title: str, highlights: list[str]) -> str:
    hls = "".join(f"<li>{_esc(x)}</li>" for x in highlights[:3])
    ad_top = AD_SHORTCODE or ""
    ad_mid = AD_INSERT_MIDDLE or AD_SHORTCODE or ""

    parts = [
        _css(),
        '<div class="dx">',
        # 내부광고(상단)
        ad_top,
        # 요약
        "<h2>요약</h2>",
        f'<p class="lead">오늘의 주제는 ‘{_esc(title)}’입니다. 핵심만 짧게 남깁니다.</p>',
        # 하이라이트
        "<h2>하이라이트 3</h2>",
        f"<ul>{hls}</ul>",
        # 배운 점
        "<h2>배운 점</h2>",
        "<p>사실과 해석을 분리해 기록합니다. 같은 상황이 오면 어떤 기준으로 빠르게 결정할지 한 문장으로 정리합니다.</p>",
        # 내부광고(중간)
        ad_mid,
        # 실행 계획
        "<h2>실행 계획</h2>",
        "<ul><li>내일 5분 안에 시작할 첫 행동 1가지</li><li>2주 동안 유지할 지표 1개</li><li>하지 않을 것 1가지</li></ul>",
        # 지표/체크리스트
        "<h2>지표/체크리스트</h2>",
        "<ul><li>집중 시간(분)</li><li>피드백 횟수</li><li>완료/보류 항목</li></ul>",
        # 회고
        "<h2>회고</h2>",
        "<p>작은 선택이 누적되면 다음 갈림길에서의 망설임이 줄어듭니다. 오늘 남긴 기준을 재사용할 수 있게 문장으로 보존합니다.</p>",
        "</div>",
    ]
    return "\n".join(parts)

# ===== 메인 =====
def main(mode: str="two-posts"):
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    # 고정 슬롯: 10시, 17시 (KST)
    slots = [10, 17]
    titles = ["프로젝트 회고 점검 노트", "작은 습관 정리 메모"]
    highlights = [
        ["가장 좋았던 선택 1가지", "의외의 장애물 1가지", "내일도 반복하고 싶은 습관 1가지"],
        ["에너지 높였던 순간 1가지", "실패에서 배운 것 1가지", "다음에 개선할 것 1가지"],
    ]

    for hh, tt, hl in zip(slots, titles, highlights):
        html_body = _build_diary_html(tt, hl)
        html_body = _ensure_min_chars(html_body, 1500)
        when = _slot_to_utc(hh)
        res = _post_wp(tt, html_body, when, DEFAULT_CATEGORY)
        print(json.dumps({"id":res.get("id"), "title":tt, "date_gmt":res.get("date_gmt")}, ensure_ascii=False))

if __name__=="__main__":
    import sys
    mode = sys.argv[sys.argv.index("--mode")+1] if "--mode" in sys.argv else "two-posts"
    main(mode)
