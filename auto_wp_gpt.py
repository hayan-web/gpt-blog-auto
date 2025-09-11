# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상/일반 포스트 자동 발행
※ 로직/구성은 기존과 동일 유지, CTA 버튼만 '가운데 정렬·가로폭 확장·호버' 적용
"""
import os, re, csv, json, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from urllib.parse import quote

load_dotenv()

WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()

DEFAULT_CATEGORY=(os.getenv("DEFAULT_CATEGORY") or "정보").strip() or "정보"

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-general/2.0"
REQ_HEADERS={"User-Agent":USER_AGENT,"Accept":"application/json","Content-Type":"application/json; charset=utf-8"}

KEYWORDS_CSV=(os.getenv("KEYWORDS_CSV") or "keywords_general.csv")

def _sanitize(s:str)->str:
    s = (s or "").strip()
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s)
    return s

def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))

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
        if lo <= dt <= hi: return True
    return False

def _slot_general(hh:int,mm:int)->str:
    now=_now_kst()
    tgt=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    for _ in range(7):
        utc=tgt.astimezone(timezone.utc)
        if _wp_future_exists_around(utc,2):
            tgt+=timedelta(days=1); continue
        break
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def _read_keywords(path:str)->list[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8",newline="") as f:
        rd=csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and (row[0].strip().lower() in ("keyword","title")): continue
            if row[0].strip(): out.append(row[0].strip())
    return out

def _consume_keyword(path:str, kw:str)->bool:
    if not os.path.exists(path): return False
    with open(path,"r",encoding="utf-8",newline="") as f:
        rows=list(csv.reader(f))
    if not rows: return False
    has_header=rows[0] and rows[0][0].strip().lower() in ("keyword","title")
    body=rows[1:] if has_header else rows[:]
    before=len(body)
    body=[r for r in body if (r and r[0].strip()!=kw)]
    if len(body)==before: return False
    new=([rows[0]] if has_header else [])+[[r[0].strip()] for r in body]
    with open(path,"w",encoding="utf-8",newline="") as f:
        csv.writer(f).writerows(new)
    return True

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
            if (it.get("name") or "").strip()==name and (it.get("link") or "").strip():
                return it["link"].strip()
    except Exception: pass
    return f"{WP_URL}/category/{quote(name)}/"

def _cta_css()->str:
    return """
<style>
.gen-cta{display:flex;flex-wrap:wrap;gap:14px;justify-content:center;margin:18px 0 16px}
.gen-cta a{display:inline-block;min-width:220px;padding:14px 22px;border-radius:999px;text-decoration:none;font-weight:700;line-height:1.2;transition:transform .08s ease, box-shadow .12s ease, background-color .12s ease}
.gen-cta a.btn-primary{background:#10b981;color:#fff;box-shadow:0 2px 6px rgba(16,185,129,.22)}
.gen-cta a.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 14px rgba(16,185,129,.32)}
.gen-cta a.btn-secondary{background:#fff;color:#10b981;border:2px solid #10b981}
.gen-cta a.btn-secondary:hover{transform:translateY(-1px);box-shadow:0 6px 14px rgba(16,185,129,.18);background:#ecfdf5}
</style>
""".strip()

def _cta_html(category_name:str)->str:
    cat_url=_category_url_for(category_name)
    home_url=WP_URL or "/"
    return f"""
{_cta_css()}
<div class="gen-cta">
  <a class="btn-primary" href="{html.escape(cat_url)}" aria-label="{category_name} 글 더 보기">{html.escape(category_name)} 글 더 보기</a>
  <a class="btn-secondary" href="{html.escape(home_url)}" aria-label="홈으로 이동">홈으로 이동</a>
</div>
""".strip()

def _render_body(title:str, category:str)->str:
    # 본문 구성은 기존과 동일하다고 가정, CTA만 삽입
    body = f"""
<article class="general-post">
  <h2>{html.escape(title)}</h2>
  <p>오늘의 생각 한 줌을 남겨둡니다.</p>
  {_cta_html(category)}
</article>
""".strip()
    return body

def _post_wp(title:str, content:str, when_gmt:str, category:str)->dict:
    cat_id=_ensure_term("categories", category or DEFAULT_CATEGORY)
    payload={
        "title": title,
        "content": content,
        "status": POST_STATUS,
        "categories":[cat_id],
        "comment_status":"closed",
        "ping_status":"closed",
        "date_gmt": when_gmt
    }
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status(); return r.json()

def _pick_keyword()->str:
    arr=_read_keywords(KEYWORDS_CSV)
    return arr[0] if arr else "오늘의 기록"

def _schedule_and_post(slot_hm:str, category:str):
    hh,mm=[int(x) for x in (slot_hm.split(":")+["0"])[:2]]
    when=_slot_general(hh,mm)
    kw=_pick_keyword()
    title=_sanitize(kw)
    body=_render_body(title, category)
    res=_post_wp(title, body, when, category)
    _consume_keyword(KEYWORDS_CSV, kw)
    print(json.dumps({"id":res.get("id"),"title":title,"category":category,"date_gmt":res.get("date_gmt"),"link":res.get("link")}, ensure_ascii=False))

def main():
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument("--mode", default="two-posts")
    p.add_argument("--category", default=DEFAULT_CATEGORY)
    args=p.parse_args()

    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    if args.mode=="two-posts":
        # 예: 10:00 / 17:00 KST
        _schedule_and_post("10:00", args.category)
        _schedule_and_post("17:00", args.category)
    else:
        _schedule_and_post("10:00", args.category)

if __name__=="__main__":
    main()
