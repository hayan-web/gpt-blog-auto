# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상/일반 포스트 자동 예약
- 일상글 제목/콘텐츠 로직은 유지
- 버튼만: 가운데 정렬 + 넓은 가로폭 + 호버 애니메이션 도입
- 카테고리 링크 자동 탐색(실패 시 폴백 URL)
"""
import os, csv, html, json, re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from urllib.parse import quote

load_dotenv()

# ===== ENV / WP =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()

DEFAULT_CATEGORY=(os.getenv("DEFAULT_CATEGORY") or "정보").strip() or "정보"

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-general/1.0"
REQ_HEADERS={
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_FILE=os.path.join(USAGE_DIR,"used_general.txt")

AFFILIATE_TIMES_KST=(os.getenv("AFFILIATE_TIMES_KST") or "12:00,16:00,18:00").split(",")

def _ensure_usage_dir():
    os.makedirs(USAGE_DIR, exist_ok=True)

def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

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
    except Exception:
        pass
    return f"{WP_URL}/category/{quote(name)}/"

def _ensure_term(kind:str, name:str)->int:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}", params={"search":name,"per_page":50,"context":"edit"},
                   auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name: return int(it["id"])
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status(); return int(r.json()["id"])

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
    from datetime import timedelta
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

def _slot_at(hh:int, mm:int)->str:
    now=_now_kst()
    tgt=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt<=now: tgt = tgt + timedelta(days=1)
    for _ in range(7):
        utc=tgt.astimezone(timezone.utc)
        if _wp_future_exists_around(utc, tol_min=2):
            print(f"[SLOT] conflict at {utc.strftime('%Y-%m-%dT%H:%M:%S')}Z -> +1d")
            tgt+=timedelta(days=1); continue
        break
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def _read_col_csv(path:str):
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

# ===== 버튼 스타일(일상글 전용) =====
def _css_buttons()->str:
    return """
<style>
.gpt-cta{display:flex;justify-content:center;margin:24px 0;gap:14px;flex-wrap:wrap}
.gpt-cta a{display:inline-block;padding:14px 28px;min-width:240px;text-align:center;border-radius:999px;font-weight:700;text-decoration:none;transition:all .25s ease}
.gpt-cta a.btn-primary{background:#10b981;color:#fff;box-shadow:0 4px 10px rgba(16,185,129,.25)}
.gpt-cta a.btn-primary:hover{background:#0e9f6e;transform:translateY(-2px);box-shadow:0 6px 14px rgba(16,185,129,.35)}
.gpt-cta a.btn-secondary{background:#fff;color:#10b981;border:2px solid #10b981}
.gpt-cta a.btn-secondary:hover{background:#ecfdf5;transform:translateY(-2px)}
@media (max-width:640px){.gpt-cta a{min-width:72%}}
</style>
""".strip()

def _cta_html(category_name:str)->str:
    cat_url = _category_url_for(category_name)
    btn1 = html.escape(f"{category_name} 글 더 보기")
    btn2 = html.escape("홈으로 이동")
    home = html.escape(WP_URL or "/")
    return f"""
<div class="gpt-cta">
  <a class="btn-primary" href="{cat_url}" aria-label="{btn1}">{btn1}</a>
  <a class="btn-secondary" href="{home}" aria-label="{btn2}">{btn2}</a>
</div>
""".strip()

def _render_body(title:str, content:str, category_name:str)->str:
    t=html.escape(title)
    c=content if content else ""
    return f"""
{_css_buttons()}
<article class="post-body">
  <h2 style="margin:0 0 14px">{t}</h2>
  <div class="post-content" style="line-height:1.85;color:#222">
    {c}
  </div>
  {_cta_html(category_name)}
</article>
""".strip()

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

def schedule_and_post(titles:list[str], category:str):
    # 예: 2개를 12:00 / 16:00에 예약
    slots=[]
    for i, t in enumerate(titles[:2]):
        try:
            hh,mm = [int(x) for x in (AFFILIATE_TIMES_KST[i].strip().split(":")+["0"])[:2]]
        except Exception:
            hh,mm = (12,0) if i==0 else (16,0)
        slots.append(_slot_at(hh,mm))

    for i, title in enumerate(titles[:2]):
        body = _render_body(title, "", category)
        res = post_wp(title, body, slots[i], category)
        print(f"[OK] scheduled ({i}) '{title}' -> {res.get('link')}")

def pick_two_titles()->list[str]:
    all_ = _read_col_csv("keywords_general.csv")
    return all_[:2]

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    titles = pick_two_titles()
    if not titles:
        print("[GENERAL] no titles")
        return
    print(f"[GENERAL] picked: {titles[:2]}")
    schedule_and_post(titles, category=DEFAULT_CATEGORY)

if __name__=="__main__":
    main()
