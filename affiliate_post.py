# -*- coding: utf-8 -*-
"""
affiliate_post.py — humanized + keyword random/rotate + vivid CTA

- 제목: 키워드 + 상위 제품명들을 조합해 사람스러운 후킹 타이틀 생성
- 버튼: 자유 문구(ENV) 또는 랜덤 후킹 문구 + 강한 hover/gradient 스타일
- 키워드 선택: KEYWORD_PICK_MODE = random | rotate | first
- 상품 선택: SEED_PICK_MODE = rank | rotate | shuffle
- 이미지: USE_IMAGE = off | hotlink | upload (CSV의 image_url/image_alt 사용)
- TLS 검증 토글: WP_TLS_VERIFY=true/false
- 대가성 문구 유지, 읽기시간/체크리스트/FAQ 포함
"""

import os, csv, json, random, requests, io, re
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from slugify import slugify

from coupang_deeplink import create_deeplinks

load_dotenv(override=False)

# ===== Env =====
WP_URL = os.getenv("WP_URL", "").rstrip("/")
WP_USER = os.getenv("WP_USER", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
WP_TLS_VERIFY = os.getenv("WP_TLS_VERIFY", "true").lower() != "false"

COUPANG_ACCESS_KEY = os.getenv("COUPANG_ACCESS_KEY")
COUPANG_SECRET_KEY = os.getenv("COUPANG_SECRET_KEY")
COUPANG_CHANNEL_ID = os.getenv("COUPANG_CHANNEL_ID") or None
COUPANG_SUBID_PREFIX = os.getenv("COUPANG_SUBID_PREFIX", "auto_wp_")

POST_STATUS = os.getenv("POST_STATUS", "future")
ALLOW_CREATE_TERMS = os.getenv("ALLOW_CREATE_TERMS", "true").lower() == "true"

DEFAULT_CATEGORY = (os.getenv("DEFAULT_CATEGORY") or "정보").strip()
DEFAULT_TAGS = [t.strip() for t in (os.getenv("DEFAULT_TAGS") or "쿠팡,추천,리뷰").split(",") if t.strip()]

AFFILIATE_CATEGORY = (os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip()
AFFILIATE_TAGS = [t.strip() for t in (os.getenv("AFFILIATE_TAGS") or "쿠팡,파트너스,추천").split(",") if t.strip()]

DISCLOSURE_TEXT = os.getenv(
    "DISCLOSURE_TEXT",
    "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
)

KEYWORDS_CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")
KEYWORD_PICK_MODE = (os.getenv("KEYWORD_PICK_MODE", "random").lower())  # random|rotate|first
AFFILIATE_TIME_KST = (os.getenv("AFFILIATE_TIME_KST") or "13:00").strip()

TOP_N = max(1, int(os.getenv("TOP_N", "3")))
TITLE_TPL = os.getenv("TITLE_TPL", "{keyword} 추천 TOP {n}: {tops} | {month}월 업데이트")
URL_CHECK_MODE = (os.getenv("URL_CHECK_MODE", "soft").lower())
SLUGIFY_ENABLE = (os.getenv("SLUGIFY_ENABLE", "true").lower() != "false")

SEED_PICK_MODE = (os.getenv("SEED_PICK_MODE", "rank").lower())  # rank|rotate|shuffle
USE_IMAGE = (os.getenv("USE_IMAGE", "off").lower())             # off|hotlink|upload
BUTTON_TEXT = (os.getenv("BUTTON_TEXT", "")).strip()

USAGE_DIR = os.getenv("USAGE_DIR", ".usage")
os.makedirs(USAGE_DIR, exist_ok=True)
CURSOR_PATH = os.path.join(USAGE_DIR, "seed_cursor.json")
KW_CURSOR_PATH = os.path.join(USAGE_DIR, "kw_cursor.json")

# ===== Seed path resolve (cleaned 우선) =====
def _resolve_seed_csv() -> str:
    env_path = os.getenv("PRODUCTS_SEED_CSV")
    if env_path and os.path.exists(env_path):
        return env_path
    if os.path.exists("products_seed.cleaned.csv"):
        return "products_seed.cleaned.csv"
    return env_path or "products_seed.csv"
SEED_CSV = _resolve_seed_csv()

# ===== WP helpers =====
def wp_auth():
    import base64
    token = base64.b64encode(f"{WP_USER}:{WP_APP_PASSWORD}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}"}

def wp_post(path: str, json_body: dict):
    url = f"{WP_URL}/wp-json/wp/v2{path}"
    r = requests.post(
        url,
        headers={**wp_auth(), "Content-Type": "application/json"},
        json=json_body,
        timeout=60,
        verify=WP_TLS_VERIFY,
    )
    if not r.ok:
        raise RuntimeError(f"WP POST {path} failed: {r.status_code} {r.text[:300]}")
    return r.json()

def wp_get(path: str, params=None):
    url = f"{WP_URL}/wp-json/wp/v2{path}"
    r = requests.get(
        url,
        headers=wp_auth(),
        params=params or {},
        timeout=60,
        verify=WP_TLS_VERIFY,
    )
    if not r.ok:
        raise RuntimeError(f"WP GET {path} failed: {r.status_code} {r.text[:300]}")
    return r.json()

def wp_upload_media_from_url(img_url: str, alt: str = "") -> Optional[str]:
    try:
        resp = requests.get(img_url, timeout=20)
        resp.raise_for_status()
        b = io.BytesIO(resp.content)
        filename = slugify(alt or "image", separator="-") or "image"
        headers = {**wp_auth(), "Content-Disposition": f'attachment; filename="{filename}.jpg"'}
        up = requests.post(
            f"{WP_URL}/wp-json/wp/v2/media",
            headers=headers,
            files={"file": (f"{filename}.jpg", b, "image/jpeg")},
            timeout=60,
            verify=WP_TLS_VERIFY,
        )
        up.raise_for_status()
        return up.json().get("source_url")
    except Exception:
        return None

def ensure_terms(taxonomy: str, names: List[str]) -> List[int]:
    ids = []
    for name in [n for n in (names or []) if n]:
        try:
            found = wp_get(f"/{taxonomy}", {"search": name, "per_page": 10})
            exact = [x for x in (found or []) if x.get("name") == name]
            if exact:
                ids.append(exact[0]["id"])
            elif ALLOW_CREATE_TERMS:
                created = wp_post(f"/{taxonomy}", {"name": name})
                if created and created.get("id"): ids.append(created["id"])
        except Exception:
            pass
    return ids

# ===== Time helpers =====
def _parse_hhmm(s: str) -> Tuple[int, int]:
    try:
        h, m = s.split(":"); return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except Exception:
        return (13, 0)

def next_time_kst_utc_str(hhmm: str) -> Optional[str]:
    if POST_STATUS != "future": return None
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    h, m = _parse_hhmm(hhmm)
    target = now_kst.replace(hour=h, minute=m, second=0, microsecond=0)
    if now_kst >= target: target = target + timedelta(days=1)
    return target.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ===== KW / Seed pickers =====
def _load_json(path: str) -> Dict:
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def _save_json(path: str, data: Dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def _normalize_terms_line(line: str) -> List[str]:
    parts = [x.strip() for x in line.split(",") if x.strip()]
    return [re.sub(r"\s+", " ", p) for p in parts]

def pick_keyword(record_file: str = KW_CURSOR_PATH) -> Dict:
    if not os.path.exists(KEYWORDS_CSV):
        return {"keyword": "추천 상품", "category": AFFILIATE_CATEGORY, "tags": ",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}

    with open(KEYWORDS_CSV, "r", encoding="utf-8") as f:
        raw = f.read().strip()

    if "\n" not in raw and "," in raw:
        terms = _normalize_terms_line(raw) or ["추천 상품"]
        mode = KEYWORD_PICK_MODE
        if mode == "first":
            kw = terms[0]
        elif mode == "rotate":
            st = _load_json(record_file); i = int(st.get("i", 0)) % len(terms)
            kw = terms[i]; st["i"] = (i + 1) % len(terms); _save_json(record_file, st)
        else:
            kw = random.choice(terms)
        return {"keyword": kw, "category": AFFILIATE_CATEGORY, "tags": ",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}

    with open(KEYWORDS_CSV, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {"keyword": "추천 상품", "category": AFFILIATE_CATEGORY, "tags": ",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}

    mode = KEYWORD_PICK_MODE
    if mode == "first":
        row = rows[0]
    elif mode == "rotate":
        st = _load_json(record_file); i = int(st.get("i", 0)) % len(rows)
        row = rows[i]; st["i"] = (i + 1) % len(rows); _save_json(record_file, st)
    else:
        row = random.choice(rows)

    if not row.get("keyword"): row["keyword"] = "추천 상품"
    if not row.get("category"): row["category"] = AFFILIATE_CATEGORY
    if not row.get("tags"): row["tags"] = ",".join(AFFILIATE_TAGS or DEFAULT_TAGS)
    return row

def _rank_value(r: Dict) -> int:
    try: return int(str(r.get("rank","")).strip())
    except: return 10**6

def _load_seed_rows(path: str) -> List[Dict]:
    rows=[]
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    return rows

def select_seed_for_keyword(rows: List[Dict], keyword: str, max_n: int) -> List[Dict]:
    cand = [r for r in rows if (r.get("keyword","").strip()==keyword)]
    cand.sort(key=lambda r: (_rank_value(r),))
    if not cand:
        cand = rows[:]
    if not cand:
        return []

    mode = SEED_PICK_MODE
    if mode == "rank":
        return cand[:max_n]
    if mode == "shuffle":
        seed = int(datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d"))
        rnd = random.Random(seed); tmp = cand[:]; rnd.shuffle(tmp); return tmp[:max_n]
    if mode == "rotate":
        st = _load_json(CURSOR_PATH); k = f"{keyword}"; start = int(st.get(k, 0))
        out = [cand[(start + i) % len(cand)] for i in range(max_n)]
        st[k] = (start + max_n) % len(cand); _save_json(CURSOR_PATH, st); return out
    return cand[:max_n]

# ===== URL validation =====
def validate_urls(rows: List[Dict]) -> List[Dict]:
    mode = URL_CHECK_MODE
    if mode == "off": return rows
    ok=[]
    for r in rows:
        u = (r.get("raw_url") or "").strip()
        if not u: continue
        good=False
        try:
            res = requests.head(u, allow_redirects=True, timeout=10)
            good = 200 <= res.status_code < 400
            if not good and mode=="soft":
                try:
                    rg = requests.get(u, allow_redirects=True, timeout=10, stream=True)
                    good = 200 <= rg.status_code < 400
                finally:
                    try: rg.close()
                    except: pass
        except Exception:
            good = (mode=="soft")
        if good or mode=="soft":
            ok.append(r)
    return ok

# ===== Copy helpers & styles =====
HOOK_BUTTONS = [
    "최저가 지금 확인","오늘 특가 보기","쿠폰 확인하고 구매","빠른 배송 가능한지 보기",
    "실시간 가격 살펴보기","지금 혜택 체크","지금 바로 보기","베스트 옵션 확인"
]
PITCH_PREFIX = ["핵심만 말하면, ","요약하자면 ","한 줄 평: ","이 모델은 "]
RECO_HEADS = ["이런 분께 추천","이런 분에게 딱 맞아요","이런 상황이면 좋아요"]
SKIP_HEADS = ["이런 분은 패스","이런 경우엔 비추천","다음과 같다면 다른 선택 추천"]
CHECKLIST = [
    "예산·보증·환불 조건을 먼저 확인하세요.",
    "배송 일정과 A/S 가능 지역을 확인하세요.",
    "실측 크기·호환 규격(포트/사이즈)을 체크하세요.",
]

STYLE = """
<style>
.cp-btn{display:inline-flex;gap:.6rem;align-items:center;padding:.72rem 1.05rem;border-radius:999px;
  border:0;background:linear-gradient(135deg,#2563eb,#0ea5e9);color:#fff;text-decoration:none;
  box-shadow:0 8px 22px rgba(37,99,235,.25);transform:translateY(0);transition:.18s transform,.18s box-shadow,.18s filter}
.cp-btn:hover{transform:translateY(-2px) scale(1.04);box-shadow:0 12px 28px rgba(2,132,199,.35);filter:saturate(1.15)}
.cp-btn:active{transform:translateY(0) scale(1.01)}
.cp-btn .em{font-weight:700}
.prod-card{display:flex;gap:14px;align-items:flex-start}
.prod-card img{width:112px;height:112px;object-fit:cover;border-radius:12px;border:1px solid #e5e7eb}
.table-wrap{overflow-x:auto;margin:12px 0}
.badge{font-size:.92em;color:#64748b}
</style>
"""

def _split_list(s: str) -> List[str]:
    return [x.strip(" ・·•-–—\t\r\n") for x in (s or "").replace("、",";").replace("|",";").split(";") if x.strip()]

def _one_line_pitch(r: Dict) -> str:
    pitch = (r.get("pitch") or "").strip()
    if pitch: return pitch
    pros = _split_list(r.get("pros","")); name = (r.get("product_name") or "").strip()
    if pros: return f"{name} — {pros[0]}"
    return f"{name} — 기본기가 탄탄한 선택"

def _badge_for_index(i: int) -> str:
    return ["종합 추천","가성비","프리미엄"][i-1] if 1 <= i <= 3 else "추천"

def _img_markup(p: Dict) -> str:
    if USE_IMAGE == "off": return ""
    src = (p.get("image_url") or "").strip()
    alt = (p.get("image_alt") or p.get("product_name") or "").strip()
    if not src: return ""
    src2 = wp_upload_media_from_url(src, alt) if USE_IMAGE == "upload" else src
    return f"<img src='{src2}' alt='{alt}' loading='lazy' decoding='async'/>"

def _cta_text() -> str:
    return BUTTON_TEXT if BUTTON_TEXT else random.choice(HOOK_BUTTONS)

def render_disclosure(txt: str) -> str:
    return f"<div style='padding:12px;border:1px solid #e5e7eb;background:#f8fafc;font-weight:600;margin:16px 0'>{txt}</div>"

def render_product_block(idx:int, p:Dict)->str:
    name = p.get("product_name","상품").strip()
    badge = _badge_for_index(idx); pitch = _one_line_pitch(p)
    pros = _split_list(p.get("pros","")); cons = _split_list(p.get("cons",""))
    fit  = _split_list(p.get("fit",""));  notes= _split_list(p.get("notes",""))
    deeplink = p.get("deeplink") or p.get("raw_url","")
    img = _img_markup(p)

    info = [f"<h3>{idx}. {name} <span class='badge'>({badge})</span></h3>",
            f"<p style='margin:6px 0;color:#334155'>{random.choice(PITCH_PREFIX)}{pitch}</p>"]
    if pros: info.append("<strong>장점</strong><ul>"+ "".join(f"<li>{x}</li>" for x in pros) +"</ul>")
    if cons: info.append("<strong>주의할 점</strong><ul>"+ "".join(f"<li>{x}</li>" for x in cons) +"</ul>")
    if fit:  info.append(f"<strong>{random.choice(RECO_HEADS)}</strong><ul>"+ "".join(f"<li>{x}</li>" for x in fit) +"</ul>")
    if notes:info.append(f"<strong>{random.choice(SKIP_HEADS)}</strong><ul>"+ "".join(f"<li>{x}</li>" for x in notes) +"</ul>")

    btn = f"<a class='cp-btn' href='{deeplink}' target='_blank' rel='sponsored noopener nofollow'><span class='em'>➜</span><span>{_cta_text()}</span></a>"
    card = f"<div class='prod-card'>{img}{''.join(info)}<p>{btn}</p></div>" if img else "".join(info) + f"<p>{btn}</p>"
    return card

def render_table(products: List[Dict])->str:
    trs=[]
    for p in products:
        name = p.get("product_name","상품")
        pros = "<br/>".join(_split_list(p.get("pros","")) or ["-"])
        cons = "<br/>".join(_split_list(p.get("cons","")) or ["-"])
        btn  = f"<a class='cp-btn' href='{p['deeplink']}' target='_blank' rel='sponsored noopener nofollow'><span class='em'>➜</span><span>{_cta_text()}</span></a>"
        trs.append(f"<tr><td><strong>{name}</strong><br/>{btn}</td><td>{pros}</td><td>{cons}</td></tr>")
    return (
        "<div class='table-wrap'>"
        "<table style='width:100%;border-collapse:separate;border-spacing:0;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden'>"
        "<thead><tr><th style='background:#f3f4f6;text-align:left;padding:10px'>상품</th>"
        "<th style='background:#f3f4f6;text-align:left;padding:10px'>장점</th>"
        "<th style='background:#f3f4f6;text-align:left;padding:10px'>유의점</th></tr></thead>"
        "<tbody>" + "".join(trs) + "</tbody></table></div>"
    )

def _reading_time_minutes(html_text: str) -> int:
    plain = re.sub(r"<[^>]+>"," ", html_text)
    chars = len(plain.strip())
    return max(1, int(round(chars / 700.0)))

def _shorten_name(name: str, limit: int = 12) -> str:
    s = re.sub(r"[()\[\]{}]", "", name or "").strip()
    return (s[:limit] + "…") if len(s) > limit else s

def _build_title(keyword: str, products: List[Dict]) -> str:
    month = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%m").lstrip("0")
    tops = " · ".join(_shorten_name(p.get("product_name","")) for p in products[:3] if p.get("product_name")) or "핵심 모델"
    return TITLE_TPL.format(keyword=keyword, n=len(products), month=month, tops=tops)

def render_post_html(title:str, keyword:str, products:List[Dict])->str:
    now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    head = [STYLE, render_disclosure(DISCLOSURE_TEXT),
            f"<p style='color:#475569'>※ 작성 기준: {now_kst}. 가격/재고/프로모션은 수시로 변동될 수 있습니다.</p>"]
    body = []
    body.append("<h2>선정 기준</h2>")
    body.append("<ul><li>장점·유의점과 실제 사용 편의성 동시 검토</li><li>가성비·휴대성·내구성 등 서로 다른 강점으로 구성</li><li>동일 역할 중복 최소화</li></ul>")
    body.append(f"<h2>TOP {len(products)} 상세 리뷰</h2>")
    for i,p in enumerate(products, start=1): body.append(render_product_block(i,p))
    body.append(f"<h2>비교 표 ({len(products)}개)</h2>")
    body.append(render_table(products))
    body.append("<h2>구매 체크리스트</h2>")
    body.append("<ul><li>예산·보증·환불 조건 확인</li><li>배송 일정/AS 센터 확인</li><li>규격/호환성 점검</li></ul>")
    body.append("<h2>FAQ</h2>")
    body.append("<details><summary><strong>어떤 기준으로 골랐나요?</strong></summary><p>사용 시나리오 기준으로 균형 있게 구성했습니다. 상황에 따라 최적의 선택은 달라질 수 있습니다.</p></details>")
    html_doc = "\n".join(head + body)
    rmin = _reading_time_minutes(html_doc)
    return f"<p style='color:#64748b'>읽는 시간 약 {rmin}분</p>\n" + html_doc

# ===== Main =====
def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY):
        print("Coupang API 키가 없어 affiliate 포스트를 건너뜁니다."); return

    topic = pick_keyword()
    keyword = topic.get("keyword") or "추천 상품"

    all_rows = _load_seed_rows(SEED_CSV)
    seed = select_seed_for_keyword(all_rows, keyword, max_n=TOP_N)
    seed = validate_urls(seed)
    if not seed:
        print(f"{SEED_CSV}에 '{keyword}' 키워드의 유효한 상품이 없습니다. 종료."); return

    origin_urls = [r["raw_url"].strip() for r in seed]
    sub_id = f"{COUPANG_SUBID_PREFIX}{datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d_%H%M')}"
    mapping = create_deeplinks(origin_urls, COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY,
                               sub_id=sub_id, channel_id=COUPANG_CHANNEL_ID)

    enriched=[]
    for r in seed:
        url = r["raw_url"].strip()
        enriched.append({**r, "deeplink": mapping.get(url, url)})

    title = _build_title(keyword, enriched)
    content_html = render_post_html(title, keyword, enriched)

    cat_name = (topic.get("category") or AFFILIATE_CATEGORY or DEFAULT_CATEGORY).strip()
    tag_names = [t.strip() for t in (topic.get("tags") or "").split(",") if t.strip()]
    tag_names = (tag_names or []) + (AFFILIATE_TAGS or DEFAULT_TAGS)
    cat_ids = ensure_terms("categories", [cat_name])
    tag_ids = ensure_terms("tags", list(dict.fromkeys(tag_names))[:10])

    payload = {"title": title, "content": content_html,
               "status": "publish" if POST_STATUS=="publish" else "future",
               "categories": cat_ids, "tags": tag_ids}
    if SLUGIFY_ENABLE:
        payload["slug"] = slugify(title, separator="-")
    if POST_STATUS=="future":
        payload["date_gmt"] = next_time_kst_utc_str(AFFILIATE_TIME_KST)

    res = wp_post("/posts", payload)
    print({"post_id": res.get("id"), "link": res.get("link"), "status": res.get("status")})

if __name__ == "__main__":
    main()
