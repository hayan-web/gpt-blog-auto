# -*- coding: utf-8 -*-
"""
update_keywords.py
- 뉴스/검색 API를 활용해 '일상용'과 '쇼핑용' 키워드를 분리 수집하고 자동 검수/정제하여 저장
- 출력 파일:
    1) keywords.csv                  : 일상용 최종 1줄(K개, 기본 10개)  ← auto_wp_gpt.py에서 사용
    2) keywords_general.csv          : 일상용 후보 라인(K개)
    3) keywords_shopping.csv         : 쇼핑용 후보 라인(shop_k개)
    4) golden_keywords.csv           : 일상 황금 키워드(gold개, header=keyword)
    5) golden_shopping_keywords.csv  : 쇼핑 황금 키워드(shop_gold개, header=keyword)

- 호출 예:
    python update_keywords.py --k 10 --gold 5 --shop-k 12 --shop-gold 5 --days 3 --parallel 8

- 환경변수(.env):
    NEWSAPI_KEY
    NAVER_CLIENT_ID, NAVER_CLIENT_SECRET  (없어도 동작)
    KEYWORDS_K=10
    BAN_KEYWORDS="금칙어1,금칙어2,..."     (옵션)
    USER_AGENT="gpt-blog-keywords/1.1"    (옵션)
"""

import os
import re
import csv
import json
import time
import math
import argparse
import random
import string
import logging
from typing import List, Dict, Tuple
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ---------- ENV ----------
USER_AGENT = os.getenv("USER_AGENT") or "gpt-blog-keywords/1.1"
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY") or ""
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID") or ""
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET") or ""
DEFAULT_K = int(os.getenv("KEYWORDS_K") or "10")

# 금칙어(ENV) + 기본 금칙어를 합집합으로 구성 (이전 TypeError 원인 수정!)
BAN_KEYWORDS_ENV = [t.strip() for t in (os.getenv("BAN_KEYWORDS") or "").split(",") if t.strip()]
DEFAULT_BANS_EXTRA = [
    x.strip() for x in """
사망,사고,화재,폭행,성폭력,성범죄,강간,혐의,검찰,기소,징역,피해자,피습,테러,총격,전쟁,참사,
도박,불법,마약,음주운전,자가격리,코로나,확진,파산,부도,성비위,갑질,자살,자해,분신,
단독,속보,영상,무릎,분노했다,충격,논란,해명,어제,오늘,내일,9월,10월,11월,12월
""".replace("\n", " ").split(",") if x.strip()
]
DEFAULT_BANS = set(BAN_KEYWORDS_ENV) | set(DEFAULT_BANS_EXTRA)

# 쇼핑 감지 단어/휴리스틱
SHOPPING_WORDS = set("""
추천 리뷰 후기 가격 최저가 세일 특가 쇼핑 쿠폰 할인 핫딜 핫딜템 언박싱 스펙 사용법 베스트
가전 노트북 스마트폰 냉장고 세탁기 건조기 에어컨 공기청정기 이어폰 헤드폰 카메라 렌즈 TV 모니터 키보드 마우스 의자 책상 침대 매트리스
에어프라이어 로봇청소기 무선청소기 가습기 제습기 식기세척기 빔프로젝터 유모차 카시트 분유 기저귀 골프 캠핑 텐트 배터리 보조배터리
가방 지갑 신발 후드 점퍼 패딩 스니커즈 러닝화 선크림 헤어드라이어 면도기 전동칫솔 워치 태블릿 케이스 케이블 충전기 허브 SSD HDD
""".split())

GENERAL_TITLE_BANS = set(["브리핑", "정리", "알아보기", "대해 알아보기", "해야 할 것", "해야할 것", "해야할것"])

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
}

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("kw")


# ---------- HTTP ----------
def http_get_json(url: str, params: dict = None, headers: dict = None, timeout: int = 10):
    h = dict(HEADERS)
    if headers:
        h.update(headers)
    try:
        r = requests.get(url, params=params or {}, headers=h, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


# ---------- Fetchers ----------
def fetch_newsapi_top(days: int = 3, page_size: int = 100) -> List[Dict]:
    """NewsAPI top-headlines + everything 조합 (ko/kr). 키 없으면 빈 리스트."""
    if not NEWSAPI_KEY:
        return []
    out = []
    # top-headlines
    url_th = "https://newsapi.org/v2/top-headlines"
    for country in ["kr"]:
        data = http_get_json(url_th, params={"country": country, "pageSize": page_size, "apiKey": NEWSAPI_KEY})
        if data and data.get("status") == "ok":
            for a in data.get("articles", []):
                out.append({"title": a.get("title") or "", "desc": a.get("description") or ""})
    # everything 최근 N일
    url_all = "https://newsapi.org/v2/everything"
    q_list = ["한국", "이슈", "핫", "업데이트", "리뷰", "추천"]
    for q in q_list:
        data = http_get_json(url_all, params={
            "q": q,
            "language": "ko",
            "sortBy": "publishedAt",
            "pageSize": 50,
            "apiKey": NEWSAPI_KEY,
        })
        if data and data.get("status") == "ok":
            for a in data.get("articles", []):
                out.append({"title": a.get("title") or "", "desc": a.get("description") or ""})
        time.sleep(0.2)
    return out


def fetch_naver_news_sample() -> List[Dict]:
    """NAVER 검색 API(뉴스)를 가볍게 조회하여 타이틀 수집. 키 없으면 빈 리스트."""
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        return []
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        "User-Agent": USER_AGENT,
    }
    base = "https://openapi.naver.com/v1/search/news.json"
    out = []
    for q in ["이슈", "핫", "주목", "리뷰", "추천"]:
        data = http_get_json(base, params={"query": q, "display": 30, "sort": "sim"}, headers=headers)
        if not data:
            continue
        for item in data.get("items", []):
            out.append({"title": item.get("title") or "", "desc": item.get("description") or ""})
        time.sleep(0.15)
    return out


# ---------- Text utils ----------
RE_HTML_TAG = re.compile(r"<[^>]+>")
RE_BRACKETS = re.compile(r"[\[\(【\(].*?[\]\)】\)]")
RE_SPACES = re.compile(r"\s+")

def clean_title(s: str) -> str:
    s = s or ""
    s = RE_HTML_TAG.sub("", s)
    s = s.replace("&quot;", "\"").replace("&apos;", "'").replace("&amp;", "&")
    s = RE_BRACKETS.sub(" ", s)
    s = re.sub(r"[\|\-–—:_/·•]+", " ", s)
    s = s.replace("…", " ").replace("·", " ").replace("▲", " ").replace("▶", " ")
    s = s.translate(str.maketrans("", "", string.punctuation))
    s = RE_SPACES.sub(" ", s).strip()
    return s


def best_segment_from_title(title: str) -> str:
    """제목을 분리기호로 나누고, 한글/숫자/영문 혼합이 가장 자연스러운 부분을 선택."""
    title = clean_title(title)
    if not title:
        return ""
    parts = re.split(r"\s{2,}| - | – | — | : | \| ", title)
    parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        parts = [title]
    # 길이 6~24자 사이를 우선, 그 외에는 가장 긴 파트
    scored = []
    for p in parts:
        L = len(p)
        score = -abs(L - 16)  # 16자 근접 선호
        # 의미없는 단어 과다 포함 감점
        if any(b in p for b in DEFAULT_BANS): score -= 5
        scored.append((score, p))
    scored.sort(reverse=True)
    return scored[0][1][:40]


def is_bad_keyword(kw: str) -> bool:
    if not kw: return True
    if kw in DEFAULT_BANS: return True
    if any(b in kw for b in DEFAULT_BANS): return True
    if len(kw) <= 2: return True
    if re.fullmatch(r"[가-힣]{1,2}", kw): return True
    if re.search(r"(사진|영상|보기|클릭|기사|신문|보도|단독|속보)", kw): return True
    return False


def is_shopping_like(kw: str) -> bool:
    if any(t in kw for t in SHOPPING_WORDS): return True
    if re.search(r"[A-Za-z]+[\-\s]?\d{2,}", kw): return True  # 모델/형번
    if re.search(r"(추천|리뷰|최저가|세일|특가|할인|구매|가격)", kw): return True
    # '제품/기기/가전' 류
    if re.search(r"(청소기|공기청정기|에어컨|제습기|가습기|전자레인지|인덕션|렌즈|키보드|마우스|모니터|의자|침대|매트리스|가전)", kw):
        return True
    return False


def info_score(kw: str) -> float:
    """일상용 점수(정보성/일반성)."""
    s = 0.0
    L = len(kw)
    s += min(L, 22) / 22.0
    s += 0.4 if re.search(r"[가-힣]{2,}", kw) else 0.0
    s += -0.9 if is_shopping_like(kw) else 0.0
    s += -0.6 if any(b in kw for b in GENERAL_TITLE_BANS) else 0.0
    return s


def shop_score(kw: str) -> float:
    """쇼핑용 점수(상품성/구매연관)."""
    s = 0.0
    L = len(kw)
    s += 0.8 if is_shopping_like(kw) else 0.0
    s += min(L, 18) / 18.0
    return s


def uniq_order(seq: List[str]) -> List[str]:
    seen = set()
    out = []
    for s in seq:
        if not s or s in seen: continue
        seen.add(s); out.append(s)
    return out


# ---------- Pipeline ----------
def collect_candidates(days: int = 3, parallel: int = 6) -> List[str]:
    """여러 소스에서 제목을 모아 문구 후보를 뽑는다."""
    tasks = []
    titles: List[str] = []

    with ThreadPoolExecutor(max_workers=max(2, parallel)) as ex:
        tasks.append(ex.submit(fetch_newsapi_top, days))
        tasks.append(ex.submit(fetch_naver_news_sample))
        for fut in as_completed(tasks):
            try:
                data = fut.result() or []
                for it in data:
                    t = it.get("title") or ""
                    d = it.get("desc") or ""
                    if t: titles.append(t)
                    if d: titles.append(d)
            except Exception:
                pass

    # 타이틀 → 분절 → 후보
    cand = []
    for t in titles:
        seg = best_segment_from_title(t)
        if seg:
            cand.append(seg)
    return cand


def split_general_shopping(cands: List[str]) -> Tuple[List[str], List[str]]:
    general, shopping = [], []
    for kw in cands:
        kw = kw.strip()
        if is_bad_keyword(kw):
            continue
        if is_shopping_like(kw):
            shopping.append(kw)
        else:
            general.append(kw)
    return uniq_order(general), uniq_order(shopping)


def rank_keywords(cands: List[str], score_fn, topn: int) -> List[str]:
    # 빈도/길이/스코어 혼합
    freq = Counter(cands)
    scored = []
    for kw, f in freq.items():
        s = score_fn(kw) + min(f, 5) * 0.25
        scored.append((s, kw))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [kw for _, kw in scored[:topn]]


def write_line_csv(path: str, arr: List[str]) -> None:
    # 쉼표로 이어 한 줄
    with open(path, "w", encoding="utf-8") as f:
        f.write(",".join(arr))


def write_column_csv(path: str, arr: List[str]) -> None:
    # header=keyword 1열 CSV
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["keyword"])
        for k in arr:
            w.writerow([k])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=DEFAULT_K, help="일상 라인 크기 (keywords.csv / keywords_general.csv)")
    ap.add_argument("--gold", type=int, default=5, help="일상 황금 키워드 개수")
    ap.add_argument("--shop-k", type=int, default=12, help="쇼핑 라인 크기 (keywords_shopping.csv)")
    ap.add_argument("--shop-gold", type=int, default=5, help="쇼핑 황금 키워드 개수")
    ap.add_argument("--days", type=int, default=3, help="최근 N일 가중")
    ap.add_argument("--parallel", type=int, default=6, help="병렬 요청 수")
    ap.add_argument("--min-volume", type=int, default=0)  # 호환용 더미 옵션
    ap.add_argument("--review", default="auto")           # 호환용 더미 옵션
    args = ap.parse_args()

    # 1) 후보 수집
    cands = collect_candidates(days=args.days, parallel=args.parallel)

    # 2) 일반 vs 쇼핑 분리 + 검수
    general_cands, shopping_cands = split_general_shopping(cands)

    # 3) 랭킹
    general_top = rank_keywords(general_cands, info_score, max(args.k, 10))
    shopping_top = rank_keywords(shopping_cands, shop_score, max(args.shop_k, 8))

    # 4) 황금 키워드(열 형태 CSV)
    golden_general = rank_keywords(general_cands, info_score, args.gold)
    golden_shopping = rank_keywords(shopping_cands, shop_score, args.shop_gold)

    # 5) 파일 저장
    # 일상(라인) — auto_wp_gpt.py가 읽음
    write_line_csv("keywords.csv", general_top[:args.k])
    write_line_csv("keywords_general.csv", general_top[:args.k])
    # 쇼핑(라인) — 참조용
    write_line_csv("keywords_shopping.csv", shopping_top[:args.shop_k])
    # 황금(열)
    write_column_csv("golden_keywords.csv", golden_general)
    write_column_csv("golden_shopping_keywords.csv", golden_shopping)

    # 6) 로그
    preview_g = " ".join(general_top[:args.k][:10])
    preview_s = " ".join(shopping_top[:min(10, len(shopping_top))])
    log.info("[GENERAL] %d collected → %d ranked → write %d", len(general_cands), len(general_top), args.k)
    log.info("[SHOPPING] %d collected → %d ranked → write %d", len(shopping_cands), len(shopping_top), args.shop_k)
    print("[OK] wrote ONLY today's {} keywords (file truncated):\n{}".format(args.k, ", ".join(general_top[:args.k][:10])))
    print("[OK] wrote ONLY today's {} shopping keywords (file truncated):\n{}".format(args.shop_k, ", ".join(shopping_top[:min(10, len(shopping_top))])))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
