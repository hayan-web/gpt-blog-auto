# -*- coding: utf-8 -*-
"""
affiliate_post.py — 쿠팡 글 자동 발행 (기사형)
- rich_templates.build_affiliate_content 사용(있으면)
- 버튼 모양은 그대로, 외곽 래퍼로 '중앙정렬'만 적용
- 섹션 순서: 1) 내부광고 2) 요약 3) 버튼 4) 본문1 5) 썸네일(주석) 6) 버튼 7) 내부광고 8) 본문2
- 본문은 공백 제외 1500자 이상 보장(중복 금지 · 최대 3블록 보강)
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
WP_URL = (os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER = os.getenv("WP_USER") or ""
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD") or ""
VERIFY_TLS = (os.getenv("WP_TLS_VERIFY") or "true").lower() != "false"

POST_STATUS = (os.getenv("POST_STATUS") or "future").strip()
AFFILIATE_CATEGORY = (os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip() or "쇼핑"
DISCLOSURE_TEXT = os.getenv("DISCLOSURE_TEXT") or ""
AD_SHORTCODE = os.getenv("AD_SHORTCODE") or ""

USAGE_DIR = os.getenv("USAGE_DIR") or ".usage"
USED_SHOP = os.path.join(USAGE_DIR, "used_shopping.txt")

REQUIRE_COUPANG_API = (os.getenv("REQUIRE_COUPANG_API") or "0").strip().lower() in ("1", "true", "yes", "on")

P_GOLD = "golden_shopping_keywords.csv"

REQ_HEADERS = {
    "User-Agent": os.getenv("USER_AGENT") or "gpt-blog-auto/aff-2.2",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ===== 유틸 =====
def _esc(s: Optional[str]) -> str:
    return html.escape((s or "").strip())

def _ensure_usage():
    os.makedirs(USAGE_DIR, exist_ok=True)

def _mark_used(kw: str):
    _ensure_usage()
    with open(USED_SHOP, "a", encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw}\n")

def _read_col_csv(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    out: List[str] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        for i, row in enumerate(rd):
            if not row:
                continue
            if i == 0 and row[0].strip().lower() in ("keyword", "title"):
                continue
            s = row[0].strip()
            if s:
                out.append(s)
    return out

def _rotate_csv_head_to_tail(path: str):
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    if not rows or len(rows) < 2:
        return
    header, data = rows[0], rows[1:]
    if not data:
        return
    head = data.pop(0)
    data.append(head)
    with open(path, "w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(header)
        wr.writerows(data)

def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s or "")

def _nchars_no_space(html_text: str) -> int:
    return len(re.sub(r"\s+", "", _strip_tags(html_text)))

def _ensure_min_chars(body_html: str, min_chars: int = 1500) -> str:
    """
    공백 제외 최소 글자 보장.
    - 중복 금지
    - 최대 3블록 보강(억지 반복 방지)
    """
    if _nchars_no_space(body_html) >= min_chars:
        return body_html

    fillers = [
        "<h3>구매 체크리스트</h3><ul><li>내 환경(공간·소음·예산) 정의</li><li>필수 → 보조 기능 순</li><li>유지관리 주기 기록</li></ul>",
        "<h3>활용 팁</h3><p>초기에는 기본 기능만 충분히 익히고, 자주 쓰는 장면에 맞춰 보조 기능을 하나씩 추가하세요.</p>",
        "<h3>유지관리</h3><p>소모품 교체·세척 주기를 미리 정하고 캘린더에 반복 알림을 설정하면 번거로움이 크게 줄어듭니다.</p>",
        "<h3>FAQ</h3><p><b>Q.</b> 사양이 높을수록 좋은가요?<br><b>A.</b> 목적 대비 과사양은 비용·관리 부담이 큽니다. 균형이 핵심입니다.</p>",
    ]
    used = set()
    buf = body_html
    for b in fillers:
        if _nchars_no_space(buf) >= min_chars:
            break
        if b not in used and b not in buf:
            buf += "\n" + b
            used.add(b)
        if len(used) >= 3:  # 최대 3블록만 추가
            break

    return buf

# ===== CSS(레거시 렌더용, 박스 없음/중앙버튼만) =====
def _css_block() -> str:
    return """
<style>
.rt{line-height:1.75;letter-spacing:-.01em}
.rt h2{font-size:1.6em;margin:1.2em 0 .4em;font-weight:800;letter-spacing:-.02em}
.rt h3{font-size:1.15em;margin:1.0em 0 .35em;font-weight:700}
.rt ul{padding-left:1.2em}
.rt .rt-meta{font-size:.925em;color:#64748b;margin:.25em 0 1.0em}
.rt .rt-center{text-align:center;margin:16px 0}
.rt .rt-center a{display:inline-block;padding:12px 18px;border-radius:10px;background:#111;color:#fff;text-decoration:none;font-weight:700}
.rt table{width:100%;border-collapse:collapse;margin:.5em 0 1em}
.rt table th,.rt table td{border:1px solid #e5e7eb;padding:.6em .7em}
</style>
""".strip()

# ===== WP =====
def _ensure_term(kind: str, name: str) -> int:
    r = requests.get(
        f"{WP_URL}/wp-json/wp/v2/{kind}",
        params={"search": name, "per_page": 50, "context": "edit"},
        auth=(WP_USER, WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=15, headers=REQ_HEADERS,
    )
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip() == name:
            return int(it["id"])
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/{kind}",
        json={"name": name},
        auth=(WP_USER, WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=15, headers=REQ_HEADERS,
    )
    r.raise_for_status()
    return int(r.json()["id"])

def post_wp(title: str, content: str, when_gmt: str, category: str) -> dict:
    cat_id = _ensure_term("categories", category or AFFILIATE_CATEGORY)
    payload = {
        "title": title,
        "content": content,
        "status": POST_STATUS,
        "categories": [cat_id],
        "comment_status": "closed",
        "ping_status": "closed",
        "date_gmt": when_gmt,
    }
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/posts",
        json=payload,
        auth=(WP_USER, WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=20, headers=REQ_HEADERS,
    )
    r.raise_for_status()
    return r.json()

# ===== 시간대/슬롯 =====
def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

def _slot_to_utc(kst_hm: str) -> str:
    hh, mm = [int(x) for x in kst_hm.split(":")]
    now = _now_kst()
    tgt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if tgt <= now:
        tgt += timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ===== 버튼: 원형 유지 + 중앙 정렬 래퍼 =====
def _button_html_local(url: str, label: str = "바로 보기") -> str:
    u = html.escape(url)
    l = html.escape(label or "바로 보기")
    return (f'<a href="{u}" target="_blank" rel="nofollow sponsored noopener" '
            'style="display:inline-block;padding:12px 18px;border-radius:10px;'
            'background:#111;color:#fff;text-decoration:none;font-weight:700">'
            f'{l}</a>')

def _get_button_core(url: str) -> str:
    """
    가능한 모든 시그니처를 시도:
    - _button_html(url, BUTTON_PRIMARY)
    - _button_html(url, "바로 보기")
    - _button_html(url)
    없으면 로컬 폴백.
    """
    try:
        return _button_html(url, BUTTON_PRIMARY)  # type: ignore  # noqa: F821
    except Exception:
        try:
            return _button_html(url, "바로 보기")  # type: ignore  # noqa: F821
        except Exception:
            try:
                return _button_html(url)  # type: ignore  # noqa: F821
            except Exception:
                label = (os.getenv("BUTTON_TEXT") or "바로 보기").strip() or "바로 보기"
                return _button_html_local(url, label)

def _center_wrap(html_btn: str) -> str:
    # 모양은 그대로 두고 위치만 중앙으로
    return f'<div class="rt-center">{html_btn}</div>'

def _get_button_html(url: str) -> str:
    return _center_wrap(_get_button_core(url))

# ===== 링크 해결 =====
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

# ===== 콘텐츠 =====
def _build_product_skeleton(keyword: str) -> Dict:
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
        "summary": f"{k} 선택 시, 성능·관리·비용 균형을 빠르게 점검할 수 있도록 핵심만 정리합니다.",
    }

def _render_legacy(product: Dict, url: str) -> str:
    """rich_templates가 없을 때의 기사형 렌더(박스 X, 중앙 버튼 O)."""
    k = _esc(product.get("title") or "추천 제품")
    summary = _esc(product.get("summary") or "")
    bh = _get_button_html(url)
    disc = _esc(DISCLOSURE_TEXT)

    parts = [
        _css_block(),
        '<div class="rt">',
        (f'<p class="rt-meta">{disc}</p>' if disc else ''),
        (AD_SHORTCODE or ''),
        '<h2>요약</h2>',
        f'<p>{summary}</p>',
        bh,
        '<h2>한 눈에 보기</h2>',
        f'<p>{k}는 ‘필수 기능 우선’으로 쓰면 효용이 큽니다. 자주 쓰는 장면을 먼저 정의하고 그에 맞는 기능부터 선택하세요.</p>',
        # (썸네일 자리) — 안정화 후 주입
        # f'<figure class="rt-thumb"><img src="{thumb_url}" alt="{k} 썸네일"></figure>',
        bh,
        (AD_SHORTCODE or ''),
        '<h2>상세 리뷰</h2>',
        '<p>성능·관리·비용을 표와 사례로 정리하면 선택이 쉬워집니다.</p>',
        '<table><thead><tr><th>항목</th><th>확인 포인트</th><th>메모</th></tr></thead>'
        '<tbody>'
        '<tr><td>성능</td><td>공간/목적 대비 충분한지</td><td>과투자 방지</td></tr>'
        '<tr><td>관리</td><td>세척·보관·소모품 주기</td><td>난도/시간</td></tr>'
        '<tr><td>비용</td><td>구매가 + 유지비</td><td>시즌 특가</td></tr>'
        '</tbody></table>',
        '<h3>장점</h3><ul>'
        '<li>빠른 접근과 직관적 조작</li>'
        '<li>꾸준한 사용을 돕는 기본 성능</li>'
        '<li>필요할 때 보조 기능 확장</li>'
        '</ul>',
        '<h3>단점</h3><ul>'
        '<li>공간/소음/전력 등 환경 제약 가능</li>'
        '<li>옵션 추가에 따른 관리 난도 상승</li>'
        '</ul>',
        '</div>',
    ]
    body = "\n".join(p for p in parts if p)
    return _ensure_min_chars(body, 1500)

def _render_rich(product: Dict, url: str) -> str:
    bh = _get_button_html(url)  # 중앙 래퍼 포함
    if HAVE_RICH:
        # rich_templates가 ad_shortcode 인자를 지원하지 않는 경우까지 대비
        try:
            html_body = build_affiliate_content(
                product=product,
                button_html=bh,
                disclosure_text=DISCLOSURE_TEXT or None,
                ad_shortcode=AD_SHORTCODE or None,
            )
        except TypeError:
            html_body = build_affiliate_content(
                product=product,
                button_html=bh,
                disclosure_text=DISCLOSURE_TEXT or None,
            )
    else:
        html_body = _render_legacy(product, url)
    return _ensure_min_chars(html_body, 1500)

# ===== 키워드 =====
def _pick_keyword() -> Optional[str]:
    pool = _read_col_csv(P_GOLD)
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

if __name__ == "__main__":
    main()
