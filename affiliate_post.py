# -*- coding: utf-8 -*-
"""
affiliate_post.py — 쿠팡글 1건 예약(기본 13:00 KST)
- 키워드: golden_shopping_keywords.csv -> keywords_shopping.csv -> keywords.csv 순
- 키워드가 비어도 '계절 폴백'으로 반드시 1건 생성
- 본문: 사람스러운 1인칭 리뷰형(1200~1300자) + 섹션/불릿/CTA/가격·가성비/장단점/추천대상
- 태그: 키워드 한 개만(쿠팡/파트너스/최저가/할인 등 금지)
- 딥링크: 쿠팡 파트너스 API 있으면 변환, 없으면 검색URL 폴백
"""

import os, re, csv, json, sys, html, urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional

import requests
from dotenv import load_dotenv
load_dotenv()

# OpenAI (>=1.x)
from openai import OpenAI

# ====== ENV ======
WP_URL = (os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER = os.getenv("WP_USER") or ""
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY = (os.getenv("WP_TLS_VERIFY") or "true").lower() != "false"
POST_STATUS = (os.getenv("POST_STATUS") or "future").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or ""
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
OPENAI_MODEL_LONG = os.getenv("OPENAI_MODEL_LONG") or OPENAI_MODEL

COUPANG_ACCESS_KEY = os.getenv("COUPANG_ACCESS_KEY") or ""
COUPANG_SECRET_KEY = os.getenv("COUPANG_SECRET_KEY") or ""
COUPANG_CHANNEL_ID = os.getenv("COUPANG_CHANNEL_ID") or ""
COUPANG_SUBID_PREFIX = os.getenv("COUPANG_SUBID_PREFIX") or "auto"
REQUIRE_COUPANG_API = (os.getenv("REQUIRE_COUPANG_API") or "").lower() == "true"

AFFILIATE_TIME_KST = os.getenv("AFFILIATE_TIME_KST") or "13:00"

DISCLOSURE_TEXT = os.getenv("DISCLOSURE_TEXT") or \
    "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공합니다."

DEFAULT_CATEGORY = os.getenv("AFFILIATE_CATEGORY") or os.getenv("DEFAULT_CATEGORY") or "쇼핑"
# 태그는 정책상 “키워드 1개만” 사용
FORCE_SINGLE_TAG = True

KEYWORDS_PRIMARY = ["golden_shopping_keywords.csv", "keywords_shopping.csv", "keywords.csv"]
PRODUCTS_SEED_CSV = os.getenv("PRODUCTS_SEED_CSV") or "products_seed.csv"

USER_AGENT = os.getenv("USER_AGENT") or "gpt-blog-affiliate/1.1"


# ====== TIME HELPERS ======
def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))

def _to_gmt_at_kst(time_hhmm: str) -> str:
    hh, mm = (time_hhmm.split(":") + ["0"])[:2]
    h = int(hh); m = int(mm)
    now = _now_kst()
    tgt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if tgt <= now: tgt += timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ====== IO ======
def _read_col_csv(path: str) -> List[str]:
    if not os.path.exists(path): return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        r = csv.reader(f)
        rows = list(r)
        for i, row in enumerate(rows):
            if not row: continue
            if i == 0 and (row[0].lower() in ("keyword", "title")):
                continue
            kw = row[0].strip()
            if kw: out.append(kw)
    return out

def _read_line_csv(path: str) -> List[str]:
    if not os.path.exists(path): return []
    with open(path, "r", encoding="utf-8") as f:
        arr = [x.strip() for x in f.readline().split(",") if x.strip()]
    return arr


# ====== KEYWORD PICK (with seasonal fallback) ======
def _seasonal_fallback() -> str:
    m = _now_kst().month
    summer = ["넥쿨러", "휴대용 선풍기", "냉감 패드", "아이스 넥밴드", "쿨링 타월", "쿨링 토퍼"]
    winter = ["전기요", "히터", "난방 텐트", "손난로", "온열 담요", "발난로"]
    spring_fall = ["무선 청소기", "로봇청소기", "공기청정기", "가습기", "에어프라이어", "무선이어폰"]
    if m in (6, 7, 8, 9):
        pool = summer
    elif m in (12, 1, 2):
        pool = winter
    else:
        pool = spring_fall
    idx = (datetime.utcnow().day - 1) % len(pool)
    return pool[idx]

def _pick_keyword() -> str:
    # 순서: 골든 쇼핑 → 쇼핑 → 일반 한 줄 → 계절 폴백
    for p in KEYWORDS_PRIMARY:
        arr = _read_col_csv(p) if p.endswith(".csv") and p != "keywords.csv" else _read_line_csv(p)
        arr = [k.strip() for k in arr if k and k.strip()]
        if arr:
            kw = arr[0].strip()
            if kw:
                return kw
    fb = _seasonal_fallback()
    print(f"[AFFILIATE] WARN: shopping keywords empty -> seasonal fallback '{fb}'")
    return fb


# ====== TAGS ======
def _clean_hashtag_token(s: str) -> str:
    s = re.sub(r"[^\w가-힣]", "", s)
    bans = {"쿠팡", "파트너스", "최저가", "할인", "세일", "쿠폰", "딜", "무료배송"}
    if s in bans or not s: return ""
    return s

def _make_tags_from_keyword(kw: str) -> List[str]:
    if not FORCE_SINGLE_TAG:
        toks = [_clean_hashtag_token(t) for t in re.split(r"\s+|,|/|_", kw)]
        toks = [t for t in toks if t][:3]
        return toks or [kw]
    return [kw]


# ====== WP ======
def _ensure_term(kind: str, name: str) -> Optional[int]:
    url = f"{WP_URL}/wp-json/wp/v2/{kind}"
    r = requests.get(url, params={"search": name, "per_page": 50},
                     auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    for item in r.json():
        if (item.get("name") or "").strip() == name:
            return int(item["id"])
    r = requests.post(url, json={"name": name},
                      auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    return int(r.json()["id"])

def _post_wp(title: str, content_html: str, when_gmt: str, category: str, tags: List[str]) -> Dict:
    cat_id = _ensure_term("categories", category or DEFAULT_CATEGORY)
    tag_ids = []
    for t in (tags or []):
        tid = _ensure_term("tags", t)
        if tid: tag_ids.append(tid)
    payload = {
        "title": title,
        "content": content_html,
        "status": POST_STATUS,
        "categories": [cat_id],
        "tags": tag_ids,
        "comment_status": "closed",
        "ping_status": "closed",
        "date_gmt": when_gmt,
    }
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                      auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20)
    r.raise_for_status()
    return r.json()


# ====== PRODUCT SOURCE / LINK ======
def _read_products_seed() -> List[Dict]:
    if not os.path.exists(PRODUCTS_SEED_CSV): return []
    out = []
    with open(PRODUCTS_SEED_CSV, "r", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        for row in rd:
            out.append(row)
    return out

def _best_seed_for_kw(seed: List[Dict], kw: str) -> Optional[Dict]:
    kw_l = kw.lower()
    scored = []
    for it in seed:
        t = (it.get("title") or it.get("name") or "").lower()
        url = (it.get("url") or it.get("link") or "")
        if not url: continue
        s = 0
        for tok in re.split(r"\s+", kw_l):
            if tok and tok in t: s += 1
        if s == 0: continue
        scored.append((s, it))
    if not scored: return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]

def _coupang_search_url(kw: str) -> str:
    base = "https://www.coupang.com/np/search"
    q = urllib.parse.quote(kw) if kw.strip() else ""
    return f"{base}?q={q}" if q else "https://www.coupang.com/"

def _deeplink(urls: List[str], subid: str) -> List[str]:
    # 키 없으면 원본 유지
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_CHANNEL_ID):
        return urls
    try:
        from coupang_deeplink import make_deeplinks
        dk = make_deeplinks(urls, COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY,
                            COUPANG_CHANNEL_ID, subid)
        out = []
        for i, u in enumerate(urls):
            out.append(dk.get(i, u))
        return out
    except Exception:
        return urls

def _pick_product_and_link(kw: str) -> Dict:
    seed = _read_products_seed()
    best = _best_seed_for_kw(seed, kw) if seed else None
    search_url = _coupang_search_url(kw)
    cand = []
    if best and (best.get("url") or best.get("link")):
        cand.append(best.get("url") or best.get("link"))
    # 키워드가 비어도 검색은 동작하도록
    if kw.strip():
        cand.append(search_url)
    else:
        cand.append("https://www.coupang.com/")  # 최후 폴백

    subid = f"{COUPANG_SUBID_PREFIX}-{datetime.utcnow().strftime('%Y%m%d')}"
    dee = _deeplink(cand, subid)
    dee_link = dee[0] if dee else cand[0]
    return {
        "title": best.get("title") if best else (kw or "오늘의 추천"),
        "url": best.get("url") or best.get("link") if best else "",
        "image": best.get("image") or best.get("img") if best else "",
        "deeplink": dee_link,
        "search_url": search_url
    }


# ====== TITLE / BODY ======
BANNED_TITLE = ["브리핑", "정리", "알아보기", "대해 알아보기", "해야 할 것", "해야할 것", "해야할것", "리뷰", "가이드"]

def _bad_title(t: str) -> bool:
    if any(p in t for p in BANNED_TITLE): return True
    L = len(t.strip())
    return not (14 <= L <= 32)

def _hook_title(product_kw: str) -> str:
    sys_p = "너는 한국어 카피라이터다. 클릭을 부르는 강한 후킹 제목만 출력."
    usr = (
        "제품/키워드: " + product_kw + "\n"
        "조건:\n"
        "- 14~32자\n"
        "- 금지어: " + ", ".join(BANNED_TITLE) + "\n"
        "- '~브리핑', '~정리', '~대해 알아보기', '~해야 할 것' 류 금지\n"
        "- '리뷰/가이드/사용기' 같은 표지어 금지(사람스럽게)\n"
        "- 출력: 제목 한 줄만"
    )
    client = OpenAI(api_key=OPENAI_API_KEY)
    for _ in range(3):
        rsp = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"system","content":sys_p},{"role":"user","content":usr}],
            temperature=0.9, max_tokens=60,
        )
        t = (rsp.choices[0].message.content or "").strip().replace("\n", " ").strip("“”\"'")
        if not _bad_title(t):
            return t
    return f"{product_kw} 제대로 써보고 알게 된 포인트"

def _strip_fences(s: str) -> str:
    s = re.sub(r"```(?:\w+)?", "", s)
    s = s.replace("```", "")
    return s.strip()

def _gen_review_html(kw: str, deeplink: str, img_url: str = "", search_url: str = "") -> str:
    """
    예시처럼: 인사/후킹 → 왜 선택 → 특징 불릿 → 가격/가성비 → 장단점 → 추천대상 → 결론 → CTA
    - 1200~1300자
    - 광고문구/과장/치유효과 금지, 사람스러운 1인칭
    - 해시태그는 본문에 넣지 않음(태그는 코드에서 처리)
    """
    sys_p = "너는 사람스러운 한국어 블로거다. 광고처럼 보이지 않게, 직접 써본 듯 차분히 쓴다."
    usr = (
        "주제 제품: " + kw + "\n"
        "링크: " + deeplink + "\n"
        "요청:\n"
        "- 도입부에 근황/상황 2~3문장으로 공감대 형성(이모지 과다 금지)\n"
        "- <h2>/<h3> 소제목, 문단은 3~5문장\n"
        "- '왜 이 제품을 선택했는지' 사람스럽게 설명\n"
        "- 핵심 포인트 불릿 <ul><li> 4~6개 포함(과장 금지)\n"
        "- 본문 중 자연스러운 문장 링크 CTA 2회: '쿠팡에서 최저가 확인하기', '쿠팡 상품 상세 보러 가기'\n"
        "- <h3> 가격과 가성비 분석 섹션 포함(숫자/가격은 가늠치, 확정/보장 금지)\n"
        "- <h3> 솔직 후기: 장점/단점 각각 불릿 3~5개\n"
        "- <h3> 이런 분께 추천: 4~6개 불릿\n"
        "- 마지막 <h2> 결론 섹션\n"
        "- 분량: 1200~1300자\n"
        "- 출력: 순수 HTML(<p>,<h2>,<h3>,<a>,<ul>,<li>,<strong>,<em>,<blockquote>,<img>)"
    )
    client = OpenAI(api_key=OPENAI_API_KEY)
    rsp = client.chat.completions.create(
        model=OPENAI_MODEL_LONG,
        messages=[{"role":"system","content":sys_p},{"role":"user","content":usr}],
        temperature=0.85, max_tokens=1100,
    )
    html_body = _strip_fences(rsp.choices[0].message.content or "")
    parts = []
    parts.append('<p style="color:#b23;">' + html.escape(DISCLOSURE_TEXT) + '</p>')
    if img_url:
        parts.append('<p><img src="' + html.escape(img_url) + '" alt="' + html.escape(kw) + '" loading="lazy"></p>')
    parts.append(html_body)
    final_link = deeplink or search_url or _coupang_search_url(kw)
    parts.append('<p style="text-align:center;"><a href="' + html.escape(final_link) + '" target="_blank" rel="sponsored noopener">쿠팡 최저가 바로가기</a></p>')
    return "\n".join(parts)


# ====== MAIN ======
def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 필요")

    kw = _pick_keyword()
    tags = _make_tags_from_keyword(kw)

    prod = _pick_product_and_link(kw)
    deeplink = prod.get("deeplink") or prod.get("url") or _coupang_search_url(kw)
    search_url = prod.get("search_url") or _coupang_search_url(kw)
    hero_img = prod.get("image") or ""

    title = _hook_title(kw)
    html_body = _gen_review_html(kw, deeplink, hero_img, search_url)

    when_gmt = _to_gmt_at_kst(AFFILIATE_TIME_KST)

    res = _post_wp(title, html_body, when_gmt, DEFAULT_CATEGORY, tags)
    print(json.dumps({
        "post_id": res.get("id"),
        "link": res.get("link"),
        "status": res.get("status"),
        "date_gmt": res.get("date_gmt"),
        "title": res.get("title", {}).get("rendered", title)
    }, ensure_ascii=False))

if __name__ == "__main__":
    sys.exit(main())
