# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상글 자동 포스팅(항상 새 키워드 사용, 스킵 금지)
- keywords_general.csv 비어도 폴백 생성 → 반드시 2개 예약
- 당일 중복 금지, used_general.txt 기록
"""
import os, csv, json, html, re, requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()

DEFAULT_CATEGORY=(os.getenv("DEFAULT_CATEGORY") or "정보").strip() or "정보"
GENERAL_TIMES_KST=(os.getenv("GENERAL_TIMES_KST") or "10:00,17:00").strip()

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-diary/2.0"
USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_FILE=os.path.join(USAGE_DIR,"used_general.txt")
NO_REPEAT_TODAY=True

REQ_HEADERS={"User-Agent":USER_AGENT,"Accept":"application/json","Content-Type":"application/json; charset=utf-8"}

FALLBACK_GEN=["가계부","정리정돈","주간 계획","홈카페","아침 루틴","운동 기록","독서 메모","식단 관리","취미 일기"]

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

def _ensure_usage(): os.makedirs(USAGE_DIR,exist_ok=True)
def _mark_used(kw:str):
    _ensure_usage()
    with open(USED_FILE,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw.strip()}\n")

def _load_used(days:int=365)->set[str]:
    used=set()
    if not os.path.exists(USED_FILE): return used
    cutoff=datetime.utcnow().date()-timedelta(days=days)
    for ln in open(USED_FILE,"r",encoding="utf-8",errors="ignore"):
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

def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))
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

def _slot_general(hhmm:str)->str:
    hh,mm=[int(x) for x in (hhmm.split(":")+["0"])[:2]]
    now=_now_kst()
    tgt=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    for _ in range(7):
        utc=tgt.astimezone(timezone.utc)
        if _wp_future_exists_around(utc,2): tgt+=timedelta(days=1); continue
        break
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def _pick_general(existing:set[str])->str:
    pool=_read_col("keywords_general.csv")
    used_today=_load_used(1) if NO_REPEAT_TODAY else set()
    for kw in pool:
        if kw in existing: continue
        if NO_REPEAT_TODAY and kw in used_today: continue
        return kw
    # 폴백 변형 생성
    i=1
    while True:
        base=random.choice(FALLBACK_GEN)
        cand=f"{base} {i}"
        if cand not in existing and (not NO_REPEAT_TODAY or cand not in used_today):
            return cand
        i+=1

def _post_wp(title:str, html_body:str, when_gmt:str, category:str)->dict:
    # 카테고리 ensure
    def _ensure_term(kind, name):
        r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}",
            params={"search":name,"per_page":50,"context":"edit"},
            headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
        r.raise_for_status()
        for it in r.json():
            if (it.get("name") or "").strip()==name: return int(it["id"])
        r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
            headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
        r.raise_for_status(); return int(r.json()["id"])
    cat_id=_ensure_term("categories", category or DEFAULT_CATEGORY)
    payload={
        "title":"오늘의 기록","content":html_body,"status":POST_STATUS,
        "categories":[cat_id],"comment_status":"closed","ping_status":"closed","date_gmt":when_gmt
    }
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
        headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20)
    r.raise_for_status(); return r.json()

def _css()->str:
    return """
<style>
.diary{line-height:1.7}
.ctr{display:flex;justify-content:center;margin:18px 0}
.btn{display:inline-flex !important;align-items:center;justify-content:center;padding:16px 28px;border-radius:9999px;background:#0ea5e9;color:#fff;text-decoration:none;font-weight:800;min-width:280px}
.btn:hover{transform:translateY(-1px);box-shadow:0 8px 20px rgba(0,0,0,.12)}
@media (max-width:540px){.btn{width:100%;min-width:0}}
</style>
"""

def _render(kw:str)->str:
    k=html.escape(kw)
    return f"""{_css()}
<div class="diary">
  <p><strong>{k}</strong>에 대한 짧은 요약을 남겨요. 오늘 느낀 점과 내일의 한 가지를 적어 둡니다.</p>
  <div class="ctr"><a class="btn" href="/" aria-label="카테고리 보기">카테고리 보기</a></div>
  <hr>
  <p>기록 포인트: 무엇을 했는지, 어떤 감정이었는지, 다음에 반복할지.</p>
</div>""".strip()

def run():
    times=[t.strip() for t in GENERAL_TIMES_KST.split(",") if t.strip()]
    used_this_run=set()
    for t in times[:2]:  # 2개만
        kw=_pick_general(used_this_run)
        used_this_run.add(kw)
        when=_slot_general(t)
        res=_post_wp("오늘의 기록", _render(kw), when, DEFAULT_CATEGORY)
        print(json.dumps({"id":res.get("id"),"title":"오늘의 기록","category":DEFAULT_CATEGORY,"date_gmt":res.get("date_gmt"),"link":res.get("link")}, ensure_ascii=False))
        _mark_used(kw)

if __name__=="__main__":
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    run()
