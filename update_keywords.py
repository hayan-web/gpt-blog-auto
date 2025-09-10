# -*- coding: utf-8 -*-
"""
update_keywords.py
- 일반/쇼핑 키워드 최대 30개 수집 → 점수화 → 골든 선별(기본 12개)
- 일반: 뉴스 기반 빈도+신선도+형태 점수
- 쇼핑: 계절/월 가중치 + 길이/스멜 보정
- 출력:
  - keywords_general.csv, golden_keywords.csv
  - keywords_shopping.csv, golden_shopping_keywords.csv
"""
import os, re, csv, math, argparse, time
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict
import requests

KST = timezone(timedelta(hours=9))

# ---------- ENV ----------
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

USER_AGENT = os.getenv("USER_AGENT", "gpt-blog-keywords/1.3")

HEADERS_NEWS = {"User-Agent": USER_AGENT, "Accept": "application/json"}
HEADERS_NAVER = {
    "User-Agent": USER_AGENT, "Accept": "application/json",
    "X-Naver-Client-Id": NAVER_CLIENT_ID or "",
    "X-Naver-Client-Secret": NAVER_CLIENT_SECRET or "",
}

# ---------- I/O ----------
def write_list_csv(path, items):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["keyword"])
        for it in items:
            if isinstance(it, (list, tuple)): it = it[0]
            w.writerow([str(it).strip()])

# ---------- TEXT UTILS ----------
BAN_REGEX = re.compile(r"(쿠폰|최저가|할인|핫딜|쇼핑|구매|세일|공동구매|무료배송|원가|대란)")
ONLY_KO = re.compile(r"[^가-힣0-9A-Za-z\s\-\&]")

def norm(s: str) -> str:
    s = s or ""
    s = s.replace("\u200b", "").replace("\u00A0", " ")
    s = ONLY_KO.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def is_shopping_like(s: str) -> bool:
    if BAN_REGEX.search(s or ""): return True
    if re.search(r"[A-Za-z]+[\-\s]?\d{2,}", s or ""): return True
    # 제품/소재/의류 카테고리 흔한 단어
    if re.search(r"(가습기|제습기|히터|전기요|패딩|코트|핫팩|선풍기|쿨링|담요|냉감|부츠|우산|레인코트|레인부츠|방수|텐트|버너|아이스박스|보온병|커피머신|에어프라이어|공기청정기|청소기)", s or ""):
        return True
    return False

def ngram_candidates(text, n=2):
    toks = [t for t in norm(text).split(" ") if 1 < len(t) <= 20]
    out = []
    if n == 1:
        out = toks
    else:
        for i in range(len(toks) - n + 1):
            out.append(" ".join(toks[i:i+n]))
    return out

# ---------- FETCH ----------
def newsapi_top(days=3, limit=120):
    if not NEWSAPI_KEY: return []
    url = "https://newsapi.org/v2/top-headlines"
    params = {"country": "kr", "pageSize": 100, "apiKey": NEWSAPI_KEY}
    items = []
    try:
        r = requests.get(url, params=params, headers=HEADERS_NEWS, timeout=15)
        r.raise_for_status()
        items.extend(r.json().get("articles", []))
    except Exception:
        pass
    # 보강: everything에서 최근 n일 범위 일반 주제 쿼리
    q = "(일상 OR 라이프 OR 생활 OR 팁 OR 방법)"
    frm = (datetime.now(KST) - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        r2 = requests.get(
            "https://newsapi.org/v2/everything",
            params={"q": q, "from": frm, "language": "ko", "sortBy": "publishedAt", "pageSize": 100, "apiKey": NEWSAPI_KEY},
            headers=HEADERS_NEWS, timeout=15)
        r2.raise_for_status()
        items.extend(r2.json().get("articles", []))
    except Exception:
        pass
    out = []
    for a in items[:limit]:
        t = a.get("title") or ""
        d = a.get("description") or ""
        p = a.get("publishedAt") or ""
        out.append({"title": t, "desc": d, "at": p})
    return out

def naver_news_sample(q="생활 팁", display=50):
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET): return []
    try:
        r = requests.get(
            "https://openapi.naver.com/v1/search/news.json",
            params={"query": q, "display": display, "sort": "date"},
            headers=HEADERS_NAVER, timeout=10)
        r.raise_for_status()
        items = r.json().get("items", [])
        out = []
        for it in items:
            out.append({"title": it.get("title", ""), "desc": it.get("description", ""), "at": it.get("pubDate", "")})
        return out
    except Exception:
        return []

# ---------- SCORING ----------
def recency_score(ts: str) -> float:
    try:
        # NewsAPI ISO → UTC, Naver RFC-822 → local guess
        if "T" in ts:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        else:
            dt = datetime.strptime(ts, "%a, %d %b %Y %H:%M:%S %z")
        age_d = max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400.0)
        return 1.5 * math.exp(-age_d/3.0)  # 0~1.5
    except Exception:
        return 0.6

def phrase_score(phrase: str) -> float:
    # 너무 짧거나 긴 표현 억제
    L = len(phrase)
    s = 0.0
    if 6 <= L <= 26: s += 0.8
    if " " in phrase: s += 0.3  # 2그램 가산
    if is_shopping_like(phrase): s -= 0.8  # 일반키워드일 땐 페널티
    return s

def aggregate_scores(rows):
    # rows: [{title, desc, at}]
    freq = Counter()
    rec = defaultdict(float)
    for r in rows:
        txt = f"{r.get('title','')} {r.get('desc','')}"
        at = r.get("at", "")
        base = recency_score(at)
        # 1-gram + 2-gram 혼합
        for n in (1, 2):
            for ph in ngram_candidates(txt, n=n):
                ph = ph.strip()
                if len(ph) < 4: continue
                freq[ph] += 1
                rec[ph] = max(rec[ph], base)
    scores = {}
    for ph, c in freq.items():
        scores[ph] = c * 0.9 + rec[ph] + phrase_score(ph)
    return scores

# ---------- SEASON ----------
def month_season_boost(month: int, kw: str) -> float:
    kw = kw.lower()
    season_map = {
        12: ["크리스마스","연말","히터","전기요","핫팩","가습기","패딩","부츠","난방","방한","온수","보온","김장","연하장"],
        1:  ["히터","전기요","핫팩","가습기","패딩","부츠","난방","방한","겨울 이불","보온","설 선물","다이어리"],
        2:  ["가습기","핫팩","전기요","방한","발열내의","졸업","입학","밸런타인","프라그먼트","코트"],
        3:  ["공기청정기","알레르기","미세먼지","우산","우비","봄 코트","스니커즈","여행가방","캠핑"],
        4:  ["공기청정기","진드기","의류케어","피크닉","자외선차단","봄 이불","가벼운 자켓","청소기"],
        5:  ["자외선차단","선크림","선풍기","제습기","긴팔셔츠","캠핑","가벼운 운동화","냉감","휴가 준비"],
        6:  ["장마","우산","레인부츠","레인코트","방수","제습기","선풍기","냉감","모기퇴치","쿨링 타월"],
        7:  ["선풍기","에어컨","쿨링 타월","냉감","물놀이","아이스박스","캠핑","여름 이불","모기퇴치"],
        8:  ["휴가","물놀이","아이스박스","쿨링 타월","냉감","샌들","서큘레이터","선풍기","자외선차단"],
        9:  ["가을 이불","가디건","가을 코트","보온병","커피머신","캠핑","운동회","학용품"],
        10: ["가을 코트","트렌치","보온병","전기장판","가습기","건조기","미세먼지","핫팩"],
        11: ["가습기","전기요","전기장판","히터","핫팩","보온병","패딩","부츠","김장","블랙프라이데이"],
    }
    base = season_map.get(month, [])
    boost = 0.0
    for token in base:
        if token.lower() in kw: boost += 1.0
    # 제품/카테고리 느낌엔 추가 보정
    if is_shopping_like(kw): boost += 0.3
    return boost  # 0~N

# ---------- PIPELINE ----------
def build_general(k=30, gold=12, days=7):
    rows = []
    rows += newsapi_top(days=days, limit=120)
    rows += naver_news_sample("생활 팁", 50)
    rows += naver_news_sample("트렌드 인사이트", 50)

    scores = aggregate_scores(rows)
    # 일반에서 쇼핑스멜은 제거
    filt = [(kw, sc) for kw, sc in scores.items() if not is_shopping_like(kw)]
    filt.sort(key=lambda x: x[1], reverse=True)

    topk = [kw for kw,_ in filt[:k]]
    goldk = [kw for kw,_ in filt[:gold]]

    write_list_csv("keywords_general.csv", topk)
    write_list_csv("golden_keywords.csv", goldk)
    print(f"[GENERAL] {len(topk)} collected → write {len(topk)} (gold {len(goldk)})")

def build_shopping(shop_k=30, shop_gold=12, days=7):
    # 간단 접근: 일반 뉴스+검색에서 얻은 n그램 중 '쇼핑스멜' 나는 것을 후보로 삼고,
    # 월/계절 가중치로 정렬
    rows = []
    rows += newsapi_top(days=days, limit=120)
    rows += naver_news_sample("세일 특가 할인 핫딜", 50)
    rows += naver_news_sample("계절 인기템 추천", 50)

    scores = aggregate_scores(rows)  # 기본 점수
    month = datetime.now(KST).month
    enriched = []
    for kw, sc in scores.items():
        if not is_shopping_like(kw):  # 쇼핑 느낌 아니면 제외
            continue
        s = sc + month_season_boost(month, kw)
        # 너무 뉴스성(정치/사건) 제거
        if re.search(r"(속보|단독|의혹|검찰|폭우|태풍|선거|파업)", kw): 
            continue
        enriched.append((kw, s))

    # 후보가 빈약하면 시즌 프리셋 주입
    if len(enriched) < shop_k//2:
        presets = {
            12: ["전기요","히터","핫팩","가습기","패딩","부츠","크리스마스 트리","전구"],
            1:  ["전기요","히터","핫팩","가습기","방한 장갑","보온병","기모 후드티"],
            2:  ["가습기","발열내의","전기요","코트","초콜릿 선물","공기청정기 필터"],
            3:  ["공기청정기","알레르기 마스크","우산","피크닉 매트","봄 이불"],
            4:  ["자외선차단","공기청정기","로봇청소기","봄 코트","의류케어"],
            5:  ["선풍기","제습기","냉감 침구","캠핑 의자","쿨링 타월"],
            6:  ["장마 우산","레인부츠","방수 스프레이","제습기","모기퇴치기","쿨링 타월"],
            7:  ["서큘레이터","쿨링 타월","아이스박스","튜브/구명조끼","여름 이불"],
            8:  ["휴가 캐리어","쿨링 타월","샌들","선풍기","서큘레이터","냉감 티셔츠"],
            9:  ["가을 이불","가디건","보온병","캠핑 랜턴","드립 커피세트"],
            10: ["전기장판","가습기","핫팩","보온병","트렌치 코트","건조대"],
            11: ["전기장판","전기요","히터","가습기","핫팩","블랙프라이데이 세일"],
        }.get(month, [])
        enriched.extend((kw, 1.2 + month_season_boost(month, kw)) for kw in presets)

    # 정렬 및 상위 선택
    enriched.sort(key=lambda x: x[1], reverse=True)
    uniq = []
    seen = set()
    for kw, sc in enriched:
        k = norm(kw)
        if not k or len(k) < 2: continue
        if k in seen: continue
        seen.add(k)
        uniq.append((k, sc))

    topk = [kw for kw,_ in uniq[:shop_k]]
    goldk = [kw for kw,_ in uniq[:shop_gold]]

    write_list_csv("keywords_shopping.csv", topk)
    write_list_csv("golden_shopping_keywords.csv", goldk)
    print(f"[SHOPPING] {len(topk)} collected → write {len(topk)} (gold {len(goldk)})")

# ---------- CLI ----------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=30)                 # 일반 최대
    ap.add_argument("--gold", type=int, default=12)              # 일반 골든
    ap.add_argument("--shop-k", type=int, default=30)            # 쇼핑 최대
    ap.add_argument("--shop-gold", type=int, default=12)         # 쇼핑 골든
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--parallel", type=int, default=8)           # 호환용(미사용)
    args = ap.parse_args()

    build_general(k=args.k, gold=args.gold, days=args.days)
    build_shopping(shop_k=args["shop_k"] if hasattr(args, "shop_k") else args.shop_k,
                   shop_gold=args["shop_gold"] if hasattr(args, "shop_gold") else args.shop_gold,
                   days=args.days)

if __name__ == "__main__":
    main()
