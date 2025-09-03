# update_keywords.py
# 오늘의 키워드 20개 수집 → keywords.csv를 "그 한 줄만" 남기도록 완전 덮어쓰기
# 소스: NewsAPI, Naver News API, Google News RSS (키 없으면 가능한 소스만 사용)

import os, re, random, requests, xml.etree.ElementTree as ET
from urllib.parse import quote

CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "").strip()
NAVER_ID = os.getenv("NAVER_CLIENT_ID", "").strip()
NAVER_SECRET = os.getenv("NAVER_CLIENT_SECRET", "").strip()

# -------------------- 수집기 --------------------
def fetch_newsapi_titles(api_key: str, page_size=50):
    if not api_key:
        return []
    try:
        url = "https://newsapi.org/v2/top-headlines"
        params = {"country": "kr", "pageSize": page_size, "language": "ko"}
        r = requests.get(url, params=params, headers={"X-Api-Key": api_key}, timeout=20)
        r.raise_for_status()
        data = r.json()
        return [a.get("title", "") for a in data.get("articles", []) if a.get("title")]
    except Exception:
        return []

def fetch_naver_titles(client_id: str, client_secret: str, per_query=15):
    if not (client_id and client_secret):
        return []
    queries = ["오늘", "속보", "경제", "정책", "기술", "신제품", "리뷰", "분석", "이슈"]
    out = []
    for q in queries:
        try:
            url = f"https://openapi.naver.com/v1/search/news.json?query={quote(q)}&display={per_query}&sort=sim"
            headers = {"X-Naver-Client-Id": client_id, "X-Naver-Client-Secret": client_secret}
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            data = r.json()
            for it in data.get("items", []):
                t = it.get("title", "")
                if t: out.append(t)
        except Exception:
            continue
    return out

def fetch_google_rss_titles():
    try:
        url = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        titles = []
        for item in root.findall(".//item"):
            t = item.findtext("title")
            if t: titles.append(t)
        return titles[:100]
    except Exception:
        return []

# -------------------- 키워드 후보 추출 --------------------
STOPWORDS = set("""
속보 단독 영상 포토 인터뷰 전문 기자 네티즌 종합 현장 단신
오늘 내일 어제 이번 지난 관련 발표 공개 확인 전망 공식 사실
""".split())

def normalize_title(t: str) -> str:
    t = re.sub(r"\s*[-–—]\s*[^-–—]+$", "", t)  # ' - 매체명' 제거
    t = re.sub(r"\[[^\]]+\]|\([^)]+\)|【[^】]+】|<[^>]+>", " ", t)  # 괄호/태그 제거
    t = re.sub(r"[“”\"'’‘·|·••▶▷▲△▼▽◆◇★☆…~!?:;]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def extract_phrases_ko(title: str):
    t = normalize_title(title)
    toks = [w for w in t.split() if 2 <= len(w) <= 12 and w not in STOPWORDS]
    cands = set()
    # 2~4그램 우선
    for n in (4, 3, 2):
        if len(toks) < n: continue
        for i in range(len(toks)-n+1):
            p = " ".join(toks[i:i+n])
            if len(p.replace(" ", "")) < 4: continue
            cands.add(p)
    # 1그램 보충
    for w in toks:
        if 2 <= len(w) <= 10:
            cands.add(w)
    return list(cands)

def rank_and_pick(phrases, k=20):
    # 빈도 + 길이 가중치
    freq = {}
    for p in phrases:
        freq[p] = freq.get(p, 0) + 1
    scored = []
    for p, c in freq.items():
        L = len(p.replace(" ", ""))
        score = c * 10 + min(L, 12)
        scored.append((score, p))
    scored.sort(reverse=True)
    pool = [p for _, p in scored[:80]]
    random.shuffle(pool)
    out, seen = [], set()
    for p in pool:
        base = p.replace(" ", "")
        if base in seen: continue
        seen.add(base)
        out.append(p)
        if len(out) >= k: break
    return out

# -------------------- CSV 쓰기(완전 덮어쓰기) --------------------
def write_today_keywords_only(keywords):
    """
    keywords.csv를 오늘 키워드 1줄만 남기도록 '완전히 덮어쓴다'.
    기존 줄은 모두 삭제됨.
    """
    new_first = ", ".join(keywords)
    with open(CSV, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_first + "\n")
    print("[OK] wrote ONLY today's 20 keywords (file truncated):")
    print(new_first)

# -------------------- 메인 --------------------
SEED_BACKUP = [
    "경제 동향", "주식 시장", "환율 전망", "부동산 정책", "전기차 배터리",
    "스마트폰 신제품", "AI 트렌드", "클라우드 보안", "원자재 가격", "반도체 수요",
    "소비자 물가", "관광 산업", "우주 탐사", "헬스케어 웨어러블", "친환경 에너지",
    "해외 직구", "업무 자동화", "생산성 도구", "디지털 마케팅", "무료 소프트웨어"
]

def main():
    titles = []
    titles += fetch_newsapi_titles(NEWSAPI_KEY, page_size=50)
    titles += fetch_naver_titles(NAVER_ID, NAVER_SECRET, per_query=15)
    titles += fetch_google_rss_titles()

    phrases = []
    for t in titles:
        phrases += extract_phrases_ko(t)

    # 소스가 비거나 부족하면 시드로 보충
    if len(set(phrases)) < 20:
        phrases += SEED_BACKUP

    picked = rank_and_pick(phrases, k=20)
    # 안전장치: 최소 2개는 확보
    if len(picked) < 2:
        picked = (SEED_BACKUP + picked)[:20]

    # 🔥 완전 덮어쓰기
    write_today_keywords_only(picked)

if __name__ == "__main__":
    main()
