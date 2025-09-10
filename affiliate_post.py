# -*- coding: utf-8 -*-
import os, re, csv, json, sys, html, urllib.parse, random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Dict, Optional, Tuple

import requests
from dotenv import load_dotenv
from openai import OpenAI, BadRequestError

load_dotenv()

# ===== ENV =====
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

AFFILIATE_TIME_KST = os.getenv("AFFILIATE_TIME_KST") or "13:00"
DISCLOSURE_TEXT = os.getenv("DISCLOSURE_TEXT") or "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공합니다."
DEFAULT_CATEGORY = os.getenv("AFFILIATE_CATEGORY") or os.getenv("DEFAULT_CATEGORY") or "쇼핑"
FORCE_SINGLE_TAG = True

BUTTON_TEXT_ENV = (os.getenv("BUTTON_TEXT") or "").strip()
KEYWORDS_PRIMARY = ["golden_shopping_keywords.csv", "keywords_shopping.csv", "keywords.csv"]
PRODUCTS_SEED_CSV = os.getenv("PRODUCTS_SEED_CSV") or "products_seed.csv"
USER_AGENT = os.getenv("USER_AGENT") or "gpt-blog-affiliate/1.3"
USAGE_DIR = os.getenv("USAGE_DIR") or ".usage"
USED_FILE = os.path.join(USAGE_DIR, "used_shopping.txt")

# NEW: 반복 방지/우선순위
AFF_USED_BLOCK_DAYS = int(os.getenv("AFF_USED_BLOCK_DAYS") or "30")  # 최근 n일 내 사용은 후순위
NO_REPEAT_TODAY = (os.getenv("NO_REPEAT_TODAY") or "1").lower() in ("1","true","yes","y","on")

REQ_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
_client = OpenAI(api_key=OPENAI_API_KEY)

def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))

# ===== CSV helpers =====
def _read_col_csv(path: str) -> List[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path, "r", encoding="utf-8") as f:
        rd = csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and row[0].lower() in ("keyword","title"): continue
            if row[0].strip(): out.append(row[0].strip())
    # 중복 제거(순서 보존)
    seen=set(); uniq=[]
    for k in out:
        if k not in seen:
            seen.add(k); uniq.append(k)
    return uniq

def _read_line_csv(path: str) -> List[str]:
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f:
        return [x.strip() for x in f.readline().split(",") if x.strip()]

def _remove_kw_from_col_csv(fn: str, kw: str) -> bool:
    if not os.path.exists(fn): return False
    rows=[]; changed=False
    with open(fn, "r", encoding="utf-8") as f:
        rd = csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and row[0].lower()=="keyword":
                rows.append(row); continue
            if row[0].strip()!=kw:
                rows.append(row)
            else:
                changed=True
    if changed:
        with open(fn, "w", encoding="utf-8", newline="") as f:
            csv.writer(f).writerows(rows)
    print(f"[ROTATE] {fn}: {'removed' if changed else 'no-op'} -> '{kw}'")
    return changed

def _rotate_csvs_on_success(kw: str):
    a=_remove_kw_from_col_csv("golden_shopping_keywords.csv", kw)
    b=_remove_kw_from_col_csv("keywords_shopping.csv", kw)
    if not (a or b):
        print("[ROTATE] nothing removed (maybe already rotated)")

# ===== Used-keyword log =====
def _ensure_usage(): os.makedirs(USAGE_DIR, exist_ok=True)

def _load_used_set(days:int=30)->set:
    """[호환용] 최근 n일 내 사용된 키워드 집합. (기존 코드 사용처 대비 유지)"""
    _ensure_usage()
    if not os.path.exists(USED_FILE): return set()
    cutoff = datetime.utcnow().date() - timedelta(days=days)
    s=set()
    with open(USED_FILE,"r",encoding="utf-8",errors="ignore") as f:
        for ln in f:
            ln=ln.strip()
            if not ln: continue
            try:
                d_str, kw = ln.split("\t",1)
                d=datetime.strptime(d_str,"%Y-%m-%d").date()
                if d>=cutoff: s.add(kw.strip())
            except Exception:
                s.add(ln)
    return s

def _load_usage_map()->Tuple[Dict[str, datetime.date], set]:
    """키워드별 마지막 사용일(last)과 '오늘 사용' 집합(used_today) 반환."""
    _ensure_usage()
    last: Dict[str, datetime.date] = {}
    used_today=set()
    today=datetime.utcnow().date()
    if os.path.exists(USED_FILE):
        with open(USED_FILE,"r",encoding="utf-8",errors="ignore") as f:
            for ln in f:
                ln=ln.strip()
                if not ln: continue
                try:
                    d_str, kw = ln.split("\t",1)
                    d=datetime.strptime(d_str,"%Y-%m-%d").date()
                    kw=kw.strip()
                except Exception:
                    continue
                prev=last.get(kw)
                last[kw] = d if (prev is None or d>prev) else prev
                if d==today:
                    used_today.add(kw)
    return last, used_today

def _mark_used(kw:str):
    _ensure_usage()
    with open(USED_FILE,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw.strip()}\n")

# ===== Time / slot =====
def _wp_has_future_at(when_gmt_dt):
    after=(when_gmt_dt - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    before=(when_gmt_dt + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S")
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/posts",
                   params={"status":"future","after":after,"before":before,"per_page":5},
                   headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD),
                   verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    return len(r.json())>0

def _slot_or_next_day(hhmm:str)->str:
    h,m=(hhmm.split(":")+["0"])[:2]; h=int(h); m=int(m)
    now=_now_kst()
    tgt=now.replace(hour=h,minute=m,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    tgt_utc=tgt.astimezone(timezone.utc)
    if _wp_has_future_at(tgt_utc):
        return (tgt+timedelta(days=1)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return tgt_utc.strftime("%Y-%m-%dT%H:%M:%S")

# ===== Picking keyword (LRU + 30일 회피) =====
def _seasonal_fallback()->str:
    m=_now_kst().month
    summer=["넥쿨러","휴대용 선풍기","냉감 패드","아이스 넥밴드","쿨링 타월","쿨링 토퍼"]
    winter=["전기요","히터","난방 텐트","손난로","온열 담요","발난로"]
    swing=["무선 청소기","로봇청소기","공기청정기","가습기","에어프라이어","무선이어폰"]
    pool = summer if m in (6,7,8,9) else winter if m in (12,1,2) else swing
    return random.choice(pool)

def _pick_keyword()->str:
    # 소스별 키워드 수집(순서 보존, 중복 제거)
    golden=_read_col_csv("golden_shopping_keywords.csv")
    shop  =_read_col_csv("keywords_shopping.csv")
    line  =_read_line_csv("keywords.csv")
    union=[]
    seen=set()
    for k in golden + shop + line:
        if k and k not in seen:
            seen.add(k); union.append(k)

    if not union:
        fb=_seasonal_fallback()
        print(f"[AFFILIATE] WARN: no shopping keywords -> seasonal '{fb}'")
        return fb

    last, used_today = _load_usage_map()
    today=datetime.utcnow().date()

    def days_since(kw:str)->int:
        d=last.get(kw)
        return (today - d).days if d else 10**6  # 한 번도 안 쓴 경우 최우선

    # 1) 당일 사용 키워드는 제외(설정 켜져 있으면)
    pool = [k for k in union if (k not in used_today) or (not NO_REPEAT_TODAY)]
    if not pool:
        pool = union[:]  # 전부 오늘 쓴 상태면 전체로 진행

    # 2) 최근 n일 내 사용은 후순위 (fresh 먼저, stale 나중)
    fresh=[k for k in pool if days_since(k) >= AFF_USED_BLOCK_DAYS]
    stale=[k for k in pool if days_since(k) <  AFF_USED_BLOCK_DAYS]

    # 3) 각 그룹 내부에서 LRU(가장 오래 안 쓴) 우선: days_since 내림차순
    fresh.sort(key=days_since, reverse=True)
    stale.sort(key=days_since, reverse=True)

    ordered = (fresh + stale) if (fresh or stale) else sorted(pool, key=days_since, reverse=True)
    pick = ordered[0]

    if fresh:
        print(f"[AFFILIATE] pick '{pick}' (unused >= {AFF_USED_BLOCK_DAYS}d or never)")
    else:
        print(f"[AFFILIATE] all candidates used < {AFF_USED_BLOCK_DAYS}d; pick least-recently-used -> '{pick}'")

    return pick

# ===== Tag util =====
def _clean_hashtag_token(s:str)->str:
    s=re.sub(r"[^\w가-힣]","",s)
    bans={"쿠팡","파트너스","최저가","할인","세일","쿠폰","딜","무료배송"}
    return "" if (not s or s in bans) else s

def _make_tags(kw:str)->List[str]:
    return [kw] if FORCE_SINGLE_TAG else [t for t in {_clean_hashtag_token(x) for x in re.split(r"\s+|,|/|_",kw)} if t][:3] or [kw]

# ===== WP =====
def _ensure_term(kind,name)->Optional[int]:
    url=f"{WP_URL}/wp-json/wp/v2/{kind}"
    r=requests.get(url,params={"search":name,"per_page":50},
                   headers=REQ_HEADERS,auth=(WP_USER,WP_APP_PASSWORD),
                   verify=WP_TLS_VERIFY,timeout=15)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name:
            return int(it["id"])
    r=requests.post(url,json={"name":name},
                    headers=REQ_HEADERS,auth=(WP_USER,WP_APP_PASSWORD),
                    verify=WP_TLS_VERIFY,timeout=15)
    r.raise_for_status()
    return int(r.json()["id"])

def _post_wp(title,content_html,when_gmt,category,tags)->Dict:
    cat_id=_ensure_term("categories",category or DEFAULT_CATEGORY)
    tag_ids=[_ensure_term("tags",t) for t in (tags or []) if t]
    payload={
        "title":title,"content":content_html,"status":POST_STATUS,
        "categories":[cat_id],"tags":[tid for tid in tag_ids if tid],
        "comment_status":"closed","ping_status":"closed","date_gmt":when_gmt,
    }
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts",json=payload,
                    headers=REQ_HEADERS,auth=(WP_USER,WP_APP_PASSWORD),
                    verify=WP_TLS_VERIFY,timeout=20)
    r.raise_for_status()
    return r.json()

# ===== Coupang link (간략) =====
def _coupang_search_url(kw:str)->str:
    return "https://www.coupang.com/np/search?q="+urllib.parse.quote(kw) if kw.strip() else "https://www.coupang.com/"

def _gen_review_html(kw, deeplink, img_url="", search_url="")->str:
    sys_p="너는 사람스러운 한국어 블로거다. 광고처럼 보이지 않게 직접 써본 것처럼 쓴다."
    usr=(f"주제 제품: {kw}\n링크: {deeplink}\n요청:\n"
         "- 도입 2~3문장\n- <h2>/<h3> 소제목\n- 불릿 4~6개(과장 금지)\n"
         "- '가격/가성비' 섹션\n- '장단점' 섹션\n- '이런 분께 추천' 섹션\n- 1200~1300자\n"
         "- HTML만 (<p>,<h2>,<h3>,<ul>,<li>,<a>,...)")
    try:
        r=_client.chat.completions.create(
            model=OPENAI_MODEL_LONG or OPENAI_MODEL,
            messages=[{"role":"system","content":sys_p},{"role":"user","content":usr}],
            temperature=0.85,max_tokens=1100,
        )
        body=(r.choices[0].message.content or "").strip()
    except BadRequestError:
        body=""
    body=re.sub(r"```(?:\w+)?","",body).replace("```","").strip()
    final=deeplink or search_url or _coupang_search_url(kw)
    css=(
    '<style>.post-affil p{line-height:1.84;margin:0 0 14px;color:#222}'
    '.post-affil h2{margin:28px 0 12px;font-size:1.45rem;line-height:1.35;border-left:6px solid #3b82f6;padding-left:10px}'
    '.post-affil h3{margin:22px 0 10px;font-size:1.15rem;color:#0f172a}'
    '.post-affil ul{padding-left:22px;margin:10px 0}.post-affil li{margin:6px 0}'
    '.post-affil .cta{text-align:center;margin:26px 0}'
    '.post-affil .btn-cta{display:inline-flex;align-items:center;gap:8px;justify-content:center;padding:16px 28px;border-radius:999px;font-weight:900;font-size:1.05rem;text-decoration:none;background:linear-gradient(135deg,#ff6a00,#ee0979);color:#fff;box-shadow:0 10px 26px rgba(238,9,121,.42)}'
    '.post-affil .btn-ghost{display:inline-flex;align-items:center;gap:8px;justify-content:center;padding:14px 22px;border-radius:999px;font-weight:700;font-size:1rem;text-decoration:none;background:#fff;color:#0f172a;border:1px solid #d1d5db;box-shadow:0 4px 10px rgba(2,6,23,.08)}'
    '@media (max-width:640px){ .post-affil .btn-cta,.post-affil .btn-ghost{width:100%}}</style>'
    )
    cta1=f'<a class="btn-ghost" href="{html.escape(final)}" target="_blank" rel="sponsored noopener">쿠팡에서 최저가 확인하기</a>'
    cta2=f'<a class="btn-cta" href="{html.escape(final)}" target="_blank" rel="sponsored noopener">제품 보러가기</a>'
    return f'{css}<div class="post-affil"><p class="disc">{html.escape(DISCLOSURE_TEXT)}</p>{body}<div class="cta">{cta1}</div><div class="cta">{cta2}</div></div>'

# ===== Main =====
def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY 필요")

    kw=_pick_keyword()
    title=f"{kw} 제대로 써보고 알게 된 포인트"
    when_gmt=_slot_or_next_day(AFFILIATE_TIME_KST)
    html_body=_gen_review_html(kw, _coupang_search_url(kw))

    res=_post_wp(title, html_body, when_gmt, DEFAULT_CATEGORY, [kw])
    if res.get("id"):
        _mark_used(kw)
        _rotate_csvs_on_success(kw)   # 안전망: 스크립트에서도 즉시 제거

    print(json.dumps({
        "post_id": res.get("id"),
        "link": res.get("link"),
        "status": res.get("status"),
        "date_gmt": res.get("date_gmt"),
        "title": res.get("title",{}).get("rendered",title),
        "keyword": kw
    }, ensure_ascii=False))

if __name__=="__main__":
    try:
        main()
    except Exception as e:
        print(f"[AFFILIATE][ERROR] {type(e).__name__}: {e}", file=sys.stderr)
        raise
