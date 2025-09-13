# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상글 2건 예약(수정본)
- 요약 '소제목' 제거(콜아웃만 표시)
- 요약 아래/중간에 녹색 버튼 2개(카테고리 링크)
- AD 상/중 삽입(있으면)
- 모든 섹션을 동일 녹색 포인트 h2(.fx2-heading)로 렌더
- 1500자 보강: 중복 금지, 최대 3블록
"""

from __future__ import annotations
import os, json, re, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional
import requests
from dotenv import load_dotenv
from python_slugify import slugify

load_dotenv()

WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
VERIFY_TLS=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()
DEFAULT_CATEGORY=(os.getenv("DEFAULT_CATEGORY") or "정보").strip() or "정보"
AD_SHORTCODE=os.getenv("AD_SHORTCODE") or ""
DIARY_BUTTON_URL=os.getenv("DIARY_BUTTON_URL") or ""

REQ_HEADERS={
    "User-Agent": os.getenv("USER_AGENT") or "gpt-blog-auto/diary-2.1",
    "Accept":"application/json",
    "Content-Type":"application/json; charset=utf-8"
}

def _css():
    return """
<style>
.fx2{line-height:1.8;letter-spacing:-.01em}
.fx2 .fx2-heading{
  position:relative;margin:1.4rem 0 .6rem;font-size:1.35rem;font-weight:800;letter-spacing:-.02em
}
.fx2 .fx2-heading:before{
  content:"";display:inline-block;width:.62rem;height:.62rem;border-radius:.2rem;
  background:#10b981;margin-right:.48rem;vertical-align:baseline
}
.fx2 .dx-meta{font-size:.925em;color:#64748b;margin:.25em 0 1.0em}
.fx2 .callout{background:#f8fafc;border-left:4px solid #10b981;padding:1rem;border-radius:.6rem}
.fx2 .fx2-btnwrap{text-align:center;margin:12px 0 18px}
.fx2 .fx2-btn{display:inline-block;padding:12px 20px;border-radius:999px;background:#16a34a;color:#fff;font-weight:800;text-decoration:none}
</style>
""".strip()

def _esc(s: Optional[str])->str: return html.escape((s or "").strip())
def _strip_tags(s: str) -> str: return re.sub(r"<[^>]+>", "", s or "")
def _nchars(x: str) -> int: return len(re.sub(r"\s+","",_strip_tags(x)))

def _ensure_min_chars(body: str, min_chars: int = 1500) -> str:
    if _nchars(body) >= min_chars:
        return body
    fillers = [
        ("메모 추가","오늘 기록에서 바로 적용 가능한 한 줄 메모를 남깁니다. 다음 선택의 난이도를 낮추는 문장이면 좋습니다."),
        ("반복 점검 문장","측정 가능한 지표 한 줄과 다음 행동을 캘린더에 배치하세요. 빈도가 품질을 만듭니다."),
        ("주의 구간","방해요소(알림/탭/물건)을 30분만 치우고 핵심 도구만 남겨 집중 루프를 만드는 연습을 합니다.")
    ]
    used = 0
    buf = body
    for title, text in fillers:
        if _nchars(buf) >= min_chars: break
        if used >= 3: break
        buf += f'\n<h2 class="fx2-heading">{_esc(title)}</h2><p>{_esc(text)}</p>'
        used += 1
    return buf

def _category_url()->str:
    if DIARY_BUTTON_URL:
        return DIARY_BUTTON_URL
    if WP_URL:
        return f"{WP_URL}/category/{slugify(DEFAULT_CATEGORY)}"
    return "/"

def _button(label:str)->str:
    return f'<div class="fx2-btnwrap"><a class="fx2-btn" href="{_esc(_category_url())}" target="_blank" rel="noopener">{_esc(label)}</a></div>'

def _build_diary_html(title: str, highlight: list[str]) -> str:
    hls = "".join(f"<li>{_esc(x)}</li>" for x in highlight[:3])
    parts = [
        _css(),
        '<div class="fx2">',
        (AD_SHORTCODE or ""),
        # 요약(소제목 없이 콜아웃)
        f'<div class="callout">오늘의 기록—‘{_esc(title)}’—을 한 단락으로 정리합니다. 핵심만 간결하게 남겨두면 복기가 빨라집니다.</div>',
        _button("정보 글 더 보기"),
        f'<h2 class="fx2-heading">하이라이트</h2>',
        f'<ul>{hls}</ul>',
        f'<h2 class="fx2-heading">실행</h2>',
        '<ul><li>내일 5분 안에 시작할 첫 행동</li><li>2주 유지할 지표 1개</li><li>하지 않을 것 1가지</li></ul>',
        (AD_SHORTCODE or ""),
        _button("정보 글 더 보기"),
        f'<h2 class="fx2-heading">지표/회고</h2>',
        '<p>집중 시간, 피드백 횟수, 완료/보류 항목을 간단히 기록합니다. 작은 진동의 누적이 다음 선택의 난이도를 낮춥니다.</p>',
        f'<h2 class="fx2-heading">작은 한 걸음</h2>',
        '<p>완벽보다 빈도가 중요합니다. 측정 가능한 지표 한 줄과 다음 행동을 캘린더에 바로 배치하세요.</p>',
        f'<h2 class="fx2-heading">되돌아보기</h2>',
        '<p>오늘의 결정이 1주 후에도 같은 결정을 돕는가를 확인합니다. 재사용 가능한 문장 한 줄을 남겨두면 실행이 쉬워집니다.</p>',
        f'<h2 class="fx2-heading">방해요소 차단</h2>',
        '<p>내일 아침 30분 동안 환경을 단순화해 보세요. 불필요한 알림/탭/물건을 치우고 핵심 도구만 남기면 집중이 쉬워집니다.</p>',
        '</div>'
    ]
    return _ensure_min_chars("\n".join([p for p in parts if p]), 1500)

# ===== WP =====
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

def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))
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
        html_body=_build_diary_html(tt, hl)
        when=_slot_to_utc(hh)
        res=_post_wp(tt, html_body, when, DEFAULT_CATEGORY)
        print(json.dumps({"id":res.get("id"),"title":tt,"date_gmt":res.get("date_gmt")}, ensure_ascii=False))

if __name__=="__main__":
    import sys
    mode=sys.argv[sys.argv.index("--mode")+1] if "--mode" in sys.argv else "two-posts"
    main(mode)
