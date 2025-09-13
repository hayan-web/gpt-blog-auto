# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상글 2건 예약
- 소제목 '요약글' 제거(본문만 보이게)
- 모든 소제목(h2/h3) 녹색 포인트 일관 적용(.dx 네임스페이스)
- CTA 버튼 복구(녹색 pill), 상단/중단 광고 삽입(AD_SHORTCODE)
- 1500자 보강: 중복 금지, 최대 3블록
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

AD_SHORTCODE=os.getenv("AD_SHORTCODE") or ""
READMORE_URL=(os.getenv("READMORE_URL") or WP_URL or "").strip()

REQ_HEADERS={
    "User-Agent": os.getenv("USER_AGENT") or "gpt-blog-auto/diary-2.1",
    "Accept":"application/json",
    "Content-Type":"application/json; charset=utf-8"
}

def _css()->str:
    return """
<style>
.dx{line-height:1.85;letter-spacing:-.01em}
.dx h2,.dx h3{margin:1.1em 0 .55em;padding-left:10px;border-left:6px solid #16a34a}
.dx h2{font-size:1.6em;font-weight:800}
.dx h3{font-size:1.18em;font-weight:700}
.dx .dx-meta{font-size:.94em;color:#64748b;margin:.25em 0 1.0em}
.dx ul{padding-left:1.2em}
.dx .callout{background:#f1f5f9;border-left:3px solid #94a3b8;padding:.9em 1em;border-radius:.6rem}
.dx .btn-wrap{text-align:center;margin:18px 0}
.dx .btn{display:inline-block;padding:12px 22px;border-radius:9999px;background:#16a34a;color:#fff;
         text-decoration:none;font-weight:800;box-shadow:0 2px 0 #0e7a32}
</style>
""".strip()

def _esc(s: Optional[str])->str:
    return html.escape((s or "").strip())

def _strip_tags(s:str)->str:
    return re.sub(r"<[^>]+>", "", s or "")

def _nchars(x:str)->int:
    return len(re.sub(r"\s+","",_strip_tags(x)))

def _ensure_min_chars(body:str, min_chars:int=1500)->str:
    if _nchars(body)>=min_chars:
        return body
    fillers=[
        "<h3>작은 한 걸음</h3><p>완벽보다 빈도가 중요합니다. 측정 가능한 지표 한 줄을 정하고 다음 행동을 캘린더에 바로 배치하세요.</p>",
        "<h3>되돌아보기</h3><p>오늘의 결정이 1주 뒤에도 같은 결정을 돕는지 확인합니다. 재사용 가능한 문장 한 줄을 남겨두면 실행이 쉬워집니다.</p>",
        "<h3>방해요소 차단</h3><p>내일 아침 30분만 환경을 단순화하세요. 알림/탭/물건을 치우고 핵심 도구만 남기면 집중이 쉬워집니다.</p>",
    ]
    used=set()
    buf=body
    for b in fillers:
        if _nchars(buf)>=min_chars: break
        if b not in used and b not in buf:
            buf+="\n"+b; used.add(b)
    return buf

def _cta()->str:
    if not READMORE_URL:
        return ""
    u=_esc(READMORE_URL)
    return f'<div class="btn-wrap"><a class="btn" href="{u}" target="_blank" rel="noopener">정보 글 더 보기</a></div>'

def _build_diary_html(title:str, highlights:list[str])->str:
    hls="".join(f"<li>{_esc(x)}</li>" for x in highlights[:3])
    parts=[
        _css(),
        '<div class="dx">',
        (AD_SHORTCODE or ""),                               # 상단 광고
        # '요약글' 소제목 제거 — 바로 요약 문단
        f'<div class="callout"><p>오늘의 기록—‘{_esc(title)}’—을 한 단락으로 정리합니다. 핵심만 간결하게 남겨두면 복기가 빨라집니다.</p></div>',
        _cta(),
        '<h2>하이라이트</h2>',
        f'<ul>{hls}</ul>',
        (AD_SHORTCODE or ""),                               # 중단 광고
        '<h2>실행</h2>',
        '<ul><li>내일 5분 안에 시작할 첫 행동</li><li>2주간 유지할 지표 1개</li><li>하지 않을 것 1가지</li></ul>',
        '<h2>지표/회고</h2>',
        '<p>집중 시간, 피드백 횟수, 완료/보류 항목을 간단히 기록합니다. 작은 진동의 누적이 다음 선택의 난이도를 낮춥니다.</p>',
        # 이하 하위 소제목도 h3로 녹색 포인트 유지
        '<h3>작은 한 걸음</h3><p>지표 한 줄 + 다음 행동을 일정에 바로 배치해 주세요.</p>',
        '<h3>되돌아보기</h3><p>오늘의 결정이 같은 맥락에서 다시 도움이 될지 점검합니다.</p>',
        '<h3>방해요소 차단</h3><p>알림/탭/물건을 치우고 핵심 도구만 남기면 집중이 쉬워집니다.</p>',
        '</div>'
    ]
    return "\n".join(p for p in parts if p)

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

def main(mode:str="two-posts"):
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    slots=[10,17]
    titles=["프로젝트 회고 점검 노트","작은 습관 정리 메모"]
    highlights=[
        ["가장 좋았던 선택 1가지","의외의 장애물 1가지","내일도 반복할 습관 1가지"],
        ["에너지 높였던 순간 1가지","실패에서 배운 점 1가지","다음에 개선할 것 1가지"],
    ]

    for hh,tt,hl in zip(slots,titles,highlights):
        html_body=_build_diary_html(tt, hl)
        html_body=_ensure_min_chars(html_body, 1500)
        when=_slot_to_utc(hh)
        res=_post_wp(tt, html_body, when, DEFAULT_CATEGORY)
        print(json.dumps({"id":res.get("id"),"title":tt,"date_gmt":res.get("date_gmt")}, ensure_ascii=False))

if __name__=="__main__":
    import sys
    mode=sys.argv[sys.argv.index("--mode")+1] if "--mode" in sys.argv else "two-posts"
    main(mode)
