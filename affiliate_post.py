# -*- coding: utf-8 -*-
"""
affiliate_post.py — 쿠팡 글 자동 발행 (기사형/녹색 스타일)
- 대가성 문구 최상단 강조
- 섹션: [광고(상)] → 요약글 → 버튼 → 정보 글 → (썸네일 주석) → 버튼 → 광고(중) → 추가 정보
- 버튼/소제목: 녹색 포인트, 중앙 정렬
- 본문은 공백 제외 1500자 이상(중복 없이 최대 3블록 보강)
"""

from __future__ import annotations
import os, csv, json, re, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional
import requests
from dotenv import load_dotenv
from coupang_api import deeplink_for_query  # 가능하면 딥링크

load_dotenv()

# ===== ENV =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
VERIFY_TLS=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"

POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()
AFFILIATE_CATEGORY=(os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip() or "쇼핑"
DISCLOSURE_TEXT=os.getenv("DISCLOSURE_TEXT") or "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공합니다."
AD_SHORTCODE=os.getenv("AD_SHORTCODE") or ""
AD_INSERT_MIDDLE=os.getenv("AD_INSERT_MIDDLE") or AD_SHORTCODE

USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_SHOP=os.path.join(USAGE_DIR,"used_shopping.txt")

REQUIRE_COUPANG_API=(os.getenv("REQUIRE_COUPANG_API") or "0").strip().lower() in ("1","true","yes","on")

P_GOLD="golden_shopping_keywords.csv"

REQ_HEADERS={
    "User-Agent": os.getenv("USER_AGENT") or "gpt-blog-auto/aff-2.2",
    "Accept":"application/json",
    "Content-Type":"application/json; charset=utf-8"
}

# ===== 스타일 =====
def _css()->str:
    return """
<style>
.ax { line-height:1.8; letter-spacing:-.01em }
.ax h2{font-size:1.6em;margin:1.2em 0 .5em;font-weight:800}
.ax h2 .bar{display:inline-block;width:.32em;height:.9em;margin-right:.45em;background:#16a34a;border-radius:.2em;vertical-align:-.1em}
.ax .cta-wrap{ text-align:center; margin:18px 0 }
.ax .cta{ display:inline-block; padding:14px 26px; border-radius:9999px; background:#16a34a; color:#fff; font-weight:800; text-decoration:none }
.ax .disclosure{ margin:8px 0 14px; padding:10px 14px; border:2px solid #16a34a; border-radius:12px; font-weight:700; color:#065f46; background:#ecfdf5 }
.ax table{ width:100%; border-collapse:collapse; margin:10px 0 }
.ax table th, .ax table td{ border:1px solid #e5e7eb; padding:8px 10px; text-align:left }
.ax .muted{ color:#64748b }
</style>
""".strip()

# ===== 유틸 =====
def _esc(s: Optional[str])->str:
    return html.escape((s or "").strip())

def _ensure_usage():
    os.makedirs(USAGE_DIR, exist_ok=True)

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
    head = data.pop(0)
    data.append(head)
    with open(path,"w",encoding="utf-8",newline="") as f:
        wr=csv.writer(f); wr.writerow(header); wr.writerows(data)

def _strip_tags(s:str)->str:
    return re.sub(r"<[^>]+>","",s or "")

def _nchars_no_space(html_text:str)->int:
    return len(re.sub(r"\s+","",_strip_tags(html_text)))

def _ensure_min_chars(body_html:str, min_chars:int=1500)->str:
    """중복 없이 최대 3블록 보강."""
    if _nchars_no_space(body_html) >= min_chars:
        return body_html
    fillers = [
        "<h3>구매 체크리스트</h3><p>내 환경(공간·소음·예산)을 먼저 정의하고 꼭 필요한 기능부터 우선순위를 매기세요.</p>",
        "<h3>활용 팁</h3><p>기본 모드로 충분히 익힌 뒤 자주 쓰는 장면에 맞춰 보조 기능을 하나씩 추가해보세요.</p>",
        "<h3>유지관리</h3><p>소모품 주기와 세척 난도를 미리 확인해 캘린더에 기록해두면 번거로움이 크게 줄어듭니다.</p>",
    ]
    used=0
    buf=body_html
    for add in fillers:
        if _nchars_no_space(buf) >= min_chars: break
        buf += "\n" + add
        used += 1
        if used>=3: break
    return buf

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

# ===== 시간대/슬롯 =====
def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

def _slot_to_utc(kst_hm:str)->str:
    hh,mm = [int(x) for x in kst_hm.split(":")]
    now=_now_kst()
    tgt=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ===== 버튼 / 링크 =====
def _cta(url:str, label:str="바로 보기")->str:
    u=_esc(url); l=_esc(label or "바로 보기")
    return f'<div class="cta-wrap"><a class="cta" href="{u}" target="_blank" rel="nofollow sponsored noopener">{l}</a></div>'

def coupang_search_url(query: str) -> str:
    from urllib.parse import quote_plus
    return f"https://search.shopping.coupang.com/search?component=&q={quote_plus(query)}&channel=rel"

def resolve_affiliate_url(product_title: str) -> str:
    if REQUIRE_COUPANG_API:
        try:
            u = deeplink_for_query(product_title)
            if isinstance(u,str) and u: 
                return u
        except Exception:
            pass
    return coupang_search_url(product_title)

# ===== 콘텐츠 렌더 =====
def _build_product_skeleton(keyword:str)->Dict:
    k=keyword.strip()
    return {
        "title": k,
        "summary": f"{k} 선택 시, 성능·관리·비용 균형을 빠르게 점검할 수 있도록 핵심만 정리합니다.",
    }

def _article_html(prod:Dict, url:str)->str:
    k = _esc(prod.get("title") or "")
    summary = _esc(prod.get("summary") or "")
    top_ad = AD_SHORTCODE or ""
    mid_ad = AD_INSERT_MIDDLE or ""

    parts = [
        _css(),
        '<div class="ax">',
        f'<div class="disclosure">{_esc(DISCLOSURE_TEXT)}</div>',  # 최상단
        top_ad,  # 내부 광고 (상)
        f'<h2><span class="bar"></span>요약글</h2>',
        f'<p class="muted">{summary}</p>',
        _cta(url, "바로 보기"),
        f'<h2><span class="bar"></span>정보 글</h2>',
        '<p>실사용 관점에서 자주 쓰는 기능과 관리 난도를 먼저 확인하면 선택이 쉬워집니다. 공간·소음·예산을 기준으로 필요한 수준만 고르는 것이 핵심입니다.</p>',
        '<!-- 썸네일 자리: 안정화 후 이미지 삽입 예정 -->',
        _cta(url, "바로 보기"),
        mid_ad,  # 내부 광고 (중)
        f'<h2><span class="bar"></span>추가 정보</h2>',
        '<p>성능·관리·비용을 표로 비교해 보면 선택이 쉬워집니다.</p>',
        '<table><thead><tr><th>항목</th><th>확인 포인트</th><th>비고</th></tr></thead>'
        '<tbody>'
        '<tr><td>성능</td><td>공간/목적 대비 충분</td><td>과투자 방지</td></tr>'
        '<tr><td>관리</td><td>세척·보관·소모품</td><td>난도/주기</td></tr>'
        '<tr><td>비용</td><td>구매가 + 유지비</td><td>시즌 특가</td></tr>'
        '</tbody></table>',
        '</div>'
    ]
    html_body = "\n".join([p for p in parts if p])
    return _ensure_min_chars(html_body, 1500)

# ===== 키워드 =====
def _pick_keyword()->Optional[str]:
    pool=_read_col_csv(P_GOLD)
    return pool[0] if pool else None

def _rotate_after_use():
    _rotate_csv_head_to_tail(P_GOLD)
    print("[ROTATE] rotated")

# ===== 메인 =====
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
    content_html = _article_html(prod, url)

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
