# update_keywords.py
# 오늘의 키워드 10개 수집 → keywords.csv를 "그 한 줄만" 남기도록 완전 덮어쓰기
# 개선점:
#  - 문장형/존댓말/조사/매체 꼬리표 제거
#  - 명사 위주 2~4그램 우선, 1그램은 보충 용도
#  - 이상한 조합/예약됨/광고성 꼬리표 필터링 강화

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
    queries = ["오늘", "경제", "정책", "기술", "신제품", "리뷰", "분석", "이슈", "산업"]
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
        return titles[:120]
    except Exception:
        return []

# -------------------- 키워드 후보 추출 --------------------
# 자주 보이는 군더더기/매체 꼬리표/문장 어미
BAN_PATTERNS = re.compile(
    r"(영상|포토|인터뷰|전문|기자|네티즌|종합|현장|단신|예약됨|속보|단독)"
)

# 조사/어미 정리
JO_SA_1 = set("은는이가을를과와도만의에")  # 한 글자 조사
JO_SA_2 = ("으로", "부터", "까지", "처럼", "보다", "에서", "에게", "조차", "마저")

# 문장형/존댓말/불필요 꼬리 제거
BAD_TAILS = (
    "입니다", "합니다", "했다", "한다", "됐다", "되어", "된", "했다는", "한다는",
    "될까", "될까?", "인가", "일까", "예정", "발표", "확인", "공식", "논란",
)

STOPWORDS = set("""
오늘 내일 어제 이번 지난 관련 발표 공개 확인 전망 공식 사실 계획 결정 효과 실시 진행
정부 당국 당국자 업계 전문가 측 관계자 대표 위원회 협회 대학 은행 증권사
""".split())

def normalize_title(t: str) -> str:
    # 뒤의 매체명/브랜드 꼬리 제거: ' - 매체', ' | 매체'
    t = re.sub(r"\s*[-–—|]\s*[^\-–—\|]{1,20}$", "", t)
    # 대괄호/괄호/태그 제거
    t = re.sub(r"\[[^\]]+\]|\([^)]+\)|【[^】]+】|<[^>]+>", " ", t)
    # 특수문자/이모지류 간소화
    t = re.sub(r"[“”\"'’‘·|•▶▷▲△▼▽◆◇★☆…~!?:;#]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def strip_particle(w: str) -> str:
    for j2 in JO_SA_2:
        if w.endswith(j2) and len(w) > len(j2) + 1:
            return w[: -len(j2)]
    if len(w) >= 2 and w[-1] in JO_SA_1:
        return w[:-1]
    return w

def valid_token(tok: str) -> bool:
    if tok in STOPWORDS: return False
    if BAN_PATTERNS.search(tok): return False
    # 영숫자/한글만 허용
    if not re.fullmatch(r"[가-힣A-Za-z0-9]+", tok): return False
    # 한 글자명사 최소화
    if len(tok) < 2: return False
    return True

def clean_phrase(p: str) -> str | None:
    if BAN_PATTERNS.search(p): return None
    for tail in BAD_TAILS:
        if p.endswith(tail):
            p = p[: -len(tail)]
            break
    toks = [strip_particle(x) for x in p.split() if x]
    toks = [t for t in toks if valid_token(t)]
    if not (2 <= len(toks) <= 4):  # 2~4그램 우선
        return None
    base = " ".join(toks)
    if len(base.replace(" ", "")) < 4:
        return None
    return base

def extract_phrases_ko(title: str):
    t = normalize_title(title)
    if not t: return []
    # 1) 2~4그램
    toks = [w for w in t.split() if len(w) <= 12]
    cands = set()
    for n in (4, 3, 2):
        if len(toks) < n: continue
        for i in range(len(toks)-n+1):
            p = " ".join(toks[i:i+n])
            cp = clean_phrase(p)
            if cp: cands.add(cp)
    # 2) 1그램 보충(명사성 토큰만)
    for w in toks:
        w2 = strip_particle(w)
        if valid_token(w2) and 2 <= len(w2) <= 10:
            cands.add(w2)
    return list(cands)

def rank_and_pick(phrases, k=10):
    # 빈도 + 길이(명사성 가중) + 2~3그램 가산
    freq = {}
    for p in phrases:
        freq[p] = freq.get(p, 0) + 1
    scored = []
    for p, c in freq.items():
        L = len(p.replace(" ", ""))
        grams = len(p.split())
        score = c * 12 + min(L, 12) + (4 if grams in (2,3) else 0)
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

# -------------------- CSV 덮어쓰기 --------------------
def write_today_keywords_only(keywords):
    new_first = ", ".join(keywords)
    with open(CSV, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_first + "\n")
    print("[OK] wrote ONLY today's 10 keywords (file truncated):")
    print(new_first)

# -------------------- 메인 --------------------
SEED_BACKUP = [
    "경제 동향","주식 시장","환율 전망","부동산 정책","전기차 배터리",
    "스마트폰 신제품","AI 트렌드","클라우드 보안","원자재 가격","반도체 수요",
    "소비자 물가","관광 산업","우주 탐사","헬스케어 웨어러블","친환경 에너지",
    "해외 직구","업무 자동화","생산성 도구","디지털 마케팅","무료 소프트웨어"
]

def main():
    titles = []
    titles += fetch_newsapi_titles(NEWSAPI_KEY, page_size=60)
    titles += fetch_naver_titles(NAVER_ID, NAVER_SECRET, per_query=18)
    titles += fetch_google_rss_titles()

    phrases = []
    for t in titles:
        phrases += extract_phrases_ko(t)

    if len(set(phrases)) < 10:
        phrases += SEED_BACKUP

    picked = rank_and_pick(phrases, k=10)
    if len(picked) < 10:
        picked = (SEED_BACKUP + picked)[:10]

    write_today_keywords_only(picked)

if __name__ == "__main__":
    main()

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=10, help="number of keywords to keep (default 10)")
    args = ap.parse_args()
    # expose desired k via env-like variable
    K = args.k if args.k and args.k > 0 else 10
    # monkey patch: rank_and_pick -> use K
    def _main_with_k(k=K):
        titles = []
        titles.extend(fetch_newsapi_titles(NEWSAPI_KEY, page_size=50))
        titles.extend(fetch_naver_titles(NAVER_ID, NAVER_SECRET, per_query=15))
        titles.extend(fetch_google_rss_titles())
        phrases = []
        for t in titles:
            phrases += extract_phrases_ko(t)
        if len(set(phrases)) < k:
            phrases += SEED_BACKUP
        picked = rank_and_pick(phrases, k=k)
        if len(picked) < 2:
            picked = (SEED_BACKUP + picked)[:k]
        write_today_keywords_only(picked)
    _main_with_k()
