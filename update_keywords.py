# update_keywords.py
# Google RSS + NewsAPI + Naver Search API (뉴스) 기반 키워드 추출
import os, re, random, requests
from dotenv import load_dotenv
load_dotenv()

CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY", "")
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

def fetch_google_news(limit=80):
    url = "https://news.google.com/rss?hl=ko&gl=KR&ceid=KR:ko"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        titles = re.findall(r"<title>(.*?)</title>", r.text, flags=re.S)
        return [re.sub(r"<.*?>", "", t).strip() for t in titles[1:limit+1] if t]
    except Exception:
        return []

def fetch_newsapi(limit=80):
    if not NEWSAPI_KEY: return []
    url = f"https://newsapi.org/v2/top-headlines?country=kr&pageSize={limit}"
    try:
        r = requests.get(url, headers={"X-Api-Key": NEWSAPI_KEY}, timeout=20)
        r.raise_for_status()
        data = r.json()
        return [a.get("title","").strip() for a in data.get("articles",[]) if a.get("title")]
    except Exception:
        return []

def fetch_naver_news(query="트렌드", display=50):
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET): return []
    url = f"https://openapi.naver.com/v1/search/news.json?query={query}&display={display}"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID,
               "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
        out = []
        for it in items:
            t = re.sub(r"<.*?>", "", it.get("title",""))
            d = re.sub(r"<.*?>", "", it.get("description",""))
            if t: out.append(t)
            if d: out.append(d)
        return out
    except Exception:
        return []

def normalize(txt):
    txt = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", txt)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()

def build_candidates(texts, max_candidates=120):
    tokens = []
    for t in texts:
        n = normalize(t)
        parts = [p for p in n.split() if 1 < len(p) <= 15]
        for k in range(2,5):
            for i in range(len(parts)-k+1):
                phrase = " ".join(parts[i:i+k])
                if 4 <= len(phrase) <= 28:
                    tokens.append(phrase)
    uniq, seen = [], set()
    for p in tokens:
        key = p.lower()
        if key in seen: continue
        seen.add(key); uniq.append(p)
        if len(uniq) >= max_candidates: break
    return uniq

def save_keywords(top_list, per_line=2, total_lines=10):
    lines = []
    idx = 0
    for _ in range(total_lines):
        chunk = top_list[idx:idx+per_line]
        if not chunk: break
        lines.append(", ".join(chunk))
        idx += per_line
    if not lines: lines = ["예시 키워드 1, 예시 키워드 2"]
    with open(CSV, "w", encoding="utf-8") as f:
        for line in lines: f.write(line + "\n")
    print(f"[OK] wrote {CSV}")

def main():
    titles = []
    titles += fetch_google_news(limit=80)
    titles += fetch_newsapi(limit=80)
    titles += fetch_naver_news(query="트렌드", display=50)
    if not titles:
        base = ["AI 최신 동향", "전기차 배터리", "워드프레스 최적화", "스마트폰 신제품", "여행 체크리스트"]
        random.shuffle(base); save_keywords(base); return
    cands = build_candidates(titles, max_candidates=120)
    random.shuffle(cands)
    save_keywords(cands, per_line=2, total_lines=10)

if __name__ == "__main__":
    main()
