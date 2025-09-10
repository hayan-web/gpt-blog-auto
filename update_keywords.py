# -*- coding: utf-8 -*-
"""
update_keywords.py — 일반/쇼핑 키워드 자동 수집·클린·선별
- 일반/쇼핑 각각 최대 K개(기본 30) 수집 후 정제/점수화 → CSV 2종 + golden 2종 작성
  * 일반: keywords_general.csv, golden_keywords.csv
  * 쇼핑: keywords_shopping.csv, golden_shopping_keywords.csv
- HTML 엔티티/태그/퍼블리셔 꼬리/연도-only/짧은 토큰/&quot 등 쓰레기 제거
- 'Namespace is not subscriptable' 오류 수정(모든 args는 속성 접근)
- 외부 API(NEWSAPI, NAVER) 실패 시에도 안전 폴백
"""

from __future__ import annotations
import os, re, csv, html, time, json
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Tuple
import requests
from dotenv import load_dotenv
load_dotenv()

# ===== ENV =====
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

USER_AGENT = os.getenv("USER_AGENT", "gpt-blog-keywords/1.3")
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json; charset=utf-8"}

# ===== I/O =====
GEN_OUT = "keywords_general.csv"
GEN_GOLD = "golden_keywords.csv"
SHOP_OUT = "keywords_shopping.csv"
SHOP_GOLD = "golden_shopping_keywords.csv"

# ===== Helpers =====
def _write_col_csv(path: str, items: List[str]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["keyword"])
        for k in items:
            w.writerow([k])

def _norm_text(s: str) -> str:
    if not s: return ""
    s = html.unescape(s)
    s = re.sub(r"<[^>]+>", " ", s)                  # strip tags
    s = re.sub(r"[\|\-–—~·•●▶▷›»]+.*$", "", s)      # cut publisher tail "제목 | 언론사"
    s = re.sub(r"\s+", " ", s).strip()
    # remove leading quotes/ticks
    s = s.strip(" \"'“”‘’`")
    return s

def _is_bad_token(s: str) -> bool:
    if not s: return True
    if any(ent in s for ent in ["&quot", "&amp", "&lt", "&gt", "&#", ";"]): return True
    if re.fullmatch(r"\d{2,4}(년|월|일)?", s): return True      # year/month/day only
    if len(s) < 2 or len(s) > 30: return True
    if re.fullmatch(r"[A-Za-z0-9 _\-]+", s) and len(s) < 5: return True  # too plain ascii short
    # generic/잡음 단어들
    ban = ["속보", "인사이트", "지디넷코리아", "오피니언", "기고", "사설", "단독", "기획", "종합", "오늘", "영상"]
    if s in ban: return True
    # 끝이 기호
    if re.search(r"[^\w가-힣)]$", s): return True
    # url-like
    if "http" in s or "www." in s: return True
    return False

def _dedup_keep_order(items: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for x in items:
        if not x or x in seen: continue
        seen.add(x); out.append(x)
    return out

def _split_candidates(title: str) -> List[str]:
    t = _norm_text(title)
    if not t: return []
    # split by separators, keep first meaningful part
    parts = re.split(r"[|\-–—:~]", t)
    parts = [p.strip() for p in parts if p.strip()]
    cands = []
    for p in parts:
        # strip trailing publisher-like tokens (예: "네이버 뉴스", "조선일보")
        p = re.sub(r"(네이버|다음|카카오|뉴스|일보|신문|TV|지디넷코리아|한겨레|중앙일보|조선일보|경향신문)$", "", p).strip()
        # trail brackets/quotes
        p = re.sub(r"^[\(\[]|[\)\]]$", "", p)
        if not _is_bad_token(p): cands.append(p)
    return cands[:2]  # 과분할 방지

def _score_general(s: str) -> float:
    score = 0.0
    # 길이 보너스(적당한 길이)
    L = len(s)
    if 7 <= L <= 18: score += 2.0
    elif 5 <= L <= 22: score += 1.0
    # 한글 포함 가점
    if re.search(r"[가-힣]", s): score += 1.5
    # 숫자 과다 패널티
    if re.search(r"\d{3,}", s): score -= 1.0
    # 클린 여부 가점
    if not _is_bad_token(s): score += 0.5
    return score

def _score_shopping(s: str, month: int) -> float:
    score = 0.0
    # 기본 길이/가독성
    L = len(s)
    if 6 <= L <= 20: score += 1.2
    if re.search(r"[가-힣]", s): score += 1.0
    # 계절 보정
    seasonal = {
        12: ["히터","전기장판","가습기","패딩","온열","핫팩","크리스마스"],
        1:  ["히터","전기장판","가습기","패딩","온열","핫팩"],
        2:  ["가습기","제습기","난방","온열","패딩"],
        3:  ["공기청정기","미세먼지","자외선","봄코트","우산"],
        4:  ["우산","우비","바람막이","운동화","피크닉","모기"],
        5:  ["선풍기","쿨링","모기","캠핑","여행가방"],
        6:  ["선풍기","휴대용 선풍기","쿨링","샤워필터","모기","썬케어"],
        7:  ["에어컨","선풍기","쿨링","모기","여름이불","워터파크"],
        8:  ["에어컨","쿨링","아이스팩","쿨매트","휴가"],
        9:  ["가을 니트","가을 이불","전기포트","가습기","트렌치코트"],
        10: ["전기장판","가습기","가을 니트","코트","무선청소기"],
        11: ["전기장판","히터","가습기","김장","코트","블랙프라이데이"],
    }
    for w in seasonal.get(month, []):
        if w in s: score += 2.0
    # 쇼핑 냄새 가점(제품 카테고리성 명사)
    if re.search(r"(선풍기|가습기|전기장판|히터|청소기|에어컨|이불|니트|코트|패딩|가방|키보드|마우스|헤드폰|충전기)", s):
        score += 1.5
    # 과도한 모델명/숫자 패널티
    if re.search(r"[A-Za-z]+[-\s]?\d{2,}", s):
        score -= 0.8
    return score

def _newsapi_titles(query: str, days: int, size: int = 50) -> List[str]:
    if not NEWSAPI_KEY: return []
    url = "https://newsapi.org/v2/everything"
    frm = (datetime.utcnow() - timedelta(days=max(1, days))).strftime("%Y-%m-%d")
    try:
        r = requests.get(url, params={
            "apiKey": NEWSAPI_KEY, "q": query, "from": frm, "language": "ko",
            "pageSize": min(100, size), "sortBy": "publishedAt"
        }, headers=HEADERS, timeout=15)
        r.raise_for_status()
        data = r.json()
        return [a.get("title","") or "" for a in data.get("articles", [])]
    except Exception as e:
        print(f"[NEWSAPI][WARN] {type(e).__name__}: {e}")
        return []

def _naver_search_news(query: str, display: int = 30) -> List[str]:
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET): return []
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    try:
        r = requests.get(url, params={"query": query, "display": min(100, display), "sort": "date"},
                         headers={**HEADERS, **headers}, timeout=15)
        r.raise_for_status()
        data = r.json()
        R = []
        for it in data.get("items", []):
            R.append(it.get("title","") or "")
        return R
    except Exception as e:
        print(f"[NAVER][WARN] {type(e).__name__}: {e}")
        return []

def _naver_search_shop(query: str, display: int = 30) -> List[str]:
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET): return []
    url = "https://openapi.naver.com/v1/search/shop.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    try:
        r = requests.get(url, params={"query": query, "display": min(100, display)},
                         headers={**HEADERS, **headers}, timeout=15)
        r.raise_for_status()
        data = r.json()
        return [it.get("title","") or "" for it in data.get("items", [])]
    except Exception as e:
        print(f"[NAVERSHOP][WARN] {type(e).__name__}: {e}")
        return []

# ===== Builders =====
def build_general(k: int = 30, gold: int = 12, days: int = 7) -> Tuple[List[str], List[str]]:
    seed_q = ["트렌드", "이슈", "심층", "분석", "관찰", "통찰", "연구", "데이터", "변화", "정책"]
    titles: List[str] = []
    for q in seed_q:
        titles += _newsapi_titles(q, days, size=60)
        titles += _naver_search_news(q, display=40)
        time.sleep(0.2)

    cands: List[str] = []
    for t in titles:
        cands += _split_candidates(t)

    # 클린 + dedup
    cands = [c for c in cands if not _is_bad_token(c)]
    cands = _dedup_keep_order(cands)
    # 점수화
    scored = sorted([(c, _score_general(c)) for c in cands], key=lambda x: x[1], reverse=True)
    picked = [c for c,_ in scored][:k]
    golds = [c for c,_ in scored][:max(1, min(gold, len(picked)))]

    print(f"[GENERAL] {len(picked)} collected → write {len(picked)} (gold {len(golds)})")
    _write_col_csv(GEN_OUT, picked)
    _write_col_csv(GEN_GOLD, golds)
    return picked, golds

def build_shopping(shop_k: int = 30, shop_gold: int = 12, days: int = 7) -> Tuple[List[str], List[str]]:
    # 계절 시드 + 범용 시드
    month = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).month
    season_seeds = {
        12: ["히터", "전기장판", "가습기", "핫팩", "겨울 이불"],
        1:  ["히터", "전기장판", "가습기", "패딩", "핫팩"],
        2:  ["가습기", "온열", "전기요", "난방 텐트"],
        3:  ["공기청정기", "봄코트", "자외선 차단"],
        4:  ["우산", "바람막이", "운동화", "피크닉 매트"],
        5:  ["선풍기", "쿨링 타월", "모기 퇴치기", "캠핑 의자"],
        6:  ["휴대용 선풍기", "쿨매트", "모기장", "샤워필터"],
        7:  ["에어컨", "아이스박스", "워터파크 준비물", "여름 이불"],
        8:  ["휴가 준비물", "쿨러백", "아이스팩", "썬케어"],
        9:  ["가을 니트", "무선청소기", "가습기", "전기포트"],
        10: ["전기장판", "가습기", "코트", "김장 용품"],
        11: ["블랙프라이데이", "히터", "전기장판", "가습기"],
    }
    base_seeds = ["베스트셀러", "가성비", "프리미엄", "신상품", "추천템"]

    titles: List[str] = []
    for q in season_seeds.get(month, []) + base_seeds:
        titles += _naver_search_shop(q, display=40)
        titles += _naver_search_news(q, display=20)
        time.sleep(0.2)

    cands: List[str] = []
    for t in titles:
        cands += _split_candidates(t)

    # 클린
    def _shop_clean(s: str) -> str:
        s = re.sub(r"\b(무료배송|정품|공식|행사|특가|세일|쿠폰|핫딜|가격)\b", "", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    cands = [_shop_clean(c) for c in cands if not _is_bad_token(c)]
    cands = [c for c in cands if len(c) >= 3]
    cands = _dedup_keep_order(cands)

    month_now = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=9))).month
    scored = sorted([(c, _score_shopping(c, month_now)) for c in cands], key=lambda x: x[1], reverse=True)
    picked = [c for c,_ in scored][:shop_k]
    golds  = [c for c,_ in scored][:max(1, min(shop_gold, len(picked)))]

    print(f"[SHOPPING] {len(picked)} collected → write {len(picked)} (gold {len(golds)})")
    _write_col_csv(SHOP_OUT, picked)
    _write_col_csv(SHOP_GOLD, golds)
    return picked, golds

# ===== CLI =====
def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=30)
    ap.add_argument("--gold", type=int, default=12)
    ap.add_argument("--shop-k", type=int, default=30)
    ap.add_argument("--shop-gold", type=int, default=12)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--parallel", type=int, default=8)  # dummy (미사용) — 인터페이스 유지
    args = ap.parse_args()

    build_general(k=args.k, gold=args.gold, days=args.days)
    build_shopping(shop_k=args.shop_k, shop_gold=args.shop_gold, days=args.days)

if __name__ == "__main__":
    main()
