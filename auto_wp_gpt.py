# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상글 2건 예약(10시/17시)
- .dx 네임스페이스: 모든 h2/h3 녹색 포인트 스타일
- 대가성 문구는 없음(일상글) / 내부 광고 상+중 삽입
- 1500자 보강은 컨테이너 내부에서만 (스타일 유지)
"""

from __future__ import annotations
import os, json, re, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional
import requests
from dotenv import load_dotenv

load_dotenv()

WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
VERIFY_TLS=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()
DEFAULT_CATEGORY=(os.getenv("DEFAULT_CATEGORY") or "정보").strip() or "정보"
AD_SHORTCODE=os.getenv("AD_SHORTCODE") or ""   # 상/중 동일 사용

REQ_HEADERS={
    "User-Agent": os.getenv("USER_AGENT") or "gpt-blog-auto/diary-2.1",
    "Accept":"application/json",
    "Content-Type":"application/json; charset=utf-8"
}

def _css():
    return """
<style>
.dx{line-height:1.85;letter-spacing:-.01em}
.dx h2{font-size:1.6em;margin:1.2em 0 .55em;font-weight:800;letter-spacing:-.02em}
.dx h2::before{content:"";display:inline-block;width:.42em;height:.95em;background:#16a34a;border-radius:.22em;margin-right:.45em;vertical-align:-.08em}
.dx h3{font-size:1.15em;margin:1.0em 0 .4em;font-weight:700}
.dx .callout{background:#f8fafc;border-left:3px solid #94a3b8;padding:.9em 1em;border-radius:.7rem}
.dx .btnwrap{text-align:center;margin:16px 0}
</style>
""".strip()

def _esc(s: Optional[str])->str:
    return html.escape((s or "").strip())

def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")

def _nchars(x: str) -> int:
    return len(re.sub(r"\s+","",_strip_tags(x)))

def _ensure_min_chars_inner(inner: str, min_chars: int = 1500) -> str:
    if _nchars(inner) >= min_chars:
        return inner
    fillers = [
        "<h3>작은 한 걸음</h3><p>완벽보다 빈도가 중요합니다. 측정 가능한 지표 한 줄과 다음 행동을 캘린더에 바로 배치하세요.</p>",
        "<h3>되돌아보기</h3><p>오늘 결정이 1주 후에도 같은 결정을 돕는가를 확인합니다. 재사용 문장 한 줄을 남겨두세요.</p>",
        "<h3>방해요소 차단</h3><p>내일 아침 30분만 환경을 단순화해 보세요. 알림/탭/물건을 치우고 핵심 도구만 남기면 집중이 쉬워집니다.</p>",
    ]
    used=set()
    buf=inner
    for b in fillers:
        if _nchars(buf) >= min_chars: break
        if b not in used and b not in buf:
            buf += "\n" + b
            used.add(b)
    return buf

def _build_diary_inner(title: str, highlight: list[str]) -> str:
    hls = "".join(f"<li>{_esc(x)}</li>" for x in highlight[:3])
    parts = []
    if AD_SHORTCODE:
        parts.append(AD_SHORTCODE)   # 상단 광고
    parts.append(f'<h2>요약글</h2><div class="callout"><p>오늘의 기록—‘{_esc(title)}’—을 한 단락으로 정리합니다. 핵심만 간결하게 남겨두면 복기가 빨라집니다.</p></div>')
    parts.append(f'<h2>하이라이트</h2><ul>{hls}</ul>')
    if AD_SHORTCODE:
        parts.append(AD_SHORTCODE)   # 중간 광고
    parts.append('<h2>실행</h2><ul><li>내일 5분 안에 시작할 첫 행동</li><li>2주 유지할 지표 1개</li><li>하지 않을 것 1가지</li></ul>')
    parts.append('<h2>지표/회고</h2><p>집중 시간, 피드백 횟수, 완료/보류 항목을 간단히 기록합니다. 작은 진동의 누적이 다음 선택의 난이도를 낮춥니다.</p>')
    return "\n".join(parts)

def _ensure_term(kind:str, name:str)->int:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}",
                   params={"search":name,"per_page":50,"context":"edit"},
                   auth=(WP_USER,WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name:
            return int(it["id"])
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
                    auth=(WP_USER,WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=15, headers=REQ_HEADERS)
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
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                    auth=(WP_USER,WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status()
    return r.json()

def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

def _slot_to_utc(hh:int)->str:
    now=_now_kst()
    tgt=now.replace(hour=hh,minute=0,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def main(mode: str="two-posts"):
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    slots=[10,17]
    titles=["프로젝트 회고 점검 노트","작은 습관 정리 메모"]
    highlights=[
        ["가장 좋았던 선택 1가지","의외의 장애물 1가지","내일도 반복하고 싶은 습관 1가지"],
        ["에너지 높였던 순간 1가지","실패에서 배운 것 1가지","다음에 개선할 것 1가지"],
    ]

    for hh,tt,hl in zip(slots, titles, highlights):
        inner=_build_diary_inner(tt, hl)
        inner=_ensure_min_chars_inner(inner, 1500)
        html_body=_css() + f'\n<div class="dx">\n{inner}\n</div>'
        when=_slot_to_utc(hh)
        res=_post_wp(tt, html_body, when, DEFAULT_CATEGORY)
        print(json.dumps({"id":res.get("id"),"title":tt,"date_gmt":res.get("date_gmt")}, ensure_ascii=False))

if __name__=="__main__":
    import sys
    mode=sys.argv[sys.argv.index("--mode")+1] if "--mode" in sys.argv else "two-posts"
    main(mode)
