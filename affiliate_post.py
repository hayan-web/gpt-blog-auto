# -*- coding: utf-8 -*-
"""
affiliate_post.py — humanized final
- 시드 CSV(products_seed.*.csv)에서 keyword에 맞는 상품 TOP_N(기본 3) 선정
- 순서 일관성: CSV에 rank(1..n)가 있으면 그 순서, 없으면 파일행 순서를 보존
- 제목: "쿠팡 파트너스" 문구 제거. TITLE_TPL로 자유 커스텀
- 본문: 사람 느낌의 자연스러운 카피(선정 기준/제품별 한줄평/추천 대상/주의점/비교표/체크리스트/FAQ)
- 딥링크: Coupang OpenAPI (subId, channelId 지원)
- 슬러그: python-slugify 사용(한글 → 안전한 URL)
- 예약: AFFILIATE_TIME_KST (기본 13:00 KST)
- URL 검사: URL_CHECK_MODE=soft 기본(HEAD → GET 폴백, 실패해도 진행)
- 카테고리/태그: AFFILIATE_* 우선, 없으면 DEFAULT_* 사용
- ALLOW_CREATE_TERMS=false면 존재하는 용어만 사용(생성 시도 안 함)

[CSV 선택 컬럼]
- rank: 표시 순서(숫자)
- pitch: 한 줄 요약
- fit: 이런 분께 추천(세미콜론 ; 로 구분)
- notes: 추가 메모(세미콜론 ; 로 구분)

[ENV]
- TOP_N              : 3
- TITLE_TPL          : "{keyword} 추천 TOP {n} | {month}월 업데이트"
- AFFILIATE_TIME_KST : "13:00"
- URL_CHECK_MODE     : "soft" | "strict" | "off"
- ALLOW_CREATE_TERMS : "true" | "false"
- SLUGIFY_ENABLE     : "true" | "false" (기본 true)
"""

import os, csv, random, requests
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

COUPANG_ACCESS_KEY = os.getenv("COUPANG_ACCESS_KEY")
COUPANG_SECRET_KEY = os.getenv("COUPANG_SECRET_KEY")
COUPANG_CHANNEL_ID = os.getenv("COUPANG_CHANNEL_ID") or None
COUPANG_SUBID_PREFIX = os.getenv("COUPANG_SUBID_PREFIX", "auto_wp_")

POST_STATUS = os.getenv("POST_STATUS", "future")  # publish | future
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
    ids = []
    for name in [n for n in (names or []) if n]:
        try:
            found = wp_get(f"/{taxonomy}", {"search": name, "per_page": 10})
            exact = [x for x in (found or []) if x.get("name") == name]
            if exact:
                ids.append(exact[0]["id"])
            elif ALLOW_CREATE_TERMS:
                created = wp_post(f"/{taxonomy}", {"name": name})
                if created and created.get("id"):
                    ids.append(created["id"])
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

# ===== Data loaders =====
def read_keywords_first(path: str) -> Dict:
    # CSV가 없거나 한 줄 콤마 나열이면 맨 앞 키워드 사용
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

def read_seed_for_keyword(path: str, keyword: str, max_n: int) -> List[Dict]:
    rows=[]
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    sel = [r for r in rows if (r.get("keyword","").strip()==keyword)]
    # rank → 원래 순서(안정)
    sel.sort(key=lambda r: (_rank_value(r),))
    if not sel:
        sel = rows
    return sel[:max_n]

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

# ===== Copy helpers (사람 느낌의 문장 생성) =====
PITCH_PREFIX = [
    "핵심만 말하면, ",
    "요약하자면 ",
    "한 줄 평: ",
    "이 모델은 ",
]
RECO_HEADS = [
    "이런 분께 추천",
    "이런 분에게 딱 맞아요",
    "이런 상황이면 좋아요",
]
SKIP_HEADS = [
    "이런 분은 패스",
    "이런 경우엔 비추천",
    "다음과 같다면 다른 선택 추천",
]
CHECKLIST = [
    "예산·보증·환불 조건을 먼저 확인하세요.",
    "배송 일정과 A/S 가능 지역을 확인하세요.",
    "실측 크기·호환 규격(포트/사이즈)을 체크하세요.",
]

def _split_list(s: str) -> List[str]:
    return [x.strip(" ・·•-–—\t\r\n") for x in (s or "").replace("、",";").replace("|",";").split(";") if x.strip()]

def _one_line_pitch(r: Dict) -> str:
    pitch = (r.get("pitch") or "").strip()
    if pitch: return pitch
    pros = _split_list(r.get("pros",""))
    name = (r.get("product_name") or "").strip()
    if pros:
        return f"{name} — {pros[0]}"
    return f"{name} — 기본기가 탄탄한 선택"

def _badge_for_index(i: int) -> str:
    return ["종합 추천", "가성비", "프리미엄"][i-1] if 1 <= i <= 3 else "추천"

def _p(text: str) -> str:
    return f"<p>{text}</p>"

def render_disclosure(txt: str) -> str:
    return f"<div style='padding:12px;border:1px solid #e5e7eb;background:#f8fafc;font-weight:600;margin:16px 0'>{txt}</div>"

def render_product_block(idx:int, p:Dict)->str:
    name = p.get("product_name","상품").strip()
    badge = _badge_for_index(idx)
    pitch = _one_line_pitch(p)
    pros = _split_list(p.get("pros",""))
    cons = _split_list(p.get("cons",""))
    fit  = _split_list(p.get("fit",""))
    notes= _split_list(p.get("notes",""))
    deeplink = p.get("deeplink") or p.get("raw_url","")
    btn  = f"<p><a href='{deeplink}' target='_blank' rel='sponsored noopener nofollow' style='display:inline-block;padding:10px 14px;border:1px solid #0f172a;border-radius:8px;text-decoration:none;'>최저가 보러가기</a></p>"

    blocks = []
    blocks.append(f"<h3>{idx}. {name} <span style='font-size:.92em;color:#64748b'>({badge})</span></h3>")
    blocks.append(_p(random.choice(PITCH_PREFIX) + pitch))

    if pros:
        blocks.append("<strong>장점</strong><ul>" + "".join(f"<li>{x}</li>" for x in pros) + "</ul>")
    if cons:
        blocks.append("<strong>주의할 점</strong><ul>" + "".join(f"<li>{x}</li>" for x in cons) + "</ul>")
    if fit:
        head = random.choice(RECO_HEADS)
        blocks.append(f"<strong>{head}</strong><ul>" + "".join(f"<li>{x}</li>" for x in fit) + "</ul>")
    if notes:
        head = random.choice(SKIP_HEADS)
        blocks.append(f"<strong>{head}</strong><ul>" + "".join(f"<li>{x}</li>" for x in notes) + "</ul>")

    blocks.append(btn)
    return "\n".join(blocks)

def render_table(products: List[Dict])->str:
    trs=[]
    for p in products:
        name = p.get("product_name","상품")
        pros = "<br/>".join(_split_list(p.get("pros","")) or ["-"])
        cons = "<br/>".join(_split_list(p.get("cons","")) or ["-"])
        btn  = f"<a href='{p['deeplink']}' target='_blank' rel='sponsored noopener nofollow' style='display:inline-block;padding:6px 10px;border:1px solid #0f172a;border-radius:6px;text-decoration:none;'>바로가기</a>"
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
    # 한글 기준: 대략 600~800자/분. 700자로 계산
    import re
    plain = re.sub(r"<[^>]+>"," ", html_text)
    chars = len(plain.strip())
    return max(1, int(round(chars / 700.0)))

def render_post_html(title:str, keyword:str, products:List[Dict])->str:
    now_kst = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    head = []
    head.append(render_disclosure(DISCLOSURE_TEXT))
    head.append(f"<p style='color:#475569'>※ 작성 기준: {now_kst}. 가격/재고/프로모션은 수시로 변동될 수 있습니다.</p>")

    # 선정 기준
    body = []
    body.append("<h2>선정 기준</h2>")
    body.append(
        "<ul>"
        "<li>시드 데이터의 장점·유의점과 실제 사용 편의성을 함께 검토</li>"
        "<li>가성비·휴대성·내구성 등 서로 다른 강점을 가진 모델로 구성</li>"
        "<li>같은 역할끼리는 겹치지 않도록 균형 배치</li>"
        "</ul>"
    )

    # 제품별 상세
    body.append(f"<h2>TOP {len(products)} 상세 리뷰</h2>")
    for i,p in enumerate(products, start=1):
        body.append(render_product_block(i,p))

    # 비교표 + 체크리스트 + FAQ
    body.append(f"<h2>비교 표 ({len(products)}개)</h2>")
    body.append(render_table(products))
    body.append("<h2>구매 체크리스트</h2>")
    body.append("<ul>" + "".join(f"<li>{x}</li>" for x in CHECKLIST) + "</ul>")
    body.append("<h2>FAQ</h2>")
    body.append("<details><summary><strong>어떤 기준으로 골랐나요?</strong></summary>"
                "<p>장점과 사용 맥락을 함께 보며, 같은 역할이 겹치지 않도록 조합했습니다. 상황에 따라 최적의 선택은 달라질 수 있습니다.</p>"
                "</details>")

    html_doc = "\n".join(head + body)
    # 읽는 시간 메타(본문 상단 안내)
    rmin = _reading_time_minutes(html_doc)
    return f"<p style='color:#64748b'>읽는 시간 약 {rmin}분</p>\n" + html_doc

# ===== Main =====
def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY):
        print("Coupang API 키가 없어 affiliate 포스트를 건너뜁니다.")
        return

    topic = read_keywords_first(KEYWORDS_CSV)
    keyword = topic.get("keyword") or "추천 상품"

    # 일관된 TOP N 선택
    seed = read_seed_for_keyword(SEED_CSV, keyword, max_n=TOP_N)
    seed = validate_urls(seed)
    if not seed:
        print(f"{SEED_CSV}에 '{keyword}' 키워드의 유효한 상품이 없습니다. 종료.")
        return

    # 딥링크 생성
    origin_urls = [r["raw_url"].strip() for r in seed]
    sub_id = f"{COUPANG_SUBID_PREFIX}{datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d_%H%M')}"
    mapping = create_deeplinks(origin_urls, COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY,
                               sub_id=sub_id, channel_id=COUPANG_CHANNEL_ID)

    enriched=[]
    for r in seed:
        url = r["raw_url"].strip()
        enriched.append({**r, "deeplink": mapping.get(url, url)})

    # 제목(‘쿠팡 파트너스’ 제거) + 슬러그
    month = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%m").lstrip("0")
    title = TITLE_TPL.format(keyword=keyword, n=len(enriched), month=month)
    content_html = render_post_html(title, keyword, enriched)

    # 카테고리/태그
    cat_name = (topic.get("category") or AFFILIATE_CATEGORY or DEFAULT_CATEGORY).strip()
    tag_names = [t.strip() for t in (topic.get("tags") or "").split(",") if t.strip()]
    tag_names = (tag_names or []) + (AFFILIATE_TAGS or DEFAULT_TAGS)
    cat_ids = ensure_terms("categories", [cat_name])
    tag_ids = ensure_terms("tags", list(dict.fromkeys(tag_names))[:10])

    payload = {
        "title": title,
        "content": content_html,
        "status": "publish" if POST_STATUS=="publish" else "future",
        "categories": cat_ids, "tags": tag_ids,
    }

    if SLUGIFY_ENABLE:
        # 한글 제목을 안전한 슬러그로
        payload["slug"] = slugify(title, separator="-")

    if POST_STATUS=="future":
        payload["date_gmt"] = next_time_kst_utc_str(AFFILIATE_TIME_KST)

    res = wp_post("/posts", payload)
    print({"post_id": res.get("id"), "link": res.get("link"), "status": res.get("status")})

if __name__ == "__main__":
    main()
