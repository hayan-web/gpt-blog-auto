# -*- coding: utf-8 -*-
"""
affiliate_post.py — 매일 13:00 KST 쿠팡 글 1건 예약 발행 (폴백 완비)

동작 개요:
1) 오늘의 키워드 선택 (keywords.csv의 첫/무작위 등 자유)
2) products_seed.csv에서 해당 키워드의 상품 목록 읽기
   - 없거나 비어있으면:
     (A) COUPANG_ACCESS_KEY/SECRET_KEY 있으면 -> search API로 실시간 수집
     (B) 없고 REQUIRE_COUPANG_API=false면 -> 쿠팡 검색페이지 정적 링크로 최소 1건 폴백
     (C) REQUIRE_COUPANG_API=true면 -> 스킵
3) (가능하면) 딥링크 생성 (실패해도 REQUIRE_COUPANG_API=false면 원본 URL로 발행)
4) 워드프레스에 13:00 KST로 예약 발행

필요 ENV:
- WP_URL, WP_USER, WP_APP_PASSWORD
- AFFILIATE_CATEGORY="쇼핑" (없으면 자동 생성)
- AFFILIATE_TAGS="쿠팡,파트너스,추천"
- AFFILIATE_TIME_KST="13:00"
- PRODUCTS_SEED_CSV="products_seed.csv"
- COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, (선택) COUPANG_CHANNEL_ID
- COUPANG_SUBID_PREFIX="auto_wp_"
- REQUIRE_COUPANG_API="false"|"true"
- DISCLOSURE_TEXT="이 포스팅은 쿠팡 파트너스 활동의 일환으로..."
"""

import os, csv, re, json, random, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv

# ---- 로드 .env ----
load_dotenv()

# ---- ENV ----
WP_URL = (os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER = os.getenv("WP_USER") or ""
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY = (os.getenv("WP_TLS_VERIFY") or "true").lower() != "false"

AFFILIATE_CATEGORY = (os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip()
AFFILIATE_TAGS = [t.strip() for t in (os.getenv("AFFILIATE_TAGS") or "쿠팡,파트너스,추천").split(",") if t.strip()]
DISCLOSURE_TEXT = os.getenv("DISCLOSURE_TEXT") or "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."

AFFILIATE_TIME_KST = (os.getenv("AFFILIATE_TIME_KST") or "13:00").strip()
PRODUCTS_SEED_CSV = os.getenv("PRODUCTS_SEED_CSV") or "products_seed.csv"

COUPANG_ACCESS_KEY = os.getenv("COUPANG_ACCESS_KEY") or ""
COUPANG_SECRET_KEY = os.getenv("COUPANG_SECRET_KEY") or ""
COUPANG_CHANNEL_ID = os.getenv("COUPANG_CHANNEL_ID") or None
COUPANG_SUBID_PREFIX = os.getenv("COUPANG_SUBID_PREFIX") or "auto_wp_"
REQUIRE_COUPANG_API = (os.getenv("REQUIRE_COUPANG_API") or "false").lower() == "true"

DEFAULT_CATEGORY = (os.getenv("DEFAULT_CATEGORY") or AFFILIATE_CATEGORY or "정보").strip()
DEFAULT_TAGS = [t.strip() for t in (os.getenv("DEFAULT_TAGS") or "쿠팡,추천,리뷰").split(",") if t.strip()]

KEYWORDS_CSV = os.getenv("KEYWORDS_CSV") or "keywords.csv"
POST_STATUS = (os.getenv("POST_STATUS") or "future").strip()  # 보통 'future'

# ---- 외부 헬퍼 ----
from coupang_deeplink import create_deeplinks
from coupang_search import search_products

# ---- 유틸 ----
def _log(s: str):
    print(s, flush=True)

def _now_kst() -> datetime:
    return datetime.now(ZoneInfo("Asia/Seoul"))

def next_time_kst_utc_str(hhmm: str) -> str:
    """KST 특정 시각 예약: 오늘 그 시각이 이미 지났으면 내일 같은 시각으로."""
    now = _now_kst()
    try:
        hh, mm = [int(x) for x in hhmm.split(":")]
    except Exception:
        hh, mm = 13, 0
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target = target + timedelta(days=1)
    # WP는 date_gmt로 UTC 시각을 받는다
    utc_dt = target.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S")

def read_keywords_first(path: str) -> str:
    """keywords.csv 한 줄에서 첫 키워드 사용(비어있으면 랜덤 seed)"""
    if not os.path.exists(path):
        return "추천 상품"
    with open(path, "r", encoding="utf-8") as f:
        line = f.readline().strip()
    parts = [x.strip() for x in line.split(",") if x.strip()]
    return parts[0] if parts else "추천 상품"

def _resolve_seed_csv() -> str:
    # cleaned 가 있으면 우선 사용
    if os.path.exists("products_seed.cleaned.csv"):
        return "products_seed.cleaned.csv"
    return PRODUCTS_SEED_CSV

def read_seed_for_keyword(path: str, keyword: str, max_n: int = 3) -> List[Dict]:
    rows: List[Dict] = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for r in reader:
            if (r.get("keyword") or "").strip() == keyword.strip():
                rows.append({
                    "keyword": r.get("keyword","").strip(),
                    "product_name": r.get("product_name","").strip(),
                    "raw_url": r.get("raw_url","").strip(),
                    "pros": r.get("pros","").strip(),
                    "cons": r.get("cons","").strip(),
                })
    # 우선 순위: 위에서부터 (품질체크가 이미 정렬해줬다고 가정)
    return rows[:max_n]

def validate_urls(rows: List[Dict]) -> List[Dict]:
    out = []
    for r in rows:
        url = (r.get("raw_url") or "").strip()
        name = (r.get("product_name") or "").strip()
        if not (url and name):
            continue
        if not re.match(r"^https?://", url):
            continue
        out.append(r)
    return out

def _ensure_category(name: str) -> int:
    """카테고리 이름으로 ID 확보(없으면 생성)"""
    if not name:
        name = DEFAULT_CATEGORY or "정보"
    # 검색
    r = requests.get(f"{WP_URL}/wp-json/wp/v2/categories", params={"search": name, "per_page": 50},
                     auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    for item in r.json():
        if (item.get("name") or "").strip() == name:
            return int(item["id"])
    # 생성
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/categories",
                      json={"name": name}, auth=(WP_USER, WP_APP_PASSWORD),
                      verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    return int(r.json()["id"])

def _ensure_tags(tag_names: List[str]) -> List[int]:
    ids: List[int] = []
    for t in tag_names or []:
        if not t:
            continue
        # 검색
        r = requests.get(f"{WP_URL}/wp-json/wp/v2/tags", params={"search": t, "per_page": 50},
                         auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
        r.raise_for_status()
        tag_id = None
        for item in r.json():
            if (item.get("name") or "").strip() == t:
                tag_id = int(item["id"]); break
        # 생성
        if tag_id is None:
            r = requests.post(f"{WP_URL}/wp-json/wp/v2/tags",
                              json={"name": t}, auth=(WP_USER, WP_APP_PASSWORD),
                              verify=WP_TLS_VERIFY, timeout=15)
            r.raise_for_status()
            tag_id = int(r.json()["id"])
        ids.append(tag_id)
    return ids

def _cta_text() -> str:
    pool = [os.getenv("BUTTON_TEXT","").strip()] if os.getenv("BUTTON_TEXT") else []
    pool += ["최저가 확인하기", "상세 보기", "혜택 보러가기", "지금 확인"]
    return random.choice([p for p in pool if p]) or "상세 보기"

def compose_html(keyword: str, products: List[Dict]) -> Tuple[str, str]:
    """title, html"""
    title = f"{keyword} 추천 베스트"
    items_html = []
    for p in products:
        name = p.get("product_name") or p.get("productName") or "추천 상품"
        deeplink = p.get("deeplink") or p.get("raw_url","")
        pros = p.get("pros") or ""
        cons = p.get("cons") or ""
        btn = f"<a href='{deeplink}' target='_blank' rel='sponsored nofollow noopener' style='display:inline-block;padding:12px 18px;border-radius:12px;background:#0f172a;color:#fff;text-decoration:none;'>{_cta_text()}</a>"
        block = f"""
        <div style="margin:20px 0;padding:16px;border:1px solid #e5e7eb;border-radius:12px;">
          <h3 style="margin:0 0 8px 0;font-size:18px;">{name}</h3>
          <ul style="margin:0 0 8px 18px;">
            {"<li>"+pros+"</li>" if pros else ""}
            {"<li>"+cons+"</li>" if cons else ""}
          </ul>
          <p>{btn}</p>
        </div>
        """
        items_html.append(block)
    body = f"""
    <p style="color:#64748b;font-size:14px;">{DISCLOSURE_TEXT}</p>
    {''.join(items_html)}
    """
    return title, body

def wp_create_or_schedule(title: str, html: str, category_name: str, tag_names: List[str], when_kst: str) -> Dict:
    cat_id = _ensure_category(category_name or DEFAULT_CATEGORY)
    tag_ids = _ensure_tags((tag_names or []) + (AFFILIATE_TAGS or DEFAULT_TAGS))
    payload = {
        "title": title,
        "content": html,
        "status": POST_STATUS,  # usually 'future'
        "categories": [cat_id],
        "tags": tag_ids,
        "comment_status": "closed",
        "ping_status": "closed",
        "date_gmt": next_time_kst_utc_str(when_kst),
    }
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                      auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20)
    r.raise_for_status()
    return r.json()

def enrich_with_deeplink(rows: List[Dict]) -> List[Dict]:
    """origin URL → deeplink. 실패 시 raw_url 유지 (REQUIRE_COUPANG_API=false일 때만)."""
    if not rows:
        return rows
    origin_urls = [(r.get("raw_url") or "").strip() for r in rows]
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY):
        if REQUIRE_COUPANG_API:
            _log("[AFFILIATE] SKIP: 쿠팡 API 키 없음 (REQUIRE_COUPANG_API=true)")
            return []
        _log("[AFFILIATE] WARN: 쿠팡 API 키 없음 -> raw_url 사용")
        return rows
    try:
        sub_id = f"{COUPANG_SUBID_PREFIX}{_now_kst().strftime('%Y%m%d_%H%M')}"
        mapping = create_deeplinks(origin_urls, COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY,
                                   sub_id=sub_id, channel_id=COUPANG_CHANNEL_ID)
        _log(f"[AFFILIATE] deeplink OK: {len(mapping)}/{len(origin_urls)}")
        enriched = []
        for r, url in zip(rows, origin_urls):
            enriched.append({**r, "deeplink": mapping.get(url, url)})
        return enriched
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        if REQUIRE_COUPANG_API:
            _log(f"[AFFILIATE] SKIP: deeplink 실패 (REQUIRE_COUPANG_API=true) -> {msg}")
            return []
        _log(f"[AFFILIATE] WARN: deeplink 실패 -> raw_url 사용 ({msg})")
        return rows

def pick_keyword() -> Dict[str, str]:
    """키워드 선택과 카테고리/태그 묶음 반환"""
    kw = read_keywords_first(KEYWORDS_CSV)
    return {"keyword": kw, "category": AFFILIATE_CATEGORY or DEFAULT_CATEGORY, "tags": ",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    topic = pick_keyword()
    keyword = topic["keyword"]
    cat_name = topic["category"]
    tag_names = topic["tags"].split(",") if topic.get("tags") else []

    seed_path = _resolve_seed_csv()
    _log(f"[AFFILIATE] keyword='{keyword}', seed='{seed_path}'")

    # 1) 씨앗 읽기
    seed = read_seed_for_keyword(seed_path, keyword, max_n=3)
    seed = validate_urls(seed)

    # 2) 씨앗이 없으면 Fallback
    if not seed:
        _log("[AFFILIATE] INFO: seed CSV 비어 있음 -> 자동 검색/폴백 시도")
        # 2-A) 쿠팡 키 있으면 search API
        if COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY:
            try:
                items = search_products(keyword, COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, limit=5, sort="salesVolume")
                seed = [{
                    "keyword": keyword,
                    "product_name": it.get("productName",""),
                    "raw_url": it.get("productUrl",""),
                    "pros": "",
                    "cons": "",
                } for it in items if it.get("productUrl")]
                _log(f"[AFFILIATE] search API fallback -> {len(seed)}건")
            except Exception as e:
                _log(f"[AFFILIATE] WARN: search API 실패 -> {e}")
        # 2-B) 쿠팡 키 없고, API 필수도 아니면 정적 검색 URL 1건
        if not seed and not REQUIRE_COUPANG_API:
            kw_enc = re.sub(r"\s+", "+", keyword.strip())
            base = f"https://www.coupang.com/np/search?q={kw_enc}"
            seed = [{
                "keyword": keyword,
                "product_name": f"{keyword} 추천 모음",
                "raw_url": base,
                "pros": "",
                "cons": "",
            }]
            _log("[AFFILIATE] keyless fallback -> static search URL 1건")

    # 여전히 없으면 스킵
    if not seed:
        _log("[AFFILIATE] SKIP: 유효한 상품 없음 (seed/URL 검사 실패)")
        return 0

    # 3) 딥링크 시도 (실패 시 원본 유지)
    rows = enrich_with_deeplink(seed)
    if not rows:
        _log("[AFFILIATE] SKIP: 딥링크 조건 미충족(또는 REQUIRE_COUPANG_API=true)")
        return 0

    # 4) 본문 작성 및 예약
    title, html = compose_html(keyword, rows)
    res = wp_create_or_schedule(title, html, cat_name, tag_names, AFFILIATE_TIME_KST)
    print(json.dumps({
        "post_id": res.get("id"),
        "link": res.get("link"),
        "status": res.get("status"),
        "date_gmt": res.get("date_gmt"),
        "title": (res.get("title") or {}).get("rendered"),
    }, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    sys.exit(main())
