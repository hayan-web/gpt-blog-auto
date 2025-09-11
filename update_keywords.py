# -*- coding: utf-8 -*-
"""
update_keywords.py
- 네이버 검색 API + 네이버 데이터랩(검색어트렌드/쇼핑인사이트) + (옵션) NewsAPI
- 최대 50개 후보 키워드 수집 후 스코어링 → 황금 키워드 선별
- 결과 파일:
  - keywords_general.csv, keywords.csv (동일, 일상글용)
  - keywords_shopping.csv (쿠팡글 후보)
  - golden_shopping_keywords.csv (쿠팡글 우선)
환경변수(.env):
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET (필수-네이버)
  NEWSAPI_KEY (선택)
  # 수집 개수/기간
  KEYWORDS_K=50          # 일반 후보 수
  KEYWORDS_GOLD=18       # 일반 황금 (참고: 현재는 사용 안 함, 형식 유지)
  SHOP_K=50              # 쇼핑 후보 수
  SHOP_GOLD=18           # 쇼핑 황금 개수
  DAYS=7                 # 데이터랩 집계 구간
  # 사용 스위치(기본 온)
  USE_NAVER_SEARCH=1
  USE_NAVER_DATALAB_SEARCH=1
  USE_NAVER_DATALAB_SHOP=1
  # 쇼핑인사이트 카테고리(선택): "50000006:가전,50000003:패션" 형식
  NAVER_SHOP_CATEGORIES=
  # 필터/사용로그
  USAGE_DIR=.usage
  USER_AGENT=gpt-blog-keywords/2.0
"""
import os, re, csv, json, time, random
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import List, Dict, Tuple
import requests
from dotenv import load_dotenv

load_dotenv()

UA = os.getenv("USER_AGENT") or "gpt-blog-keywords/2.0"
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

# ===== 파라미터 =====
K_GENERAL  = int(os.getenv("KEYWORDS_K"   , "50"))
K_GOLD_GEN = int(os.getenv("KEYWORDS_GOLD", "18"))
K_SHOP     = int(os.getenv("SHOP_K"       , os.getenv("KEYWORDS_K","50")))
K_GOLD_SH  = int(os.getenv("SHOP_GOLD"    , os.getenv("KEYWORDS_GOLD","18")))
DAYS_RANGE = int(os.getenv("DAYS"         , "7"))

USE_NAVER_SEARCH          = (os.getenv("USE_NAVER_SEARCH"         ,"1").lower() in ("1","true","y","on"))
USE_NAVER_DATALAB_SEARCH  = (os.getenv("USE_NAVER_DATALAB_SEARCH" ,"1").lower() in ("1","true","y","on"))
USE_NAVER_DATALAB_SHOP    = (os.getenv("USE_NAVER_DATALAB_SHOP"   ,"1").lower() in ("1","true","y","on"))

NAVER_ID     = os.getenv("NAVER_CLIENT_ID") or ""
NAVER_SECRET = os.getenv("NAVER_CLIENT_SECRET") or ""
NEWSAPI_KEY  = os.getenv("NEWSAPI_KEY") or ""

USAGE_DIR  = os.getenv("USAGE_DIR") or ".usage"
USED_FILE  = Path(USAGE_DIR) / "used_shopping.txt"

# 선택 입력: "50000006:가전,50000003:패션" → [(code, name), ...]
def _parse_shop_categories(s: str) -> List[Tuple[str,str]]:
    out=[]
    for part in (s or "").split(","):
        part=part.strip()
        if not part: continue
        if ":" in part:
            code, name = part.split(":",1)
            out.append((code.strip(), name.strip()))
        else:
            out.append((part.strip(), part.strip()))
    return out
NAVER_SHOP_CATS = _parse_shop_categories(os.getenv("NAVER_SHOP_CATEGORIES",""))

# ===== 사전/토큰 =====
SHOP_CATS  = [
  "가습기","미니 가습기","초음파 가습기","가열식 가습기",
  "청소기","무선 청소기","핸디 청소기","물걸레 청소기",
  "전기포트","전기주전자","보조배터리",
  "히터","전기요","제습기","서큘레이터","선풍기",
  "니트","가디건","스웨터","케이프","숄","니트 원피스"
]
ADJ = ["미니","무선","휴대용","초음파","가열식","가을","겨울","브이넥","라운드","오버핏","크롭","롱","반팔","폴라","하이넥","레이스"]

STOPWORDS = set("단독 속보 인터뷰 사진 영상 전문 기자 안내 공지 모집 이벤트 할인 세일 특가 혜택 역대급 클릭 바로가기 썸네일 LIVE 생중계 생방송 무료 체험".split())
DROP_TOK  = set("공식 무상 정품 신상 인기 베스트 새상품 리뷰 후기 사용기 광고 예약됨 최저가 역대급 100% 필구 대박".split())

# ===== 공용 유틸 =====
def norm(s:str)->str:
    s=(s or "").strip()
    s=re.sub(r"[“”‘’\"\'\[\]\(\)\|]", " ", s)
    s=re.sub(r"\s+", " ", s)
    return s

def tokenize_ko(s:str)->List[str]:
    s=re.sub(r"[^가-힣a-zA-Z0-9\s]", " ", s)
    return [t for t in s.split() if t]

def write_col_csv(path:Path, items:List[str], header:str="keyword"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([header])
        for it in items:
            it=it.strip()
            if it: w.writerow([it])

def load_used_recent(n_days:int=30)->List[str]:
    try:
        if not USED_FILE.exists(): return []
        cutoff = datetime.utcnow().date() - timedelta(days=n_days)
        out=[]
        for line in USED_FILE.read_text("utf-8").splitlines():
            line=line.strip()
            if not line or "\t" not in line: 
                continue
            d,kw = line.split("\t",1)
            try:
                if datetime.strptime(d,"%Y-%m-%d").date() >= cutoff:
                    out.append(kw.strip())
            except Exception:
                out.append(kw.strip())
        return out[-200:]
    except Exception:
        return []

# ===== 소스: NewsAPI (선택) =====
def fetch_newsapi(q:str, from_days:int=7, size:int=60)->List[Dict]:
    if not NEWSAPI_KEY: return []
    url = "https://newsapi.org/v2/everything"
    params = {
        "apiKey": NEWSAPI_KEY,
        "q": q, "language": "ko",
        "from": (datetime.utcnow()-timedelta(days=from_days)).date().isoformat(),
        "sortBy": "publishedAt",
        "pageSize": size
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=15); r.raise_for_status()
        return r.json().get("articles", [])
    except Exception as e:
        print(f"[NEWSAPI][WARN] {type(e).__name__}: {e}")
        return []

# ===== 소스: 네이버 검색 API =====
def _naver_get(url:str, params:dict)->dict:
    if not (NAVER_ID and NAVER_SECRET): 
        return {}
    headers = {**HEADERS, "X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET}
    try:
        r = requests.get(url, params=params, headers=headers, timeout=12); r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[NAVER][WARN] GET {url.split('/')[-1]}: {type(e).__name__}: {e}")
        return {}

def naver_search_news(q:str, display:int=50)->List[Dict]:
    if not USE_NAVER_SEARCH: return []
    js = _naver_get("https://openapi.naver.com/v1/search/news.json",
                    {"query": q, "display": display, "sort":"date"})
    return js.get("items", []) if js else []

def naver_search_web(q:str, display:int=50)->List[Dict]:
    if not USE_NAVER_SEARCH: return []
    js = _naver_get("https://openapi.naver.com/v1/search/webkr",
                    {"query": q, "display": display})
    return js.get("items", []) if js else []

def harvest_texts()->List[str]:
    pool=[]
    # 1) 네이버 뉴스/웹
    queries = ["트렌드", "출시", "신제품", "업데이트", "가을", "겨울", "핫", "행사", "프로모션"]
    for q in queries:
        for it in naver_search_news(q, display=50):
            pool += [it.get("title",""), it.get("description","")]
        for it in naver_search_web(q, display=30):
            pool += [it.get("title",""), it.get("description","")]
        time.sleep(0.15)
    # 2) (옵션) NewsAPI 보강
    if NEWSAPI_KEY:
        for q in ["트렌드", "이슈", "발표", "업데이트"]:
            for art in fetch_newsapi(q, from_days=DAYS_RANGE, size=40):
                pool += [art.get("title",""), art.get("description","")]
            time.sleep(0.15)
    return [norm(t) for t in pool if t]

# ===== 소스: 네이버 데이터랩 (검색어트렌드) =====
def _naver_post(url:str, payload:dict)->dict:
    if not (NAVER_ID and NAVER_SECRET):
        return {}
    headers = {**HEADERS, "X-Naver-Client-Id": NAVER_ID, "X-Naver-Client-Secret": NAVER_SECRET, "Content-Type":"application/json"}
    try:
        r = requests.post(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"[NAVER][WARN] POST {url.split('/')[-1]}: {type(e).__name__}: {e}")
        return {}

def chunk(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

def datalab_search_trends(groups:List[Dict], days:int=7, time_unit:str="date",
                          device:str="", gender:str="", ages:List[str]=None)->Dict[str, float]:
    """
    groups: [{"groupName":"가습기","keywords":["가습기","미니 가습기"]}, ...]
    return: {groupName: momentum_score}
    """
    if not USE_NAVER_DATALAB_SEARCH or not groups:
        return {}
    start = (date.today() - timedelta(days=max(7, days*2))).isoformat()
    end   = date.today().isoformat()
    result={}
    for pack in chunk(groups, 5):  # API 제약: 그룹 최대 5개/호출
        payload = {
            "startDate": start, "endDate": end, "timeUnit": time_unit,
            "keywordGroups": pack
        }
        if device: payload["device"]=device
        if gender: payload["gender"]=gender
        if ages:   payload["ages"]=ages
        js = _naver_post("https://openapi.naver.com/v1/datalab/search", payload)
        if not js: 
            time.sleep(0.2); 
            continue
        try:
            for series in js.get("results", []):
                name = series.get("title") or series.get("keyword") or series.get("keywords",[None])[0]
                data = series.get("data", [])
                if not data: 
                    continue
                # momentum: 최근 N일 평균 - 그 이전 N일 평균
                vals = [float(x.get("ratio",0.0)) for x in data]
                if len(vals) < 6: 
                    score = sum(vals[-3:]) / max(1, len(vals[-3:]))
                else:
                    recent = vals[-days:]
                    prev   = vals[-(2*days):-days] if len(vals) >= 2*days else vals[:-days]
                    score = (sum(recent)/max(1,len(recent))) - (sum(prev)/max(1,len(prev)))
                result[norm(name)] = result.get(norm(name), 0.0) + score
        except Exception:
            pass
        time.sleep(0.2)
    return result

# ===== 소스: 네이버 데이터랩 (쇼핑인사이트) =====
def datalab_shopping_keywords(category_code:str, start:str, end:str, time_unit:str="date",
                               device:str="", gender:str="", ages:List[str]=None)->List[Tuple[str,float]]:
    """
    카테고리별 연관 키워드(인기도) 수집.
    참고: 일부 계정/권한에서 미지원일 수 있어 실패시 빈 리스트 반환.
    """
    if not USE_NAVER_DATALAB_SHOP or not category_code:
        return []
    payload = {"startDate": start, "endDate": end, "timeUnit": time_unit, "category": category_code}
    if device: payload["device"]=device
    if gender: payload["gender"]=gender
    if ages:   payload["ages"]=ages
    js = _naver_post("https://openapi.naver.com/v1/datalab/shopping/category/keywords", payload)
    out=[]
    try:
        for it in js.get("results", []):
            kw  = norm(it.get("keyword") or it.get("title") or "")
            val = float(it.get("ratio", 0.0))
            if kw: out.append((kw, val))
    except Exception:
        # 일부 스펙에서 다른 필드 구조를 반환 → 포용적으로 파싱
        try:
            for series in js.get("results", []):
                for row in series.get("data", []):
                    kw  = norm(row.get("title") or row.get("keyword") or "")
                    val = float(row.get("ratio", 0.0))
                    if kw: out.append((kw, val))
        except Exception:
            return []
    return out

# ===== 키워드 추출/분리/스코어링 =====
def extract_phrases(texts:List[str])->List[str]:
    """가벼운 규칙 기반 한국어 구문 추출."""
    cands=set()
    for t in texts:
        toks = tokenize_ko(t)
        toks = [x for x in toks if x not in STOPWORDS and x not in DROP_TOK]
        n=len(toks)
        for win in (2,3,4):
            for i in range(0, max(0,n-win+1)):
                seg=" ".join(toks[i:i+win])
                if len(seg) < 4: continue
                cands.add(seg)
        for x in toks:
            if 2 < len(x) < 10 and re.search(r"[가-힣]", x):
                cands.add(x)
    return list(cands)

def expand_shopping_base()->List[str]:
    base=set(SHOP_CATS)
    for cat in SHOP_CATS:
        for a in ADJ:
            if a not in cat:
                base.add(f"{a} {cat}")
    return list(base)

def split_shop_general(cands:List[str])->Tuple[List[str], List[str]]:
    shop, general=[], []
    shop_words = set([w for cat in SHOP_CATS for w in cat.split()])
    for s in cands:
        if any(c in s for c in SHOP_CATS) or any(w in s for w in shop_words):
            shop.append(s)
        else:
            general.append(s)
    return shop, general

def score_keyword(s:str, is_shop:bool, source_freq:Dict[str,int], recent_block:set,
                  dl_momentum:Dict[str,float], shop_boost:Dict[str,float])->float:
    base = source_freq.get(s, 1)
    length=len(s)
    score = base
    if 6 <= length <= 16: score += 1.2
    elif 4 <= length <= 22: score += 0.6
    else: score -= 0.6
    # 데이터랩 모멘텀(최근 상승) 반영
    if s in dl_momentum:
        score += dl_momentum[s] * 0.12
    # 쇼핑 인사이트 연관 키워드 가중
    if is_shop and s in shop_boost:
        score += shop_boost[s] * 0.04
    # 형용사 + 품목 보너스
    if is_shop and any(a in s for a in ADJ): score += 0.6
    if is_shop and any(c in s for c in SHOP_CATS): score += 0.7
    # 최근 사용 감점
    if s in recent_block: score -= 2.0
    # 날짜 고정 난수로 경미한 다양성
    rnd = random.Random(abs(hash(f"{date.today()}|{s}")))
    score += rnd.uniform(-0.25, 0.25)
    return score

def pick_top(cands:List[str], k:int, is_shop:bool, recent_block:set,
             dl_momentum:Dict[str,float], shop_boost:Dict[str,float])->List[str]:
    # 출현 빈도
    freq={}
    for c in cands: freq[c]=freq.get(c,0)+1
    # 정제/중복 제거
    seen=set(); dedup=[]
    for c in cands:
        cc=norm(c)
        if not cc or cc in seen: continue
        seen.add(cc); dedup.append(cc)
    # 스코어
    scored=[(score_keyword(c, is_shop, freq, recent_block, dl_momentum, shop_boost), c) for c in dedup]
    scored.sort(key=lambda x:x[0], reverse=True)
    out=[c for _,c in scored[:k*2]]  # 1차 넉넉히 가져옴
    # 과도한 편중 방지(품목 버킷)
    balanced=[]; bucket={}
    for w in out:
        key=None
        for cat in SHOP_CATS:
            if cat in w or cat.split()[0] in w:
                key=cat.split()[0]; break
        if key:
            if bucket.get(key,0)>= max(1, k//10): 
                continue
            bucket[key]=bucket.get(key,0)+1
        balanced.append(w)
        if len(balanced)>=k: break
    return balanced[:k]

def choose_golden(full:List[str], g:int)->List[str]:
    if not full: return []
    head=full[:max(8,g)]
    tail=full[max(8,g):max(40,len(full))]
    rnd = random.Random(abs(hash(date.today().isoformat())))
    rnd.shuffle(tail)
    pool=(head+tail)[:max(g*2, g+6)]
    out=[]
    for s in pool:
        if any((s in x) or (x in s) for x in out):
            continue
        out.append(s)
        if len(out)>=g: break
    return out[:g]

# ===== 실행 =====
def main():
    print(f"[KW] collect start (days={DAYS_RANGE}, K_GEN={K_GENERAL}, K_SHOP={K_SHOP})")
    texts = harvest_texts()

    # 1) 규칙 추출 + 쇼핑 기본 확장
    phrases = extract_phrases(texts)
    phrases += expand_shopping_base()

    # 2) 데이터랩 검색어트렌드 모멘텀
    dl_momentum={}
    if USE_NAVER_DATALAB_SEARCH and NAVER_ID and NAVER_SECRET:
        groups=[]
        for cat in SHOP_CATS:
            kw = [cat]
            for a in ADJ:
                if a not in cat: kw.append(f"{a} {cat}")
            groups.append({"groupName": cat, "keywords": kw[:5]})  # 그룹 내 키워드 1~5개
        # 과도 호출 방지: 상위 N 그룹만 사용
        groups = groups[:50]
        dl_momentum = datalab_search_trends(groups, days=DAYS_RANGE, time_unit="date")

    # 3) 데이터랩 쇼핑인사이트(카테고리 연관 키워드) → boost 테이블
    shop_boost={}
    if USE_NAVER_DATALAB_SHOP and NAVER_SHOP_CATS and NAVER_ID and NAVER_SECRET:
        start = (date.today() - timedelta(days=max(7,DAYS_RANGE*2))).isoformat()
        end   = date.today().isoformat()
        for code, name in NAVER_SHOP_CATS:
            rows = datalab_shopping_keywords(code, start, end, time_unit="date")
            for kw, val in rows:
                shop_boost[kw] = shop_boost.get(kw, 0.0) + float(val)
            time.sleep(0.2)

    # 4) 분리
    shop_cands, general_cands = split_shop_general(phrases)

    # 5) 최근 사용 차단(쇼핑만)
    recent_block=set(load_used_recent(30))

    # 6) 선별
    top_general = pick_top(general_cands, K_GENERAL, is_shop=False, recent_block=set(),
                           dl_momentum=dl_momentum, shop_boost={})
    top_shop    = pick_top(shop_cands   , K_SHOP   , is_shop=True , recent_block=recent_block,
                           dl_momentum=dl_momentum, shop_boost=shop_boost)
    golden_shop = choose_golden(top_shop, K_GOLD_SH)

    # 7) 저장
    write_col_csv(Path("keywords_general.csv"), top_general)
    write_col_csv(Path("keywords.csv"      ), top_general)    # 호환
    write_col_csv(Path("keywords_shopping.csv"), top_shop)
    write_col_csv(Path("golden_shopping_keywords.csv"), golden_shop)

    # 8) 로그
    print(f"[GENERAL] {len(top_general)} → keywords_general.csv (head={top_general[:5]})")
    print(f"[SHOP]    {len(top_shop)} → keywords_shopping.csv (gold={len(golden_shop)})")
    print(f"[GOLD]    {golden_shop[:8]} …")
    if shop_boost:
        print(f"[SHOP-INSIGHT] categories={len(NAVER_SHOP_CATS)} boost_keys={len(shop_boost)}")
    if dl_momentum:
        print(f"[DATALAB-SEARCH] groups={min(50,len(dl_momentum))} (momentum keys)")

# --- CLI flags 호환 래퍼 (기존 워크플로 인자 사용 가능) ---
if __name__ == "__main__":
    import argparse, os
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--k", type=int)
    p.add_argument("--gold", type=int)
    p.add_argument("--shop-k", type=int, dest="shop_k")
    p.add_argument("--shop-gold", type=int, dest="shop_gold")
    p.add_argument("--days", type=int)
    p.add_argument("--parallel", type=int)  # 무시용
    args, _ = p.parse_known_args()
    if args.k:         os.environ["KEYWORDS_K"] = str(args.k)
    if args.gold:      os.environ["KEYWORDS_GOLD"] = str(args.gold)
    if args.shop_k:    os.environ["SHOP_K"] = str(args.shop_k)
    if args.shop_gold: os.environ["SHOP_GOLD"] = str(args.shop_gold)
    if args.days:      os.environ["DAYS"] = str(args.days)
    main()
