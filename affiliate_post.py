# -*- coding: utf-8 -*-
"""
affiliate_post.py — 쿠팡글 1건 예약(기본 13:00 KST, AFFILIATE_TIME_KST로 변경 가능)
- golden_shopping_keywords.csv 우선 → keywords_shopping.csv → 폴백
- 계절/트렌드 가중치 선택 + BAN_KEYWORDS, NO_REPEAT_TODAY, LRU(최근 30일 미사용 우선)
- 성공 시 소스 CSV에서 즉시 제거, .usage/used_shopping.txt 기록
- WP 예약 슬롯 충돌 시 다음날로 이월(최대 7일 재시도)
"""
import os, re, csv, json, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Tuple
import requests
from dotenv import load_dotenv
load_dotenv()

WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()

AFFILIATE_TIME_KST=(os.getenv("AFFILIATE_TIME_KST") or "13:00").strip()
AFF_USED_BLOCK_DAYS=int(os.getenv("AFF_USED_BLOCK_DAYS","30"))
NO_REPEAT_TODAY=str(os.getenv("NO_REPEAT_TODAY","1")).lower() in ("1","true","yes","y","on")
BAN_KEYWORDS=[x.strip() for x in (os.getenv("BAN_KEYWORDS") or "").split(",") if x.strip()]
AFF_FALLBACK_KEYWORDS=[x.strip() for x in (os.getenv("AFF_FALLBACK_KEYWORDS") or "").split(",") if x.strip()]

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-affiliate/1.3"
REQ_HEADERS={"User-Agent":USER_AGENT, "Accept":"application/json", "Content-Type":"application/json; charset=utf-8"}

USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_FILE=os.path.join(USAGE_DIR,"used_shopping.txt")

def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))

def _wp_future_exists_around(when_gmt_dt: datetime, tol_min: int = 2) -> bool:
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    try:
        r = requests.get(
            url,
            params={"status":"future","per_page":100,"orderby":"date","order":"asc","context":"edit"},
            headers=REQ_HEADERS, auth=(WP_USER, WP_APP_PASSWORD),
            verify=WP_TLS_VERIFY, timeout=20,
        )
        r.raise_for_status()
        items = r.json()
    except Exception as e:
        print(f"[WP][WARN] future list fetch failed: {type(e).__name__}: {e}")
        return False
    tgt = when_gmt_dt.astimezone(timezone.utc)
    delta = timedelta(minutes=max(1,int(tol_min)))
    lo, hi = tgt - delta, tgt + delta
    for it in items:
        dstr = (it.get("date_gmt") or "").strip()
        if not dstr: continue
        try:
            dt = datetime.fromisoformat(dstr.replace("Z","+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            else: dt = dt.astimezone(timezone.utc)
        except Exception:
            continue
        if lo <= dt <= hi:
            return True
    return False

def _slot_or_next_day_kst(timestr: str) -> str:
    h, m = [int(x) for x in timestr.split(":")]
    now=_now_kst()
    target_kst=now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target_kst<=now: target_kst+=timedelta(days=1)
    for _ in range(7):
        when_gmt_dt = target_kst.astimezone(timezone.utc)
        if _wp_future_exists_around(when_gmt_dt, tol_min=2):
            print(f"[SLOT] conflict at {when_gmt_dt.strftime('%Y-%m-%dT%H:%M:%S')}Z -> push +1d")
            target_kst += timedelta(days=1); continue
        break
    return target_kst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# === usage ===
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

# === csv ===
def _read_col_csv(path:str)->List[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8",newline="") as f:
        rd=csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and (row[0].strip().lower() in ("keyword","title")): continue
            k=(row[0] or "").strip()
            if k: out.append(k)
    return out

def _save_col_csv(path:str, items:List[str])->None:
    with open(path,"w",encoding="utf-8",newline="") as f:
        w=csv.writer(f); w.writerow(["keyword"])
        for k in items: w.writerow([k])

def _consume_from_sources(kw:str):
    for path in ("golden_shopping_keywords.csv","keywords_shopping.csv"):
        lst=_read_col_csv(path)
        if kw in lst:
            lst.remove(kw); _save_col_csv(path,lst)
            print(f"[ROTATE] removed '{kw}' from {path}")
            return

# === selection ===
def _score_with_season(kw:str)->float:
    m=_now_kst().month
    score=0.0
    seasonal={
        12: ["히터","전기장판","가습기","핫팩","겨울 이불"],
        1:  ["히터","전기장판","가습기","패딩","핫팩"],
        2:  ["가습기","온열","전기요","난방 텐트"],
        3:  ["공기청정기","봄코트","자외선","청소기"],
        4:  ["우산","바람막이","운동화","피크닉"],
        5:  ["선풍기","쿨링","모기","캠핑"],
        6:  ["휴대용 선풍기","쿨링","모기장","샤워필터"],
        7:  ["에어컨","아이스박스","워터","여름 이불"],
        8:  ["휴가","쿨러백","아이스팩","썬케어"],
        9:  ["가을 니트","무선청소기","가습기","전기포트"],
        10: ["전기장판","가습기","코트","김장"],
        11: ["블랙프라이데이","히터","전기장판","가습기"],
    }
    for w in seasonal.get(m, []):
        if w in kw: score+=2.0
    if re.search(r"[가-힣]", kw): score+=0.8
    if re.search(r"[A-Za-z]+[-\s]?\d{2,}", kw): score-=0.5
    return score

def _choose_keyword()->str:
    used=_load_used_set(AFF_USED_BLOCK_DAYS)
    today=_now_kst().date()
    # 후보 풀
    pools=[
        _read_col_csv("golden_shopping_keywords.csv"),
        _read_col_csv("keywords_shopping.csv")
    ]
    # 필터
    def ok(k:str)->bool:
        if not k: return False
        if any(k == b or b in k for b in BAN_KEYWORDS): return False
        if NO_REPEAT_TODAY and f"{today:%Y-%m-%d}\t{k}" in {f"{today:%Y-%m-%d}\t{x}" for x in used}: return False
        return True

    cands=[k for pool in pools for k in pool if ok(k)]
    if not cands and AFF_FALLBACK_KEYWORDS:
        return AFF_FALLBACK_KEYWORDS[0]

    # 점수 + LRU
    scored=[(k, _score_with_season(k), (k not in used)) for k in cands]
    # 1) 최근 30일 미사용 우선 True > False, 2) 점수 desc
    scored.sort(key=lambda x: (not x[2], -x[1], len(x[0])))
    return scored[0][0] if scored else (AFF_FALLBACK_KEYWORDS[0] if AFF_FALLBACK_KEYWORDS else "계절 아이템")

# === post ===
def _ensure_term(kind:str, name:str)->int:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}", params={"search":name,"per_page":50,"context":"edit"},
                   auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name: return int(it["id"])
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status(); return int(r.json()["id"])

def _wp_create(date_gmt_str: str, title: str, content_html: str, category="쇼핑", tag="")->dict:
    cat_id=_ensure_term("categories", category or "쇼핑")
    tag_ids=[]
    if tag:
        try:
            tid=_ensure_term("tags", tag); tag_ids=[tid]
        except Exception: pass
    payload={
        "title": title,
        "content": content_html,
        "status": POST_STATUS,
        "categories":[cat_id],
        "tags": tag_ids,
        "comment_status":"closed",
        "ping_status":"closed",
        "date_gmt": date_gmt_str,
    }
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                      headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD),
                      verify=WP_TLS_VERIFY, timeout=25)
    r.raise_for_status()
    return r.json()

def _build_body(kw:str)->str:
    # 간결한 정보형(광고 문구 최소화)
    body = f"""
<div class="aff-note">
  <h2>{kw} 제대로 고르는 포인트</h2>
  <ul>
    <li>용도/환경에 맞는 핵심 스펙만 확인</li>
    <li>리뷰는 과장 대신 일관성 있는 불만 여부 체크</li>
    <li>시즌/전력/소음/관리 난이도 4가지만 비교</li>
  </ul>
  <h3>간단 비교표</h3>
  <table><thead><tr><th>체크</th><th>포인트</th></tr></thead>
  <tbody>
    <tr><td>성능</td><td>환경 대비 충분한지</td></tr>
    <tr><td>관리</td><td>소모품/세척/보관</td></tr>
    <tr><td>비용</td><td>구입가 + 유지비</td></tr>
  </tbody></table>
  <p><em>시즌 아이템은 재고/가격 변동이 크니 타이밍을 보며 선택하세요.</em></p>
</div>
""".strip()
    return body

def run_one():
    kw=_choose_keyword()
    when=_slot_or_next_day_kst(AFFILIATE_TIME_KST)
    title=f"{kw} 제대로 써보고 알게 된 포인트"
    body=_build_body(kw)
    res=_wp_create(when, title, body, category="쇼핑", tag=kw)
    _mark_used(kw)
    _consume_from_sources(kw)
    print(json.dumps({
        "post_id": res.get("id"),
        "link": res.get("link"),
        "status": res.get("status"),
        "date_gmt": res.get("date_gmt"),
        "title": res.get("title",{}).get("rendered"),
        "keyword": kw,
    }, ensure_ascii=False))

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    run_one()

if __name__=="__main__":
    main()
