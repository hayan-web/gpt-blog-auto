# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상글 2건 예약
- 기사형 섹션 구조 + .dx 네임스페이스 CSS
- AD 상/중 삽입, 중앙 버튼 유지
- '요약글' 소제목 제거(텍스트/콜아웃만 표시)
- 1500자 보강: 중복 금지, 최대 3블록
- keywords_general.csv에서 2개 키워드 사용 후 머리를 꼬리로 회전 (영구 반영은 워크플로 커밋 단계에서 처리)
"""

from __future__ import annotations
import os, json, re, html, csv
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Dict, Optional, List
import requests
from dotenv import load_dotenv
try:
    from slugify import slugify  # 일반 경로
except Exception:
    from python_slugify import slugify  # 일부 환경

load_dotenv()

WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
VERIFY_TLS=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()
DEFAULT_CATEGORY=(os.getenv("DEFAULT_CATEGORY") or "정보").strip() or "정보"
AD_SHORTCODE=os.getenv("AD_SHORTCODE") or ""
AD_INSERT_MIDDLE=os.getenv("AD_INSERT_MIDDLE") or AD_SHORTCODE
KEYWORDS_CSV=(os.getenv("KEYWORDS_CSV") or "keywords_general.csv").strip()

REQ_HEADERS={
    "User-Agent": os.getenv("USER_AGENT") or "gpt-blog-auto/diary-2.2",
    "Accept":"application/json",
    "Content-Type":"application/json; charset=utf-8"
}

def _css():
    return """
<style>
.dx { line-height:1.8; letter-spacing:-.01em }
.dx h2{font-size:1.6em;margin:1.2em 0 .4em;font-weight:800;letter-spacing:-.02em}
.dx h3,.dx h4{font-size:1.15em;margin:1.0em 0 .35em;font-weight:700;
  border-left:6px solid #10b981;padding-left:.55em}
.dx p>strong:first-child{display:block;margin:1.0em 0 .35em;font-weight:700;
  border-left:6px solid #10b981;padding-left:.55em}
.dx .dx-meta{font-size:.925em;color:#64748b;margin:.25em 0 1.0em}
.dx ul{padding-left:1.2em}
.dx .callout{background:#f8fafc;border-left:3px solid #94a3b8;padding:.9em 1em;border-radius:.6rem}
.dx .center{ text-align:center;margin:16px 0 }
.dx .btn{ display:inline-block;padding:12px 18px;border-radius:9999px;
  background:#16a34a;color:#fff;text-decoration:none;font-weight:700;min-width:180px }
</style>
""".strip()

def _esc(s: Optional[str])->str:
    return html.escape((s or "").strip())

def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")

def _nchars(x: str) -> int:
    return len(re.sub(r"\s+","",_strip_tags(x)))

def _ensure_min_chars(body: str, min_chars: int = 1500) -> str:
    if _nchars(body) >= min_chars:
        return body
    fillers = [
        "<h3>작은 한 걸음</h3><p>완벽보다 빈도가 중요합니다. 측정 가능한 지표를 한 줄로 정리하고, 다음 행동을 캘린더에 바로 배치하세요.</p>",
        "<h3>되돌아보기</h3><p>오늘의 결정이 1주 후에도 같은 결정을 돕는가를 확인합니다. 재사용 가능한 문장 한 줄을 남겨두면 다음 실행이 쉬워집니다.</p>",
        "<h3>방해요소 차단</h3><p>내일 아침 30분 동안만 환경을 단순화해보세요. 불필요한 알림/탭/물건을 치우고 핵심 도구만 남기면 집중이 쉬워집니다.</p>",
    ]
    used=set()
    buf=body
    for b in fillers:
        if _nchars(buf) >= min_chars: break
        if b not in used and b not in buf:
            buf += "\n" + b
            used.add(b)
    return buf

def _center_btn(url:str, label:str)->str:
    u=_esc(url); l=_esc(label or "정보 글 더 보기")
    return f'<div class="center"><a class="btn" href="{u}" target="_blank" rel="nofollow noopener">{l}</a></div>'

def _build_diary_html(title: str, highlight: list[str], info_btn_url:str) -> str:
    # 요약(소제목 제거) / 하이라이트 / 실행 / 지표+회고 (+ 광고 상/중)
    hls = "".join(f"<li>{_esc(x)}</li>" for x in highlight[:3])
    parts = [
        _css(),
        '<div class="dx">',
        (AD_SHORTCODE or ""),
        # 요약은 소제목 없이 callout만
        f'<div class="callout"><p>오늘의 기록—‘{_esc(title)}’—을 한 단락으로 정리합니다. 핵심만 간결하게 남겨두면 복기가 빨라집니다.</p></div>',
        _center_btn(info_btn_url, "정보 글 더 보기"),
        '<h3>하이라이트</h3>',
        f'<ul>{hls}</ul>',
        (AD_INSERT_MIDDLE or ""),
        '<h3>실행</h3>',
        '<ul><li>내일 5분 안에 시작할 첫 행동</li><li>2주 유지할 지표 1개</li><li>하지 않을 것 1가지</li></ul>',
        '<h3>지표/회고</h3>',
        '<p>집중 시간, 피드백 횟수, 완료/보류 항목을 간단히 기록합니다. 작은 진동의 누적이 다음 선택의 난이도를 낮춥니다.</p>',
        '</div>'
    ]
    return "\n".join(p for p in parts if p)

# ===== 키워드 회전 =====
def _read_col_csv(path:str)->List[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8",newline="") as f:
        rd=csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and row[0].strip().lower() in ("keyword","title"): continue
            s=row[0].strip()
            if s: out.append(s)
    return out

def _write_rotated(path:str, items:List[str], k:int):
    if not os.path.exists(path): return
    with open(path,"r",encoding="utf-8",newline="") as f:
        rows=list(csv.reader(f))
    if not rows: return
    header = rows[0]
    data = [ [x] for x in items[k:] + items[:k] ]  # k개 사용 → 뒤로 회전
    with open(path,"w",encoding="utf-8",newline="") as f:
        wr=csv.writer(f); wr.writerow(header); wr.writerows(data)

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

def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

def _slot_to_utc(hh:int)->str:
    now=_now_kst()
    tgt=now.replace(hour=hh,minute=0,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ===== 메인 =====
def main(mode: str="two-posts"):
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    # 키워드 2개 뽑기
    pool=_read_col_csv(KEYWORDS_CSV)
    kw1 = pool[0] if len(pool)>=1 else "프로젝트 회고"
    kw2 = pool[1] if len(pool)>=2 else "작은 습관"

    titles=[
        f"{kw1} 정리 메모",
        f"{kw2} 회고 점검 노트",
    ]
    highlights=[
        ["가장 좋았던 선택 1가지","의외의 장애물 1가지","내일 반복할 한 가지"],
        ["에너지를 높인 순간 1가지","실패에서 배운 것 1가지","다음 개선 1가지"],
    ]
    btn_url = WP_URL or "#"

    # 사용한 2개 회전
    if len(pool)>=2:
        _write_rotated(KEYWORDS_CSV, pool, 2)
    elif len(pool)==1:
        _write_rotated(KEYWORDS_CSV, pool, 1)

    # 10시 / 17시
    slots=[10,17]
    for i,(hh,tt,hl) in enumerate(zip(slots, titles, highlights)):
        html_body=_build_diary_html(tt, hl, btn_url)
        html_body=_ensure_min_chars(html_body, 1500)
        when=_slot_to_utc(hh)
        res=_post_wp(tt, html_body, when, DEFAULT_CATEGORY)
        print(json.dumps({"id":res.get("id"),"title":tt,"date_gmt":res.get("date_gmt")}, ensure_ascii=False))

if __name__=="__main__":
    import sys
    mode=sys.argv[sys.argv.index("--mode")+1] if "--mode" in sys.argv else "two-posts"
    main(mode)
