# -*- coding: utf-8 -*-
"""
affiliate_post.py — robust with diagnostics & fallback
- seed CSV에서 TOP_N(기본 3) 선정(순서: rank → 파일행)
- 제목: 키워드 + 상위 제품 2개 요약(외 n) | {month}월 업데이트
- 본문: 사람 말투(선정 기준/제품별 요약/비교표/체크리스트/FAQ)
- 딥링크: Coupang OpenAPI. 실패/키없음시 선택적으로 raw_url fallback
- ENV:
    TOP_N=3
    TITLE_TPL="{keyword} 추천 TOP {n}: {tops} | {month}월 업데이트"
    AFFILIATE_TIME_KST="13:00"
    URL_CHECK_MODE="soft|strict|off" (default soft)
    ALLOW_CREATE_TERMS=true|false
    SLUGIFY_ENABLE=true|false
    REQUIRE_COUPANG_API=true|false   # 없거나 실패하면 스킵할지 여부(기본 false)
    BUTTON_TEXT="CTA 문구"           # 비우면 랜덤
"""
import os, csv, random, requests, re
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from slugify import slugify
from coupang_deeplink import create_deeplinks

load_dotenv(override=False)

# ===== Env =====
WP_URL = os.getenv("WP_URL","").rstrip("/")
WP_USER = os.getenv("WP_USER","")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD","")

COUPANG_ACCESS_KEY = os.getenv("COUPANG_ACCESS_KEY")
COUPANG_SECRET_KEY = os.getenv("COUPANG_SECRET_KEY")
COUPANG_CHANNEL_ID = os.getenv("COUPANG_CHANNEL_ID") or None
COUPANG_SUBID_PREFIX = os.getenv("COUPANG_SUBID_PREFIX","auto_wp_")

POST_STATUS = os.getenv("POST_STATUS","future")
ALLOW_CREATE_TERMS = os.getenv("ALLOW_CREATE_TERMS","true").lower()=="true"
REQUIRE_COUPANG_API = os.getenv("REQUIRE_COUPANG_API","false").lower()=="true"

DEFAULT_CATEGORY = (os.getenv("DEFAULT_CATEGORY") or "정보").strip()
DEFAULT_TAGS = [t.strip() for t in (os.getenv("DEFAULT_TAGS") or "쿠팡,추천,리뷰").split(",") if t.strip()]
AFFILIATE_CATEGORY = (os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip()
AFFILIATE_TAGS = [t.strip() for t in (os.getenv("AFFILIATE_TAGS") or "쿠팡,파트너스,추천").split(",") if t.strip()]

DISCLOSURE_TEXT = os.getenv(
    "DISCLOSURE_TEXT",
    "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공받습니다."
)

KEYWORDS_CSV = os.getenv("KEYWORDS_CSV","keywords.csv")
AFFILIATE_TIME_KST = (os.getenv("AFFILIATE_TIME_KST") or "13:00").strip()

TOP_N = max(1, int(os.getenv("TOP_N","3")))
TITLE_TPL = os.getenv("TITLE_TPL","{keyword} 추천 TOP {n}: {tops} | {month}월 업데이트")
URL_CHECK_MODE = os.getenv("URL_CHECK_MODE","soft").lower()
SLUGIFY_ENABLE = os.getenv("SLUGIFY_ENABLE","true").lower()!="false"

BUTTON_TEXT = (os.getenv("BUTTON_TEXT","").strip())

def _cta_text() -> str:
    return BUTTON_TEXT or random.choice(["쿠팡에서 가격 보기","최저가 확인하기","지금 혜택 보기","무료배송 여부 확인"])

# ===== Seed resolve =====
def _resolve_seed_csv()->str:
    env = os.getenv("PRODUCTS_SEED_CSV")
    if env and os.path.exists(env): return env
    if os.path.exists("products_seed.cleaned.csv"): return "products_seed.cleaned.csv"
    return env or "products_seed.csv"
SEED_CSV = _resolve_seed_csv()

# ===== WP helpers =====
def wp_auth():
    import base64
    token = base64.b64encode(f"{WP_USER}:{WP_APP_PASSWORD}".encode("utf-8")).decode("utf-8")
    return {"Authorization": f"Basic {token}"}

def wp_post(path: str, json_body: dict):
    url = f"{WP_URL}/wp-json/wp/v2{path}"
    r = requests.post(url, headers={**wp_auth(),"Content-Type":"application/json"}, json=json_body, timeout=30)
    if not r.ok:
        raise RuntimeError(f"WP POST {path} failed: {r.status_code} {r.text[:300]}")
    return r.json()

def wp_get(path: str, params=None):
    url = f"{WP_URL}/wp-json/wp/v2{path}"
    r = requests.get(url, headers=wp_auth(), params=params or {}, timeout=30)
    if not r.ok:
        raise RuntimeError(f"WP GET {path} failed: {r.status_code} {r.text[:300]}")
    return r.json()

def ensure_terms(taxonomy: str, names: List[str])->List[int]:
    ids=[]
    for name in [n for n in (names or []) if n]:
        try:
            found=wp_get(f"/{taxonomy}", {"search":name, "per_page":10})
            exact=[x for x in (found or []) if x.get("name")==name]
            if exact: ids.append(exact[0]["id"])
            elif ALLOW_CREATE_TERMS:
                created=wp_post(f"/{taxonomy}", {"name":name})
                if created and created.get("id"): ids.append(created["id"])
        except Exception:
            pass
    return ids

# ===== Time =====
def _parse_hhmm(s:str)->Tuple[int,int]:
    try:
        h,m=s.split(":"); return max(0,min(23,int(h))), max(0,min(59,int(m)))
    except: return (13,0)

def next_time_kst_utc_str(hhmm:str)->Optional[str]:
    if POST_STATUS!="future": return None
    now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
    h,m=_parse_hhmm(hhmm)
    target=now_kst.replace(hour=h,minute=m,second=0,microsecond=0)
    if now_kst>=target: target += timedelta(days=1)
    return target.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# ===== Data loaders =====
def read_keywords_first(path:str)->Dict:
    if not os.path.exists(path):
        return {"keyword":"추천 상품","category":AFFILIATE_CATEGORY,"tags":",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}
    with open(path,"r",encoding="utf-8") as f:
        raw=f.read().strip()
    if "\n" not in raw and "," in raw:
        terms=[x.strip() for x in raw.split(",") if x.strip()]
        kw=terms[0] if terms else "추천 상품"
        return {"keyword":kw,"category":AFFILIATE_CATEGORY,"tags":",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}
    with open(path,"r",encoding="utf-8") as f:
        rows=list(csv.DictReader(f))
    row=rows[0] if rows else {"keyword":"추천 상품","category":AFFILIATE_CATEGORY,"tags":",".join(AFFILIATE_TAGS or DEFAULT_TAGS)}
    if not row.get("keyword"): row["keyword"]="추천 상품"
    return row

def _rank_value(r:Dict)->int:
    try: return int(str(r.get("rank","")).strip())
    except: return 10**6

def read_seed_for_keyword(path:str, keyword:str, max_n:int)->List[Dict]:
    rows=[]
    if os.path.exists(path):
        with open(path,"r",encoding="utf-8") as f:
            rows=list(csv.DictReader(f))
    sel=[r for r in rows if (r.get("keyword","").strip()==keyword)]
    sel.sort(key=lambda r:(_rank_value(r),))
    if not sel: sel=rows
    return sel[:max_n]

# ===== URL validation =====
def _head_or_get_ok(u:str)->bool:
    try:
        r=requests.head(u,allow_redirects=True,timeout=10); 
        if 200<=r.status_code<400: return True
    except: pass
    try:
        rg=requests.get(u,allow_redirects=True,timeout=10,stream=True)
        return 200<=rg.status_code<400
    except: return False

def validate_urls(rows:List[Dict])->List[Dict]:
    mode=URL_CHECK_MODE
    if mode=="off": return rows
    ok=[]
    for r in rows:
        u=(r.get("raw_url") or "").strip()
        if not u: continue
        good=False
        try:
            if mode=="strict":
                h=requests.head(u,allow_redirects=True,timeout=10)
                good=200<=h.status_code<400
            else:
                good=_head_or_get_ok(u)
        except: 
            good=(mode=="soft")
        if good or mode=="soft": ok.append(r)
    return ok

# ===== Copy helpers =====
def _split_list(s:str)->List[str]:
    return [x.strip(" ・·•-–—\t\r\n") for x in (s or "").replace("、",";").replace("|",";").split(";") if x.strip()]

def _shorten_name(name:str, limit:int=10)->str:
    s=re.sub(r"[()\[\]{}]","",(name or "")).strip()
    s=re.sub(r"\b(공식|정품|베스트|인기|특가)\b","",s).strip()
    s=re.sub(r"\s{2,}"," ",s)
    return (s[:limit]+"…") if len(s)>limit else s

def _build_title(keyword:str, products:List[Dict])->str:
    month = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%m").lstrip("0")
    tops=[_shorten_name(p.get("product_name","")) for p in products if p.get("product_name")]
    tops=[t for t in tops if t]
    tops_str=""
    if len(tops)>=2:
        etc=len(tops)-2
        tops_str=f"{tops[0]} · {tops[1]}" + (f" 외 {etc}" if etc>0 else "")
    elif len(tops)==1:
        tops_str=f"{tops[0]}"
    return TITLE_TPL.format(keyword=keyword, n=len(products), month=month, tops=tops_str)

def _reading_time_minutes(html_text:str)->int:
    import re
    plain=re.sub(r"<[^>]+>"," ",html_text)
    chars=len(plain.strip())
    return max(1,int(round(chars/700.0)))

def render_disclosure(txt:str)->str:
    return f"<div style='padding:12px;border:1px solid #e5e7eb;background:#f8fafc;font-weight:600;margin:16px 0'>{txt}</div>"

def render_product_block(idx:int, p:Dict)->str:
    name=p.get("product_name","상품").strip()
    pitch=(p.get("pitch") or "").strip() or ( _split_list(p.get("pros",""))[:1] or ["기본기가 탄탄한 선택"] )[0]
    pros=_split_list(p.get("pros",""))
    cons=_split_list(p.get("cons",""))
    fit=_split_list(p.get("fit",""))
    notes=_split_list(p.get("notes",""))
    deeplink=p.get("deeplink") or p.get("raw_url","")
    btn=f"<p><a href='{deeplink}' target='_blank' rel='sponsored noopener nofollow' style='display:inline-block;padding:12px 16px;border-radius:10px;background:linear-gradient(135deg,#2563eb,#4338ca);color:#fff;font-weight:700;text-decoration:none;box-shadow:0 6px 16px rgba(0,0,0,.12);transition:transform .12s ease, box-shadow .12s ease;' onmouseover=\"this.style.transform='translateY(-1px)';this.style.boxShadow='0 10px 20px rgba(0,0,0,.18)';\" onmouseout=\"this.style.transform='none';this.style.boxShadow='0 6px 16px rgba(0,0,0,.12)';\">{_cta_text()}</a></p>"
    blocks=[]
    badge=["종합 추천","가성비","프리미엄"][idx-1] if 1<=idx<=3 else "추천"
    blocks.append(f"<h3>{idx}. {name} <span style='font-size:.92em;color:#64748b'>({badge})</span></h3>")
    blocks.append(f"<p>한 줄 평: {pitch}</p>")
    if pros:  blocks.append("<strong>장점</strong><ul>"+ "".join(f"<li>{x}</li>" for x in pros) +"</ul>")
    if cons:  blocks.append("<strong>주의할 점</strong><ul>"+ "".join(f"<li>{x}</li>" for x in cons) +"</ul>")
    if fit:   blocks.append("<strong>이런 분께 추천</strong><ul>"+ "".join(f"<li>{x}</li>" for x in fit) +"</ul>")
    if notes: blocks.append("<strong>이런 경우엔 비추천</strong><ul>"+ "".join(f"<li>{x}</li>" for x in notes) +"</ul>")
    blocks.append(btn)
    return "\n".join(blocks)

def render_table(products:List[Dict])->str:
    trs=[]
    for p in products:
        name=p.get("product_name","상품")
        pros="<br/>".join(_split_list(p.get("pros","")) or ["-"])
        cons="<br/>".join(_split_list(p.get("cons","")) or ["-"])
        btn=f"<a href='{p['deeplink']}' target='_blank' rel='sponsored noopener nofollow' style='display:inline-block;padding:8px 12px;border-radius:8px;background:#0f172a;color:#fff;text-decoration:none;'>"+_cta_text()+"</a>"
        trs.append(f"<tr><td><strong>{name}</strong><br/>{btn}</td><td>{pros}</td><td>{cons}</td></tr>")
    return ("<div class='table-wrap'><table style='width:100%;border-collapse:separate;border-spacing:0;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden'>"
            "<thead><tr><th style='background:#f3f4f6;text-align:left;padding:10px'>상품</th>"
            "<th style='background:#f3f4f6;text-align:left;padding:10px'>장점</th>"
            "<th style='background:#f3f4f6;text-align:left;padding:10px'>유의점</th></tr></thead>"
            "<tbody>"+ "".join(trs) +"</tbody></table></div>")

def render_post_html(title:str, keyword:str, products:List[Dict])->str:
    now_kst=datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d")
    head=[render_disclosure(DISCLOSURE_TEXT),
          f"<p style='color:#475569'>※ 작성 기준: {now_kst}. 가격/재고/프로모션은 수시로 변동될 수 있습니다.</p>"]
    body=[]
    body.append("<h2>선정 기준</h2>")
    body.append("<ul><li>시드 데이터의 장점·유의점과 실제 사용 편의성 검토</li><li>가성비·휴대성·내구성 등 다른 강점으로 구성</li><li>같은 역할의 중복 최소화</li></ul>")
    body.append(f"<h2>TOP {len(products)} 상세 리뷰</h2>")
    for i,p in enumerate(products, start=1):
        body.append(render_product_block(i,p))
    body.append(f"<h2>비교 표 ({len(products)}개)</h2>")
    body.append(render_table(products))
    body.append("<h2>구매 체크리스트</h2>")
    body.append("<ul><li>예산·보증·환불 조건 확인</li><li>배송 일정·A/S 가능 지역 확인</li><li>실측 크기·호환 규격 체크</li></ul>")
    body.append("<h2>FAQ</h2><details><summary><strong>어떤 기준으로 골랐나요?</strong></summary><p>장점과 사용 맥락을 함께 보고, 같은 역할이 겹치지 않게 조합했습니다. 상황에 따라 최적의 선택은 달라질 수 있습니다.</p></details>")
    html_doc="\n".join(head+body)
    rmin=_reading_time_minutes(html_doc)
    return f"<p style='color:#64748b'>읽는 시간 약 {rmin}분</p>\n"+html_doc

# ===== Main =====
def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    topic=read_keywords_first(KEYWORDS_CSV)
    keyword=topic.get("keyword") or "추천 상품"
    print(f"[AFFILIATE] keyword='{keyword}', seed='{SEED_CSV}'")

    seed=read_seed_for_keyword(SEED_CSV, keyword, max_n=TOP_N)
    seed=validate_urls(seed)
    if not seed:
        print("[AFFILIATE] SKIP: 유효한 상품 없음 (seed/URL 검사 실패)")
        return

    # 딥링크: 키 없거나 실패 시 동작 선택
    mapping={}
    if COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY:
        try:
            origin_urls=[(r.get("raw_url") or "").strip() for r in seed]
            sub_id=f"{COUPANG_SUBID_PREFIX}{datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y%m%d_%H%M')}"
            mapping=create_deeplinks(origin_urls, COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY,
                                     sub_id=sub_id, channel_id=COUPANG_CHANNEL_ID)
            print(f"[AFFILIATE] deeplink OK: {len(mapping)}/{len(origin_urls)}")
        except Exception as e:
            msg=str(e)[:200]
            if REQUIRE_COUPANG_API:
                print(f"[AFFILIATE] SKIP: deeplink 실패 (REQUIRE_COUPANG_API=true) -> {msg}")
                return
            print(f"[AFFILIATE] WARN: deeplink 실패 -> raw_url 사용 ({msg})")
    else:
        if REQUIRE_COUPANG_API:
            print("[AFFILIATE] SKIP: 쿠팡 API 키 없음 (REQUIRE_COUPANG_API=true)")
            return
        print("[AFFILIATE] WARN: 쿠팡 API 키 없음 -> raw_url 사용")

    enriched=[]
    for r in seed:
        url=(r.get("raw_url") or "").strip()
        enriched.append({**r, "deeplink": mapping.get(url, url)})

    title=_build_title(keyword, enriched)
    content_html=render_post_html(title, keyword, enriched)

    cat_name=(topic.get("category") or AFFILIATE_CATEGORY or DEFAULT_CATEGORY).strip()
    tag_names=[t.strip() for t in (topic.get("tags") or "").split(",") if t.strip()]
    tag_names=(tag_names or []) + (AFFILIATE_TAGS or DEFAULT_TAGS)

    cat_ids=ensure_terms("categories",[cat_name])
    tag_ids=ensure_terms("tags", list(dict.fromkeys(tag_names))[:10])

    payload={"title":title,"content":content_html,"status":"publish" if POST_STATUS=="publish" else "future",
             "categories":cat_ids,"tags":tag_ids}
    if SLUGIFY_ENABLE:
        payload["slug"]=slugify(title, separator="-")
    if POST_STATUS=="future":
        payload["date_gmt"]=next_time_kst_utc_str(AFFILIATE_TIME_KST)

    res=wp_post("/posts", payload)
    out={"post_id": res.get("id"), "link": res.get("link"), "status": res.get("status")}
    print(out)

if __name__=="__main__":
    main()
