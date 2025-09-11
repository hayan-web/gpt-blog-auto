# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일반(일상/뉴스) 글 자동 포스팅 (안정판)
- 키워드: keywords_general.csv 에서 1~N개 소비 (중복 회피)
- 제목: 키워드 기반 한국어 자연 문장 (후킹/담백 혼합), 길이 가드
- 본문: 상단 AdSense + '짧은 서문/핵심 포인트/한줄 메모/관련 링크' 블록
- CTA: 카테고리 글 모아보기 + 홈으로 이동 (가운데 정렬/가로 확장/호버만 적용)
- 예약 충돌: date_gmt ±2분 내 미래 글 있으면 +1일 롤오버
"""
import os, re, csv, json, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List
import requests
from dotenv import load_dotenv
from urllib.parse import quote

load_dotenv()

# ===== ENV =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()

EXISTING_CATEGORIES=[x.strip() for x in (os.getenv("EXISTING_CATEGORIES") or "뉴스,정보").split(",") if x.strip()]
DEFAULT_CATEGORY="정보"

GENERAL_TIMES_KST=[x.strip() for x in (os.getenv("GENERAL_TIMES_KST") or "10:00,17:00").split(",") if x.strip()]
MODE=os.getenv("MODE") or ""
USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-general/2.0"
USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_FILE=os.path.join(USAGE_DIR,"used_general.txt")

REQ_HEADERS={
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ===== Helpers =====
def _ensure_usage_dir(): os.makedirs(USAGE_DIR, exist_ok=True)

def _read_col_csv(path:str)->List[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8",newline="") as f:
        rd=csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and row[0].strip().lower() in ("keyword","title"): continue
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

def _mark_used(kw:str):
    _ensure_usage_dir()
    with open(USED_FILE,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw.strip()}\n")

def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

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
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except Exception:
            continue
        if lo <= dt <= hi:
            return True
    return False

def _slot_for(hhmm:str)->str:
    hh, mm = [int(x) for x in (hhmm.split(":")+["0"])[:2]]
    now = _now_kst()
    tgt = now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt <= now: tgt += timedelta(days=1)
    for _ in range(7):
        utc = tgt.astimezone(timezone.utc)
        if _wp_future_exists_around(utc, tol_min=2):
            print(f"[SLOT] conflict at {utc.strftime('%Y-%m-%dT%H:%M:%S')}Z -> push +1d")
            tgt += timedelta(days=1); continue
        break
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

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

def _category_url_for(name:str)->str:
    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/categories",
            params={"search": name, "per_page": 50, "context":"view"},
            headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=12
        )
        r.raise_for_status()
        items = r.json()
        for it in items:
            if (it.get("name") or "").strip() == name:
                link = (it.get("link") or "").strip()
                if link: return link
        if items and (items[0].get("link") or "").strip():
            return items[0]["link"].strip()
    except Exception as e:
        print(f"[CAT][WARN] fallback category url for '{name}': {type(e).__name__}: {e}")
    return f"{WP_URL}/category/{quote(name)}/"

def post_wp(title:str, html_body:str, when_gmt:str, category:str)->dict:
    cat_id=_ensure_term("categories", category or DEFAULT_CATEGORY)
    payload={
        "title": title,
        "content": html_body,
        "status": POST_STATUS,
        "categories": [cat_id],
        "comment_status": "closed",
        "ping_status": "closed",
        "date_gmt": when_gmt
    }
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status(); return r.json()

# ===== Title & Body =====
def _clean(s:str)->str:
    s = html.unescape((s or "").strip())
    s = s.replace("“","").replace("”","").replace("‘","").replace("’","").strip('"\' ')
    s = re.sub(r"\s+", " ", s)
    return s

def build_title_from_keyword(kw:str)->str:
    kw = _clean(kw)
    # 뉴스성/정보성에 따라 담백한 한 문장
    if any(x in kw for x in ("속보","브리핑","단독","발표","출시","공개","전망","분석","뉴스")):
        base = f"{kw} – 오늘 한 줄로 정리"
    else:
        base = f"{kw}에 대한 오늘의 기록"
    # 길이 가드
    if len(base) < 14: base = f"{kw}에 대한 짧은 기록"
    if len(base) > 42: base = base[:41].rstrip()+"…"
    return base

def _adsense_block()->str:
    return """
<div class="gen-ad">
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-7409421510734308" crossorigin="anonymous"></script>
<ins class="adsbygoogle" style="display:block" data-ad-client="ca-pub-7409421510734308" data-ad-slot="9228101213" data-ad-format="auto" data-full-width-responsive="true"></ins>
<script>(adsbygoogle=window.adsbygoogle||[]).push({});</script>
</div>
""".strip()

def _css()->str:
    return """
<style>
.gen-wrap{font-family:inherit}
.gen-cta{display:flex;gap:12px;flex-wrap:wrap;justify-content:center;align-items:center;margin:18px 0 10px}
.gen-cta a{display:inline-block;padding:14px 22px;border-radius:9999px;text-decoration:none;font-weight:700;min-width:240px;text-align:center;transition:transform .18s ease,box-shadow .18s ease,opacity .18s ease}
.gen-cta a.btn-prim{background:#16a34a;color:#fff;box-shadow:0 6px 16px rgba(22,163,74,.2)}
.gen-cta a.btn-prim:hover{transform:translateY(-2px);box-shadow:0 10px 22px rgba(22,163,74,.28)}
.gen-cta a.btn-sec{background:#fff;color:#16a34a;border:2px solid #16a34a}
.gen-cta a.btn-sec:hover{background:#f0fdf4}
.gen-h2{margin:26px 0 12px;font-size:1.42rem;line-height:1.35;border-left:6px solid #22c55e;padding-left:10px}
.gen-wrap p{line-height:1.9;margin:0 0 14px;color:#222}
.gen-wrap ul{padding-left:22px;margin:8px 0}
.gen-wrap li{margin:6px 0}
.gen-note{font-style:italic;color:#334155;margin-top:6px}
.gen-ad{margin:12px 0 22px}
</style>
""".strip()

def _cta(category_name:str)->str:
    cat_url=_category_url_for(category_name)
    home=WP_URL or "/"
    return f"""
<div class="gen-cta">
  <a class="btn-prim" href="{html.escape(cat_url)}" aria-label="{html.escape(category_name)} 글 더 보기">{html.escape(category_name)} 글 더 보기</a>
  <a class="btn-sec" href="{html.escape(home)}" aria-label="홈으로 이동">홈으로 이동</a>
</div>
""".strip()

def choose_category_for(kw:str)->str:
    s=_clean(kw)
    if any(x in s for x in ("속보","브리핑","단독","PICK","발표","출시","공개","이슈","뉴스")):
        return "뉴스" if "뉴스" in EXISTING_CATEGORIES else DEFAULT_CATEGORY
    return "정보" if "정보" in EXISTING_CATEGORIES else DEFAULT_CATEGORY

def render_body(kw:str, category:str)->str:
    k=html.escape(_clean(kw))
    return f"""
{_css()}
<div class="gen-wrap">
  {_adsense_block()}
  {_cta(category)}

  <h2 class="gen-h2">오늘의 한 줄</h2>
  <p>{k}에 대해 오늘 느낀 생각을 짧게 남깁니다. 복잡하게 설명하기보다, 한 줄로 요약해 두면 다음에 돌아봤을 때 맥락을 쉽게 붙잡을 수 있어요.</p>

  <h2 class="gen-h2">핵심 포인트</h2>
  <ul>
    <li>무엇이 중요한지 한 문장으로 메모</li>
    <li>오늘 새로 알게 된 사실 1~2개</li>
    <li>내가 다음에 확인할 체크리스트</li>
  </ul>

  <h2 class="gen-h2">짧은 메모</h2>
  <p>“{k}”를(을) 중심으로 하루를 정리합니다. 소소한 깨달음, 아쉬움, 다음 액션을 간단히 적어두면 누적 가치가 커져요.</p>

  {_adsense_block()}

  <h2 class="gen-h2">관련해 보면 좋은 것</h2>
  <p>연관 이슈, 참고 링크, 과거 기록을 한 곳에 묶어두세요. 다음 결정을 훨씬 빨리 내릴 수 있습니다.</p>

  {_cta(category)}
</div>
""".strip()

# ===== Runner =====
def pick_keywords(n:int=2)->List[str]:
    pool=_read_col_csv("keywords_general.csv")
    if not pool: return ["오늘의 기록"]*n
    return pool[:n]

def schedule_and_post(kw:str, hhmm:str)->dict:
    category = choose_category_for(kw)
    title = build_title_from_keyword(kw)
    body = render_body(kw, category)
    when_gmt = _slot_for(hhmm)
    res = post_wp(title, body, when_gmt, category=category)
    _consume_col_csv("keywords_general.csv", kw)
    _mark_used(kw)
    out = {"id": res.get("id"), "title": title, "category": category, "date_gmt": res.get("date_gmt"), "link": res.get("link")}
    print(json.dumps(out, ensure_ascii=False))
    return out

def main():
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode", default="two-posts", help="two-posts | single")
    args=ap.parse_args()
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    times = GENERAL_TIMES_KST[:]
    if args.mode=="single":
        times = [times[0] if times else "10:00"]
    elif not times:
        times = ["10:00","17:00"]

    kws = pick_keywords(len(times))
    for kw, hhmm in zip(kws, times):
        schedule_and_post(kw, hhmm)

if __name__=="__main__":
    main()
