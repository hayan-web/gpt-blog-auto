# -*- coding: utf-8 -*-
"""
affiliate_post.py — 쿠팡 글 자동 발행 (기사형, 내부광고 포함)
- 섹션: 1) 내부광고 2) 요약 3) 버튼 4) 본문1 5) 썸네일 주석 6) 버튼 7) 내부광고 8) 본문2
- 버튼 모양은 기존 함수 유지, 중앙정렬 래퍼만 추가
- 1500자 보강: FAQ/체크리스트/가이드로 자연 보강(과잉 반복 금지)
- 쿠팡 링크: 검색 결과 딥링크 우선, 실패 시 일반 검색 URL
"""

from __future__ import annotations
import os, csv, json, re, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional
import requests
from dotenv import load_dotenv

# 딥링크 유틸 (검색 페이지 전용)
from coupang_api import deeplink_for_search as coupang_deeplink, coupang_search_url

load_dotenv()

# ====== Rich 템플릿(선택) ======
HAVE_RICH = False
try:
    from rich_templates import build_affiliate_content
    HAVE_RICH = True
except Exception:
    HAVE_RICH = False

# ====== ENV ======
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
VERIFY_TLS=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"

POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()
AFFILIATE_CATEGORY=(os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip() or "쇼핑"

# 내부 광고(숏코드 등)
AD_SHORTCODE = (os.getenv("AD_SHORTCODE") or "").strip()            # 상단 광고
AD_INSERT_MIDDLE = (os.getenv("AD_INSERT_MIDDLE") or "").strip()    # 중간/하단 광고 (없으면 상단과 동일 코드 사용)

DISCLOSURE_TEXT=os.getenv("DISCLOSURE_TEXT") or ""

USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_SHOP=os.path.join(USAGE_DIR,"used_shopping.txt")

REQUIRE_COUPANG_API=(os.getenv("REQUIRE_COUPANG_API") or "0").strip().lower() in ("1","true","yes","on")

P_GOLD="golden_shopping_keywords.csv"

REQ_HEADERS={
    "User-Agent": os.getenv("USER_AGENT") or "gpt-blog-auto/aff-2.2",
    "Accept":"application/json",
    "Content-Type":"application/json; charset=utf-8"
}

# ====== 스타일(박스 X, 소제목만 살짝) ======
def _css():
    return """
<style>
.aff { line-height:1.8; letter-spacing:-.01em }
.aff h2{font-size:1.6em;margin:1.2em 0 .5em;font-weight:800;letter-spacing:-.02em}
.aff h3{font-size:1.15em;margin:1.0em 0 .4em;font-weight:700}
.aff ul{padding-left:1.2em}
.aff table{width:100%;border-collapse:collapse;margin:.6em 0}
.aff table th,.aff table td{border:1px solid #e5e7eb;padding:.55em .6em;text-align:left}
</style>
""".strip()

# ====== 유틸 ======
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
    """자연스러운 섹션만 추가(FAQ/체크리스트/가이드). 과도 반복 금지."""
    if _nchars_no_space(body_html) >= min_chars:
        return body_html

    fillers = [
        "<h3>구매 체크리스트</h3><ul><li>사용 공간/용도 정하기</li><li>관리 난도(세척/보관/소모품)</li><li>총비용(구매가+유지비)</li></ul>",
        "<h3>활용 가이드</h3><p>기본 기능을 먼저 익힌 뒤, 자주 쓰는 장면에 맞춰 보조 기능을 단계적으로 추가하세요.</p>",
        "<h3>FAQ</h3><p><b>Q.</b> 사양이 높을수록 좋은가요?<br><b>A.</b> 목적 대비 과사양은 비용과 관리 부담을 키웁니다. 실제 사용 시나리오와 균형이 핵심입니다.</p>",
    ]
    buf = body_html
    for add in fillers:
        if _nchars_no_space(buf) >= min_chars: break
        if add not in buf:
            buf += "\n" + add

    # 여전히 부족하면 한 번만 노트 추가
    if _nchars_no_space(buf) < min_chars:
        buf += "\n<p>실사용 기준으로 기능-관리-비용 균형을 확인하면 선택이 빨라집니다.</p>"

    return buf

# ====== WP ======
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

# ====== 시간대 ======
def _now_kst(): 
    return datetime.now(ZoneInfo("Asia/Seoul"))

def _slot_to_utc(kst_hm:str)->str:
    hh,mm = [int(x) for x in kst_hm.split(":")]
    now=_now_kst()
    tgt=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ====== 버튼: 기존 모양 유지 + 중앙 래퍼 ======
def _button_html_local(url: str, label: str = "바로 보기") -> str:
    u = html.escape(url); l = html.escape(label or "바로 보기")
    return (f'<a href="{u}" target="_blank" rel="nofollow sponsored noopener" '
            'style="display:inline-block;padding:12px 18px;border-radius:10px;'
            'background:#111;color:#fff;text-decoration:none;font-weight:700">'
            f'{l}</a>')

def _get_button_core(url: str) -> str:
    try:
        return _button_html(url, BUTTON_PRIMARY)  # type: ignore  # noqa: F821
    except Exception:
        try:
            return _button_html(url, "바로 보기")  # type: ignore  # noqa: F821
        except Exception:
            label = (os.getenv("BUTTON_TEXT") or "바로 보기").strip() or "바로 보기"
            return _button_html_local(url, label)

def _center_wrap(html_btn: str) -> str:
    return f'<div style="text-align:center;margin:16px 0">{html_btn}</div>'

def _get_button_html(url: str) -> str:
    return _center_wrap(_get_button_core(url))

# ====== 링크 ======
def resolve_affiliate_url(keyword: str) -> str:
    # 검색 페이지 딥링크 우선
    try:
        if REQUIRE_COUPANG_API:
            u = coupang_deeplink(keyword)
            if isinstance(u, str) and u:
                return u
    except Exception:
        pass
    # 폴백
    return coupang_search_url(keyword)

# ====== 콘텐츠 ======
def _build_product_skeleton(keyword:str)->Dict:
    k = keyword.strip()
    return {
        "title": k,
        "summary": f"{k} 선택 시, 성능·관리·비용의 균형을 빠르게 점검할 수 있도록 핵심만 정리합니다.",
    }

def _render_legacy(product:Dict, url:str)->str:
    k = _esc(product.get("title") or "추천 제품")
    summary = _esc(product.get("summary") or "")
    btn = _get_button_html(url)

    ad_top = AD_SHORTCODE or ""
    ad_mid = AD_INSERT_MIDDLE or AD_SHORTCODE or ""

    parts = [
        _css(),
        '<div class="aff">',
        # 1) 내부광고(상단)
        ad_top,
        # 2) 요약
        "<h2>요약</h2>",
        f"<p>{summary}</p>",
        # 3) 버튼
        btn,
        # 4) 본문1 (짧게)
        "<h2>핵심 한 단락</h2>",
        "<p>실사용 장면에서 가장 자주 쓰는 기능이 무엇인지, 관리 난도(세척·보관·소모품)를 감당할 수 있는지를 먼저 확인하세요.</p>",
        # 5) 썸네일(안정화되면 추가) → 자리만 유지
        "<!-- 썸네일 자리: 안정화 후 이미지 삽입 예정 -->",
        # 6) 버튼
        btn,
        # 7) 내부광고(중간/하단)
        ad_mid,
        # 8) 본문2(나머지)
        "<h2>상세 비교</h2>",
        "<table><thead><tr><th>항목</th><th>확인 포인트</th><th>비고</th></tr></thead>"
        "<tbody>"
        "<tr><td>성능</td><td>공간/목적 대비 충분</td><td>과투자 방지</td></tr>"
        "<tr><td>관리</td><td>세척·보관·소모품</td><td>난도/주기</td></tr>"
        "<tr><td>비용</td><td>구매가 + 유지비</td><td>시즌 특가</td></tr>"
        "</tbody></table>",
        "</div>",
    ]
    return _ensure_min_chars("\n".join(p for p in parts if p), 1500)

def _render_rich(product:Dict, url:str)->str:
    """rich_templates가 있다면 같은 섹션 배치를 보장하도록 인자 전달."""
    btn = _get_button_html(url)
    ad_top = AD_SHORTCODE or ""
    ad_mid = AD_INSERT_MIDDLE or AD_SHORTCODE or ""

    if HAVE_RICH:
        html_body = build_affiliate_content(
            product=product,
            button_html=btn,
            disclosure_text=DISCLOSURE_TEXT or None,
            ad_top=ad_top or None,
            ad_middle=ad_mid or None,
            css_override=_css(),
            layout_order=["ad_top","summary","button","body_short","thumb","button","ad_middle","body_long"],
        )
    else:
        html_body = _render_legacy(product, url)

    return _ensure_min_chars(html_body, 1500)

# ====== 키워드 ======
def _pick_keyword()->Optional[str]:
    pool=_read_col_csv(P_GOLD)
    return pool[0] if pool else None

def _rotate_after_use():
    _rotate_csv_head_to_tail(P_GOLD)
    print("[ROTATE] rotated")

# ====== 메인 ======
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
    content_html = _render_rich(prod, url)

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
