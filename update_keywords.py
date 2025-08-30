# update_keywords.py : 네이버 뉴스 API + NewsAPI.org 기반 키워드 (엔티티/인코딩/바이그램 개선, 2개 저장)
import os, csv, re, sys, html, unicodedata
import requests
from collections import Counter
from dotenv import load_dotenv

load_dotenv()
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET")
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")

if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
    print("[오류] .env에 NAVER_CLIENT_ID, NAVER_CLIENT_SECRET 설정 필요", file=sys.stderr); sys.exit(1)
if not NEWSAPI_KEY:
    print("[오류] .env에 NEWSAPI_KEY 설정 필요", file=sys.stderr); sys.exit(1)

STOPWORDS = {
    # 조사/접속사/보조어
    "은","는","이","가","을","를","과","와","도","만","로","으로","에서","에게","께","의",
    "및","등","또","또한","그리고","그러나","하지만","보다","관련","대해","대한","위해",
    # 흔한 동사/형용사
    "하다","한다","했다","된다","됐다","있다","없다","이어","로서","였다","됐다",
    # 보도 관용어/불필요
    "속보","단독","종합","기자","사진","영상","인터뷰","전문","기사","보도",
    # 날짜/숫자
    "오늘","어제","내일","현재","지난","이번","당시","년","월","일","시","분","초",
    "0","1","2","3","4","5","6","7","8","9",
    # 너무 일반적인 상위개념
    "정부","정책","경제","사회","세계","한국","국내","해외","코로나",
    # 의미 빈약/노이즈
    "사건","사고","quot","nbsp"
}

def norm(s: str) -> str:
    if not s: return ""
    s = html.unescape(s)                         # &quot; 등 엔티티 해제
    s = unicodedata.normalize("NFC", s)          # 한글 정규화
    s = re.sub(r"[^가-힣0-9A-Za-z\s]", " ", s)   # 특수문자 제거
    s = re.sub(r"\s+", " ", s).strip()
    return s

def tokenize(text: str):
    toks = []
    for w in text.split():
        if len(w) <= 1: continue
        if re.fullmatch(r"\d+", w): continue
        lw = w.lower()
        if lw in STOPWORDS: continue
        toks.append(lw)
    return toks

def extract_bigrams(toks):
    res = []
    for a,b in zip(toks, toks[1:]):
        if a in STOPWORDS or b in STOPWORDS: continue
        if len(a) <= 1 or len(b) <= 1: continue
        if re.search(r"\d", a+b): continue
        # 최소 한 글자는 한글 포함(너무 일반 영문 제외)
        if not (re.search(r"[가-힣]", a) or re.search(r"[가-힣]", b)): 
            continue
        res.append(f"{a} {b}")
    return res

def naver_news_titles(query="이슈", display=50):
    url = "https://openapi.naver.com/v1/search/news.json"
    headers = {"X-Naver-Client-Id": NAVER_CLIENT_ID, "X-Naver-Client-Secret": NAVER_CLIENT_SECRET}
    params = {"query": query, "display": display, "sort": "sim"}
    r = requests.get(url, headers=headers, params=params, timeout=15); r.raise_for_status()
    data = r.json()
    titles = []
    for it in data.get("items", []):
        titles.append(norm(it.get("title","")))
        titles.append(norm(it.get("description","")))
    return [t for t in titles if t]

def newsapi_titles(page_size=100):
    url = "https://newsapi.org/v2/top-headlines"
    params = {"country":"kr","pageSize":page_size,"apiKey":NEWSAPI_KEY}
    r = requests.get(url, params=params, timeout=15); r.raise_for_status()
    data = r.json()
    titles=[]
    for art in data.get("articles", []):
        titles.append(norm(art.get("title","")))
        titles.append(norm(art.get("description","")))
    return [t for t in titles if t]

def pick_keywords(titles, want=2):
    uni, bi = Counter(), Counter()
    for t in titles:
        toks = tokenize(t)
        if not toks: continue
        uni.update(toks)
        bi.update(extract_bigrams(toks))

    scored = Counter()
    for k,c in bi.items(): scored[k] += c*4   # 바이그램 가중치↑
    for k,c in uni.items(): scored[k] += c

    result=[]
    for term,_ in scored.most_common():
        if len(term.replace(" ",""))<2: continue
        # 유니그램이 너무 일반적이면 패스
        if " " not in term and term in {"정부","정책","경제","사회","세계","한국"}: continue
        if term in result: continue
        result.append(term)
        if len(result)>=want: break

    # 부족하면 유니그램으로 보충
    if len(result)<want:
        for ug,_ in uni.most_common():
            if ug not in result and ug not in STOPWORDS and len(ug)>1:
                result.append(ug)
            if len(result)>=want: break
    return result

def main():
    titles = []
    try:
        titles += naver_news_titles("이슈", 70)
        titles += naver_news_titles("사건", 70)
    except Exception as e:
        print("[경고] 네이버 뉴스 수집 실패:", e, file=sys.stderr)
    try:
        titles += newsapi_titles(100)
    except Exception as e:
        print("[경고] NewsAPI 수집 실패:", e, file=sys.stderr)

    hot = pick_keywords(titles, want=2) if titles else []

    # ⚠️ Windows에서 한글 깨짐 방지: utf-8-sig(BOM)로 저장
    with open("keywords.csv","w",newline="",encoding="utf-8-sig") as f:
        w=csv.writer(f)
        for kw in hot:
            w.writerow([kw])

    print("오늘의 키워드:", hot)

if __name__ == "__main__":
    main()
