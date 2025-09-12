# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 자동 워드프레스 포스팅 (일상글 2건 + 쿠팡글은 별도 스크립트)
- 일반(일상) 글은 매 플로우마다 '완전 새로운 키워드'로 작성
- keywords_general.csv가 비어도 폴백 풀/ENV로 반드시 2건 생성
- 최근 사용/당일 중복 방지(.usage/used_general.txt)
"""

import os, csv, json, html, re, random
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
VERIFY_TLS=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"

POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()
DEFAULT_CATEGORY=(os.getenv("DEFAULT_CATEGORY") or "정보").strip() or "정보"
EXISTING_CATEGORIES=[c.strip() for c in (os.getenv("EXISTING_CATEGORIES") or "").split(",") if c.strip()]

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-auto/diary-2.0"
USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_GENERAL=os.path.join(USAGE_DIR,"used_general.txt")

NO_REPEAT_TODAY=(os.getenv("NO_REPEAT_TODAY") or "1").lower() in ("1","true","y","yes","on")
GEN_USED_BLOCK_DAYS=int(os.getenv("AFF_USED_BLOCK_DAYS") or "30")  # 동일 정책

KEYWORDS_CSV=(os.getenv("KEYWORDS_CSV") or "keywords_general.csv").strip()

# 폴백(ENV 우선, 없으면 내장)
FALLBACK_GENERAL_ENV=[w.strip() for w in (os.getenv("GENERAL_FALLBACK_KEYWORDS") or "").split(",") if w.strip()]
FALLBACK_GENERAL_DEFAULT=[
    "가계부","정리정돈","시간관리","운동 루틴","독서 메모","주간 계획","월간 회고","홈카페",
    "집안일 팁","디지털 정리","습관 기록","작은 실험","배운 점","루틴 점검","하루 요약",
]

REQ_HEADERS={"User-Agent":USER_AGENT,"Accept":"application/json","Content-Type":"application/json; charset=utf-8"}

# ===== WP helpers =====
def _ensure_term(kind:str, name:str)->int:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}",
                   params={"search":name,"per_page":50,"context":"edit"},
                   auth=(WP_USER,WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name:
            return int(it["id"])
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
                    auth=(WP_USER,WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    return int(r.json()["id"])

def post_wp(title:str, content:str, when_gmt:str, category:str)->dict:
    cat_id=_ensure_term("categories", category or DEFAULT_CATEGORY)
    payload={
        "title": title,
        "content": content,
        "status": POST_STATUS,
        "categories": [cat_id],
        "comment_status": "closed",
        "ping_status": "closed",
        "date_gmt": when_gmt
    }
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                    auth=(WP_USER,WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status()
    return r.json()

# ===== usage =====
def _ensure_usage(): os.makedirs(USAGE_DIR,exist_ok=True)

def _load_used(days:int)->set:
    _ensure_usage()
    if not os.path.exists(USED_GENERAL): return set()
    cutoff=datetime.utcnow().date()-timedelta(days=days)
    out=set()
    for ln in open(USED_GENERAL,"r",encoding="utf-8",errors="ignore"):
        ln=ln.strip()
        if not ln: continue
        try:
            d_str,kw=ln.split("\t",1)
            if datetime.strptime(d_str,"%Y-%m-%d").date()>=cutoff:
                out.add(kw.strip())
        except Exception:
            out.add(ln)
    return out

def _mark_used(kw:str):
    _ensure_usage()
    with open(USED_GENERAL,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw}\n")

# ===== keywords =====
def _read_col_csv(path:str)->List[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8",newline="") as f:
        rd=csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and row[0].strip().lower() in ("keyword","title"): continue
            s=row[0].strip()
            if s: out.append(s)
    return out

def _fresh_general_keyword() -> str:
    used_block=_load_used(GEN_USED_BLOCK_DAYS)
    used_today=_load_used(1) if NO_REPEAT_TODAY else set()

    pool=_read_col_csv(KEYWORDS_CSV)
    if not pool:
        base = FALLBACK_GENERAL_ENV or FALLBACK_GENERAL_DEFAULT
        pool = [*base]  # 반드시 채움

    random.shuffle(pool)
    for kw in pool:
        if kw in used_block or kw in used_today:
            continue
        return kw

    # 전부 소진된 경우: 날짜 시드로 가벼운 변형을 만들어 강제로 신선도 확보
    seed=datetime.utcnow().strftime("%Y%m%d%H%M")
    return f"{random.choice(pool)} {seed}"

# ===== content =====
def _css():
    return """
<style>
.diary-wrap{line-height:1.75}
.diary-cta{display:flex;justify-content:center;margin:18px 0}
.diary-btn{display:inline-block;padding:14px 22px;border-radius:999px;background:#0ea5e9;color:#fff;text-decoration:none;font-weight:800}
.diary-card{padding:14px 16px;border:1px solid #e5e7eb;border-radius:12px;background:#fafafa;margin:14px 0}
.diary-h2{margin:10px 0 6px;font-size:1.25rem;color:#334155}
.diary-hr{border:0;border-top:1px solid #e5e7eb;margin:16px 0}
</style>
""".strip()

def _build_title(kw:str)->str:
    # 키워드 기반 다양화 (고정 "오늘의 기록" 금지)
    tpl=[
        f"{kw} — 오늘 한 줄 회고",
        f"오늘의 {kw} 기록",
        f"{kw} 메모: 하루 요약",
        f"{kw} 점검 노트",
    ]
    random.shuffle(tpl)
    return tpl[0]

def _render_diary(kw:str, category:str)->str:
    cat=html.escape(category or DEFAULT_CATEGORY)
    kw_e=html.escape(kw)
    return f"""
{_css()}
<div class="diary-wrap">
  <p>배운 점/기록에 대한 짧은 요약을 남겨요. 아래 체크리스트를 따라 한 가지라도 써봅니다.</p>
  <div class="diary-card">
    <strong class="diary-h2">프롬프트</strong>
    <ul>
      <li>오늘의 <em>{kw_e}</em>에서 잘한 1가지</li>
      <li>아쉬웠던 1가지와 바로잡기</li>
      <li>내일 같은 장면에서 취할 1가지 행동</li>
    </ul>
  </div>
  <hr class="diary-hr"/>
  <div class="diary-cta">
    <a class="diary-btn" href="{WP_URL}/category/{cat}" aria-label="카테고리 보기">카테고리 보기</a>
  </div>
</div>
""".strip()

# ===== scheduling =====
def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))

def _slot_kst(hour:int, minute:int)->str:
    now=_now_kst()
    tgt=now.replace(hour=hour,minute=minute,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def _two_slots()->list[str]:
    # 10:00, 17:00 KST
    return [_slot_kst(10,0), _slot_kst(17,0)]

# ===== main modes =====
def _post_one_diary():
    kw=_fresh_general_keyword()
    title=_build_title(kw)
    body=_render_diary(kw, DEFAULT_CATEGORY)
    when_gmt=_slot_kst(10,0)
    res=post_wp(title, body, when_gmt, DEFAULT_CATEGORY)
    print(json.dumps({"id":res.get("id"),"title":title,"date_gmt":res.get("date_gmt"),"link":res.get("link")}, ensure_ascii=False))
    _mark_used(kw)

def _post_two_diaries():
    slots=_two_slots()
    for when in slots:
        kw=_fresh_general_keyword()
        title=_build_title(kw)
        body=_render_diary(kw, DEFAULT_CATEGORY)
        res=post_wp(title, body, when, DEFAULT_CATEGORY)
        print(json.dumps({"id":res.get("id"),"title":title,"date_gmt":res.get("date_gmt"),"link":res.get("link")}, ensure_ascii=False))
        _mark_used(kw)

def main():
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["one","two-posts"], default="two-posts")
    args=ap.parse_args()

    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    if args.mode=="one":
        _post_one_diary()
    else:
        _post_two_diaries()

if __name__=="__main__":
    main()
