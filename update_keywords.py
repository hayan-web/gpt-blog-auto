# update_keywords.py
# 소스: Naver Search API(뉴스), NewsAPI(top-headlines, KR), Google News RSS(KR)
# 목표: 최소 5~20개 키워드 수집 → 랜덤 섞기 → 첫 줄은 오늘의 랜덤 키워드로 배치 → keywords.csv 생성

import os, re, csv, io, json, random, time
import requests
from urllib.parse import quote
from xml.etree import ElementTree as ET

# ─ env
KEYWORDS_CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")

NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID") or ""
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET") or ""
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY") or ""

MIN_COUNT = 5       # 최소 키워드 개수
MAX_COUNT = 20      # 최대 키워드 개수

# ─ 금칙어 / 안전보충 키워드
BLOCK_WORDS = {
    # 민감/정책 리스크/저품질 키워드 (필요시 자유롭게 추가/수정)
    "성관계","포르노","야동","불법촬영","음란","노골","강간","몰카",
    "혐오","증오","폭력","자해","테러","마약","총기","선동","가짜뉴스"
}
SAFE_FALLBACKS = [
    "오늘의 이슈", "실생활 가이드", "트렌드 한눈에",
    "초보자용 핵심정리", "알쓸정보 톡톡", "생활 팁 모음"
]

def _is_allowed(kw: str) -> bool:
    t = (kw or "").lower()
    return not any(b in t for b in BLOCK_WORDS)

# ─ 유틸
STOPWORDS = set("""
단독 속보 이슈 현장 종합 인터뷰 업데이트 기자 사진 영상 포토 단체 공식 발표
논란 결과 이유 변화 분석 전망 관련 오늘 어제 내일 방금 지금 해당 주요
""".split())

def clean_title(t: str) -> str:
    if not t:
        return ""
    t = re.sub(r"\[.*?\]|\(.*?\)|【.*?】|〈.*?〉|「.*?」|『.*?』|<.*?>", " ", t)
    t = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", t)
    t = re.sub(r"[\"'“”‘’•·…~_=+^#@%&*|/:;]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def extract_candidates(titles: list[str]) -> list[str]:
    cands = []
    for t in titles:
        t = clean_title(t)
        if not t:
            continue
        # 1) 긴 구절 우선: "한글/영문/숫자 2~12자"가 2~4개 이어진 구절
        phrases = re.findall(r"(?:[가-힣A-Za-z0-9]{2,12}(?:\s|$)){2,4}", t)
        for p in phrases:
            p = p.strip()
            if 4 <= len(p) <= 28:
                cands.append(p)

        # 2) 백업: 개별 단어 기반 2~3어 조합
        words = [w for w in re.findall(r"[가-힣A-Za-z0-9]{2,}", t) if w not in STOPWORDS]
        for i in range(len(words)-1):
            pair = f"{words[i]} {words[i+1]}"
            if 4 <= len(pair) <= 28:
                cands.append(pair)
        for i in range(len(words)-2):
            tri = f"{words[i]} {words[i+1]} {words[i+2]}"
            if 6 <= len(tri) <= 32:
                cands.append(tri)
    # 정리
    uniq, seen = [], set()
    for s in cands:
        s = re.sub(r"\s+", " ", s).strip()
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq

# ─ 소스 1: Google News RSS (무료, 키 필요 없음)
def fetch_google_news_titles() -> list[str]:
    titles = []
    try:
        url = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        for item in root.findall(".//item/title"):
            titles.append(item.text or "")
    except Exception as e:
        print(f"[경고] Google News RSS 실패: {e}")
    return titles

# ─ 소스 2: NewsAPI (무료 키 필요)
def fetch_newsapi_titles() -> list[str]:
    titles = []
    if not NEWSAPI_KEY:
        return titles
    try:
        url = f"https://newsapi.org/v2/top-headlines?country=kr&pageSize=50&apiKey={NEWSAPI_KEY}"
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        for art in data.get("articles", []):
            titles.append(art.get("title") or "")
    except Exception as e:
        print(f"[경고] NewsAPI 실패: {e}")
    return titles

# ─ 소스 3: Naver Search API (뉴스)
def fetch_naver_news_titles() -> list[str]:
    titles = []
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
            r = requests.get(url, headers=headers, timeout=15)
            r.raise_for_status()
            data = r.json()
            for it in data.get("items", []):
                titles.append(it.get("title") or "")
            time.sleep(0.2)  # 속도 제한 완화
        except Exception as e:
            print(f"[경고] Naver API 실패({q}): {e}")
    return titles

def main():
    all_titles: list[str] = []
    all_titles += fetch_google_news_titles()
    all_titles += fetch_newsapi_titles()
    all_titles += fetch_naver_news_titles()

    if not all_titles:
        print("[오류] 어떤 소스에서도 제목을 가져오지 못했습니다.")
        return

    cands = extract_candidates(all_titles)
    random.shuffle(cands)

    # 한국어 위주 + 길이/중복 필터
    filtered: list[str] = []
    seen = set()
    for s in cands:
        if len(s) < 4 or len(s) > 32:
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
        if len(filtered) >= MAX_COUNT:
            break

    # 최저 개수 보장 (모자라면 안전 키워드로 보충)
    while len(filtered) < MIN_COUNT:
        pick = random.choice(SAFE_FALLBACKS)
        if pick.lower() not in seen:
            filtered.append(pick)
            seen.add(pick.lower())

    if not filtered:
        print("[오류] 키워드 후보를 구성하지 못했습니다.")
        return

    # 오늘의 랜덤 1개를 맨 위로 (auto_wp_gpt.py는 첫 줄을 사용)
    random.shuffle(filtered)
    today = random.choice(filtered)
    filtered.remove(today)
    keywords = [today] + filtered

    # 파일 저장 (UTF-8, 개행 고정, BOM 없음)
    with io.open(KEYWORDS_CSV, "w", encoding="utf-8", newline="\n") as f:
        w = csv.writer(f)
        for kw in keywords:
            w.writerow([kw])

    print(f"[완료] 키워드 {len(keywords)}개 저장 (첫 줄: '{today}')")

if __name__ == "__main__":
    main()
