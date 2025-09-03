# -*- coding: utf-8 -*-
"""
update_keywords.py
- 뉴스/검색 타이틀을 모아 2~4단어 키워드 5~20개 추출
- 중복 제거 + 랜덤 셔플
- 0개일 경우: 백업 키워드(ENV 또는 파일)로 대체
- 결과를 keywords.csv 에 저장(첫 줄 = 오늘 쓸 키워드)

외부 의존성: requests, python-dotenv (requirements.txt에 포함)
환경변수:
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET (선택)
  NEWSAPI_KEY (선택)
  BACKUP_KEYWORDS (선택, 콤마 구분)  예: "아이폰 16 배터리, 카카오톡 보안 설정, ..."
  KEYWORDS_CSV (선택, 기본값 "keywords.csv")
"""

import os
import re
import csv
import json
import time
import random
import logging
from urllib.parse import quote
from xml.etree import ElementTree as ET

import requests
from dotenv import load_dotenv

load_dotenv()

# ===== 설정 =====
KEYWORDS_CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID") or ""
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET") or ""
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY") or ""

MIN_KEYS, MAX_KEYS = 5, 20
NGRAM_MIN, NGRAM_MAX = 2, 4
TIMEOUT = 15
RETRY = 3
BACKOFF = 2

# 기본 질의(한국어 일반/IT/생활 섞어 다양성 확보)
SEED_QUERIES = [
    "오늘의 뉴스", "실시간 이슈", "테크 뉴스", "모바일 소식",
    "생활 꿀팁", "정부 발표", "경제 동향", "문화 트렌드"
]

# 정책/브랜드 리스크 키워드(필요 시 추가)
BLOCK_WORDS = {
    "성관계","포르노","야동","불법촬영","음란","노골","강간","몰카",
    "혐오","증오","폭력","자해","테러","마약","총기","선동","가짜뉴스"
}

SAFE_FALLBACKS = [
    "생활 정보 모음", "알뜰 소비 팁", "모바일 설정 가이드",
    "블로그 최적화 방법", "워드프레스 이미지 최적화"
]

STOPWORDS = {
    "단독","속보","이슈","현장","종합","인터뷰","업데이트","기자","사진","영상","포토",
    "단체","공식","발표","논란","결과","이유","변화","분석","전망","관련","오늘","어제",
    "내일","방금","지금","해당","주요","특집","칼럼","사설","오피니언","전문"
}

# ===== 로깅 =====
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("update_keywords")


def _is_allowed(kw: str) -> bool:
    t = (kw or "").lower()
    return not any(b in t for b in BLOCK_WORDS)


# ===== HTTP GET(재시도) =====
def http_get(url: str, params: dict | None = None, headers: dict | None = None) -> requests.Response | None:
    last_err = None
    for i in range(RETRY):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp
            last_err = f"HTTP {resp.status_code} {resp.text[:180]}"
            log.warning(f"GET failed {i+1}/{RETRY}: {last_err}")
        except requests.RequestException as e:
            last_err = repr(e)
            log.warning(f"GET error {i+1}/{RETRY}: {last_err}")
        time.sleep(BACKOFF ** i)
    log.error(f"GET failed: {url} ({last_err})")
    return None


# ===== 소스 1: Google News RSS (전체 피드) =====
def fetch_google_news_titles() -> list[str]:
    titles: list[str] = []
    try:
        url = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"
        resp = http_get(url)
        if not resp:
            return titles
        root = ET.fromstring(resp.content)
        for item in root.findall(".//item/title"):
            t = item.text or ""
            if t:
                titles.append(t)
    except Exception as e:
        log.warning(f"Google News RSS parse error: {e}")
    return titles


# ===== 소스 2: NewsAPI (top-headlines, KR) =====
def fetch_newsapi_titles() -> list[str]:
    titles: list[str] = []
    if not NEWSAPI_KEY:
        return titles
    url = "https://newsapi.org/v2/top-headlines"
    params = {"country": "kr", "pageSize": 50, "apiKey": NEWSAPI_KEY}
    resp = http_get(url, params=params)
    if not resp:
        return titles
    try:
        data = resp.json()
        for art in data.get("articles", []):
            t = art.get("title") or ""
            if t:
                titles.append(t)
    except Exception as e:
        log.warning(f"NewsAPI parse error: {e}")
    return titles


# ===== 소스 3: Naver Search API (뉴스, 다중 질의) =====
def fetch_naver_news_titles() -> list[str]:
    titles: list[str] = []
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        return titles
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
    }
    queries = ["속보", "오늘", "현장", "브리핑", "발표", "이슈", "분석"]
    for q in queries:
        try:
            url = f"https://openapi.naver.com/v1/search/news.json?query={quote(q)}&display=30&sort=sim"
            resp = http_get(url, headers=headers)
            if not resp:
                continue
            data = resp.json()
            for it in data.get("items", []):
                t = it.get("title") or ""
                if t:
                    titles.append(t)
            time.sleep(0.2)  # 속도 제한 여유
        except Exception as e:
            log.warning(f"Naver API parse fail ({q}): {e}")
    return titles


# ===== 전처리 & n-gram =====
def clean_title(t: str) -> str:
    if not t:
        return ""
    t = re.sub(r"<\/?b>", "", t)                      # 네이버 태그 제거
    t = re.sub(r"\[.*?\]|\(.*?\)|【.*?】|〈.*?〉|「.*?」|『.*?』|<.*?>", " ", t)
    t = re.sub(r"[\"'“”‘’•·…~_=+^#@%&*|/:;]", " ", t)
    t = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def tokenize_ko(s: str) -> list[str]:
    toks = [w for w in re.split(r"\s+", s) if w]
    toks = [w for w in toks if w not in STOPWORDS and not w.isdigit()]
    toks = [w for w in toks if len(w) > 1]
    return toks


def ngrams(words: list[str], n: int) -> list[str]:
    return [" ".join(words[i:i+n]) for i in range(0, max(0, len(words)-n+1))]


def extract_candidates(titles: list[str]) -> list[str]:
    pool: set[str] = set()
    for t in titles:
        t = clean_title(t)
        if not t:
            continue
        toks = tokenize_ko(t)
        # 2~4 gram 조합
        for n in range(NGRAM_MIN, NGRAM_MAX + 1):
            for g in ngrams(toks, n):
                if any(sw in g for sw in STOPWORDS):
                    continue
                if re.search(r"\b\d{1,4}\b", g):
                    # 연도/숫자만 있는 조합은 패스
                    continue
                if 6 <= len(g) <= 32:
                    pool.add(g)
    return list(pool)


# ===== 백업 키워드 로딩 =====
def load_backup_keywords() -> list[str]:
    env_k = os.getenv("BACKUP_KEYWORDS", "").strip()
    if env_k:
        items = [x.strip() for x in env_k.split(",") if x.strip()]
        if items:
            return items

    if os.path.exists("backup_keywords.txt"):
        with open("backup_keywords.txt", "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
            if lines:
                return lines
    return []


def main():
    log.info("🔎 Collecting titles...")
    titles: list[str] = []

    # 여러 소스에서 수집
    titles += fetch_google_news_titles()
    titles += fetch_newsapi_titles()
    titles += fetch_naver_news_titles()

    # 중복 제거
    titles = list(dict.fromkeys([t for t in titles if t]))
    log.info(f"Collected titles: {len(titles)}")

    candidates = extract_candidates(titles)
    random.shuffle(candidates)

    # 한국어 포함 + 길이/블럭 필터 + 최대치 컷
    filtered: list[str] = []
    seen = set()
    for s in candidates:
        if not (6 <= len(s) <= 32):
            continue
        if not re.search(r"[가-힣]", s):
            continue
        if not _is_allowed(s):
            continue
        key = s.lower()
        if key in seen:
            continue
        seen.add(key)
        filtered.append(s)
        if len(filtered) >= MAX_KEYS:
            break

    # 최저 개수 보장 (모자라면 안전 키워드로 보충)
    if len(filtered) < MIN_KEYS:
        backup = load_backup_keywords() or SAFE_FALLBACKS
        random.shuffle(backup)
        for pick in backup:
            k = pick.lower()
            if k not in seen:
                filtered.append(pick)
                seen.add(k)
            if len(filtered) >= MIN_KEYS:
                break

    if not filtered:
        log.error("❌ 키워드 후보를 구성하지 못했습니다. 내장 안전 키워드 사용.")
        filtered = SAFE_FALLBACKS[:MIN_KEYS]

    # 오늘의 랜덤 1개를 맨 위로 (auto_wp_gpt.py는 첫 줄을 사용)
    random.shuffle(filtered)
    today = random.choice(filtered)
    filtered.remove(today)
    keywords = [today] + filtered

    # 파일 저장 (UTF-8, 개행 고정, BOM 없음)
    os.makedirs(os.path.dirname(KEYWORDS_CSV) or ".", exist_ok=True)
    with open(KEYWORDS_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for kw in keywords:
            w.writerow([kw])

    log.info(f"📝 keywords.csv updated. Count={len(keywords)} | First(today): {today}")


if __name__ == "__main__":
    main()