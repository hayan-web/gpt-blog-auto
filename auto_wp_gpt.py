# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상글 2건 예약(10:00/17:00 KST)
- 후킹형 제목(금칙어 차단)
- 태그=키워드 1개만
"""

import os, re, json, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List
import requests
from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI
_oai = OpenAI()

WP_URL = (os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER = os.getenv("WP_USER") or ""
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY = (os.getenv("WP_TLS_VERIFY") or "true").lower() != "false"

OPENAI_MODEL = os.getenv("OPENAI_MODEL_LONG") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
KEYWORDS_CSV = os.getenv("KEYWORDS_CSV") or "keywords.csv"
POST_STATUS = (os.getenv("POST_STATUS") or "future").strip()

def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))
def _to_gmt_at_kst_time(h:int, m:int=0) -> str:
    now = _now_kst()
    tgt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if tgt <= now: tgt += timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

BANNED_TITLE_PATTERNS = ["브리핑","정리","알아보기","대해 알아보기","에 대해 알아보기","해야 할 것","해야할 것","해야할것"]

def _bad_title(t:str)->bool:
    if any(p in t for p in BANNED_TITLE_PATTERNS): return True
    return not (12 <= len(t.strip()) <= 32)

def hook_title(kw:str)->str:
    sys_p = "너는 한국어 카피라이터다. 클릭을 부르는 짧고 강한 제목만 출력."
    usr = f"""
키워드: {kw}
조건:
- 14~26자
- 금지어: {", ".join(BANNED_TITLE_PATTERNS)}
- ~브리핑, ~정리, ~대해 알아보기 류 금지
- '가이드/리뷰'와 같은 표지어는 피해서 자연스럽게
- 출력은 제목 1줄
"""
    for _ in range(3):
        rsp = _oai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[{"role":"system","content":sys_p},{"role":"user","content":usr}],
            temperature=0.9, max_tokens=60,
        )
        t = (rsp.choices[0].message.content or "").strip().replace("\n"," ")
        if not _bad_title(t): return t
    return f"{kw}, 오늘 이거 하나만 기억하세요"

def gen_body(kw:str)->str:
    sys_p = "너는 짧고 읽기 쉬운 한국어 칼럼니스트다."
    usr = f"""
주제: {kw}
형식:
- 오프닝 훅 2~3문장
- 소제목 2개와 짧은 본문(각 3~4문장), 불릿 1개 섞기
- 마무리 한 문장(실천 촉구/인사이트)
금지: '브리핑/정리/알아보기/가이드/AI' 표현
분량: 700~1000자
출력: 간단한 HTML(<h3>, <p>, <ul><li>) 포함
"""
    rsp = _oai.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role":"system","content":sys_p},{"role":"user","content":usr}],
        temperature=0.85, max_tokens=900,
    )
    return (rsp.choices[0].message.content or "").strip()

def _ensure_category(name:str)->int:
    r = requests.get(f"{WP_URL}/wp-json/wp/v2/categories",
                     params={"search": name, "per_page": 50},
                     auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    for item in r.json():
        if (item.get("name") or "").strip()==name: return int(item["id"])
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/categories",
                      json={"name": name}, auth=(WP_USER, WP_APP_PASSWORD),
                      verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    return int(r.json()["id"])

def _ensure_tag(tag:str)->int:
    r = requests.get(f"{WP_URL}/wp-json/wp/v2/tags",
                     params={"search": tag, "per_page": 50},
                     auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    for item in r.json():
        if (item.get("name") or "").strip()==tag: return int(item["id"])
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/tags",
                      json={"name": tag}, auth=(WP_USER, WP_APP_PASSWORD),
                      verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    return int(r.json()["id"])

def post_wp(title:str, html:str, when_gmt:str, category:str="정보", tag:str="")->dict:
    cat_id = _ensure_category(category)
    tags = [_ensure_tag(tag)] if tag else []
    payload = {
        "title": title, "content": html, "status": POST_STATUS,
        "categories": [cat_id], "tags": tags,
        "comment_status":"closed","ping_status":"closed",
        "date_gmt": when_gmt
    }
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                      auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20)
    r.raise_for_status()
    return r.json()

def read_keywords(n:int=2)->List[str]:
    if not os.path.exists(KEYWORDS_CSV): return ["오늘의 인사이트","작은 성취"]
    with open(KEYWORDS_CSV,"r",encoding="utf-8") as f:
        arr=[x.strip() for x in f.readline().split(",") if x.strip()]
    if len(arr)<n: arr += arr[:max(0, n-len(arr))]
    return arr[:n]

def run_two_posts():
    kws = read_keywords(2)
    times = [ (10,0), (17,0) ]
    for idx,(kw,(h,m)) in enumerate(zip(kws,times)):
        title = hook_title(kw)
        body  = gen_body(kw)
        link = post_wp(title, body, _to_gmt_at_kst_time(h,m), category="정보", tag=kw).get("link")
        print(f"[OK] scheduled ({idx}) '{title}' -> {link}")

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["two-posts"], default="two-posts")
    args = ap.parse_args()
    if args.mode=="two-posts":
        run_two_posts()

if __name__ == "__main__":
    sys.exit(main())
