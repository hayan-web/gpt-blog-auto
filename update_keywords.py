# update_keywords.py  (일반 키워드 시드 추가 + 비었을 때 폴백 보강)
# -*- coding: utf-8 -*-
import os, re, csv, json, time, argparse
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Iterable
import requests

NAVER_CLIENT_ID     = (os.getenv("NAVER_CLIENT_ID") or "").strip()
NAVER_CLIENT_SECRET = (os.getenv("NAVER_CLIENT_SECRET") or "").strip()
USER_AGENT          = os.getenv("USER_AGENT") or "gpt-blog-keywords/1.3"

OUT_GENERAL  = "keywords_general.csv"
OUT_SHOPPING = "keywords_shopping.csv"
OUT_GOLDEN   = "golden_shopping_keywords.csv"

BAN_KEYWORDS = [w.strip() for w in (os.getenv("BAN_KEYWORDS") or "").split(",") if w.strip()]

# 쇼핑 시드
DEFAULT_SEEDS = [
    "니트","니트 원피스","가디건","스웨터","선풍기","미니 선풍기","휴대용 선풍기",
    "가습기","미니 가습기","초음파 가습기","가열식 가습기","청소기","무선 청소기",
    "핸디 청소기","물걸레 청소기","전기포트","전기 주전자","보조배터리","무선충전 보조배터리",
    "전기요","히터","제습기"
]
# 일반(일상형) 시드 — 뉴스/설명글 아닌 ‘일상 기록’ 계열로 안정적 수집
DEFAULT_GENERAL_SEEDS = [
    "아침 루틴","시간 관리","집 정리","하루 회고","주간 계획","작업 집중",
    "산책 기록","홈카페","취미 기록","사진 정리","가계부","생활 루틴",
    "작은 습관","정리정돈","하루 일정"
]

SHOP_PAT = re.compile(r"(니트|원피스|가디건|스웨터|선풍기|가습기|청소기|전기포트|주전자|보조배터리|전기요|히터|제습기)")

def _headers()->Dict[str,str]:
    return {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

def _post_json(url: str, body: dict, timeout=12, retries=2) -> dict:
    last=None
    for i in range(retries+1):
        try:
            r=requests.post(url, headers=_headers(), json=body, timeout=timeout)
            if r.status_code==429 and i<retries:
                time.sleep(0.9+0.4*i); continue
            r.raise_for_status(); return r.json()
        except requests.HTTPError as e:
            last=e
            if r is not None and r.status_code in (400,401,403,404):
                print(f"[NAVER][WARN] {url} -> {r.status_code}: {r.text[:160]}"); return {}
            if i<retries: time.sleep(0.6+0.3*i)
        except Exception as e:
            last=e
            if i<retries: time.sleep(0.5+0.3*i)
    if last: print(f"[NAVER][WARN] {type(last).__name__}: {last}")
    return {}

def _dedupe(seq: Iterable[str])->List[str]:
    seen=set(); out=[]
    for s in seq:
        k=(s or "").strip()
        if not k or k in seen: continue
        seen.add(k); out.append(k)
    return out

def _normalize_kw(s:str)->str:
    s=(s or "").strip()
    s=re.sub(r"\s+"," ",s)
    return s if len(s)>1 else ""

def _is_shopping_kw(kw:str)->bool:
    return bool(SHOP_PAT.search(kw))

def _ban(kw:str)->bool:
    if not kw: return True
    if len(kw)>40: return True
    if any(b and (b in kw) for b in BAN_KEYWORDS): return True
    if not re.search(r"[가-힣]", kw): return True
    return False

def _score_from_series(rows: List[Dict])->float:
    if not rows: return 0.0
    ratios=[float(x.get("ratio",0)) for x in rows if x and "ratio" in x]
    if not ratios: return 0.0
    n=len(ratios); last=ratios[-1]
    a=ratios[-3:] if n>=3 else ratios
    b=ratios[-6:-3] if n>=6 else ratios[:max(0,n-3)]
    m1=sum(a)/max(1,len(a))
    m0=sum(b)/max(1,len(b)) if b else 0.0
    momentum=max(0.0,m1-m0)
    recency=last*0.3
    return round(momentum+recency,4)

def collect_datalab_search(seeds: List[str], days:int=7)->Dict[str,float]:
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        print("[DATALAB-SEARCH][SKIP] NAVER 키 누락"); return {}
    end=datetime.now().date(); start=end-timedelta(days=max(1,days))
    groups=[{"groupName": s[:20] or "seed", "keywords":[s]} for s in _dedupe(seeds)]
    acc:Dict[str,float]={}
    for i in range(0,len(groups),5):  # keywordGroups ≤ 5
        body={"startDate":start.strftime("%Y-%m-%d"),
              "endDate":end.strftime("%Y-%m-%d"),
              "timeUnit":"date",
              "keywordGroups":groups[i:i+5]}
        data=_post_json("https://openapi.naver.com/v1/datalab/search", body)
        for res in data.get("results",[]):
            kws=res.get("keywords") or []
            rows=res.get("data") or []
            sc=_score_from_series(rows)
            for k in kws:
                k=_normalize_kw(k)
                if not _ban(k):
                    acc[k]=max(acc.get(k,0.0), sc)
        time.sleep(0.2)
    print(f"[DATALAB-SEARCH] groups={len(groups)} (momentum keys)")
    return acc

def collect_datalab_shopping_candidates(days:int=7)->Dict[str,float]:
    # 접근권 없으면 경고만 찍고 조용히 패스
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET): return {}
    end=datetime.now().date(); start=end-timedelta(days=max(1,days))
    url="https://openapi.naver.com/v1/datalab/shopping/category/keywords"
    categories=["50000003","50000005"]
    acc={}
    for c in categories:
        body={"startDate":start.strftime("%Y-%m-%d"),
              "endDate":end.strftime("%Y-%m-%d"),
              "timeUnit":"date",
              "category":c}
        data=_post_json(url, body)
        try:
            for res in data.get("results",[]):
                for kv in res.get("keywords",[]):
                    kw=_normalize_kw(kv.get("keyword") or "")
                    if _ban(kw): continue
                    ratio=float(kv.get("ratio") or 0)
                    acc[kw]=max(acc.get(kw,0.0), ratio)
        except Exception:
            return {}
        time.sleep(0.15)
    if acc: print(f"[DATALAB-SHOP] candidates={len(acc)}")
    return acc

def _write_col_csv(path:str, items:List[str]):
    with open(path,"w",encoding="utf-8",newline="") as f:
        w=csv.writer(f); w.writerow(["keyword"])
        for s in items: w.writerow([s])

def _rank_and_split(scored:Dict[str,float], k_general:int, k_shop:int)->Tuple[List[str],List[str]]:
    pairs=sorted(scored.items(), key=lambda x:x[1], reverse=True)
    gens, shops=[],[]
    for kw,_ in pairs:
        (shops if _is_shopping_kw(kw) else gens).append(kw)
    return gens[:k_general], shops[:k_shop]

def _pick_golden(shop_list:List[str], gold_n:int)->List[str]:
    return shop_list[:gold_n]

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=int(os.getenv("KEYWORDS_K") or 50))
    ap.add_argument("--gold", type=int, default=12)
    ap.add_argument("--shop-k", type=int, default=50)
    ap.add_argument("--shop-gold", type=int, default=16)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--parallel", type=int, default=4)  # 호환용
    args=ap.parse_args()

    env_seeds=[s.strip() for s in (os.getenv("AFF_FALLBACK_KEYWORDS") or "").split(",") if s.strip()]
    seeds=_dedupe(env_seeds + DEFAULT_SEEDS + DEFAULT_GENERAL_SEEDS)

    print(f"[KW] collect start (days={args.days}, K_GEN={args.k}, K_SHOP={args.shop_k})")

    scored=collect_datalab_search(seeds, days=args.days)
    shop_boost=collect_datalab_shopping_candidates(days=args.days)
    if shop_boost:
        for kw,v in shop_boost.items():
            scored[kw]=max(scored.get(kw,0.0), v*0.8)

    cleaned={k:v for k,v in scored.items() if not _ban(k)}
    if len(cleaned)<20:
        for s in seeds:
            if not _ban(s):
                cleaned[s]=max(cleaned.get(s,0.0), 0.1)

    gen_list, shop_list = _rank_and_split(cleaned, k_general=args.k, k_shop=args.shop_k)

    # 일반이 비면 안전한 기본 시드로 채움
    if not gen_list:
        gen_list = DEFAULT_GENERAL_SEEDS[:args.k]

    golden=_pick_golden(shop_list, args.shop_gold)

    _write_col_csv(OUT_GENERAL, gen_list)
    _write_col_csv(OUT_SHOPPING, shop_list)
    _write_col_csv(OUT_GOLDEN, golden)

    def _head(path:str,n=4):
        try:
            with open(path,"r",encoding="utf-8") as f:
                rows=[r.strip() for r in f.read().splitlines() if r.strip()]
                return rows[1:1+n]
        except Exception:
            return []
    print(f"[GENERAL] {len(gen_list)} → {OUT_GENERAL} (head={_head(OUT_GENERAL)})")
    print(f"[SHOP]    {len(shop_list)} → {OUT_SHOPPING} (gold={len(golden)})")
    if golden: print(f"[GOLD]    {golden[:8]} …")

if __name__=="__main__":
    main()
