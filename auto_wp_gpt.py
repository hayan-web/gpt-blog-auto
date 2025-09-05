# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상글 2건 예약(10:00 / 17:00 KST)
- keywords_general.csv 우선, 없으면 keywords.csv에서 '쇼핑스멜' 제거
- 후킹형 제목, 정보형 본문(+CSS), 쇼핑 단어 금지
"""
import os, re, json, sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List
import requests
from dotenv import load_dotenv
load_dotenv()

from openai import OpenAI, BadRequestError
_oai = OpenAI()

# ===== ENV =====
WP_URL = (os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER = os.getenv("WP_USER") or ""
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY = (os.getenv("WP_TLS_VERIFY") or "true").lower() != "false"

OPENAI_MODEL = os.getenv("OPENAI_MODEL_LONG") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
POST_STATUS = (os.getenv("POST_STATUS") or "future").strip()

# ===== SCHED =====
def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))
def _to_gmt_at_kst_time(h:int, m:int=0) -> str:
    now = _now_kst()
    tgt = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if tgt <= now: tgt += timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ===== SHOPPING FILTER =====
SHOPPING_WORDS = set("""
추천 리뷰 후기 가격 최저가 세일 특가 쇼핑 쿠폰 할인 핫딜 언박싱 스펙 사용법 베스트
가전 노트북 스마트폰 냉장고 세탁기 건조기 에어컨 공기청정기 이어폰 헤드폰 카메라 렌즈 TV 모니터 키보드 마우스 의자 책상 침대 매트리스
에어프라이어 로봇청소기 무선청소기 가습기 제습기 식기세척기 빔프로젝터 유모차 카시트 분유 기저귀 골프 캠핑 텐트 보조배터리 배터리
가방 지갑 신발 패딩 스니커즈 선크림 드라이어 면도기 전동칫솔 워치 태블릿 케이스 케이블 충전기 허브 SSD HDD
쿠팡 파트너스 링크 딜 특가전 무료배송 사은품 공동구매 라이브커머스
""".split())

def is_shopping_like(kw: str) -> bool:
    k = kw or ""
    if any(w in k for w in SHOPPING_WORDS): return True
    if re.search(r"[A-Za-z]+[\-\s]?\d{2,}", k): return True
    if re.search(r"(구매|판매|가격|최저가|할인|특가|딜|프로모션|쿠폰|배송)", k): return True
    return False

# ===== WP =====
def _ensure_category(name:str)->int:
    name = name or "정보"
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

def _ensure_tag(tag:str)->int|None:
    t = (tag or "").strip()
    if not t: return None
    r = requests.get(f"{WP_URL}/wp-json/wp/v2/tags",
                     params={"search": t, "per_page": 50},
                     auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    for item in r.json():
        if (item.get("name") or "").strip()==t: return int(item["id"])
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/tags",
                      json={"name": t}, auth=(WP_USER, WP_APP_PASSWORD),
                      verify=WP_TLS_VERIFY, timeout=15)
    r.raise_for_status()
    return int(r.json()["id"])

def post_wp(title:str, html:str, when_gmt:str, category:str="정보", tag:str="")->dict:
    cat_id = _ensure_category(category)
    tag_ids = []
    if tag:
        tid = _ensure_tag(tag)
        if tid: tag_ids = [tid]
    payload = {
        "title": title, "content": html, "status": POST_STATUS,
        "categories": [cat_id], "tags": tag_ids,
        "comment_status":"closed","ping_status":"closed",
        "date_gmt": when_gmt
    }
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                      auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20)
    r.raise_for_status()
    return r.json()

# ===== KEYWORDS =====
def _read_line(path:str)->List[str]:
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f:
        arr=[x.strip() for x in f.readline().split(",") if x.strip()]
    return arr

def pick_daily_keywords(n:int=2)->List[str]:
    arr = _read_line("keywords_general.csv")
    if not arr:
        base = _read_line("keywords.csv")
        arr = [k for k in base if not is_shopping_like(k)]
    if len(arr) < n:
        arr += ["오늘의 작은 통찰", "생각이 자라는 순간", "일상을 바꾸는 관찰"]
    seen=set(); out=[]
    for k in arr:
        if k not in seen and not is_shopping_like(k):
            seen.add(k); out.append(k)
        if len(out)>=n: break
    return out[:n]

# ===== OpenAI helper (Chat→Responses 폴백; temperature 미지원 모델 자동 재시도) =====
def _ask_chat_then_responses(model: str, system: str, user: str, max_tokens: int, temperature: float) -> str:
    try:
        r = _oai.chat.completions.create(
            model=model,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        return (r.choices[0].message.content or "").strip()
    except BadRequestError as e:
        kwargs = dict(model=model, input=f"[시스템]\n{system}\n\n[사용자]\n{user}", max_output_tokens=max_tokens)
        try:
            rr = _oai.responses.create(**kwargs, temperature=temperature)
        except BadRequestError as e2:
            if "temperature" in str(e2):
                rr = _oai.responses.create(**kwargs)
            else:
                raise
        txt = getattr(rr, "output_text", None)
        if isinstance(txt, str) and txt.strip():
            return txt.strip()
        if getattr(rr, "output", None) and rr.output and rr.output[0].content:
            try:
                return rr.output[0].content[0].text.strip()
            except Exception:
                pass
        return ""

# ===== TITLE / BODY =====
BANNED_TITLE_PATTERNS = ["브리핑","정리","알아보기","대해 알아보기","에 대해 알아보기","해야 할 것","해야할 것","해야할것"]

def _bad_title(t:str)->bool:
    if any(p in t for p in BANNED_TITLE_PATTERNS): return True
    L=len(t.strip())
    return not (14 <= L <= 26)

def hook_title(kw:str)->str:
    sys_p = "너는 한국어 카피라이터다. 클릭을 부르는 짧고 강한 제목만 출력."
    usr = f"""키워드: {kw}
조건:
- 14~26자
- 금지어: {", ".join(BANNED_TITLE_PATTERNS)}
- '~브리핑', '~정리', '~대해 알아보기', '~해야 할 것' 류 금지
- 쇼핑/구매/할인/최저가/쿠폰/딜/가격 관련 단어 금지
- '리뷰/가이드/사용기' 표지어 지양
- 출력은 제목 1줄"""
    for _ in range(3):
        t = _ask_chat_then_responses(OPENAI_MODEL, sys_p, usr, max_tokens=60, temperature=0.9)
        t = (t or "").strip().replace("\n"," ")
        if not _bad_title(t): return t
    return f"{kw}, 오늘 시야가 넓어지는 순간"

def strip_code_fences(s: str) -> str:
    s = re.sub(r"```(?:\w+)?", "", s).replace("```", "").strip().strip("“”\"'")
    return s

def _css_block()->str:
    return """
<style>
.post-info p{line-height:1.86;margin:0 0 14px;color:#222}
.post-info h2{margin:28px 0 12px;font-size:1.42rem;line-height:1.35;border-left:6px solid #22c55e;padding-left:10px}
.post-info h3{margin:22px 0 10px;font-size:1.12rem;color:#0f172a}
.post-info ul{padding-left:22px;margin:10px 0}
.post-info li{margin:6px 0}
.post-info table{border-collapse:collapse;width:100%;margin:16px 0}
.post-info thead th{background:#f1f5f9}
.post-info th,.post-info td{border:1px solid #e2e8f0;padding:8px 10px;text-align:left}
</style>
"""

def gen_body_info(kw:str)->str:
    sys_p = "너는 사람스러운 한국어 칼럼니스트다. 광고/구매 표현 없이 지식형 글을 쓴다."
    usr = f"""주제: {kw}
스타일: 정의 → 배경/원리 → 실제 영향/사례 → 관련 연구/수치(개념적) → 비교/표 1개 → 적용 팁 → 정리
요건:
- 도입부 2~3문장에 '왜 지금 이 주제가 흥미로운지' 훅
- 본문은 3~5문장 단락으로 나누고, 소제목 <h2>/<h3> 사용
- 간단한 2~3행 표 1개를 <table><thead><tbody>로 구성 (과장 없이)
- 불릿 <ul><li> 1세트 포함
- '구매, 가격, 할인, 최저가, 쿠폰, 쿠팡, 쇼핑' 등 상업 단어 금지
- 'AI/작성' 같은 메타표현 금지
- 분량: 1000~1300자 (한국어 기준)
- 출력: 순수 HTML만(<p>, <h2>, <h3>, <ul>, <li>, <table>, <thead>, <tbody>, <tr>, <th>, <td>)"""
    body = _ask_chat_then_responses(OPENAI_MODEL, sys_p, usr, max_tokens=950, temperature=0.8)
    return _css_block() + '\n<div class="post-info">\n' + strip_code_fences(body) + "\n</div>"

# ===== RUNNERS =====
def run_two_posts():
    kws = pick_daily_keywords(2)
    times = [ (10,0), (17,0) ]
    for idx,(kw,(h,m)) in enumerate(zip(kws,times)):
        title = hook_title(kw)
        html  = gen_body_info(kw)
        link = post_wp(title, html, _to_gmt_at_kst_time(h,m), category="정보", tag=kw).get("link")
        print(f"[OK] scheduled ({idx}) '{title}' -> {link}")

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["two-posts"], default="two-posts")
    args = ap.parse_args()
    if args.mode=="two-posts":
        run_two_posts()

if __name__ == "__main__":
    sys.exit(main())
