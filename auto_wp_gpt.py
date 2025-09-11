# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상글 자동 포스팅(카테고리 CTA 1개만, 중앙/대형 버튼)
구조:
  1) 내부광고 → 2) 요약글 → 3) 버튼 → 4) 본문1(짧게)
  6) 버튼 → 7) 내부광고 → 8) 본문2(나머지)

- '홈으로 이동' 버튼 완전 제거 (요청사항)
- 버튼은 항상 가운데 정렬 + 가로확장 + 호버 강조
- 기본 카테고리: 환경변수 DEFAULT_CATEGORY(없으면 '정보')
- 예약 슬롯: 환경변수 GENERAL_TIMES_KST="10:00,17:00" (없으면 이 기본값)
- 충돌 회피: 같은 시각(±2분) 'future' 포스트 있으면 +1일 이월
- 키워드: keywords_general.csv 1열 소비(헤더 'keyword' 감지)
"""
import os, csv, html, re, json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List
import requests
from dotenv import load_dotenv

load_dotenv()

# ===== ENV =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"

POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()  # 보통 'future'
DEFAULT_CATEGORY=(os.getenv("DEFAULT_CATEGORY") or "정보").strip() or "정보"

GENERAL_TIMES_KST=(os.getenv("GENERAL_TIMES_KST") or "10:00,17:00").strip()
USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-general/2.0"

USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
KEYWORDS_CSV=(os.getenv("KEYWORDS_CSV") or "keywords_general.csv").strip()

REQ_HEADERS={
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ===== Utilities =====
def _ensure_usage_dir():
    os.makedirs(USAGE_DIR, exist_ok=True)

def _read_col_csv(path:str)->List[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8",newline="") as f:
        rd=csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and (row[0].strip().lower()=="keyword"): continue
            if row[0].strip(): out.append(row[0].strip())
    return out

def _consume_col_csv(path:str, kw:str)->bool:
    if not os.path.exists(path): return False
    with open(path,"r",encoding="utf-8",newline="") as f:
        rows=list(csv.reader(f))
    if not rows: return False
    has_header=rows[0] and rows[0][0].strip().lower()=="keyword"
    body=rows[1:] if has_header else rows[:]
    before=len(body)
    body=[r for r in body if (r and r[0].strip()!=kw)]
    if len(body)==before: return False
    new_rows=([rows[0]] if has_header else [])+[[r[0].strip()] for r in body]
    with open(path,"w",encoding="utf-8",newline="") as f:
        csv.writer(f).writerows(new_rows)
    return True

def _normalize(s:str)->str:
    s=(s or "").strip()
    s=html.unescape(s)
    s=re.sub(r"\s+"," ",s)
    s=s.strip(" \"'“”‘’·,.-")
    return s

# ===== WP helpers =====
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
        r=requests.get(f"{WP_URL}/wp-json/wp/v2/categories",
                       params={"search":name,"per_page":50,"context":"view"},
                       headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD),
                       verify=WP_TLS_VERIFY, timeout=12)
        r.raise_for_status()
        for it in r.json():
            if (it.get("name") or "").strip()==name:
                link=(it.get("link") or "").strip()
                if link: return link
        if r.json():
            link=(r.json()[0].get("link") or "").strip()
            if link: return link
    except Exception:
        pass
    from urllib.parse import quote
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

# ===== Slot / schedule =====
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
    except Exception:
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

def _parse_times_kst(times_csv:str)->List[str]:
    out=[]
    for t in times_csv.split(","):
        t=t.strip()
        if not t: continue
        parts=(t.split(":")+["0"])[:2]
        try:
            hh, mm = int(parts[0]), int(parts[1])
            out.append(f"{hh:02d}:{mm:02d}")
        except Exception:
            continue
    return out or ["10:00","17:00"]

def _slot_general(slot_idx:int)->str:
    times=_parse_times_kst(GENERAL_TIMES_KST)
    # slot_idx가 범위 밖이면 첫번째로
    hh, mm=[int(x) for x in (times[slot_idx % len(times)].split(":")+["0"])[:2]]
    now=_now_kst()
    tgt=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt <= now: tgt += timedelta(days=1)
    # 최대 7일 이월
    for _ in range(7):
        utc=tgt.astimezone(timezone.utc)
        if _wp_future_exists_around(utc, tol_min=2):
            tgt+=timedelta(days=1); continue
        break
    final=tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[SLOT] scheduled UTC = {final}")
    return final

# ===== Body / CSS / CTA =====
def _css()->str:
    return """
<style>
.gen-wrap{font-family:inherit}
.gen-cta{display:flex;justify-content:center;align-items:center;margin:18px 0 10px}
.gen-cta a{display:inline-block;padding:16px 24px;border-radius:9999px;text-decoration:none;font-weight:800;min-width:280px;text-align:center;transition:transform .18s ease,box-shadow .18s ease,opacity .18s ease}
.gen-cta a.btn-prim{background:#16a34a;color:#fff;box-shadow:0 6px 16px rgba(22,163,74,.2)}
.gen-cta a.btn-prim:hover{transform:translateY(-2px);box-shadow:0 10px 22px rgba(22,163,74,.28)}
.gen-h2{margin:26px 0 12px;font-size:1.42rem;line-height:1.35;border-left:6px solid #22c55e;padding-left:10px}
.gen-wrap p{line-height:1.9;margin:0 0 14px;color:#222}
.gen-wrap ul{padding-left:22px;margin:8px 0}
.gen-wrap li{margin:6px 0}
.gen-note{font-style:italic;color:#334155;margin-top:6px}
.gen-ad{margin:12px 0 22px}
</style>
""".strip()

def _adsense_block()->str:
    return """
<div class="gen-ad">
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-7409421510734308" crossorigin="anonymous"></script>
<ins class="adsbygoogle" style="display:block" data-ad-client="ca-pub-7409421510734308" data-ad-slot="9228101213" data-ad-format="auto" data-full-width-responsive="true"></ins>
<script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
</div>
""".strip()

def _cta(category_name:str)->str:
    cat_url=_category_url_for(category_name)
    return f"""
<div class="gen-cta">
  <a class="btn-prim" href="{html.escape(cat_url)}" aria-label="{html.escape(category_name)} 글 더 보기">{html.escape(category_name)} 글 더 보기</a>
</div>
""".strip()

def _clean(s:str)->str:
    return _normalize(s)

def render_body(keyword:str, category:str)->str:
    k=html.escape(_clean(keyword))
    return f"""
{_css()}
<div class="gen-wrap">
  <!-- 1. 내부광고 -->
  {_adsense_block()}

  <!-- 2. 요약글 -->
  <h2 class="gen-h2">요약글</h2>
  <p>“{k}”에 대한 오늘의 요약을 한 단락으로 남깁니다. 맥락과 핵심만 간결하게 적어 두면 나중에 빠르게 복기할 수 있어요.</p>

  <!-- 3. 버튼 -->
  {_cta(category)}

  <!-- 4. 본문1 (짧게) -->
  <h2 class="gen-h2">본문 1</h2>
  <p>{k}와(과) 관련해 바로 적용 가능한 포인트를 짧게 정리합니다. 오늘 느낀 결론 또는 핵심 인사이트 2~3가지를 메모해 두세요.</p>

  <!-- 6. 버튼 -->
  {_cta(category)}

  <!-- 7. 내부광고 -->
  {_adsense_block()}

  <!-- 8. 본문2 (나머지) -->
  <h2 class="gen-h2">본문 2</h2>
  <p>위에서 남긴 요약/포인트의 배경과 예시, 참고 링크를 덧붙입니다. 수치·체크리스트처럼 재사용 가능한 형식이면 더 좋아요.</p>
</div>
""".strip()

# ===== Title / Keyword =====
def pick_keyword()->str:
    kws=_read_col_csv(KEYWORDS_CSV)
    if kws:
        return kws[0]
    return "오늘의 기록"

def consume_keyword(kw:str):
    _consume_col_csv(KEYWORDS_CSV, kw)

def build_title(kw:str)->str:
    s=_normalize(kw)
    s=re.sub(r'^\[.*?\]\s*','',s)  # 뉴스형 괄호 제거
    s=re.sub(r'^"+|"+$','',s)
    s=s or "오늘의 기록"
    # 너무 길면 자름(모바일 1~2줄)
    if len(s)>36: s=s[:35].rstrip()+"…"
    return s

# ===== Main routine =====
def _post_one(slot_idx:int, category:str):
    kw = pick_keyword()
    title = build_title(kw)
    when_gmt = _slot_general(slot_idx)
    body = render_body(kw, category)
    res = post_wp(title, body, when_gmt, category)
    print(json.dumps({
        "id": res.get("id"),
        "title": title,
        "category": category,
        "date_gmt": res.get("date_gmt"),
        "link": res.get("link")
    }, ensure_ascii=False))
    consume_keyword(kw)

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument("--mode", choices=["single","two-posts"], default="two-posts")
    p.add_argument("--category", default=DEFAULT_CATEGORY)
    args=p.parse_args()

    if args.mode=="two-posts":
        _post_one(0, args.category)
        _post_one(1, args.category)
    else:
        _post_one(0, args.category)

if __name__=="__main__":
    main()
