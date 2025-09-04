# -*- coding: utf-8 -*-
"""
affiliate_post.py — humanized + rotation + image support + TLS verify toggle

- 상품 선정 가변화: SEED_PICK_MODE = rank | rotate | shuffle
- 버튼 UI: 캡슐 스타일(hover/press, 아이콘)
- 이미지: CSV의 image_url/image_alt 사용, USE_IMAGE=off|hotlink|upload
- 워드프레스 TLS 검증 토글: WP_TLS_VERIFY=true/false
- 자연스러운 카피/선정기준/체크리스트/FAQ/읽는시간 표시
- 절대 .env 내용을 여기에 붙이지 마세요 (.env는 별도 파일)

CSV 선택 컬럼:
  rank, pitch, fit, notes, image_url, image_alt
"""

import os, csv, json, random, requests, io
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
AFFILIATE_TIME_KST = (os.getenv("AFFILIATE_TIME_KST") or "13:00").strip()

TOP_N = max(1, int(os.getenv("TOP_N", "3")))
TITLE_TPL = os.getenv("TITLE_TPL", "{keyword} 추천 TOP {n} | {month}월 업데이트")
URL_CHECK_MODE = (os.getenv("URL_CHECK_MODE", "soft").lower())
SLUGIFY_ENABLE = (os.getenv("SLUGIFY_ENABLE", "true").lower() != "false")

SEED_PICK_MODE = (os.getenv("SEED_PICK_MODE", "rank").lower())  # rank|rotate|shuffle
USAGE_DIR = os.getenv("USAGE_DIR", ".usage")
os.makedirs(USAGE_DIR, exist_ok=True)
CURSOR_PATH = os.path.join(USAGE_DIR, "seed_cursor.json")

USE_IMAGE = (os.getenv("USE_IMAGE", "off").lower())  # off | hotlink | upload
BUTTON_TEXT = os.getenv("BUTTON_TEXT", "쿠팡에서 가격 보기")

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
    """외부 이미지를 다운로드해 WP 미디어로 업로드 후 source_url 반환 (USE_IMAGE=upload에서 사용)."""
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

# ===== Data loaders & pickers =====
def read_keywords_first(path: str) -> Dict:
    if not os.path.exists(path):
        return {"keyword": "추천 상품", "category": AFFILIATE_CATEGORY, "tags": ",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read().strip()
    if "\n" not in raw and "," in raw:
        terms = [x.strip() for x in raw.split(",") if x.strip()]
        kw = terms[0] if terms else "추천 상품"
        return {"keyword": kw, "category": AFFILIATE_CATEGORY, "tags": ",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}
    with open(path, "r", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    row = rows[0] if rows else {"keyword": "추천 상품", "category": AFFILIATE_CATEGORY, "tags": ",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}
    if not row.get("keyword"): row["keyword"] = "추천 상품"
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

def _load_cursor() -> Dict:
    if os.path.exists(CURSOR_PATH):
        try:
            with open(CURSOR_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_cursor(obj: Dict):
    try:
        with open(CURSOR_PATH, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

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
        rnd = random.Random(seed)
        tmp = cand[:]
        rnd.shuffle(tmp)
        return tmp[:max_n]

    if mode == "rotate":
        cur = _load_cursor()
        k = f"{keyword}"
        start = int(cur.get(k, 0))
        out = []
        for i in range(max_n):
            out.append(cand[(start + i) % len(cand)])
        cur[k] = (start + max_n) % len(cand)
        _save_cursor(cur)
        return out

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
PITCH_PREFIX = ["핵심만 말하면, ", "요약하자면 ", "한 줄 평: ", "이 모델은 "]
RECO_HEADS = ["이런 분께 추천", "이런 분에게 딱 맞아요", "이런 상황이면 좋아요"]
SKIP_HEADS = ["이런 분은 패스", "이런 경우엔 비추천", "다음과 같다면 다른 선택 추천"]
CHECKLIST = [
    "예산·보증·환불 조건을 먼저 확인하세요.",
    "배송 일정과 A/S 가능 지역을 확인하세요.",
    "실측 크기·호환 규격(포트/사이즈)을 체크하세요.",
]

STYLE = """
<style>
.cp-btn{display:inline-flex;gap:.5rem;align-items:center;padding:.625rem .9rem;border-radius:999px;
  border:1px solid #0f172a;background:#0f172a;color:#fff;text-decoration:none;box-shadow:0 2px 0 rgba(0,0,0,.1);
  transition:.2s transform,.2s box-shadow,.2s background}
.cp-btn:hover{transform:translateY(-1px);box-shadow:0 6px 14px rgba(2,6,23,.18);background:#111827}
.cp-btn:active{transform:translateY(0)}
.cp-btn .icon{width:18px;height:18px;display:inline-block;border:2px solid currentColor;border-left-color:transparent;border-radius:50%}
.prod-card{display:flex;gap:12px;align-items:flex-start}
.prod-card img{width:112px;height:112px;object-fit:cover;border-radius:12px;border:1px solid #e5e7eb}
.table-wrap{overflow-x:auto;margin:12px 0}
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
    return ["종합 추천", "가성비", "프리미엄"][i-1] if 1 <= i <= 3 else "추천"

def _img_markup(p: Dict) -> str:
    if USE_IMAGE == "off": return ""
    src = (p.get("image_url") or "").strip()
    alt = (p.get("image_alt") or p.get("product_name") or "").strip()
    if not src: return ""
    src2 = wp_upload_media_from_url(src, alt) if USE_IMAGE == "upload" else src
    return f"<img src='{src2}' alt='{alt}' loading='lazy' decoding='async'/>"

def render_disclosure(txt: str) -> str:
    return f"<div style='padding:12px;border:1px solid #e5e7eb;background:#f8fafc;font-weight:600;margin:16px 0'>{txt}</div>"

def render_product_block(idx:int, p:Dict)->str:
    name = p.get("product_name","상품").strip()
    badge = _badge_for_index(idx); pitch = _one_line_pitch(p)
    pros = _split_list(p.get("pros","")); cons = _split_list(p.get("cons",""))
    fit  = _split_list(p.get("fit",""));  notes= _split_list(p.get("notes",""))
    deeplink = p.get("deeplink") or p.get("raw_url","")
    img = _img_markup(p)
    info = [f"<h3>{idx}. {name} <span style='font-size:.92em;color:#64748b'>({badge})</span></h3>",
            f"<p style='margin:6px 0;color:#334155'>{random.choice(PITCH_PREFIX)}{pitch}</p>"]
    if pros: info.append("<strong>장점</strong><ul>"+ "".join(f"<li>{x}</li>" for x in pros) +"</ul>")
    if cons: info.append("<strong>주의할 점</strong><ul>"+ "".join(f"<li>{x}</li>" for x in cons) +"</ul>")
    if fit:  info.append(f"<strong>{random.choice(RECO_HEADS)}</strong><ul>"+ "".join(f"<li>{x}</li>" for x in fit) +"</ul>")
    if notes:info.append(f"<strong>{random.choice(SKIP_HEADS)}</strong><ul>"+ "".join(f"<li>{x}</li>" for x in notes) +"</ul>")
    btn = f"<a class='cp-btn' href='{deeplink}' target='_blank' rel='sponsored noopener nofollow'><span class='icon'></span><span>{BUTTON_TEXT}</span></a>"
    card = f"<div class='prod-card'>{img}{''.join(info)}<p>{btn}</p></div>" if img else "".join(info) + f"<p>{btn}</p>"
    return card

def render_table(products: List[Dict])->str:
    trs=[]
    for p in products:
        name = p.get("product_name","상품")
        pros = "<br/>".join(_split_list(p.get("pros","")) or ["-"])
        cons = "<br/>".join(_split_list(p.get("cons","")) or ["-"])
        btn  = f"<a class='cp-btn' href='{p['deeplink']}' target='_blank' rel='sponsored noopener nofollow'><span class='icon'></span><span>{BUTTON_TEXT}</span></a>"
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
    import re
    plain = re.sub(r"<[^>]+>"," ", html_text)
    chars = len(plain.strip())
    return max(1, int(round(chars / 700.0)))

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

    topic = read_keywords_first(KEYWORDS_CSV)
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

    month = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%m").lstrip("0")
    title = TITLE_TPL.format(keyword=keyword, n=len(enriched), month=month)
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
