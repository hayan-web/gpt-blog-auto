# -*- coding: utf-8 -*-
"""
affiliate_post.py — 쿠팡 글 자동 발행 (기사형·수정본)
- 대가성 문구 최상단 강조 박스
- 섹션 순서: (1) 내부광고 → (2) 요약(소제목 없음, 콜아웃만) → (3) 버튼 → (4) 본문1(짧게)
              → (5) 썸네일(추가 예정 자리 주석) → (6) 버튼 → (7) 내부광고 → (8) 본문2(+표/체크리스트/FAQ)
- 모든 섹션 h2에 동일 녹색 포인트 스타일(.fx2-heading)
- 버튼은 기존 모양 유지 + 중앙 정렬 래퍼
- 본문 보강: 공백 제외 1500자 목표, **중복 금지 / 최대 3블록**만 추가
- Coupang 딥링크 실패 시 검색URL로 폴백
"""

from __future__ import annotations
import os, csv, json, re, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional
import requests
from dotenv import load_dotenv
from coupang_api import deeplink_for_query  # 딥링크 시도

load_dotenv()

# ===== ENV =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
VERIFY_TLS=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"

POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()
AFFILIATE_CATEGORY=(os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip() or "쇼핑"
DISCLOSURE_TEXT=os.getenv("DISCLOSURE_TEXT") or "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
AD_SHORTCODE=os.getenv("AD_SHORTCODE") or ""

USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_SHOP=os.path.join(USAGE_DIR,"used_shopping.txt")

REQUIRE_COUPANG_API=(os.getenv("REQUIRE_COUPANG_API") or "0").strip().lower() in ("1","true","yes","on")
P_GOLD="golden_shopping_keywords.csv"

REQ_HEADERS={
    "User-Agent": os.getenv("USER_AGENT") or "gpt-blog-auto/aff-2.2",
    "Accept":"application/json",
    "Content-Type":"application/json; charset=utf-8"
}

# ===== 공통 유틸 =====
def _css()->str:
    return """
<style>
.fx2{line-height:1.8;letter-spacing:-.01em}
.fx2 .fx2-disclosure{
  background:#ecfdf5;border:1px solid #10b981;color:#065f46;
  padding:.85rem 1rem;border-radius:.8rem;margin:0 0 1rem;font-weight:700
}
.fx2 .fx2-heading{
  position:relative;margin:1.4rem 0 .6rem;font-size:1.35rem;font-weight:800;letter-spacing:-.02em
}
.fx2 .fx2-heading:before{
  content:"";display:inline-block;width:.62rem;height:.62rem;border-radius:.2rem;
  background:#10b981;margin-right:.48rem;vertical-align:baseline
}
.fx2 .callout{
  background:#f8fafc;border-left:4px solid #10b981;padding:1rem;border-radius:.6rem;margin:.4rem 0 1.0rem
}
.fx2 .fx2-btnwrap{text-align:center;margin:12px 0 18px}
.fx2 .fx2-btn{
  display:inline-block;padding:12px 20px;border-radius:999px;background:#16a34a;
  color:#fff;font-weight:800;text-decoration:none
}
.fx2 table{width:100%;border-collapse:collapse;margin:.5rem 0 1.0rem}
.fx2 thead th{background:#f1f5f9}
.fx2 th,.fx2 td{border:1px solid #e2e8f0;padding:.6rem .7rem}
</style>
""".strip()

def _esc(s: Optional[str])->str:
    return html.escape((s or "").strip())

def _strip_tags(s:str)->str:
    return re.sub(r"<[^>]+>","", s or "")

def _nchars_no_space(html_text:str)->int:
    return len(re.sub(r"\s+","",_strip_tags(html_text)))

def _ensure_usage(): os.makedirs(USAGE_DIR, exist_ok=True)

def _mark_used(kw:str):
    _ensure_usage()
    with open(USED_SHOP,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw}\n")

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

def _rotate_csv_head_to_tail(path:str):
    if not os.path.exists(path): return
    with open(path,"r",encoding="utf-8",newline="") as f:
        rows=list(csv.reader(f))
    if len(rows)<2: return
    header, data = rows[0], rows[1:]
    if not data: return
    data.append(data.pop(0))
    with open(path,"w",encoding="utf-8",newline="") as f:
        wr=csv.writer(f); wr.writerow(header); wr.writerows(data)

# ===== 버튼 (원형 유지 + 중앙 정렬 래퍼) =====
def _button_html_local(url: str, label: str = "바로 보기") -> str:
    u = html.escape(url); l = html.escape(label or "바로 보기")
    core = (f'<a class="fx2-btn" href="{u}" target="_blank" rel="nofollow sponsored noopener">{l}</a>')
    return f'<div class="fx2-btnwrap">{core}</div>'

def _get_button_core(url: str) -> str:
    # 프로젝트에 기존 _button_html/BUTTON_PRIMARY가 있다면 그대로 사용
    try:
        return _button_html(url, BUTTON_PRIMARY)  # type: ignore  # noqa: F821
    except Exception:
        try:
            return _button_html(url, "바로 보기")  # type: ignore  # noqa: F821
        except Exception:
            label=(os.getenv("BUTTON_TEXT") or "바로 보기").strip() or "바로 보기"
            return _button_html_local(url, label)

def _get_button_html(url: str) -> str:
    # 기존 버튼을 감싸 중앙 정렬만 보장
    core = _get_button_core(url)
    if 'fx2-btnwrap' in core:  # 이미 래퍼 포함(로컬 폴백)
        return core
    return f'<div class="fx2-btnwrap">{core}</div>'

# ===== 링크 =====
def coupang_search_url(query: str) -> str:
    from urllib.parse import quote_plus
    return f"https://search.shopping.coupang.com/search?component=&q={quote_plus(query)}&channel=rel"

def resolve_affiliate_url(product_title: str) -> str:
    if REQUIRE_COUPANG_API:
        try:
            u = deeplink_for_query(product_title)
            if isinstance(u, str) and u:
                return u
        except Exception:
            pass
    return coupang_search_url(product_title)

# ===== 보강 (중복 금지·최대 3블록) =====
def _ensure_min_chars(body_html:str, min_chars:int=1500)->str:
    if _nchars_no_space(body_html) >= min_chars:
        return body_html

    fillers = [
        ("구매 체크리스트",
         "내 환경(공간·소음·예산)을 먼저 정의하고 꼭 필요한 기능부터 우선순위를 매기세요. "
         "사소해 보이는 관리 난도(세척, 보관, 소모품)까지 포함해 총비용을 가늠하면 선택이 쉬워집니다."),
        ("활용 팁",
         "초기에는 기본 모드만 충분히 익히세요. 자주 쓰는 장면을 기준으로 보조 기능을 하나씩 추가하면 "
         "과투자 없이 체감 품질이 올라갑니다."),
        ("유지관리 가이드",
         "소모품 교체 주기와 세척 난도를 미리 확인해 캘린더에 기록해두면 번거로움이 크게 줄어듭니다. "
         "구매가뿐 아니라 유지비(전기·소모품·시간)까지 함께 보세요."),
        ("FAQ 추가",
         "Q. 사양은 높을수록 좋은가요? A. 목적 대비 과사양은 비용/관리 부담을 키울 수 있습니다. "
         "내 사용 시나리오에 맞는 균형이 핵심입니다.")
    ]

    used = 0
    buf = body_html
    for title, text in fillers:
        if _nchars_no_space(buf) >= min_chars: break
        if used >= 3: break
        buf += f'\n<h2 class="fx2-heading">{_esc(title)}</h2><p>{_esc(text)}</p>'
        used += 1

    return buf

# ===== WordPress =====
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

def post_wp(title:str, content:str, when_gmt:str, category:str)->dict:
    cat_id=_ensure_term("categories", category or AFFILIATE_CATEGORY)
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

# ===== 시간 =====
def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))

def _slot_to_utc(kst_hm:str)->str:
    hh,mm = [int(x) for x in kst_hm.split(":")]
    now=_now_kst()
    tgt=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ===== 콘텐츠 =====
def _build_product_skeleton(keyword:str)->Dict:
    k = keyword.strip()
    return {
        "title": k,
        "summary": f"{k} 선택 시 성능·관리·비용 균형을 빠르게 점검할 수 있도록 핵심만 정리합니다."
    }

def _render_article(prod:Dict, url:str)->str:
    k = _esc(prod.get("title") or "추천 제품")
    summary = _esc(prod.get("summary") or "")
    bh = _get_button_html(url)

    table_html = (
        "<table><thead><tr><th>항목</th><th>확인 포인트</th><th>비고</th></tr></thead>"
        "<tbody>"
        "<tr><td>성능</td><td>공간/목적 대비 충분</td><td>과투자 방지</td></tr>"
        "<tr><td>관리</td><td>세척·보관·소모품</td><td>난도/주기</td></tr>"
        "<tr><td>비용</td><td>구매가 + 유지비</td><td>시즌 특가</td></tr>"
        "</tbody></table>"
    )

    parts = [
        _css(),
        '<div class="fx2">',
        f'<div class="fx2-disclosure">{_esc(DISCLOSURE_TEXT)}</div>',
        (AD_SHORTCODE or ""),
        # 요약(소제목 없이 콜아웃만)
        f'<div class="callout">{summary}</div>',
        bh,
        # 본문1 (짧게)
        f'<h2 class="fx2-heading">정보 글</h2>',
        "<p>실사용 관점에서 자주 쓰는 기능과 관리 난도를 먼저 확인하면 선택이 쉬워집니다. "
        "공간·소음·예산을 기준으로 필요한 수준만 고르는 것이 핵심입니다.</p>",
        # 썸네일 자리(안정화 후 넣을 예정)
        "<!-- thumbnail placeholder -->",
        bh,
        (AD_SHORTCODE or ""),
        # 본문2
        f'<h2 class="fx2-heading">추가 정보</h2>',
        "<p>성능·관리·비용을 표로 비교해 보면 빠르게 기준을 잡을 수 있습니다.</p>",
        table_html,
        f'<h2 class="fx2-heading">구매 체크리스트</h2>',
        "<p>내 환경(공간·소음·예산)을 먼저 정의하고 꼭 필요한 기능부터 우선순위를 매기세요.</p>",
        f'<h2 class="fx2-heading">활용 팁</h2>',
        "<p>처음에는 기본 모드만 익히고, 자주 쓰는 장면에 보조 기능을 하나씩 추가하세요.</p>",
        f'<h2 class="fx2-heading">유지관리</h2>',
        "<p>소모품 주기와 세척 난도를 미리 확인해 캘린더에 기록해두면 번거로움이 크게 줄어듭니다.</p>",
        f'<h2 class="fx2-heading">FAQ</h2>',
        "<p><b>Q.</b> 사양은 높을수록 무조건 좋나요? <b>A.</b> 목적 대비 과사양은 비용/관리 부담이 큽니다. "
        "사용 시나리오에 맞는 균형을 고르세요.</p>",
        "</div>"
    ]
    return _ensure_min_chars("\n".join([p for p in parts if p]))

# ===== 키워드 & 메인 =====
def _pick_keyword()->Optional[str]:
    pool=_read_col_csv(P_GOLD)
    return pool[0] if pool else None

def _rotate_after_use():
    _rotate_csv_head_to_tail(P_GOLD)
    print("[ROTATE] rotated")

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    slot = (os.getenv("AFFILIATE_TIME_KST") or "12:00").strip()
    print(f"[AFFILIATE] slot={slot}")

    kw = _pick_keyword()
    if not kw:
        print("[AFFILIATE] SKIP: no keyword")
        return

    url = resolve_affiliate_url(kw)
    prod = _build_product_skeleton(kw)
    content_html = _render_article(prod, url)

    when_gmt = _slot_to_utc(slot)
    title = f"{kw} 이렇게 쓰니 편해요"
    res = post_wp(title, content_html, when_gmt, AFFILIATE_CATEGORY)
    print(json.dumps({
        "post_id": res.get("id"),
        "link": res.get("link"),
        "status": res.get("status"),
        "date_gmt": res.get("date_gmt"),
        "title": title,
        "keyword": kw
    }, ensure_ascii=False))

    _mark_used(kw)
    _rotate_after_use()

if __name__=="__main__":
    main()
