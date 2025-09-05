# -*- coding: utf-8 -*-
"""
affiliate_post.py — 쿠팡글: 13:00 KST 예약, 사람스러운 '리뷰형' 본문(1200~1300자),
후킹형 제목(금칙어 차단), 해시태그=키워드 1개, 쿠팡/파트너스 태그 금지.

우선순위 키워드:
  1) golden_shopping_keywords.csv
  2) golden_keywords.csv
  3) keywords.csv 첫 번째

본문 구성:
  - 오프닝 훅(사용 맥락/문제 제기)
  - 실제 사용감/추천 포인트(자연스러운 1인칭)
  - 선택 팁(체크리스트)
  - 마무리(한 줄 요약)
  - 길이: 1200~1300 '자' (최종 보정)

CTA/카드:
  - 씨앗/검색 결과로 2~5개 카드 + 버튼

환경변수(.env):
  WP_URL, WP_USER, WP_APP_PASSWORD, (선택) WP_TLS_VERIFY
  OPENAI_API_KEY, OPENAI_MODEL, OPENAI_MODEL_LONG
  AFFILIATE_CATEGORY, AFFILIATE_TIME_KST
  PRODUCTS_SEED_CSV, COUPANG_* (선택)
  REQUIRE_COUPANG_API, DISCLOSURE_TEXT, BUTTON_TEXT
"""

import os, csv, re, json, random, sys, hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict, Tuple

import requests
from dotenv import load_dotenv
load_dotenv()

# ====== ENV ======
WP_URL = (os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER = os.getenv("WP_USER") or ""
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY = (os.getenv("WP_TLS_VERIFY") or "true").lower() != "false"

OPENAI_MODEL = os.getenv("OPENAI_MODEL_LONG") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"

AFFILIATE_CATEGORY = (os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip()
DISCLOSURE_TEXT = os.getenv("DISCLOSURE_TEXT") or "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
AFFILIATE_TIME_KST = (os.getenv("AFFILIATE_TIME_KST") or "13:00").strip()

PRODUCTS_SEED_CSV = os.getenv("PRODUCTS_SEED_CSV") or "products_seed.csv"
COUPANG_ACCESS_KEY = os.getenv("COUPANG_ACCESS_KEY") or ""
COUPANG_SECRET_KEY = os.getenv("COUPANG_SECRET_KEY") or ""
COUPANG_CHANNEL_ID = os.getenv("COUPANG_CHANNEL_ID") or None
COUPANG_SUBID_PREFIX = os.getenv("COUPANG_SUBID_PREFIX") or "auto_wp_"
REQUIRE_COUPANG_API = (os.getenv("REQUIRE_COUPANG_API") or "false").lower() == "true"

DEFAULT_CATEGORY = (os.getenv("DEFAULT_CATEGORY") or AFFILIATE_CATEGORY or "정보").strip()
KEYWORDS_CSV = os.getenv("KEYWORDS_CSV") or "keywords.csv"
POST_STATUS = (os.getenv("POST_STATUS") or "future").strip()

# ====== OpenAI ======
from openai import OpenAI
_oai = OpenAI()

# ====== Coupang helpers ======
from coupang_deeplink import create_deeplinks
from coupang_search import search_products

# ====== Utils ======
def _log(s: str): print(s, flush=True)
def _now_kst() -> datetime: return datetime.now(ZoneInfo("Asia/Seoul"))

def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]

def next_time_kst_utc_str(hhmm: str) -> str:
    now = _now_kst()
    try: hh, mm = [int(x) for x in hhmm.split(":")]
    except Exception: hh, mm = 13, 0
    tgt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if tgt <= now: tgt += timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def read_keywords_first(path: str) -> str:
    if not os.path.exists(path): return "추천 상품"
    with open(path, "r", encoding="utf-8") as f:
        parts = [x.strip() for x in f.readline().strip().split(",") if x.strip()]
    return parts[0] if parts else "추천 상품"

def _resolve_seed_csv() -> str:
    return "products_seed.cleaned.csv" if os.path.exists("products_seed.cleaned.csv") else PRODUCTS_SEED_CSV

def read_seed_for_keyword(path: str, keyword: str, max_n: int = 5) -> List[Dict]:
    rows = []
    if not os.path.exists(path): return rows
    with open(path, "r", encoding="utf-8") as f:
        rd = csv.DictReader(f)
        for r in rd:
            if (r.get("keyword") or "").strip() == keyword.strip():
                rows.append({
                    "keyword": r.get("keyword","").strip(),
                    "product_name": r.get("product_name","").strip(),
                    "raw_url": r.get("raw_url","").strip(),
                    "pros": r.get("pros","").strip(),
                    "cons": r.get("cons","").strip(),
                    "imageUrl": r.get("imageUrl","").strip() if "imageUrl" in r else "",
                })
    return rows[:max_n]

def validate_urls(rows: List[Dict]) -> List[Dict]:
    out = []
    for r in rows:
        url = (r.get("raw_url") or "").strip()
        name = (r.get("product_name") or "").strip()
        if not (url and name): continue
        if not re.match(r"^https?://", url): continue
        out.append(r)
    return out

# ====== WP ======
def _ensure_category(name: str) -> int:
    name = name or DEFAULT_CATEGORY or "정보"
    r = requests.get(f"{WP_URL}/wp-json/wp/v2/categories",
                     params={"search": name, "per_page": 50},
                     auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    for item in r.json():
        if (item.get("name") or "").strip() == name:
            return int(item["id"])
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/categories",
                      json={"name": name}, auth=(WP_USER, WP_APP_PASSWORD),
                      verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    return int(r.json()["id"])

def _ensure_tags(tag_names: List[str]) -> List[int]:
    ids = []
    for t in tag_names:
        t = t.strip()
        if not t: continue
        r = requests.get(f"{WP_URL}/wp-json/wp/v2/tags",
                         params={"search": t, "per_page": 50},
                         auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
        r.raise_for_status()
        tag_id = None
        for item in r.json():
            if (item.get("name") or "").strip() == t:
                tag_id = int(item["id"]); break
        if tag_id is None:
            r = requests.post(f"{WP_URL}/wp-json/wp/v2/tags",
                              json={"name": t}, auth=(WP_USER, WP_APP_PASSWORD),
                              verify=WP_TLS_VERIFY, timeout=15)
            r.raise_for_status()
            tag_id = int(r.json()["id"])
        ids.append(tag_id)
    return ids

def wp_create_or_schedule(title: str, html: str, category_name: str, tag_names: List[str], when_kst: str) -> Dict:
    cat_id = _ensure_category(category_name or DEFAULT_CATEGORY)
    tag_ids = _ensure_tags(tag_names)  # 태그는 [키워드]만
    payload = {
        "title": title,
        "content": html,
        "status": POST_STATUS,
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

# ====== Coupang ======
def enrich_with_deeplink(rows: List[Dict]) -> List[Dict]:
    if not rows: return rows
    origin = [(r.get("raw_url") or "").strip() for r in rows]
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY):
        if REQUIRE_COUPANG_API: _log("[AFFILIATE] SKIP: 쿠팡 API 키 없음 (REQUIRE_COUPANG_API=true)"); return []
        _log("[AFFILIATE] WARN: 쿠팡 API 키 없음 -> raw_url 사용"); return rows
    try:
        sub_id = f"{COUPANG_SUBID_PREFIX}{_now_kst().strftime('%Y%m%d_%H%M')}"
        mapping = create_deeplinks(origin, COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY,
                                   sub_id=sub_id, channel_id=COUPANG_CHANNEL_ID)
        _log(f"[AFFILIATE] deeplink OK: {len(mapping)}/{len(origin)}")
        return [{**r, "deeplink": mapping.get(url, url)} for r, url in zip(rows, origin)]
    except Exception as e:
        if REQUIRE_COUPANG_API: _log(f"[AFFILIATE] SKIP: deeplink 실패 (REQUIRE_COUPANG_API=true) -> {e}"); return []
        _log(f"[AFFILIATE] WARN: deeplink 실패 -> raw_url 사용 ({e})"); return rows

def fetch_or_fallback_products(keyword: str, seed_path: str) -> List[Dict]:
    seed = validate_urls(read_seed_for_keyword(seed_path, keyword, max_n=5))
    if seed: return seed
    _log("[AFFILIATE] INFO: seed CSV 비어 -> 자동 검색/폴백")
    items = []
    if COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY:
        try:
            items = search_products(keyword, COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, limit=5, sort="salesVolume")
            seed = [{
                "keyword": keyword,
                "product_name": it.get("productName",""),
                "raw_url": it.get("productUrl",""),
                "imageUrl": it.get("imageUrl",""),
                "pros": "",
                "cons": "",
            } for it in items if it.get("productUrl")]
            _log(f"[AFFILIATE] search API -> {len(seed)}건")
            if seed: return seed
        except Exception as e:
            _log(f"[AFFILIATE] WARN: search 실패 -> {e}")
    if not REQUIRE_COUPANG_API:
        kw_enc = re.sub(r"\s+", "+", keyword.strip())
        bases = [
            f"https://www.coupang.com/np/search?q={kw_enc}&sort=salesVolumeDesc",
            f"https://www.coupang.com/np/search?q={kw_enc}&sort=bestAsc",
            f"https://www.coupang.com/np/search?q={kw_enc}&sort=accuracyDesc",
            f"https://www.coupang.com/np/search?q={kw_enc}&brand=&rating=4",
        ]
        seed = [{
            "keyword": keyword,
            "product_name": f"{keyword} 인기상품 모음 #{i+1}",
            "raw_url": u,
            "imageUrl": "",
            "pros": "",
            "cons": "",
        } for i,u in enumerate(bases[:random.randint(3,5)])]
        _log(f"[AFFILIATE] keyless fallback -> {len(seed)} 카드")
        return seed
    return []

# ====== Title / Body (LLM) ======
BANNED_TITLE_PATTERNS = [
    "브리핑", "정리", "알아보기", "알아보자", "대해 알아보기", "에 대해 알아보기",
    "해야 할 것", "해야할 것", "해야할것"
]

def _bad_title(t: str) -> bool:
    t = t.strip()
    if any(p in t for p in BANNED_TITLE_PATTERNS): return True
    if len(t) < 10 or len(t) > 35: return True
    return False

def _remember_and_check_unique(title: str) -> bool:
    os.makedirs(".cache", exist_ok=True)
    path = ".cache/title_history.txt"
    seen = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                seen.add(line.strip())
    h = _hash(title)
    if h in seen: return False
    with open(path, "a", encoding="utf-8") as f:
        f.write(h + "\n")
    return True

def gen_hook_title(keyword: str) -> str:
    sys_prompt = "너는 한국어 카피라이터다. 클릭을 부르는 짧고 강한 제목만 출력한다."
    user = f"""
키워드: {keyword}
조건:
- 길이 14~26자, 느낌표/물음표는 0~1개까지만
- 금지어: {", ".join(BANNED_TITLE_PATTERNS)}
- 형식 금지: ~브리핑, ~정리, ~대해 알아보기, ~해야 할 것 류
- '리뷰', '가이드', '사용기' 같은 단어도 가급적 피하고 자연스럽게
- 출력은 제목 1줄만
"""
    for _ in range(3):
        rsp = _oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"system","content":sys_prompt},
                      {"role":"user","content":user}],
            temperature=0.9,
            max_tokens=60,
        )
        t = (rsp.choices[0].message.content or "").strip().replace("\n"," ")
        if not _bad_title(t) and _remember_and_check_unique(t): return t
    # 실패 시 템플릿 백업
    fallback = random.choice([
        f"{keyword}, 한 번 써보니 답이 보였다",
        f"{keyword} 이렇게 고르니 실패가 줄었다",
        f"쓰다 보니 알게 된 {keyword} 진짜 포인트",
        f"{keyword} 이 가격에 이 퀄리티라니?",
    ])
    _remember_and_check_unique(fallback)
    return fallback

def clip_to_range(text: str, min_chars=1200, max_chars=1300) -> str:
    s = re.sub(r"\s+\n", "\n", text).strip()
    # 글자수 기준 보정
    if len(s) > max_chars:
        # 문장 경계에서 자르기
        cut = s[:max_chars]
        last = max(cut.rfind("다."), cut.rfind("."), cut.rfind("요."), cut.rfind("!") , cut.rfind("?"))
        if last >= min_chars*0.8:
            s = cut[:last+1]
        else:
            s = cut
    elif len(s) < min_chars:
        s += "\n\n" + "덧붙이면, 위 기준만 챙겨도 비용 대비 만족도가 높았습니다. 결국 중요한 건 내 생활에 맞는 선택이고, 부담 없이 오랫동안 잘 쓰이는가죠. 오늘 추천을 고른 이유도 여기에 있습니다."
    return s

def gen_human_review(keyword: str, products: List[Dict]) -> str:
    # 제품 이름 상위 1~3개만 요약해 프롬프트에 투입
    names = [p.get("product_name") or p.get("productName") for p in products if (p.get("product_name") or p.get("productName"))]
    names = [n for n in names if n][:3]
    sys_prompt = "너는 실제 사용자처럼 자연스럽게 쓰는 한국어 리뷰 작성자다. 광고 문구처럼 보이지 않게 담백하게 쓴다."
    user = f"""
주제 키워드: {keyword}
리뷰 대상(참고용 이름): {", ".join(names) if names else "카테고리 전반"}
요청:
- 1인칭 자연스러운 톤, 말투는 담백/생활 밀착
- 도입부에서 '왜 바꿨는지/왜 필요했는지' 맥락 제시(훅)
- 본문: 쓰면서 느낀 포인트 3~4가지(소음/무게/전력/배터리/정리편의/호환 등 범용 항목), 상황 예시 2개
- 구매 팁: 체크리스트 4~5줄 (공백 없이 한 문장씩)
- 마무리: "결국 ~"로 시작하는 한 줄 요약
- 'AI'나 '작성', '본 글은' 같은 표현 금지, 과장/과도한 감탄금지
- 단락은 3~5문장 기준으로 자주 끊기
- 길이: 1200~1300자 (한글 기준)
- 출력: 본문만, HTML 없이
"""
    rsp = _oai.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role":"system","content":sys_prompt},
                  {"role":"user","content":user}],
        temperature=0.85,
        max_tokens=800,
    )
    body = (rsp.choices[0].message.content or "").strip()
    return clip_to_range(body, 1200, 1300)

# ====== Compose HTML ======
def _cta_text() -> str:
    explicit = os.getenv("BUTTON_TEXT")
    if explicit: return explicit.strip()
    return random.choice(["최저가 확인하기","상세 보기","혜택 보러가기","지금 확인"])

def cards_html(keyword: str, products: List[Dict]) -> str:
    is_search_fallback = any("coupang.com/np/search" in (p.get("raw_url") or "") for p in products)
    blocks = []
    for p in products:
        name = p.get("product_name") or p.get("productName") or f"{keyword} 추천"
        link = p.get("deeplink") or p.get("raw_url","")
        pros = p.get("pros") or ""
        cons = p.get("cons") or ""
        img  = p.get("imageUrl") or ""
        label = _cta_text() if not is_search_fallback or os.getenv("BUTTON_TEXT") else f"쿠팡에서 '{keyword}' 검색 결과 보기"
        img_html = f"<img src='{img}' alt='{name}' style='max-width:100%;border-radius:10px;margin:0 0 8px 0;'/>" if img else ""
        blocks.append(f"""
<div style="margin:20px 0;padding:16px;border:1px solid #e5e7eb;border-radius:12px;">
  <h3 style="margin:0 0 8px 0;font-size:18px;">{name}</h3>
  {img_html}
  <ul style="margin:0 0 8px 18px;">
    {"<li>"+pros+"</li>" if pros else ""}
    {"<li>"+cons+"</li>" if cons else ""}
  </ul>
  <p><a href="{link}" target="_blank" rel="sponsored nofollow noopener" style="display:inline-block;padding:12px 18px;border-radius:12px;background:#0f172a;color:#fff;text-decoration:none;">{label}</a></p>
</div>""")
    return "".join(blocks)

def compose_post(keyword: str, products: List[Dict]) -> Tuple[str, str]:
    title = gen_hook_title(keyword)
    review = gen_human_review(keyword, products)
    html = f"""
<p style="color:#64748b;font-size:14px;">{DISCLOSURE_TEXT}</p>
<div style="margin:16px 0 24px 0; line-height:1.8;">{review.replace("\n","<br/>")}</div>
{cards_html(keyword, products)}
"""
    return title, html

# ====== Keyword pick ======
def pick_keyword() -> Tuple[str, List[str]]:
    # 1) golden_shopping
    if os.path.exists("golden_shopping_keywords.csv"):
        with open("golden_shopping_keywords.csv","r",encoding="utf-8") as f:
            rows=list(csv.DictReader(f))
        if rows and (rows[0].get("keyword") or "").strip():
            kw=rows[0]["keyword"].strip()
            return kw, [kw]
    # 2) golden_general
    if os.path.exists("golden_keywords.csv"):
        with open("golden_keywords.csv","r",encoding="utf-8") as f:
            rows=list(csv.DictReader(f))
        if rows and (rows[0].get("keyword") or "").strip():
            kw=rows[0]["keyword"].strip()
            return kw, [kw]
    # 3) 일반
    kw = read_keywords_first(KEYWORDS_CSV)
    return kw, [kw]

# ====== Main ======
def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    keyword, tags = pick_keyword()
    seed_path = _resolve_seed_csv()
    _log(f"[AFFILIATE] keyword='{keyword}', seed='{seed_path}'")

    products = fetch_or_fallback_products(keyword, seed_path)
    if not products:
        _log("[AFFILIATE] SKIP: 유효한 상품 없음"); return 0

    products = enrich_with_deeplink(products)
    if not products:
        _log("[AFFILIATE] SKIP: 딥링크 조건 미충족"); return 0

    title, html = compose_post(keyword, products)
    res = wp_create_or_schedule(title, html, AFFILIATE_CATEGORY, tags, AFFILIATE_TIME_KST)
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
