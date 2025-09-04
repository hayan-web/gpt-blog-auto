# -*- coding: utf-8 -*-
"""
affiliate_post.py (final with URL_CHECK_MODE)
- products_seed.(cleaned.)csv 에서 키워드에 맞는 상품 N개(기본 3) 선택
- URL 유효성 검사(HEAD, 필요 시 GET 폴백 / soft 모드) 후 쿠팡 파트너스 딥링크 생성
- 대가성 고지 + 비교 표(개수 표기) + 체크리스트/FAQ 본문 HTML 생성
- 제목을 "TOP N"으로 자동 맞춤 (실제 개수 기준)
- 카테고리/태그: 쿠팡 전용(AFFILIATE_*)을 우선 적용
- 예약 시각: AFFILIATE_TIME_KST (기본 13:00 KST) -> date_gmt 로 예약
- ALLOW_CREATE_TERMS=false 이면 카테고리/태그 자동 생성 시도 없이 있는 것만 사용

URL_CHECK_MODE:
  - strict:  HEAD(200~399)만 통과
  - soft  :  HEAD가 실패하면 GET로 재확인, 그래도 실패면 일단 통과(깃허브 러너 내성)
  - off   :  URL 검사 건너뜀
"""
import os, csv, requests
from typing import List, Dict, Optional
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

from coupang_deeplink import create_deeplinks

load_dotenv(override=False)

# ===== Env =====
WP_URL = os.getenv("WP_URL", "").rstrip("/")
WP_USER = os.getenv("WP_USER", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")

COUPANG_ACCESS_KEY = os.getenv("COUPANG_ACCESS_KEY")
COUPANG_SECRET_KEY = os.getenv("COUPANG_SECRET_KEY")
COUPANG_CHANNEL_ID = os.getenv("COUPANG_CHANNEL_ID") or None
COUPANG_SUBID_PREFIX = os.getenv("COUPANG_SUBID_PREFIX", "auto_wp_")

POST_STATUS = os.getenv("POST_STATUS", "future")  # publish | future

# 대가성 문구 기본값 (요청 적용)
DISCLOSURE_TEXT = os.getenv(
    "DISCLOSURE_TEXT",
    "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
)

# 기본(폴백) 카테고리/태그
DEFAULT_CATEGORY = (os.getenv("DEFAULT_CATEGORY") or "정보").strip()
DEFAULT_TAGS = [t.strip() for t in (os.getenv("DEFAULT_TAGS") or "쿠팡,추천,리뷰").split(",") if t.strip()]

# 쿠팡 전용 카테고리/태그 (우선 적용)
AFFILIATE_CATEGORY = (os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip()
AFFILIATE_TAGS = [t.strip() for t in (os.getenv("AFFILIATE_TAGS") or "쿠팡,파트너스,추천").split(",") if t.strip()]

# 예약 시각 (KST, HH:MM). 기본 13:00
AFFILIATE_TIME_KST = (os.getenv("AFFILIATE_TIME_KST") or "13:00").strip()

KEYWORDS_CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")

# 시드 파일: cleaned 우선
def _resolve_seed_csv() -> str:
    env_path = os.getenv("PRODUCTS_SEED_CSV")
    if env_path and os.path.exists(env_path):
        return env_path
    if os.path.exists("products_seed.cleaned.csv"):
        return "products_seed.cleaned.csv"
    return env_path or "products_seed.csv"

SEED_CSV = _resolve_seed_csv()

# 용어 자동 생성 허용 여부
ALLOW_CREATE_TERMS = (os.getenv("ALLOW_CREATE_TERMS", "true").lower() == "true")

# URL 검사 모드 (strict|soft|off)
URL_CHECK_MODE = (os.getenv("URL_CHECK_MODE", "soft").lower())

# ===== WordPress REST helpers =====
def wp_auth():
    import base64
    token = base64.b64encode(f"{WP_USER}:{WP_APP_PASSWORD}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}"}

def wp_post(path: str, json_body: dict):
    url = f"{WP_URL}/wp-json/wp/v2{path}"
    r = requests.post(url, headers={**wp_auth(), "Content-Type": "application/json"}, json=json_body, timeout=30)
    if not r.ok:
        raise RuntimeError(f"WP POST {path} failed: {r.status_code} {r.text[:300]}")
    return r.json()

def wp_get(path: str, params=None):
    url = f"{WP_URL}/wp-json/wp/v2{path}"
    r = requests.get(url, headers=wp_auth(), params=params or {}, timeout=30)
    if not r.ok:
        raise RuntimeError(f"WP GET {path} failed: {r.status_code} {r.text[:300]}")
    return r.json()

def ensure_terms(taxonomy: str, names: List[str]) -> List[int]:
    """
    taxonomy: 'categories' | 'tags'
    - 이름으로 검색하여 정확히 일치하면 id 사용
    - ALLOW_CREATE_TERMS=True 이고 없으면 생성 시도
    - 실패/미존재 시 해당 항목은 건너뛰기
    """
    ids = []
    for name in [n for n in (names or []) if n]:
        try:
            found = wp_get(f"/{taxonomy}", {"search": name, "per_page": 10})
            matched = [x for x in (found or []) if x.get("name") == name]
            if matched:
                ids.append(matched[0]["id"])
            elif ALLOW_CREATE_TERMS:
                created = wp_post(f"/{taxonomy}", {"name": name})
                if created and created.get("id"):
                    ids.append(created["id"])
        except Exception:
            pass
    return ids

# ===== Time helpers =====
def _parse_hhmm(s: str) -> Optional[tuple]:
    try:
        hh, mm = s.split(":")
        hh = max(0, min(23, int(hh)))
        mm = max(0, min(59, int(mm)))
        return hh, mm
    except Exception:
        return None

def next_time_kst_utc_str(hhmm: str) -> Optional[str]:
    if POST_STATUS != "future":
        return None
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    h_m = _parse_hhmm(hhmm) or (13, 0)
    target = now_kst.replace(hour=h_m[0], minute=h_m[1], second=0, microsecond=0)
    if now_kst >= target:
        target = target + timedelta(days=1)
    return target.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ===== Data loaders =====
def read_keywords_first(path: str) -> Dict:
    # CSV가 없거나 한 줄짜리 콤마 나열이면 맨 앞 키워드 사용
    if not os.path.exists(path):
        return {"keyword": "추천 상품", "category": AFFILIATE_CATEGORY, "tags": ",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if "\n" not in raw and "," in raw:
        terms = [x.strip() for x in raw.split(",") if x.strip()]
        kw = terms[0] if terms else "추천 상품"
        return {"keyword": kw, "category": AFFILIATE_CATEGORY, "tags": ",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row = rows[0] if rows else {"keyword": "추천 상품", "category": AFFILIATE_CATEGORY, "tags": ",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}
    if not row.get("keyword"):
        row["keyword"] = "추천 상품"
    return row

def read_seed_for_keyword(path: str, keyword: str, max_n: int = 3) -> List[Dict]:
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    selected = [r for r in rows if (r.get("keyword", "").strip() == keyword)][:max_n]
    if not selected:
        selected = rows[:max_n]
    return selected

# ===== URL validation (HEAD + optional GET; soft/off modes) =====
def validate_urls(rows: List[Dict]) -> List[Dict]:
    mode = URL_CHECK_MODE
    if mode == "off":
        return rows
    ok = []
    for r in rows:
        u = (r.get("raw_url") or "").strip()
        if not u:
            continue
        good = False
        try:
            res = requests.head(u, allow_redirects=True, timeout=10)
            good = 200 <= res.status_code < 400
            if not good and mode in ("soft",):
                # 일부 환경에서 HEAD 차단 → GET으로 재확인
                try:
                    rg = requests.get(u, allow_redirects=True, timeout=10, stream=True)
                    good = 200 <= rg.status_code < 400
                finally:
                    try:
                        rg.close()
                    except Exception:
                        pass
        except Exception:
            # soft 모드면 네트워크 실패여도 통과 (딥링크 API가 다시 검증)
            good = (mode == "soft")
        if good or mode == "soft":
            ok.append(r)
    return ok

# ===== Rendering =====
def render_disclosure(txt: str) -> str:
    return f"<div style='padding:12px;border:1px solid #eee;background:#fafafa;font-weight:600;margin-bottom:16px;'>{txt}</div>"

def render_table(products: List[Dict]) -> str:
    trs = []
    for p in products:
        name = p.get("product_name", "상품")
        pros = "<br/>".join([x.strip() for x in (p.get("pros") or "").split(";") if x.strip()] or [p.get("pros", "")])
        cons = "<br/>".join([x.strip() for x in (p.get("cons") or "").split(";") if x.strip()] or [p.get("cons", "")])
        btn = f"<a href='{p['deeplink']}' target='_blank' rel='sponsored noopener nofollow' style='display:inline-block;padding:10px 14px;border:1px solid #111;text-decoration:none;margin-top:6px;'>쿠팡 최저가 보기</a>"
        trs.append(f"<tr><td><strong>{name}</strong><br/>{btn}</td><td>{pros or '-'}</td><td>{cons or '-'}</td></tr>")
    return "<div class='table-wrap'><table><thead><tr><th>상품</th><th>장점</th><th>유의점</th></tr></thead><tbody>" + "".join(trs) + "</tbody></table></div>"

def render_post_html(title: str, intro: str, products: List[Dict]) -> str:
    body = [
        render_disclosure(DISCLOSURE_TEXT),
        f"<h2>{title} 핵심 요약</h2>",
        f"<p>{intro}</p>",
        f"<h2>비교 표 ({len(products)}개)</h2>",
        render_table(products),
        "<h2>구매 체크리스트</h2>",
        "<ul><li>예산/보증/환불 조건 확인</li><li>재고/배송 일정 확인</li></ul>",
        "<h2>FAQ</h2>",
        "<details><summary><strong>어떤 기준으로 골랐나요?</strong></summary><p>가성비/리뷰/기본 스펙을 우선 반영했습니다. 가격과 재고는 수시로 변동될 수 있습니다.</p></details>",
    ]
    return "\n".join(body)

# ===== Main =====
def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY):
        print("Coupang API 키가 없어 affiliate 포스트를 건너뜁니다.")
        return

    topic = read_keywords_first(KEYWORDS_CSV)
    keyword = topic.get("keyword") or "추천 상품"

    # 쿠팡 전용 카테고리/태그 우선
    cat_name = (topic.get("category") or AFFILIATE_CATEGORY or DEFAULT_CATEGORY).strip()
    tag_names = [t.strip() for t in (topic.get("tags") or "").split(",") if t.strip()]
    if AFFILIATE_TAGS:
        tag_names += AFFILIATE_TAGS
    else:
        tag_names += DEFAULT_TAGS

    # 시드 로드/유효성
    seed = read_seed_for_keyword(SEED_CSV, keyword, max_n=3)
    seed = validate_urls(seed)
    if not seed:
        print(f"{SEED_CSV}에 '{keyword}' 키워드의 유효한 상품이 없습니다. 종료.")
        return

    # 딥링크
    origin_urls = [r["raw_url"].strip() for r in seed]
    sub_id = f"{COUPANG_SUBID_PREFIX}{datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d_%H%M')}"
    mapping = create_deeplinks(
        origin_urls,
        COUPANG_ACCESS_KEY,
        COUPANG_SECRET_KEY,
        sub_id=sub_id,
        channel_id=COUPANG_CHANNEL_ID
    )

    enriched = []
    for r in seed:
        url = r["raw_url"].strip()
        enriched.append({**r, "deeplink": mapping.get(url, url)})

    # 제목: TOP N 자동화
    n = len(enriched)
    if n >= 2:
        title = f"{keyword} 추천 TOP {n} (쿠팡 파트너스)"
    else:
        title = f"{keyword} 추천 모음 (쿠팡 파트너스)"
    intro = f"{keyword} 고민 끝! 장단점/체크리스트/바로가기 링크까지 한 번에 살펴보세요."
    content_html = render_post_html(title, intro, enriched)

    # 카테고리/태그 id 확보
    cat_ids = ensure_terms("categories", [cat_name])
    tag_ids = ensure_terms("tags", list(dict.fromkeys(tag_names))[:10])

    payload = {
        "title": title,
        "content": content_html,
        "status": "publish" if POST_STATUS == "publish" else "future",
        "categories": cat_ids, "tags": tag_ids,
    }
    if POST_STATUS == "future":
        payload["date_gmt"] = next_time_kst_utc_str(AFFILIATE_TIME_KST)

    res = wp_post("/posts", payload)
    print({"post_id": res.get("id"), "link": res.get("link"), "status": res.get("status")})

if __name__ == "__main__":
    main()
