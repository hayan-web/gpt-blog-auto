# -*- coding: utf-8 -*-
"""
affiliate_post.py — Coupang Partners 글 자동 포스팅 (템플릿 고정/상단 CTA/고지문 강조)
- 상단 고지문(강조) + 상단 CTA 버튼 + 기존 섹션 구조 복원
- 하단 CTA 버튼 유지
- URL 없을 때 쿠팡 검색 링크 폴백(이탈 방지)
- 골든키워드 회전/사용로그/예약 충돌 회피(기존 동작 유지)
"""
import os, re, csv, json, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Tuple, Optional
import requests

from dotenv import load_dotenv
load_dotenv()

# ===== ENV =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()

DEFAULT_CATEGORY=(os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip() or "쇼핑"
DEFAULT_TAGS=(os.getenv("AFFILIATE_TAGS") or "").strip()
DISCLOSURE_TEXT=(os.getenv("DISCLOSURE_TEXT") or "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공합니다.").strip()

BUTTON_TEXT=(os.getenv("BUTTON_TEXT") or "쿠팡에서 최저가 확인하기").strip()
USE_IMAGE=((os.getenv("USE_IMAGE") or "").strip().lower() in ("1","true","y","yes","on"))

AFFILIATE_TIME_KST=(os.getenv("AFFILIATE_TIME_KST") or "13:00").strip()

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-affiliate/1.6"
USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_FILE=os.path.join(USAGE_DIR,"used_shopping.txt")

# switches/guards
NO_REPEAT_TODAY=(os.getenv("NO_REPEAT_TODAY") or "1").lower() in ("1","true","y","yes","on")
AFF_USED_BLOCK_DAYS=int(os.getenv("AFF_USED_BLOCK_DAYS") or "30")

# seeds
PRODUCTS_SEED_CSV=(os.getenv("PRODUCTS_SEED_CSV") or "products_seed.csv")
FALLBACK_KWS=os.getenv("AFF_FALLBACK_KEYWORDS") or "휴대용 선풍기, 제습기, 무선 청소기"

REQ_HEADERS={
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ===== TIME =====
def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

def _slot_affiliate()->str:
    """ AFFILIATE_TIME_KST 기준으로 충돌 시 +1일씩 밀어 예약 """
    hh, mm = [int(x) for x in (AFFILIATE_TIME_KST.split(":")+["0"])[:2]]
    now = _now_kst()
    tgt = now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt <= now: tgt += timedelta(days=1)

    # 충돌 확인 (±2분)
    for _ in range(7):
        utc = tgt.astimezone(timezone.utc)
        if _wp_future_exists_around(utc, tol_min=2):
            print(f"[SLOT] conflict at {utc.strftime('%Y-%m-%dT%H:%M:%S')}Z -> push +1d")
            tgt += timedelta(days=1)
            continue
        break
    final = tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[SLOT] scheduled UTC = {final}")
    return final

def _wp_future_exists_around(when_gmt_dt: datetime, tol_min: int = 2) -> bool:
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    try:
        r = requests.get(
            url, params={"status":"future","per_page":100,"orderby":"date","order":"asc","context":"edit"},
            headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20
        ); r.raise_for_status()
        items = r.json()
    except Exception as e:
        print(f"[WP][WARN] future list fetch failed: {type(e).__name__}: {e}")
        return False
    tgt = when_gmt_dt.astimezone(timezone.utc)
    win = timedelta(minutes=max(1,int(tol_min)))
    lo, hi = tgt - win, tgt + win
    for it in items:
        d=(it.get("date_gmt") or "").strip()
        if not d: continue
        try:
            dt=datetime.fromisoformat(d.replace("Z","+00:00"))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
            else: dt=dt.astimezone(timezone.utc)
        except Exception:
            continue
        if lo <= dt <= hi:
            return True
    return False

# ===== USED LOG =====
def _ensure_usage_dir(): os.makedirs(USAGE_DIR, exist_ok=True)

def _load_used_set(days:int=30)->set:
    _ensure_usage_dir()
    if not os.path.exists(USED_FILE): return set()
    cutoff=datetime.utcnow().date()-timedelta(days=days)
    used=set()
    with open(USED_FILE,"r",encoding="utf-8",errors="ignore") as f:
        for line in f:
            line=line.strip()
            if not line: continue
            try:
                d_str, kw = line.split("\t",1)
                if datetime.strptime(d_str,"%Y-%m-%d").date()>=cutoff:
                    used.add(kw.strip())
            except Exception:
                used.add(line)
    return used

def _mark_used(kw:str):
    _ensure_usage_dir()
    with open(USED_FILE,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw.strip()}\n")

# ===== CSV HELPERS =====
def _read_col_csv(path:str)->List[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8",newline="") as f:
        rd=csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and (row[0].strip().lower() in ("keyword","title")): continue
            if row[0].strip(): out.append(row[0].strip())
    return out

def _consume_col_csv(path:str, kw:str)->bool:
    if not os.path.exists(path): return False
    with open(path,"r",encoding="utf-8",newline="") as f:
        rows=list(csv.reader(f))
    if not rows: return False
    has_header=rows[0] and rows[0][0].strip().lower() in ("keyword","title")
    body=rows[1:] if has_header else rows[:]
    before=len(body)
    body=[r for r in body if (r and r[0].strip()!=kw)]
    if len(body)==before: return False
    new_rows=([rows[0]] if has_header else [])+[[r[0].strip()] for r in body]
    with open(path,"w",encoding="utf-8",newline="") as f:
        csv.writer(f).writerows(new_rows)
    return True

# ===== PICK KEYWORD =====
def pick_affiliate_keyword()->str:
    used_today = _load_used_set(1) if NO_REPEAT_TODAY else set()
    used_block = _load_used_set(AFF_USED_BLOCK_DAYS)

    gold=_read_col_csv("golden_shopping_keywords.csv")
    shop=_read_col_csv("keywords_shopping.csv")
    pool=[k for k in gold+shop if k and (k not in used_block)]
    if NO_REPEAT_TODAY:
        removed=[k for k in pool if k in used_today]
        if removed: print(f"[FILTER] removed (used today): {removed[:8]}")
        pool=[k for k in pool if k not in used_today]

    if pool: return pool[0].strip()

    # fallback
    fb=[x.strip() for x in FALLBACK_KWS.split(",") if x.strip()]
    if fb:
        print(f"[AFFILIATE] fallback -> '{fb[0]}'")
        return fb[0]
    return "휴대용 선풍기"

# ===== PRODUCT URL (safe fallback to Coupang search) =====
def resolve_product_url(keyword:str)->str:
    # 1) products_seed.csv 우선
    if os.path.exists(PRODUCTS_SEED_CSV):
        try:
            with open(PRODUCTS_SEED_CSV,"r",encoding="utf-8") as f:
                rd=csv.DictReader(f)
                for r in rd:
                    if (r.get("keyword") or "").strip()==keyword and (r.get("url") or "").strip():
                        return r["url"].strip()
                    if (r.get("product_name") or "").strip()==keyword and (r.get("url") or "").strip():
                        return r["url"].strip()
                    if (r.get("raw_url") or "").strip() and (r.get("product_name") or "").strip()==keyword:
                        return r["raw_url"].strip()
        except Exception as e:
            print(f"[SEED][WARN] read error: {e}")

    # 2) 안전 폴백: 쿠팡 검색 페이지
    from urllib.parse import quote_plus
    q = quote_plus(keyword)
    return f"https://www.coupang.com/np/search?q={q}"

# ===== WP =====
def _ensure_term(kind:str, name:str)->int:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}", params={"search":name,"per_page":50,"context":"edit"},
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
        try:
            tid=_ensure_term("tags", tag); tag_ids=[tid]
        except Exception:
            pass
    payload={
        "title": title,
        "content": html_body,
        "status": POST_STATUS,
        "categories": [cat_id],
        "tags": tag_ids,
        "comment_status": "closed",
        "ping_status": "closed",
        "date_gmt": when_gmt
    }
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status(); return r.json()

# ===== TEMPLATE =====
def _css_block()->str:
    return """
<style>
.aff-wrap{font-family:inherit}
.aff-disclosure{margin:0 0 16px;padding:12px 14px;border:2px solid #ef4444;background:#fff1f2;color:#991b1b;font-weight:700;border-radius:10px}
.aff-cta{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0 22px}
.aff-cta a{display:inline-block;padding:12px 18px;border-radius:999px;text-decoration:none;background:#2563eb;color:#fff;font-weight:700}
.aff-cta a:hover{opacity:.95}
.aff-section h2{margin:28px 0 12px;font-size:1.42rem;line-height:1.35;border-left:6px solid #22c55e;padding-left:10px}
.aff-section h3{margin:18px 0 10px;font-size:1.12rem}
.aff-section p{line-height:1.9;margin:0 0 14px;color:#222}
.aff-section ul{padding-left:22px;margin:10px 0}
.aff-section li{margin:6px 0}
.aff-table{border-collapse:collapse;width:100%;margin:16px 0}
.aff-table th,.aff-table td{border:1px solid #e2e8f0;padding:10px;text-align:left}
.aff-table thead th{background:#f1f5f9}
.aff-note{font-style:italic;color:#334155;margin-top:6px}
</style>
""".strip()

def render_affiliate_html(keyword:str, url:str, image:str="")->str:
    """ 상단 고지문 + 상단 CTA + 본문 섹션 + 하단 CTA """
    btn_txt = html.escape(BUTTON_TEXT)
    disc = html.escape(DISCLOSURE_TEXT)
    url_esc = html.escape(url or "#")
    kw_esc = html.escape(keyword)

    img_html = ""
    if image and USE_IMAGE:
        img_html = f'<figure style="margin:0 0 18px"><img src="{html.escape(image)}" alt="{kw_esc}" loading="lazy" decoding="async" style="max-width:100%;height:auto;border-radius:12px"></figure>'

    return f"""
{_css_block()}
<div class="aff-wrap aff-section">
  <p class="aff-disclosure"><strong>{disc}</strong></p>

  <div class="aff-cta">
    <a href="{url_esc}" target="_blank" rel="nofollow sponsored noopener" aria-label="{btn_txt}">{btn_txt}</a>
  </div>

  {img_html}

  <h2>{kw_esc} 선택 시 고려해야 할 요소</h2>
  <p>{kw_esc}를(을) 선택할 때는 용도·공간·소음·관리 편의·예산의 균형을 먼저 잡아야 합니다. 이하 1분 체크리스트로 빠르게 감만 잡고 상세 섹션에서 구체화하세요.</p>
  <ul>
    <li>필요 환경: 어느 공간/누구용인지</li>
    <li>핵심 스펙: 성능 대비 과투자 방지</li>
    <li>관리 난도: 세척·보관·소모품</li>
    <li>총비용: 구매가 + 유지비</li>
  </ul>

  <h2>주요 특징</h2>
  <ul>
    <li>간편한 사용성과 휴대/이동성</li>
    <li>상황별 풍속/모드 조절(있다면 자동/타이머 활용)</li>
    <li>USB/무선 등 전원 옵션과 호환성</li>
    <li>거치대/스트랩 등 액세서리로 활용성 확대</li>
  </ul>

  <h2>가격/가성비</h2>
  <p>동급 제품의 가격대는 시즌·재고·프로모션에 따라 크게 변동합니다. 아래 기준으로 합리 범위를 먼저 잡아보세요.</p>
  <table class="aff-table">
    <thead><tr><th>체크</th><th>포인트</th></tr></thead>
    <tbody>
      <tr><td>성능</td><td>공간/목적 대비 충분한지</td></tr>
      <tr><td>관리</td><td>세척·보관·소모품 비용/난도</td></tr>
      <tr><td>비용</td><td>구매가 + 유지비, 시즌 특가</td></tr>
    </tbody>
  </table>
  <p class="aff-note">* 시즌 아이템은 타이밍이 가성비를 좌우합니다.</p>

  <h2>장단점</h2>
  <h3>장점</h3>
  <ul>
    <li>가벼운 사용 난도, 어디서든 간편</li>
    <li>필요 기능 위주 선택 시 경제적</li>
    <li>모드·거치 옵션 등 확장성</li>
  </ul>
  <h3>단점</h3>
  <ul>
    <li>배터리/소모품 교체 주기 고려</li>
    <li>상위급 대비 세밀한 성능 한계</li>
  </ul>

  <h2>이런 분께 추천</h2>
  <ul>
    <li>여행/야외/서브 용도로 간편한 제품이 필요한 분</li>
    <li>가볍게 시작해보고 이후 업그레이드 계획인 분</li>
    <li>선물/비상용 등 무난한 선택지를 찾는 분</li>
  </ul>

  <div class="aff-cta" style="margin-top:22px">
    <a href="{url_esc}" target="_blank" rel="nofollow sponsored noopener" aria-label="{btn_txt}">{btn_txt}</a>
  </div>
</div>
""".strip()

# ===== TITLE =====
def build_title(keyword:str)->str:
    # 예: "{키워드} 제대로 써보고 알게 된 포인트"
    s=f"{keyword} 제대로 써보고 알게 된 포인트"
    s = re.sub(r"\s+"," ", html.unescape(s)).strip()
    return s[:90]

# ===== RUN =====
def rotate_sources(kw:str):
    changed=False
    if _consume_col_csv("golden_shopping_keywords.csv",kw):
        print(f"[ROTATE] removed '{kw}' from golden_shopping_keywords.csv"); changed=True
    if _consume_col_csv("keywords_shopping.csv",kw):
        print(f"[ROTATE] removed '{kw}' from keywords_shopping.csv"); changed=True
    if not changed:
        print("[ROTATE] nothing removed (maybe already rotated)")

def run_once():
    print(f"[USAGE] NO_REPEAT_TODAY={NO_REPEAT_TODAY}, AFF_USED_BLOCK_DAYS={AFF_USED_BLOCK_DAYS}")
    kw = pick_affiliate_keyword()
    url = resolve_product_url(kw)

    when_gmt = _slot_affiliate()
    title = build_title(kw)
    body = render_affiliate_html(kw, url)

    res = post_wp(title, body, when_gmt, category=DEFAULT_CATEGORY, tag=kw)
    link = res.get("link")
    print(json.dumps({"post_id":res.get("id") or res.get("post") or 0, "link": link, "status":res.get("status"), "date_gmt":res.get("date_gmt"), "title": title, "keyword": kw}, ensure_ascii=False))

    _mark_used(kw)
    rotate_sources(kw)

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    run_once()

if __name__=="__main__":
    main()
