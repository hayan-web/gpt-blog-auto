# update_keywords.py
# 오늘의 키워드 10개 수집 → keywords.csv를 "그 한 줄만" 남기도록 완전 덮어쓰기
# 소스: NewsAPI, Naver News API, Google News RSS (키 없으면 가능한 소스만 사용)
#
# ▶ 개선 사항
# - 사용자 에이전트/타임아웃/재시도 내장
# - HTML 엔티티/태그/매체명 꼬리표 정리 강화
# - 중복 제거(공백/구두점 무시) 강화
# - 환경변수/CLI로 수량(k), 금지어(BAN_KEYWORDS), 백업(키워드 이전본) 제어
# - 네트워크 실패 시 시드로 자동 보충
#
# Env:
#   KEYWORDS_CSV         (default: keywords.csv)
#   NEWSAPI_KEY          (optional)
#   NAVER_CLIENT_ID      (optional)
#   NAVER_CLIENT_SECRET  (optional)
#   KEYWORDS_K           (default: 10)
#   BAN_KEYWORDS         (optional, "단어1,단어2" 형태. 포함되면 제외)
#   BACKUP_OLD_KEYWORDS  (default: true => .cache/keywords_YYYYMMDD.txt 백업)
#   USER_AGENT           (default: "gpt-blog-keywords/1.0")
#
# CLI:
#   --k 15           : 15개 추출
#   --no-google      : Google RSS 비활성
#   --no-newsapi     : NewsAPI 비활성
#   --no-naver       : Naver 비활성
#   --dry-run        : 파일 미쓰고 결과만 출력

import os, re, random, time, html, argparse, requests, xml.etree.ElementTree as ET
from urllib.parse import quote
from datetime import datetime
from pathlib import Path

CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")
NEWSAPI_KEY = (os.getenv("NEWSAPI_KEY") or "").strip()
NAVER_ID = (os.getenv("NAVER_CLIENT_ID") or "").strip()
NAVER_SECRET = (os.getenv("NAVER_CLIENT_SECRET") or "").strip()
K_DEFAULT = int(os.getenv("KEYWORDS_K", "10"))
BAN_KEYWORDS = [x.strip() for x in (os.getenv("BAN_KEYWORDS", "")).split(",") if x.strip()]
BACKUP_OLD = (os.getenv("BACKUP_OLD_KEYWORDS", "true").lower() != "false")
USER_AGENT = os.getenv("USER_AGENT", "gpt-blog-keywords/1.0")

TIMEOUT = 20
RETRIES = 2
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT})

# -------------------- 공용 HTTP --------------------
def http_get(url, **kwargs):
    # 간단 재시도
    for attempt in range(1, RETRIES + 1):
        try:
            r = SESSION.get(url, timeout=TIMEOUT, **kwargs)
            r.raise_for_status()
            return r
        except Exception:
            if attempt >= RETRIES:
                return None
            time.sleep(0.7 * attempt)

# -------------------- 수집기 --------------------
def fetch_newsapi_titles(api_key: str, page_size=50):
    if not api_key:
        return []
    try:
        url = "https://newsapi.org/v2/top-headlines"
        params = {"country": "kr", "pageSize": page_size, "language": "ko"}
        r = http_get(url, params=params, headers={"X-Api-Key": api_key})
        if not r: return []
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
            headers = {
                "X-Naver-Client-Id": client_id,
                "X-Naver-Client-Secret": client_secret,
                "User-Agent": USER_AGENT,
            }
            r = http_get(url, headers=headers)
            if not r: 
                continue
            data = r.json()
            for it in data.get("items", []):
                t = it.get("title", "")
                if t: out.append(t)
        except Exception:
            continue
    return out

def fetch_google_rss_titles():
    titles = []
    feeds = [
        "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko",                # 주요뉴스
        "https://news.google.com/rss/headlines/section/topic/TECHNOLOGY?hl=ko&gl=KR&ceid=KR:ko",  # 기술
        "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=ko&gl=KR&ceid=KR:ko",    # 경제
    ]
    for url in feeds:
        try:
            r = http_get(url)
            if not r: 
                continue
            root = ET.fromstring(r.text)
            for item in root.findall(".//item"):
                t = item.findtext("title")
                if t: titles.append(t)
        except Exception:
            continue
    # 너무 많으면 상위 일부만
    return titles[:150]

# -------------------- 키워드 후보 추출 --------------------
STOPWORDS = set("""
속보 단독 영상 포토 인터뷰 전문 기자 네티즌 종합 현장 단신
오늘 내일 어제 이번 지난 관련 발표 공개 확인 전망 공식 사실
""".split())

def normalize_title(t: str) -> str:
    # 매체명 꼬리표 제거: "제목 - 매체", "제목 – 매체"
    t = re.sub(r"\s*[-–—]\s*[^-–—]+$", "", t)
    # 괄호/태그/특수 라벨 제거
    t = re.sub(r"\[[^\]]+\]|\([^)]+\)|【[^】]+】|<[^>]+>", " ", t)
    t = html.unescape(t)
    # 구두점/기호 정리
    t = re.sub(r"[“”\"'’‘·|•▶▷▲△▼▽◆◇★☆…~!?:;]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t

def extract_phrases_ko(title: str):
    t = normalize_title(title)
    toks = [w for w in t.split() if 2 <= len(w) <= 12 and w not in STOPWORDS]
    cands = set()
    # 2~4그램
    for n in (4, 3, 2):
        if len(toks) < n: 
            continue
        for i in range(len(toks)-n+1):
            p = " ".join(toks[i:i+n])
            if len(p.replace(" ", "")) < 4: 
                continue
            cands.add(p)
    # 1그램 보충
    for w in toks:
        if 2 <= len(w) <= 10:
            cands.add(w)
    return list(cands)

def rank_and_pick(phrases, k=10):
    # 금지어 필터
    if BAN_KEYWORDS:
        banned = set(BAN_KEYWORDS)
        phrases = [p for p in phrases if not any(b in p for b in banned)]
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
    # 상위 풀에서 무작위 섞어 k개 추출, 공백/구두점 제거 기준으로 중복 방지
    pool = [p for _, p in scored[:80]]
    random.shuffle(pool)
    out, seen = [], set()
    for p in pool:
        base = re.sub(r"[\s\W_]+", "", p)
        if base in seen: 
            continue
        seen.add(base.lower())
        out.append(p)
        if len(out) >= k: 
            break
    return out

# -------------------- CSV 쓰기(완전 덮어쓰기) --------------------
def backup_old_keywords():
    try:
        if not BACKUP_OLD or not os.path.exists(CSV):
            return
        today = datetime.now().strftime("%Y%m%d")
        Path(".cache").mkdir(parents=True, exist_ok=True)
        dst = f".cache/keywords_{today}.txt"
        # 덮어쓰지 않도록 번호 증가
        idx = 1
        base = dst
        while os.path.exists(dst):
            dst = base.replace(".txt", f"_{idx}.txt")
            idx += 1
        with open(CSV, "r", encoding="utf-8") as f, open(dst, "w", encoding="utf-8") as w:
            w.write(f.read())
        print(f"[backup] saved previous keywords -> {dst}")
    except Exception as e:
        print("[backup warn]", e)

def write_today_keywords_only(keywords):
    """
    keywords.csv를 오늘 키워드 1줄만 남기도록 '완전히 덮어쓴다'.
    기존 줄은 모두 삭제됨.
    """
    new_first = ", ".join(keywords)
    backup_old_keywords()
    with open(CSV, "w", encoding="utf-8", newline="\n") as f:
        f.write(new_first + "\n")
    print("[OK] wrote ONLY today's keywords (file truncated):")
    print(new_first)

# -------------------- 메인 --------------------
SEED_BACKUP = [
    "경제 동향","주식 시장","환율 전망","부동산 정책","전기차 배터리",
    "스마트폰 신제품","AI 트렌드","클라우드 보안","원자재 가격","반도체 수요",
    "소비자 물가","관광 산업","우주 탐사","헬스케어 웨어러블","친환경 에너지",
    "해외 직구","업무 자동화","생산성 도구","디지털 마케팅","무료 소프트웨어"
]

def main():
    ap = argparse.ArgumentParser(description="Update today's keywords and overwrite keywords.csv")
    ap.add_argument("--k", type=int, default=K_DEFAULT, help="추출할 키워드 개수 (default: KEYWORDS_K or 10)")
    ap.add_argument("--no-google", action="store_true")
    ap.add_argument("--no-newsapi", action="store_true")
    ap.add_argument("--no-naver", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="파일 쓰지 않고 출력만")
    args = ap.parse_args()

    titles = []

    if not args.no_newsapi:
        titles += fetch_newsapi_titles(NEWSAPI_KEY, page_size=50)
    if not args.no_naver:
        titles += fetch_naver_titles(NAVER_ID, NAVER_SECRET, per_query=15)
    if not args.no_google:
        titles += fetch_google_rss_titles()

    phrases = []
    for t in titles:
        phrases += extract_phrases_ko(t)

    # 소스가 비거나 부족하면 시드로 보충
    if len(set(phrases)) < args.k:
        phrases += SEED_BACKUP

    picked = rank_and_pick(phrases, k=args.k)
    if len(picked) < min(2, args.k):
        picked = (SEED_BACKUP + picked)[:args.k]

    if args.dry_run:
        print("[dry-run] keywords:", ", ".join(picked))
        return

    write_today_keywords_only(picked)

if __name__ == "__main__":
    main()
