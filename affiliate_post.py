# -*- coding: utf-8 -*-
"""
affiliate_post.py — 쿠팡글 1건 예약(기본 13:00 KST)
- 키워드: golden_shopping_keywords.csv -> keywords_shopping.csv -> keywords.csv -> 계절 폴백
- 본문: 사람스러운 1인칭 리뷰형(1200~1300자) + 인라인 CSS
- 태그: 키워드 1개만(쿠팡/파트너스/최저가/할인 금지)
- 딥링크: 키 있으면 API 변환, 없으면 검색 URL 폴백
"""
import os, re, csv, json, sys, html, urllib.parse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional

import requests
from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI, BadRequestError

# ===== ENV =====
WP_URL = (os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER = os.getenv("WP_USER") or ""
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY = (os.getenv("WP_TLS_VERIFY") or "true").lower() != "false"
POST_STATUS = (os.getenv("POST_STATUS") or "future").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or ""
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
OPENAI_MODEL_LONG = os.getenv("OPENAI_MODEL_LONG") or ""

COUPANG_ACCESS_KEY = os.getenv("COUPANG_ACCESS_KEY") or ""
COUPANG_SECRET_KEY = os.getenv("COUPANG_SECRET_KEY") or ""
COUPANG_CHANNEL_ID = os.getenv("COUPANG_CHANNEL_ID") or ""
COUPANG_SUBID_PREFIX = os.getenv("COUPANG_SUBID_PREFIX") or "auto"

AFFILIATE_TIME_KST = os.getenv("AFFILIATE_TIME_KST") or "13:00"
DISCLOSURE_TEXT = os.getenv("DISCLOSURE_TEXT") or \
    "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공합니다."

DEFAULT_CATEGORY = os.getenv("AFFILIATE_CATEGORY") or os.getenv("DEFAULT_CATEGORY") or "쇼핑"
FORCE_SINGLE_TAG = True

KEYWORDS_PRIMARY = ["golden_shopping_keywords.csv", "keywords_shopping.csv", "keywords.csv"]
PRODUCTS_SEED_CSV = os.getenv("PRODUCTS_SEED_CSV") or "products_seed.csv"
USER_AGENT = os.getenv("USER_AGENT") or "gpt-blog-affiliate/1.2"

# ===== TIME =====
def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))
def _to_gmt_at_kst(hhmm: str) -> str:
    h, m = (hhmm.split(":") + ["0"])[:2]
    now = _now_kst()
    tgt = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
    if tgt <= now: tgt += timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ===== CSV IO =====
def _read_col_csv(path: str) -> List[str]:
    if not os.path.exists(path): return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        rd = csv.reader(f)
        for i, row in enumerate(rd):
            if not row: continue
            if i == 0 and (row[0].lower() in ("keyword", "title")):
                continue
            if row[0].strip(): out.append(row[0].strip())
    return out

def _read_line_csv(path: str) -> List[str]:
    if not os.path.exists(path): return []
    with open(path, "r", encoding="utf-8") as f:
        return [x.strip() for x in f.readline().split(",") if x.strip()]

# ===== KEYWORDS (seasonal fallback) =====
def _seasonal_fallback() -> str:
    m = _now_kst().month
    summer = ["넥쿨러", "휴대용 선풍기", "냉감 패드", "아이스 넥밴드", "쿨링 타월", "쿨링 토퍼"]
    winter = ["전기요", "히터", "난방 텐트", "손난로", "온열 담요", "발난로"]
    swing  = ["무선 청소기", "로봇청소기", "공기청정기", "가습기", "에어프라이어", "무선이어폰"]
    pool = summer if m in (6,7,8,9) else winter if m in (12,1,2) else swing
    return pool[(datetime.utcnow().day-1) % len(pool)]

def _pick_keyword() -> str:
    for p in KEYWORDS_PRIMARY:
        arr = _read_col_csv(p) if p.endswith(".csv") and p != "keywords.csv" else _read_line_csv(p)
        arr = [k for k in arr if k]
        if arr: return arr[0]
    fb = _seasonal_fallback()
    print(f"[AFFILIATE] WARN: shopping keywords empty -> seasonal fallback '{fb}'")
    return fb

# ===== TAGS =====
def _clean_hashtag_token(s: str) -> str:
    s = re.sub(r"[^\w가-힣]", "", s)
    bans = {"쿠팡","파트너스","최저가","할인","세일","쿠폰","딜","무료배송"}
    return "" if (not s or s in bans) else s

def _make_tags_from_keyword(kw: str) -> List[str]:
    return [kw] if FORCE_SINGLE_TAG else [t for t in {_clean_hashtag_token(x) for x in re.split(r"\s+|,|/|_", kw)} if t][:3] or [kw]

# ===== WP =====
def _ensure_term(kind: str, name: str) -> Optional[int]:
    url = f"{WP_URL}/wp-json/wp/v2/{kind}"
    r = requests.get(url, params={"search": name, "per_page": 50},
                     auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip() == name:
            return int(it["id"])
    r = requests.post(url, json={"name": name},
                      auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    return int(r.json()["id"])

def _post_wp(title: str, content_html: str, when_gmt: str, category: str, tags: List[str]) -> Dict:
    cat_id = _ensure_term("categories", category or DEFAULT_CATEGORY)
    tag_ids = [ _ensure_term("tags", t) for t in (tags or []) if t ]
    payload = {
        "title": title, "content": content_html, "status": POST_STATUS,
        "categories": [cat_id], "tags": [tid for tid in tag_ids if tid],
        "comment_status":"closed", "ping_status":"closed", "date_gmt": when_gmt,
    }
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                      auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20)
    r.raise_for_status()
    return r.json()

# ===== Coupang link =====
def _read_products_seed() -> List[Dict]:
    if not os.path.exists(PRODUCTS_SEED_CSV): return []
    with open(PRODUCTS_SEED_CSV, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))

def _best_seed_for_kw(seed: List[Dict], kw: str) -> Optional[Dict]:
    kw_l = kw.lower()
    scored = []
    for it in seed:
        title = (it.get("title") or it.get("name") or "").lower()
        url = (it.get("url") or it.get("link") or "")
        if not url: continue
        sc = sum(tok in title for tok in kw_l.split())
        if sc: scored.append((sc, it))
    if not scored: return None
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[0][1]

def _coupang_search_url(kw: str) -> str:
    if kw.strip():
        return "https://www.coupang.com/np/search?q=" + urllib.parse.quote(kw)
    return "https://www.coupang.com/"

def _deeplink(urls: List[str], subid: str) -> List[str]:
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_CHANNEL_ID):
        return urls
    try:
        from coupang_deeplink import make_deeplinks
        dk = make_deeplinks(urls, COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, COUPANG_CHANNEL_ID, subid)
        return [dk.get(i, u) for i, u in enumerate(urls)]
    except Exception:
        return urls

def _pick_product_and_link(kw: str) -> Dict:
    seed = _read_products_seed()
    best = _best_seed_for_kw(seed, kw) if seed else None
    search_url = _coupang_search_url(kw)
    cand = []
    if best and (best.get("url") or best.get("link")):
        cand.append(best.get("url") or best.get("link"))
    cand.append(search_url if kw.strip() else "https://www.coupang.com/")
    subid = f"{COUPANG_SUBID_PREFIX}-{datetime.utcnow().strftime('%Y%m%d')}"
    dee = _deeplink(cand, subid)
    return {
        "title": best.get("title") if best else (kw or "오늘의 추천"),
        "url": best.get("url") or best.get("link") if best else "",
        "image": best.get("image") or best.get("img") if best else "",
        "deeplink": (dee[0] if dee else cand[0]),
        "search_url": search_url
    }

# ===== OpenAI helper (Chat → Responses 폴백; temperature 미지원 모델 자동 재시도) =====
_client = OpenAI(api_key=OPENAI_API_KEY)
MODEL_TITLE = OPENAI_MODEL or "gpt-4o-mini"
MODEL_BODY  = OPENAI_MODEL_LONG or OPENAI_MODEL or "gpt-4o-mini"

def _ask_chat_then_responses(model: str, system: str, user: str, max_tokens: int, temperature: float) -> str:
    try:
        r = _client.chat.completions.create(
            model=model,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (r.choices[0].message.content or "").strip()
    except BadRequestError as e:
        # Chat 미지원/파라미터 오류 → Responses 사용
        kwargs = dict(model=model, input=f"[시스템]\n{system}\n\n[사용자]\n{user}", max_output_tokens=max_tokens)
        try:
            rr = _client.responses.create(**kwargs, temperature=temperature)
        except BadRequestError as e2:
            # 일부 모델은 temperature 자체를 지원하지 않음 → 제거 후 재시도
            if "temperature" in str(e2):
                rr = _client.responses.create(**kwargs)
            else:
                raise
        # output_text helper 또는 수동 추출
        txt = getattr(rr, "output_text", None)
        if isinstance(txt, str) and txt.strip():
            return txt.strip()
        if getattr(rr, "output", None) and rr.output and rr.output[0].content:
            try:
                return rr.output[0].content[0].text.strip()
            except Exception:
                pass
        return ""

# ===== TITLE / BODY =====
BANNED_TITLE = ["브리핑","정리","알아보기","대해 알아보기","해야 할 것","해야할 것","해야할것","리뷰","가이드"]

def _bad_title(t: str) -> bool:
    if any(p in t for p in BANNED_TITLE): return True
    L = len(t.strip()); return not (14 <= L <= 32)

def _hook_title(product_kw: str) -> str:
    sys_p = "너는 한국어 카피라이터다. 클릭을 부르는 강한 후킹 제목만 출력."
    usr = (
        f"제품/키워드: {product_kw}\n"
        "- 14~32자\n"
        f"- 금지어: {', '.join(BANNED_TITLE)}\n"
        "- '~브리핑/~정리/~대해 알아보기/~해야 할 것' 금지\n"
        "- '리뷰/가이드/사용기' 같은 표지어 금지\n"
        "- 출력: 제목 한 줄만"
    )
    for _ in range(3):
        t = _ask_chat_then_responses(MODEL_TITLE, sys_p, usr, max_tokens=60, temperature=0.9)
        t = (t or "").strip().replace("\n"," ").strip("“”\"'")
        if not _bad_title(t): return t
    return f"{product_kw} 제대로 써보고 알게 된 포인트"

def _strip_fences(s: str) -> str:
    s = re.sub(r"```(?:\w+)?", "", s).replace("```",""); return s.strip()

def _css_block() -> str:
    return """
<style>
.post-affil p{line-height:1.84;margin:0 0 14px;color:#222}
.post-affil h2{margin:28px 0 12px;font-size:1.45rem;line-height:1.35;border-left:6px solid #3b82f6;padding-left:10px}
.post-affil h3{margin:22px 0 10px;font-size:1.15rem;color:#0f172a}
.post-affil ul{padding-left:22px;margin:10px 0}
.post-affil li{margin:6px 0}
.post-affil .cta{text-align:center;margin:24px 0}
.post-affil .cta a{display:inline-block;padding:10px 18px;border:1px solid #94a3b8;border-radius:10px;text-decoration:none}
.post-affil .disc{color:#a21caf;font-size:.92rem;margin:10px 0 18px}
</style>
"""

def _gen_review_html(kw: str, deeplink: str, img_url: str = "", search_url: str = "") -> str:
    sys_p = "너는 사람스러운 한국어 블로거다. 광고처럼 보이지 않게 직접 써본 것처럼 쓴다."
    usr = (
        f"주제 제품: {kw}\n링크: {deeplink}\n요청:\n"
        "- 도입 근황/상황 2~3문장\n"
        "- <h2>/<h3> 소제목, 문단 3~5문장\n"
        "- '왜 선택했는지' 사람스럽게\n"
        "- 불릿 <ul><li> 4~6개(과장/치유표현 금지)\n"
        "- 본문 중 자연스러운 텍스트 링크 2회: '쿠팡에서 최저가 확인하기', '쿠팡 상품 상세 보러 가기'\n"
        "- <h3> 가격과 가성비 분석(대략적 표현)\n"
        "- <h3> 솔직 후기: 장점/단점 3~5개씩\n"
        "- <h3> 이런 분께 추천: 4~6개\n"
        "- 마지막 <h2> 결론\n"
        "- 분량 1200~1300자\n"
        "- 출력: 순수 HTML(<p>,<h2>,<h3>,<a>,<ul>,<li>,<strong>,<em>,<blockquote>,<img>)"
    )
    body = _ask_chat_then_responses(MODEL_BODY, sys_p, usr, max_tokens=1100, temperature=0.85)
    body = _strip_fences(body or "")
    parts = [_css_block(), '<div class="post-affil">', f'<p class="disc">{html.escape(DISCLOSURE_TEXT)}</p>']
    if img_url:
        parts.append(f'<p><img src="{html.escape(img_url)}" alt="{html.escape(kw)}" loading="lazy"></p>')
    parts.append(body)
    final_link = deeplink or search_url or _coupang_search_url(kw)
    parts.append(f'<p class="cta"><a href="{html.escape(final_link)}" target="_blank" rel="sponsored noopener">쿠팡 최저가 바로가기</a></p>')
    parts.append("</div>")
    return "\n".join(parts)

# ===== MAIN =====
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
