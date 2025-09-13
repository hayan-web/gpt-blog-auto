# -*- coding: utf-8 -*-
"""
affiliate_post.py — 쿠팡 글 자동 발행 (기사형)
- rich_templates.build_affiliate_content 사용(있으면) + 광고/버튼/섹션은 여기서 조립
- 버튼 모양은 그대로, 외곽 래퍼로 '중앙정렬'만 적용
- 섹션 순서: 1) 내부광고 2) 요약 3) 버튼 4) 본문1 5) 썸네일(주석) 6) 버튼 7) 내부광고 8) 본문2
- 본문은 공백 제외 1500자 이상 보장(무한 반복 금지, 자연스러운 보강)
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

# ===== Rich 템플릿 여부 =====
HAVE_RICH = False
try:
    from rich_templates import build_affiliate_content
    HAVE_RICH = True
except Exception:
    HAVE_RICH = False

# ===== ENV =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
VERIFY_TLS=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"

POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()
AFFILIATE_CATEGORY=(os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip() or "쇼핑"
DISCLOSURE_TEXT=os.getenv("DISCLOSURE_TEXT") or ""
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
            if i==0 and row[0].strip().lower() in ("keyword","title"):
                continue
            s=row[0].strip()
            if s: out.append(s)
    return out

def _rotate_csv_head_to_tail(path:str):
    if not os.path.exists(path): return
    with open(path,"r",encoding="utf-8",newline="") as f:
        rows=list(csv.reader(f))
    if not rows or len(rows)<2: return
    header, data = rows[0], rows[1:]
    if not data: return
    head = data.pop(0)
    data.append(head)
    with open(path,"w",encoding="utf-8",newline="") as f:
        wr=csv.writer(f); wr.writerow(header); wr.writerows(data)

def _strip_tags(s:str)->str:
    return re.sub(r"<[^>]+>", "", s or "")

def _nchars_no_space(html_text:str)->int:
    return len(re.sub(r"\s+","",_strip_tags(html_text)))

def _ensure_min_chars(body_html:str, min_chars:int=1500)->str:
    """채워넣기 문구는 다양화하고 중복을 피함."""
    if _nchars_no_space(body_html) >= min_chars:
        return body_html
    fillers = [
        "<h3>구매 체크리스트</h3><p>내 공간·예산·소음 허용치를 먼저 정의하고, 꼭 필요한 기능부터 우선순위를 매기세요.</p>",
        "<h3>활용 팁</h3><p>처음엔 기본 모드만 익히고, 자주 쓰는 장면에 맞춰 보조 기능을 단계적으로 추가하세요.</p>",
        "<h3>유지관리</h3><p>소모품 주기와 세척 난도를 미리 확인해 캘린더에 기록해두면 번거로움이 줄어듭니다.</p>",
        "<h3>FAQ</h3><p><b>Q.</b> 고사양이 항상 유리할까요? <b>A.</b> 목적 대비 과사양은 비용/관리 부담을 키울 수 있습니다.</p>",
        "<h3>비교 포인트</h3><p>성능·관리·비용을 표로 정리하면 선택이 쉬워집니다.</p>",
    ]
    used=set()
    buf = body_html
    for add in fillers:
        if _nchars_no_space(buf) >= min_chars: break
        if add not in used and add not in buf:
            buf += "\n" + add
            used.add(add)
    # 그래도 부족하면 짧은 문단만 최대 3회 추가
    tail = (
        "실사용 기준으로 자주 쓰는 기능과 관리 난도를 먼저 확인하면 만족도가 높습니다. "
        "총비용은 구매가 + 유지비(전기/소모품/시간)로 계산해보세요."
    )
    cnt = 0
    while _nchars_no_space(buf) < min_chars and cnt < 3:
        buf += f"\n<p>{tail}</p>"
        cnt += 1
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

# ===== 버튼: 원형 유지 + 중앙 정렬 래퍼 =====
def _button_html_local(url: str, label: str = "바로 보기") -> str:
    u = html.escape(url); l = html.escape(label or "바로 보기")
    return (f'<a href="{u}" target="_blank" rel="nofollow sponsored noopener" '
            'style="display:inline-block;padding:12px 18px;border-radius:10px;'
            'background:#111;color:#fff;text-decoration:none;font-weight:700">'
            f'{l}</a>')

def _get_button_core(url: str) -> str:
    # 기존 프로젝트 버튼 함수가 있으면 그대로 사용
    try:
        return _button_html(url, BUTTON_PRIMARY)  # type: ignore  # noqa: F821
    except Exception:
        try:
            return _button_html(url, "바로 보기")  # type: ignore  # noqa: F821
        except Exception:
            label = (os.getenv("BUTTON_TEXT") or "바로 보기").strip() or "바로 보기"
            return _button_html_local(url, label)

def _center_wrap(html_btn: str) -> str:
    # 모양은 그대로, 위치만 중앙
    return f'<div class="rt-center" style="text-align:center;margin:16px 0">{html_btn}</div>'

def _get_button_html(url: str) -> str:
    return _center_wrap(_get_button_core(url))

# ===== 링크 해결 =====
def coupang_search_url(query: str) -> str:
    from urllib.parse import quote_plus
    return f"https://search.shopping.coupang.com/search?component=&q={quote_plus(query)}&channel=rel"

def resolve_affiliate_url(product_title: str) -> str:
    if REQUIRE_COUPANG_API:
        try:
            u = deeplink_for_query(product_title)  # 내부에서 실패 시 예외 발생/None → 폴백
            if isinstance(u, str) and u:
                return u
        except Exception:
            pass
    return coupang_search_url(product_title)

# ===== 콘텐츠 =====
def _build_product_skeleton(keyword:str)->Dict:
    k = keyword.strip()
    return {
        "title": k,
        "features": [],
        "pros": [],
        "cons": [],
        "tips": [],
        "criteria": [],
        "specs": [],
        "faqs": [],
        "summary": f"{k} 선택 시, 성능·관리·비용 균형을 빠르게 점검할 수 있도록 핵심만 정리합니다."
    }

def _css_headings()->str:
    return """
<style>
.ax h2{font-size:1.6em;margin:1.1em 0 .5em;font-weight:800;letter-spacing:-.02em}
.ax h3{font-size:1.15em;margin:.9em 0 .35em;font-weight:700}
.ax table{border-collapse:collapse;width:100%;margin:.6em 0}
.ax table th,.ax table td{border:1px solid #e5e7eb;padding:.55em .6em}
.ax table thead th{background:#f8fafc;font-weight:700}
.ax p{line-height:1.8}
</style>
""".strip()

def _render_core_section(product:Dict, url:str)->str:
    """rich 템플릿이 있으면 코어 본문만 생성, 없으면 간단 예비 본문."""
    bh = _get_button_html(url)
    if HAVE_RICH:
        # 광고 인자(ad_top/ad_middle 등) 전달 금지 → 시그니처 불일치 방지
        return build_affiliate_content(
            product=product,
            button_html=bh,
            disclosure_text=DISCLOSURE_TEXT or None,
        )
    # Legacy 코어
    return (
        "<h3>핵심 비교</h3>"
        "<table><thead><tr><th>항목</th><th>확인 포인트</th><th>비고</th></tr></thead>"
        "<tbody>"
        "<tr><td>성능</td><td>공간/목적 대비 충분</td><td>과투자 방지</td></tr>"
        "<tr><td>관리</td><td>세척·보관·소모품</td><td>난도/주기</td></tr>"
        "<tr><td>비용</td><td>구매가 + 유지비</td><td>시즌 특가</td></tr>"
        "</tbody></table>"
    )

def _assemble_article(product:Dict, url:str)->str:
    """구성: 1 광고 / 2 요약 / 3 버튼 / 4 본문1 / 5 썸네일 주석 / 6 버튼 / 7 광고 / 8 본문2"""
    k = _esc(product.get("title") or "추천 제품")
    summary = _esc(product.get("summary") or "")
    bh = _get_button_html(url)
    core = _render_core_section(product, url)

    body1 = (
        "<h2>요약</h2>"
        f"<p>{summary}</p>"
        f"{bh}"
        "<h2>본문 1</h2>"
        "<p>실사용 관점에서 자주 쓰는 기능과 관리 난도를 먼저 확인하면 선택이 쉬워집니다. "
        "공간·소음·예산을 기준으로 필요한 수준만 고르는 것이 핵심입니다.</p>"
    )
    thumb_comment = "<!-- 썸네일 자리: 안정화되면 이미지 삽입 예정 -->"
    body2 = (
        f"{bh}"
        f"{AD_SHORTCODE or ''}"
        "<h2>본문 2</h2>"
        "<p>성능·관리·비용의 균형을 표로 정리하고, 내 환경에 맞는 기준을 체크하세요. "
        "초기구매가뿐 아니라 유지비(전기/소모품/시간)까지 합산하면 체감 만족도를 예측할 수 있습니다.</p>"
        f"{core}"
    )

    article = "\n".join([
        _css_headings(),
        '<div class="ax">',
        (AD_SHORTCODE or ""),  # 1) 내부광고(상)
        body1,                 # 2) 요약 + 3) 버튼 + 4) 본문1
        thumb_comment,         # 5) 썸네일(주석)
        body2,                 # 6) 버튼(상단에 이미 포함) + 7) 내부광고 + 8) 본문2(+core)
        '</div>'
    ])
    return _ensure_min_chars(article, 1500)

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
    content_html = _assemble_article(prod, url)

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
