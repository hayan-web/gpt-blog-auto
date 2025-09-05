# -*- coding: utf-8 -*-
"""
update_keywords.py
- 매 실행마다 '일상용/쇼핑용' 키워드 수집 → 정제/검수 → '황금키워드' 선별
- 실패/무응답이어도 시즌/카테고리 폴백으로 항상 파일 생성
생성 파일:
  - keywords_general.csv          (header: keyword)  # 일상글 풀
  - keywords_shopping.csv         (header: keyword)  # 쇼핑글 풀
  - golden_keywords.csv           (header: keyword)  # 일상 '황금'
  - golden_shopping_keywords.csv  (header: keyword)  # 쇼핑 '황금'
"""

import os, csv, re, json, random, time, math, html, urllib.parse
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from dotenv import load_dotenv
load_dotenv()

# ===== ENV =====
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY") or ""
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID") or ""
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET") or ""
USER_AGENT = os.getenv("USER_AGENT") or "gpt-blog-keywords/1.1"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") or ""
OPENAI_MODEL = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
OPENAI_MODEL_LONG = os.getenv("OPENAI_MODEL_LONG") or ""

KEYWORDS_K = int(os.getenv("KEYWORDS_K", "10"))     # 일반
SHOP_K = 12                                         # 쇼핑 기본
GOLD_N = 5                                          # 일반 황금
GOLD_SHOP_N = 5                                     # 쇼핑 황금

BAN_ENV = (os.getenv("BAN_KEYWORDS") or "").strip()
BAN_LIST = [b.strip() for b in re.split(r"[,\n]", BAN_ENV) if b.strip()]
BAN_SET = set(BAN_LIST)

HEADERS = {"User-Agent": USER_AGENT}

# ===== 폴백 리스트 =====
SEASONAL_SHOP = {
    "summer": ["넥쿨러","휴대용 선풍기","쿨링 타월","아이스 넥밴드","쿨매트","쿨링 토퍼","모기퇴치기","제빙기"],
    "winter": ["전기요","히터","난방 텐트","온열 담요","손난로","가습기","전기장판","난방매트"],
    "swing":  ["무선 청소기","로봇청소기","공기청정기","에어프라이어","무선이어폰","스탠드 책상","홈트 기구","키보드 마우스"]
}
GENERAL_SEED = [
    "일 잘하는 방법", "집중력 올리는 루틴", "하루 10분 정리법", "퇴근 후 에너지 회복",
    "워라밸 만드는 습관", "작은 변화 시작하기", "마음 다잡는 글귀", "불안 줄이는 메모법",
    "아침 루틴 점검", "저녁 감사일기"
]

# ===== 도우미 =====
def _now_kst():
    return datetime.now(timezone(timedelta(hours=9), name="KST"))

def _season_key():
    m = _now_kst().month
    if m in (6,7,8,9): return "summer"
    if m in (12,1,2): return "winter"
    return "swing"

def _write_col_csv(path, items):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["keyword"])
        for x in items:
            w.writerow([x])

def _clean_token(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace("…","").replace("·"," ").replace("·"," ")
    s = re.sub(r"[\[\]〈〉＜＞(){}#\"'|]", "", s)
    return s

def _ko_ratio(s: str) -> float:
    if not s: return 0.0
    ko = sum(1 for ch in s if '\uac00' <= ch <= '\ud7a3')
    return ko / max(1, len(s))

def _is_bad(s: str) -> bool:
    if not s or len(s) < 2 or len(s) > 22: return True
    if s in BAN_SET: return True
    if any(b in s for b in BAN_SET): return True
    if re.search(r"(성인|도박|토토|카지노|불법|주식추천|코인|비트코인|정치|선거)", s): return True
    if re.search(r"^[0-9\s\-\.\,]+$", s): return True
    if _ko_ratio(s) < 0.3: return True
    return False

def _dedup_keep_order(seq):
    seen=set(); out=[]
    for x in seq:
        if x not in seen:
            seen.add(x); out.append(x)
    return out

# ===== 수집기 =====
def collect_newsapi():
    if not NEWSAPI_KEY: return []
    url = "https://newsapi.org/v2/top-headlines"
    params = {"country":"kr","pageSize":100}
    try:
        r = requests.get(url, params=params, headers={"X-Api-Key":NEWSAPI_KEY, **HEADERS}, timeout=12)
        r.raise_for_status()
        data = r.json()
        titles = [a.get("title","") for a in (data.get("articles") or [])]
        return [_clean_token(t) for t in titles if t]
    except Exception:
        return []

def collect_naver_news_api(q="오늘", display=30):
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET): return []
    url = "https://openapi.naver.com/v1/search/news.json"
    try:
        r = requests.get(url, params={"query":q,"display":display,"sort":"sim"},
                         headers={"X-Naver-Client-Id":NAVER_CLIENT_ID,
                                  "X-Naver-Client-Secret":NAVER_CLIENT_SECRET,
                                  **HEADERS}, timeout=12)
        if r.status_code != 200: return []
        items = r.json().get("items",[])
        titles = [_clean_token(html.unescape(it.get("title",""))) for it in items]
        return titles
    except Exception:
        return []

def scrape_naver_ranking():
    # 구조 변경시 자동 실패-무시
    urls = [
        "https://news.naver.com/main/ranking/popularDay.naver",
        "https://news.naver.com/main/home.naver"
    ]
    out=[]
    for u in urls:
        try:
            r = requests.get(u, headers=HEADERS, timeout=10)
            if r.status_code != 200: continue
            # 대충 제목 추출(HTML class가 바뀌어도 최대한 긁도록)
            titles = re.findall(r'>([^<]{6,40})</a>', r.text)
            clean = [_clean_token(t) for t in titles]
            out.extend([t for t in clean if t])
        except Exception:
            pass
    return out

def collect_naver_shopping_api(q="인기", display=30):
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET): return []
    url = "https://openapi.naver.com/v1/search/shop.json"
    try:
        r = requests.get(url, params={"query":q,"display":display,"sort":"sim"},
                         headers={"X-Naver-Client-Id":NAVER_CLIENT_ID,
                                  "X-Naver-Client-Secret":NAVER_CLIENT_SECRET,
                                  **HEADERS}, timeout=12)
        if r.status_code != 200: return []
        items = r.json().get("items",[])
        names = [_clean_token(html.unescape(it.get("title",""))) for it in items]
        # 상품명 → 핵심 키워드만 남기기 (괄호/옵션 제거)
        names = [re.split(r"\(|\[|\-|\|", n)[0].strip() for n in names]
        return names
    except Exception:
        return []

# ===== 스코어링 / 선별 =====
def rank_general(cands):
    scored=[]
    for s in cands:
        if _is_bad(s): continue
        # 너무 상업적인 어휘는 감점
        if re.search(r"(최저가|할인|쿠폰|무료배송|가격|배송)", s): 
            score = 0.2
        else:
            score = 1.0
        # 길이 보정(12~18자 가점)
        L=len(s); score *= (1.0 + max(0, 1 - abs(15-L)/12))
        scored.append((score, s))
    scored.sort(key=lambda x:x[0], reverse=True)
    return [s for _,s in scored]

def rank_shopping(cands):
    scored=[]
    for s in cands:
        if _is_bad(s): continue
        # 제품/카테고리 단어 가점
        if re.search(r"(기|기기|청소기|공기청정|가습기|선풍기|히터|전기요|매트|이어폰|키보드|쌀|물티슈|세제|가전|주방|화장품|의자|책상|유모차|카시트|등산|캠핑|텐트|의류|신발|가방|SSD|램|마우스)", s):
            score = 1.3
        else:
            score = 0.8
        L=len(s); score *= (1.0 + max(0, 1 - abs(10-L)/10))
        scored.append((score, s))
    scored.sort(key=lambda x:x[0], reverse=True)
    return [s for _,s in scored]

# ===== 선택적 LLM 재순위 =====
def _rerank_with_llm(items, purpose="general", topn=10):
    if not (OPENAI_API_KEY and items): 
        return items[:topn]
    try:
        from openai import OpenAI, BadRequestError
        client = OpenAI(api_key=OPENAI_API_KEY)
        sys_p = "너는 한국어 키워드 선별 전문가다. 중복·금칙어·과장표현을 배제하고 클릭유도가 높은 표현만 남겨라."
        if purpose == "shopping":
            usr = "아래 후보 중 '쇼핑 의도'가 뚜렷하고 범용성이 높은 10개만 골라, 줄바꿈으로만 출력:\n" + "\n".join(items)
        else:
            usr = "아래 후보 중 정보성/에세이에 어울리는 10개만 골라, 줄바꿈으로만 출력:\n" + "\n".join(items)
        # Responses API 호환 (max_output_tokens)
        try:
            res = client.chat.completions.create(
                model=OPENAI_MODEL, 
                messages=[{"role":"system","content":sys_p},{"role":"user","content":usr}],
                temperature=0.3, max_tokens=300)
            txt = (res.choices[0].message.content or "").strip()
        except BadRequestError:
            res = client.responses.create(model=(OPENAI_MODEL_LONG or OPENAI_MODEL),
                                          input=f"[시스템]\n{sys_p}\n\n[사용자]\n{usr}",
                                          max_output_tokens=300, temperature=0.3)
            try: txt = res.output_text.strip()
            except Exception: txt = ""
        picks = [ _clean_token(x) for x in re.split(r"[\n,]", txt) if _clean_token(x) ]
        return _dedup_keep_order(picks)[:topn] or items[:topn]
    except Exception:
        return items[:topn]

# ===== 메인 =====
def main(k=10, gold=5, shop_k=12, shop_gold=5, days=3, parallel=8):
    # ---- 1) 수집 (병렬) ----
    tasks = []
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        tasks.append(ex.submit(collect_newsapi))
        tasks.append(ex.submit(collect_naver_news_api, "오늘"))
        tasks.append(ex.submit(collect_naver_news_api, "하루"))
        tasks.append(ex.submit(scrape_naver_ranking))
        tasks.append(ex.submit(collect_naver_shopping_api, "베스트"))
        tasks.append(ex.submit(collect_naver_shopping_api, "인기 상품"))

        results = [t.result() for t in tasks]

    # 일반 후보
    gen_raw = _dedup_keep_order(results[0] + results[1] + results[2] + results[3])
    # 문장에서 핵심 키워드 추출(간단화: 특수문자 제거 후 16자 이내 절삭)
    gen_clean = []
    for t in gen_raw:
        t = _clean_token(t)
        # 콜론/대시 앞 토막 쓰기
        t = re.split(r"[:\-–—\|]", t)[0].strip()
        # ~에 대해/… 정리 류 제거
        t = re.sub(r"(에 대해|정리|브리핑|현황|발표|공식|속보)$", "", t).strip()
        if t: gen_clean.append(t)
    gen_ranked = rank_general(gen_clean)

    # 쇼핑 후보
    shop_raw = _dedup_keep_order(results[4] + results[5])
    # 폴백(시즌)
    if not shop_raw:
        pool = SEASONAL_SHOP[_season_key()]
        # 시즌 상품을 살짝 변형
        shop_raw = [x for x in pool] + [f"{x} 추천" for x in pool]

    shop_ranked = rank_shopping(shop_raw)

    # ---- 2) 상위 추출 ----
    general_top = gen_ranked[:max(3, k)]
    shopping_top = shop_ranked[:max(5, shop_k)]

    # 빈 경우 강제 폴백
    if not general_top:
        general_top = GENERAL_SEED[:k]
    if not shopping_top:
        pool = SEASONAL_SHOP[_season_key()]
        shopping_top = (pool + [f"{x} 추천" for x in pool])[:shop_k]

    # ---- 3) LLM 재순위(선택) → 황금키워드 ----
    general_golden = _rerank_with_llm(general_top, "general", topn=gold)
    shopping_golden = _rerank_with_llm(shopping_top, "shopping", topn=shop_gold)

    # ---- 4) 파일 저장 ----
    _write_col_csv("keywords_general.csv", general_top[:k])
    _write_col_csv("keywords_shopping.csv", shopping_top[:shop_k])
    _write_col_csv("golden_keywords.csv", general_golden[:gold])
    _write_col_csv("golden_shopping_keywords.csv", shopping_golden[:shop_gold])

    print(f"[GENERAL] {len(general_top)} collected → write {k}")
    print(f"[SHOPPING] {len(shopping_top)} collected → write {shop_k}")
    print("[OK] wrote:", "keywords_general.csv, keywords_shopping.csv, golden_keywords.csv, golden_shopping_keywords.csv")

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--gold", type=int, default=5)
    ap.add_argument("--shop-k", type=int, default=12)
    ap.add_argument("--shop-gold", type=int, default=5)
    ap.add_argument("--days", type=int, default=3)      # 호환용(현재 미사용)
    ap.add_argument("--parallel", type=int, default=8)
    args = ap.parse_args()
    main(k=args.k, gold=args.gold, shop_k=args.shop_k, shop_gold=args.shop_gold,
         days=args.days, parallel=args.parallel)
