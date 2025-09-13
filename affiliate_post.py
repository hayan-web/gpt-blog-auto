# -*- coding: utf-8 -*-
"""
affiliate_post.py — 쿠팡 글 자동 발행
- rich_templates.build_affiliate_content 사용(있으면)
- 버튼 모양/위치 절대 불변: 기존 _button_html/BUTTON_PRIMARY 그대로 호출(없으면 로컬 폴백)
- 본문은 공백 제외 1500자 이상 보장(자연스러운 보강 섹션 자동 추가)
- 키워드는 golden_shopping_keywords.csv에서 사용 → 회전(rotated) + 사용 로그 기록
- GitHub Actions에서 슬롯별로 스크립트를 개별 실행하므로,
  이 파일은 ENV 'AFFILIATE_TIME_KST' 한 개만 소비한다(내부 반복 X).
"""

from __future__ import annotations
import os, csv, json, re, html, random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional
import requests
from dotenv import load_dotenv

load_dotenv()

# ====== 옵션: rich 템플릿 사용 가능 여부 ======
HAVE_RICH = False
try:
    from rich_templates import build_affiliate_content  # 새 파일
    HAVE_RICH = True
except Exception:
    HAVE_RICH = False

# ====== ENV ======
WP_URL = (os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER = os.getenv("WP_USER") or ""
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD") or ""
VERIFY_TLS = (os.getenv("WP_TLS_VERIFY") or "true").lower() != "false"

POST_STATUS = (os.getenv("POST_STATUS") or "future").strip()
AFFILIATE_CATEGORY = (os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip() or "쇼핑"
DISCLOSURE_TEXT = os.getenv("DISCLOSURE_TEXT") or ""

USAGE_DIR = os.getenv("USAGE_DIR") or ".usage"
USED_SHOP = os.path.join(USAGE_DIR, "used_shopping.txt")

REQUIRE_COUPANG_API = (os.getenv("REQUIRE_COUPANG_API") or "0").strip() in ("1", "true", "yes", "on")

# 파일 경로
P_GOLD = "golden_shopping_keywords.csv"

# ====== 공통 유틸 ======
REQ_HEADERS = {
    "User-Agent": os.getenv("USER_AGENT") or "gpt-blog-auto/aff-2.0",
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

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
    """첫 데이터 행을 끝으로 회전. 헤더는 유지."""
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

def _ensure_min_chars(body_html: str, min_chars: int = 1500) -> str:
    """공백 제외 1500자 이상이 되도록 자연스러운 보강 섹션을 추가."""
    def _nchars(x: str) -> int:
        return len(re.sub(r"\s+", "", _strip_tags(x)))
    if _nchars(body_html) >= min_chars:
        return body_html

    fillers = [
        "<h3>구매 체크리스트</h3><p>내 환경(공간, 소음 허용치, 예산)을 먼저 정의한 뒤, 꼭 필요한 기능부터 우선순위를 매기세요. 성능을 올리면 관리 난도와 비용이 같이 오르는지 확인하는 것이 중요합니다.</p>",
        "<h3>활용 팁</h3><p>처음에는 기본 모드만 충분히 익히고, 실제 생활 패턴에서 자주 쓰는 상황에 맞춰 보조 기능을 하나씩 추가해보세요. 유지관리 주기를 캘린더에 기록하면 번거로움이 크게 줄어듭니다.</p>",
        "<h3>유지관리 비용 가이드</h3><p>초기 구매가뿐 아니라 소모품 교체, 전기요금, 세척에 드는 시간까지 포함해 총비용을 계산해보면 선택이 훨씬 쉬워집니다.</p>",
        "<h3>자주 묻는 질문(추가)</h3><p><strong>Q.</strong> 사양이 높을수록 무조건 좋은가요?<br><strong>A.</strong> 과사양은 비용과 관리부담을 키울 수 있습니다. 사용 목적과 공간 규모에 맞는 균형이 핵심입니다.</p>",
    ]
    buf = body_html
    i = 0
    while _nchars(buf) < min_chars and i < len(fillers):
        buf += f"\n{fillers[i]}"
        i += 1

    j = 0
    base = ("실사용 기준으로 핵심 기능부터 점검하세요. 성능-관리-비용의 균형이 맞을 때 만족도가 높습니다. "
            "필요 이상으로 스펙을 올리기보다, 자주 쓰는 상황에서 체감되는 요소를 구체적으로 비교하면 결정이 빨라집니다.")
    while _nchars(buf) < min_chars and j < 8:
        buf += f"\n<p class='aff-note'>{base}</p>"
        j += 1
    return buf

# ====== WP ======
def _ensure_term(kind: str, name: str) -> int:
    r = requests.get(
        f"{WP_URL}/wp-json/wp/v2/{kind}",
        params={"search": name, "per_page": 50, "context": "edit"},
        auth=(WP_USER, WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=15, headers=REQ_HEADERS
    )
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip() == name:
            return int(it["id"])
    r = requests.post(
        f"{WP_URL}/wp-json/wp/v2/{kind}",
        json={"name": name},
        auth=(WP_USER, WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=15, headers=REQ_HEADERS
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
        auth=(WP_USER, WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=20, headers=REQ_HEADERS
    )
    r.raise_for_status()
    return r.json()

# ====== 시간대/슬롯 ======
def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

def _slot_to_utc(kst_hm: str) -> str:
    """'HH:MM' KST -> 다음 해당 시각(미래)의 UTC ISO"""
    hh, mm = [int(x) for x in kst_hm.split(":")]
    now = _now_kst()
    tgt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if tgt <= now:
        tgt += timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ====== 버튼: 원형 유지(있으면 그대로), 없으면 임시 폴백만 사용 ======
def _button_html_local(url: str, label: str = "바로 보기") -> str:
    u = html.escape(url)
    l = html.escape(label)
    return (
        f'<div class="rt-btn-wrap" style="margin:16px 0;display:flex;gap:12px;flex-wrap:wrap">'
        f'<a href="{u}" target="_blank" rel="nofollow sponsored noopener" '
        f'style="display:inline-block;padding:12px 18px;border-radius:10px;'
        f'background:#111;color:#fff;text-decoration:none;font-weight:700">{l}</a>'
        f'</div>'
    )

def _get_button_html(url: str) -> str:
    """프로젝트에 _button_html/BUTTON_PRIMARY가 있으면 그대로 사용, 없으면 로컬 폴백."""
    try:
        # 기존 프로젝트 스타일 유지
        return _button_html(url, BUTTON_PRIMARY)  # type: ignore  # noqa: F821
    except Exception:
        try:
            # 일부 프로젝트는 텍스트 인자를 직접 받는 경우가 있음
            return _button_html(url, "바로 보기")  # type: ignore  # noqa: F821
        except Exception:
            # 로컬 폴백
            label = (os.getenv("BUTTON_TEXT") or "바로 보기").strip() or "바로 보기"
            return _button_html_local(url, label)

# ====== 링크 해결(딥링크 or 검색 URL) ======
def coupang_search_url(query: str) -> str:
    from urllib.parse import quote_plus
    return f"https://search.shopping.coupang.com/search?component=&q={quote_plus(query)}&channel=rel"

def resolve_affiliate_url(product_title: str, deep_link_func=None) -> str:
    """
    REQUIRE_COUPANG_API=1 이고 deep_link_func가 유효하면 딥링크 시도, 아니면 검색 URL 폴백.
    """
    if REQUIRE_COUPANG_API and callable(deep_link_func):
        try:
            url = deep_link_func(product_title)
            if url and isinstance(url, str):
                return url
        except Exception:
            pass
    return coupang_search_url(product_title)

# ====== 콘텐츠 생성 ======
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
        "summary": f"{k} 선택 시, 성능-관리-비용 균형을 빠르게 점검할 수 있도록 핵심만 정리합니다.",
    }

def _render_legacy(product: Dict, url: str) -> str:
    """rich_templates가 없을 때의 예비 렌더러(버튼은 동일 호출)."""
    k = _esc(product.get("title") or "추천 제품")
    disc = _esc(DISCLOSURE_TEXT)
    bh = _get_button_html(url)

    table_html = """
<table class="aff-table">
  <thead><tr><th>항목</th><th>확인 포인트</th><th>비고</th></tr></thead>
  <tbody>
    <tr><td>성능</td><td>공간/목적 대비 충분한지</td><td>과투자 방지</td></tr>
    <tr><td>관리</td><td>세척·보관·소모품</td><td>난도/주기</td></tr>
    <tr><td>비용</td><td>구매가 + 유지비</td><td>시즌 특가</td></tr>
  </tbody>
</table>
""".strip()

    body = f"""
<p class="aff-disclosure"><strong>{disc}</strong></p>
<h2 class="aff-sub">{k} 한 눈에 보기</h2>
<p>{k}를 중심으로 핵심만 간단히 정리했어요. 요약 → 선택 기준 → 팁 → 장단점 순서예요.</p>
<hr class="aff-hr">
{bh}
<h3>선택 기준 3가지</h3>
<ul>
  <li>성능: 내 공간/목적 대비 충분한지</li>
  <li>관리: 세척/보관/소모품 주기와 난도</li>
  <li>비용: 초기구매 + 유지비(전기/소모품)</li>
</ul>
{table_html}
<h3>활용 팁</h3>
<p>처음엔 기본 모드만 익히고, 자주 쓰는 상황을 파악한 뒤 보조 기능을 단계적으로 추가하세요. 관리 주기를 달력에 기록하면 번거로움이 크게 줄어요.</p>
<h3>장단점 요약</h3>
<ul>
  <li><b>장점:</b> 편의성, 시간 절약, 일관된 성능</li>
  <li><b>단점:</b> 공간/소음/전력 등의 제약 발생 가능</li>
</ul>
{bh}
""".strip()

    return _ensure_min_chars(body, min_chars=1500)

def _render_rich(product: Dict, url: str) -> str:
    """
    중요: 버튼 HTML은 '정의 이후'에 생성되도록 이 함수 안에서 호출.
    절대 버튼 스타일/위치를 바꾸지 않기 위해 기존 _button_html / BUTTON_PRIMARY 그대로 사용.
    """
    bh = _get_button_html(url)

    if HAVE_RICH:
        html_body = build_affiliate_content(
            product=product,
            button_html=bh,
            disclosure_text=DISCLOSURE_TEXT if DISCLOSURE_TEXT else None,
        )
    else:
        html_body = _render_legacy(product, url)

    return _ensure_min_chars(html_body, min_chars=1500)

# ====== 키워드 픽/회전 ======
def _pick_keyword() -> Optional[str]:
    pool = _read_col_csv(P_GOLD)
    if not pool:
        return None
    return pool[0]  # 항상 맨 앞 키워드 사용

def _rotate_after_use():
    _rotate_csv_head_to_tail(P_GOLD)
    print("[ROTATE] rotated")

# ====== 메인 ======
def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    # Actions에서 슬롯별로 스크립트를 '여러 번' 호출 → 여기서는 단일 슬롯만 처리
    slot = (os.getenv("AFFILIATE_TIME_KST") or "12:00").strip()
    print(f"[AFFILIATE] slot={slot}")

    kw = _pick_keyword()
    if not kw:
        print("[AFFILIATE] SKIP: no keyword")
        return

    # 링크 URL: 딥링크 함수가 있다면 resolve_affiliate_url로 연결, 기본은 검색 URL
    url = resolve_affiliate_url(kw)

    prod = _build_product_skeleton(kw)
    content_html = _render_rich(prod, url)   # 버튼/위치 불변 + 1500자 보장

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
