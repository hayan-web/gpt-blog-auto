# -*- coding: utf-8 -*-
"""
affiliate_post.py — Coupang Partners 자동 포스팅(단일 '제품 보기' 버튼, 스킵 금지)
- 키워드: 풀 비어도 절대 스킵하지 않고 폴백/변형으로 1개 확보
- 밴: .usage/ban_keywords_shopping.txt + BAN_KEYWORDS 동시 적용(부분문자열 매칭)
- 사용한 키워드는 즉시 회전 & used_shopping.txt 기록
"""
import os, re, csv, json, html, random, requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Tuple
from urllib.parse import quote_plus
from dotenv import load_dotenv
load_dotenv()

# ===== ENV =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()

DEFAULT_CATEGORY=(os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip() or "쇼핑"
DISCLOSURE_TEXT=(os.getenv("DISCLOSURE_TEXT") or "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공합니다.").strip()

BUTTON_PRIMARY=(os.getenv("BUTTON_TEXT") or "제품 보기").strip()
USE_IMAGE=((os.getenv("USE_IMAGE") or "").strip().lower() in ("1","true","y","yes","on"))
AFFILIATE_TIME_KST=(os.getenv("AFFILIATE_TIME_KST") or "13:00").strip()

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-affiliate/3.0"
USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_FILE=os.path.join(USAGE_DIR,"used_shopping.txt")

NO_REPEAT_TODAY=(os.getenv("NO_REPEAT_TODAY") or "1").lower() in ("1","true","y","yes","on")
AFF_USED_BLOCK_DAYS=int(os.getenv("AFF_USED_BLOCK_DAYS") or "30")

PRODUCTS_SEED_CSV=(os.getenv("PRODUCTS_SEED_CSV") or "products_seed.csv")
BAN_FROM_ENV=[s.strip() for s in (os.getenv("BAN_KEYWORDS") or "").split(",") if s.strip()]
AFF_FALLBACK=[s.strip() for s in (os.getenv("AFF_FALLBACK_KEYWORDS") or "").split(",") if s.strip()]

REQ_HEADERS={"User-Agent":USER_AGENT,"Accept":"application/json","Content-Type":"application/json; charset=utf-8"}

# ===== ban / used =====
BAN_FILE=os.path.join(USAGE_DIR,"ban_keywords_shopping.txt")
def _load_bans():
    bans=set(BAN_FROM_ENV)
    if os.path.exists(BAN_FILE):
        for ln in open(BAN_FILE,"r",encoding="utf-8",errors="ignore"):
            ln=ln.strip()
            if ln: bans.add(ln)
    return sorted(bans, key=len, reverse=True)

def _read_recent_used(n:int=8)->list[str]:
    p=USED_FILE
    if not os.path.exists(p): return []
    lines=[ln.strip() for ln in open(p,"r",encoding="utf-8").read().splitlines() if ln.strip()]
    body=[ln.split("\t",1)[1] if "\t" in ln else ln for ln in lines]
    return list(reversed(body[-n:]))

def _load_used(days:int=365)->set[str]:
    used=set()
    if not os.path.exists(USED_FILE): return used
    cutoff=datetime.utcnow().date()-timedelta(days=days)
    with open(USED_FILE,"r",encoding="utf-8",errors="ignore") as f:
        for ln in f:
            ln=ln.strip()
            if not ln: continue
            if "\t" in ln:
                d,k=ln.split("\t",1)
                try:
                    if datetime.strptime(d,"%Y-%m-%d").date()>=cutoff:
                        used.add(k.strip())
                except:
                    used.add(k.strip())
            else:
                used.add(ln)
    return used

def _mark_used(kw:str):
    os.makedirs(USAGE_DIR,exist_ok=True)
    with open(USED_FILE,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw.strip()}\n")

def _banned_or_used(kw:str, bans:list[str], used:set[str])->bool:
    if kw in used: return True
    return any(b and b in kw for b in bans)

# ===== csv utils =====
def _read_col(path:str)->list[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8",newline="") as f:
        rd=csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and (row[0].strip().lower() in ("keyword","title")): continue
            if row[0].strip(): out.append(row[0].strip())
    return out

def _consume_col(path:str, kw:str)->bool:
    if not os.path.exists(path): return False
    rows=list(csv.reader(open(path,"r",encoding="utf-8",newline="")))
    if not rows: return False
    has_header=rows[0] and rows[0][0].strip().lower() in ("keyword","title")
    body=rows[1:] if has_header else rows[:]
    before=len(body)
    body=[r for r in body if (r and r[0].strip()!=kw)]
    if len(body)==before: return False
    new_rows=([rows[0]] if has_header else [])+[[r[0].strip()] for r in body]
    csv.writer(open(path,"w",encoding="utf-8",newline="")).writerows(new_rows)
    return True

# ===== picker =====
def _variants(base:str)->list[str]:
    mods=["미니","컴팩트","저전력","저소음","가성비","프리미엄","USB","무선","휴대용","대용량"]
    out=[f"{m} {base}" for m in mods] + [base]
    return out

def pick_affiliate_keyword()->str:
    bans=_load_bans()
    used_today=_load_used(1) if NO_REPEAT_TODAY else set()
    used_block=_load_used(AFF_USED_BLOCK_DAYS)
    recent=set(_read_recent_used(8))

    gold=_read_col("golden_shopping_keywords.csv")
    shop=_read_col("keywords_shopping.csv")

    pool=[]
    for k in gold+shop:
        if not k: continue
        if _banned_or_used(k,bans,used_block): continue
        if NO_REPEAT_TODAY and k in used_today: continue
        if k in recent: continue
        pool.append(k)

    if pool:
        return pool[0]

    # 폴백에서도 무조건 하나 만든다
    bases = [b for b in AFF_FALLBACK if b and not any(ban in b for ban in bans)]
    if not bases:  # 안전한 내장 폴백
        bases = ["히터","제습기","보조배터리","무선 청소기","전기포트"]

    for b in bases:
        for cand in _variants(b):
            if not _banned_or_used(cand,bans,used_block|used_today|recent):
                return cand

    # 마지막 보호 — 그래도 없으면 금지어를 회피하는 임의 조합 생성
    i=1
    while True:
        cand = f"저전력 {random.choice(bases)} {i}"
        if not _banned_or_used(cand,bans,used_block|used_today|recent):
            return cand
        i+=1

# ===== URL =====
def resolve_product_url(keyword:str)->str:
    p="products_seed.csv"
    if os.path.exists(p):
        try:
            for r in csv.DictReader(open(p,"r",encoding="utf-8")):
                if (r.get("keyword") or "").strip()==keyword and (r.get("url") or "").strip():
                    return r["url"].strip()
                if (r.get("product_name") or "").strip()==keyword and (r.get("url") or "").strip():
                    return r["url"].strip()
                if (r.get("raw_url") or "").strip() and (r.get("product_name") or "").strip()==keyword:
                    return r["raw_url"].strip()
        except: pass
    return f"https://www.coupang.com/np/search?q={quote_plus(keyword)}"

# ===== WP =====
def _ensure_term(kind:str, name:str)->int:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}",
                   params={"search":name,"per_page":50,"context":"edit"},
                   auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name: return int(it["id"])
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status(); return int(r.json()["id"])

def post_wp(title:str, html_body:str, when_gmt:str, category:str, tag:str)->dict:
    cat_id=_ensure_term("categories", category or DEFAULT_CATEGORY)
    tag_ids=[]
    if tag:
        try: tag_ids=[_ensure_term("tags", tag)]
        except: pass
    payload={
        "title": title, "content": html_body, "status": POST_STATUS,
        "categories": [cat_id], "tags": tag_ids,
        "comment_status": "closed","ping_status": "closed","date_gmt": when_gmt
    }
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status(); return r.json()

def _now_kst(): from zoneinfo import ZoneInfo; return datetime.now(ZoneInfo("Asia/Seoul"))
def _wp_future_exists_around(when_gmt_dt, tol_min:int=2)->bool:
    try:
        r=requests.get(f"{WP_URL}/wp-json/wp/v2/posts",
            params={"status":"future","per_page":100,"orderby":"date","order":"asc","context":"edit"},
            headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20)
        r.raise_for_status(); items=r.json()
    except: return False
    tgt=when_gmt_dt.astimezone(timezone.utc); win=timedelta(minutes=max(1,int(tol_min)))
    lo,hi=tgt-win,tgt+win
    for it in items:
        d=(it.get("date_gmt") or "").strip()
        if not d: continue
        try:
            dt=datetime.fromisoformat(d.replace("Z","+00:00"))
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except: continue
        if lo<=dt<=hi: return True
    return False

def _slot_affiliate()->str:
    hh,mm=[int(x) for x in (AFFILIATE_TIME_KST.split(":")+["0"])[:2]]
    now=_now_kst()
    tgt=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    for _ in range(7):
        utc=tgt.astimezone(timezone.utc)
        if _wp_future_exists_around(utc,2):
            tgt+=timedelta(days=1); continue
        break
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ===== style / html =====
def _css_block()->str:
    return """
<style>
.aff-wrap{font-family:inherit;line-height:1.65}
.aff-disclosure{margin:0 0 16px;padding:12px 14px;border:2px solid #334155;background:#f1f5f9;color:#0f172a;border-radius:12px;font-size:.96rem}
.aff-sub{margin:10px 0 6px;font-size:1.2rem;color:#334155}
.aff-hr{border:0;border-top:1px solid #e5e7eb;margin:16px 0}
.aff-cta-row{display:flex;align-items:center;justify-content:center;gap:14px;width:100%;margin:24px auto 18px;text-align:center}
.aff-btn{display:inline-flex !important;align-items:center;justify-content:center;padding:16px 28px;font-size:1.08rem;line-height:1;min-width:280px;border-radius:9999px;text-decoration:none;font-weight:800;box-sizing:border-box}
.aff-btn--primary{background:#0ea5e9;color:#fff}
.aff-btn:hover{transform:translateY(-1px);box-shadow:0 8px 20px rgba(0,0,0,.12)}
@media (max-width:540px){.aff-btn{width:100%;min-width:0}}
.aff-table{width:100%;border-collapse:collapse;margin:8px 0 14px}
.aff-table th,.aff-table td{border:1px solid #e5e7eb;padding:8px 10px;text-align:left}
.aff-table thead th{background:#f8fafc}
.aff-wrap h2{margin:18px 0 6px}.aff-wrap h3{margin:16px 0 6px}
</style>
"""

def _cta_single(url:str, label:str)->str:
    u=html.escape(url or "#"); l=html.escape(label or "제품 보기")
    return f'<div class="aff-cta-row"><a class="aff-btn aff-btn--primary" href="{u}" target="_blank" rel="nofollow sponsored noopener" aria-label="{l}">{l}</a></div>'

def render_affiliate_html(keyword:str, url:str)->str:
    disc=html.escape(DISCLOSURE_TEXT); kw=html.escape(keyword)
    table_html="""
<table class="aff-table">
  <thead><tr><th>항목</th><th>확인 포인트</th><th>비고</th></tr></thead>
  <tbody>
    <tr><td>성능</td><td>공간/목적 대비 충분한지</td><td>과투자 방지</td></tr>
    <tr><td>관리</td><td>세척·보관·소모품</td><td>난도/주기</td></tr>
    <tr><td>비용</td><td>구매가 + 유지비</td><td>시즌 특가</td></tr>
  </tbody>
</table>
""".strip()
    body=f"""
{_css_block()}
<div class="aff-wrap">
  <p class="aff-disclosure"><strong>{disc}</strong></p>
  <h2 class="aff-sub">{kw} 한 눈에 보기</h2>
  <p>{kw}를 중심으로 핵심만 간단히 정리했어요. 요약→선택 기준→팁→장단점 순서예요.</p>
  <hr class="aff-hr">
  {_cta_single(url, BUTTON_PRIMARY)}
  <h3>선택 기준 3가지</h3><p>공간/목적, 관리 난도, 총비용.</p>{table_html}<hr class="aff-hr">
  <h3>장점</h3><p>간편한 접근성, 부담 없는 유지비, 상황별 확장성.</p><hr class="aff-hr">
  <h3>단점</h3><p>소모품/배터리 주기, 상위급 대비 성능 한계.</p><hr class="aff-hr">
  <h3>추천</h3><p>가볍게 시작하고 필요하면 업그레이드하려는 분께 적합.</p>
  {_cta_single(url, BUTTON_PRIMARY)}
</div>
""".strip()
    return body

# ===== rotate & run =====
def rotate_sources(kw:str):
    changed=False
    if _consume_col("golden_shopping_keywords.csv",kw): changed=True
    if _consume_col("keywords_shopping.csv",kw): changed=True
    print("[ROTATE] rotated" if changed else "[ROTATE] nothing removed")

def _build_title(kw:str)->str:
    t = re.sub(r"\s+"," ", f"{kw} 이렇게 쓰니 편해요").strip()
    return t[:42]

def run_once():
    kw = pick_affiliate_keyword()
    url = resolve_product_url(kw)
    when_gmt = _slot_affiliate()
    title = _build_title(kw)
    html_body = render_affiliate_html(kw, url)
    res = post_wp(title, html_body, when_gmt, category=DEFAULT_CATEGORY, tag=kw)
    print(json.dumps({"post_id":res.get("id") or 0,"link":res.get("link"),
                      "status":res.get("status"),"date_gmt":res.get("date_gmt"),
                      "title":title,"keyword":kw}, ensure_ascii=False))
    _mark_used(kw)
    rotate_sources(kw)

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    run_once()

if __name__=="__main__":
    main()
