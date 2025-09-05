# -*- coding: utf-8 -*-
"""
update_keywords.py — 선별형 키워드 파이프라인 (일상/쇼핑 분리, 자동검수, 병렬)

출력 파일:
  # 일반(일상글용)
  - keywords.csv                  ← 첫 줄에 오늘의 10개(기존 호환)
  - keywords_general.csv          ← 전체 상위 k(한 줄)
  - golden_keywords.csv           ← 황금키워드 g개 (keyword,score)
  - candidates_general.csv        ← 후보 상세 (keyword,score,freq,volume,commerce_score)
  - review_general.csv            ← 검수표(approve 컬럼 포함)

  # 쇼핑(쿠팡글용)
  - shopping_keywords.csv         ← 쇼핑용 상위 k(한 줄)
  - golden_shopping_keywords.csv  ← 쇼핑 황금키워드 g개 (keyword,score)
  - candidates_shopping.csv       ← 후보 상세
  - review_shopping.csv           ← 검수표(approve 컬럼 포함)

사용:
  python update_keywords.py --k 10 --gold 3 --shop-k 12 --shop-gold 3 --review auto --parallel 8

ENV(.env):
  NEWSAPI_KEY(선택), NAVER_CLIENT_ID/SECRET(선택), USER_AGENT,
  KEYWORDS_K, BAN_KEYWORDS (쉼표구분)
"""

import os, re, csv, json, time
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Dict, Tuple, Optional
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

# ---------- ENV ----------
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY") or ""
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID") or ""
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET") or ""
USER_AGENT = os.getenv("USER_AGENT") or "keywords-bot/1.1"
OUT_TOPK = int(os.getenv("KEYWORDS_K") or "10")
BAN_KEYWORDS_ENV = [t.strip() for t in (os.getenv("BAN_KEYWORDS") or "").split(",") if t.strip()]

DEFAULT_BANS = set(BAN_KEYWORDS_ENV + """
사망,사고,화재,폭행,성폭력,성범죄,강간,혐의,검찰,기소,징역,피해자,피습,테러,총격,전쟁,참사,
도박,불법,마약,음주운전,자가격리,코로나,확진,파산,부도,성비위,갑질,자살,자해,분신,
단독,속보,영상,무릎,분노했다,충격,논란,해명,어제,오늘,내일,9월,10월,11월,12월
""".replace("\n","")).difference({""})

HEADERS = {"User-Agent": USER_AGENT}
SESSION = requests.Session()

# ---------- 유틸 ----------
def _norm(s: str) -> str:
    s = re.sub(r"<[^>]+>", " ", s)
    s = s.replace("&quot;", " ").replace("&amp;","&").replace("&nbsp;"," ")
    s = re.sub(r"[“”\"\'‘’·•…—–\-\–\—\_~`:+=<>|/\\\\]", " ")
    s = re.sub(r"\s+", " ", s).strip()
    return s

def _is_hangul_token(tok: str) -> bool:
    if not (2 <= len(tok) <= 15): return False
    if re.search(r"[^\w가-힣]", tok): return False
    if re.fullmatch(r"\d+", tok): return False
    return True

def _bad_phrase(s: str) -> bool:
    if any(b in s for b in DEFAULT_BANS): return True
    if re.search(r"\b\d{1,2}\s*월\b", s): return True
    return not (2 <= len(s) <= 30)

def _z(x, mean, std): return 0 if std == 0 else (x - mean) / std
def _uniq(s: str) -> str: return re.sub(r"\s+", "", s)

# ---------- 원천 수집 (병렬) ----------
def _fetch_news_page(params: Dict) -> List[str]:
    try:
        r = SESSION.get("https://newsapi.org/v2/everything",
                        params=params, headers={"X-Api-Key": NEWSAPI_KEY, **HEADERS}, timeout=12)
        if r.status_code != 200: return []
        arts = (r.json() or {}).get("articles") or []
        out = []
        for a in arts:
            t = _norm((a.get("title") or "") + " " + (a.get("description") or ""))
            if t: out.append(t)
        return out
    except Exception:
        return []

def fetch_news_candidates(days:int=3, limit:int=300, parallel:int=6) -> List[str]:
    if not NEWSAPI_KEY: return []
    to = datetime.utcnow()
    frm = to - timedelta(days=days)
    base = dict(language="ko",
                from_=frm.strftime("%Y-%m-%dT%H:%M:%SZ"),
                to=to.strftime("%Y-%m-%dT%H:%M:%SZ"),
                sortBy="publishedAt",
                pageSize=100,
                q="*")
    pages = [1,2,3]
    jobs = []
    out: List[str] = []
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        for p in pages:
            params = dict(base); params["page"] = p
            # requests는 'from' 예약어 이슈 → alias from_ 사용, 실제 전송 키 조정
            params["from"] = params.pop("from_")
            jobs.append(ex.submit(_fetch_news_page, params))
        for fut in as_completed(jobs):
            out.extend(fut.result())
    return out[:limit]

# ---------- NAVER 데이터랩 (병렬 배치) ----------
def naver_datalab_volumes(keywords: List[str], days:int=30, parallel:int=6) -> Dict[str, float]:
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET): return {}
    end = datetime.now().date()
    start = end - timedelta(days=days)
    headers = {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        "Content-Type": "application/json; charset=utf-8",
    }
    url = "https://openapi.naver.com/v1/datalab/search"

    def _batch(req_group: List[str]) -> Dict[str, float]:
        payload = {
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": end.strftime("%Y-%m-%d"),
            "timeUnit": "date",
            "keywordGroups": [{"groupName": kw, "keywords": [kw]} for kw in req_group]
        }
        try:
            r = SESSION.post(url, headers=headers, data=json.dumps(payload), timeout=12)
            if r.status_code != 200: return {}
            js = r.json()
            res = {}
            for item in js.get("results", []):
                kw = item.get("title")
                ratio = sum(p.get("ratio", 0.0) for p in item.get("data", []))
                res[kw] = float(ratio)
            return res
        except Exception:
            return {}

    out: Dict[str, float] = {}
    batch = 5
    with ThreadPoolExecutor(max_workers=parallel) as ex:
        futs = []
        for i in range(0, len(keywords), batch):
            futs.append(ex.submit(_batch, keywords[i:i+batch]))
        for fut in as_completed(futs):
            out.update(fut.result())
    return out

# ---------- 후보 생성 ----------
def extract_candidates(lines: List[str]) -> Counter:
    tok_cnt = Counter(); phrase_cnt = Counter()
    stop = set("그리고 그러나 하지만 또한 대한 등에 관련 발표 계획 추진 검토 이번 최대 최소 올해 내년 위해 위한 대해서".split())
    for line in lines:
        clean = re.sub(r"[^0-9A-Za-z가-힣\s]", " ", line)
        toks = [t for t in clean.split() if _is_hangul_token(t) and t not in stop]
        for t in toks: tok_cnt[t] += 1
        for n in (2,3):
            for i in range(len(toks)-n+1):
                ph = " ".join(toks[i:i+n])
                if _bad_phrase(ph): continue
                phrase_cnt[ph] += 1
    cand = Counter(); cand.update(tok_cnt); cand.update(phrase_cnt)
    return cand

# ---------- 상업성 점수 ----------
COMMERCE_TERMS = set("""
추천 리뷰 후기 가이드 비교 가격 최저가 세일 특가 쇼핑 쿠폰 할인 핫딜 언박싱 구성 스펙 사용법 꿀팁 베스트
가전 노트북 스마트폰 냉장고 세탁기 에어컨 공기청정기 이어폰 헤드폰 카메라 렌즈 TV 모니터 키보드 마우스 의자 책상 침대 매트리스
화장품 향수 가방 신발 운동화 골프 캠핑 유모차 카시트 분유 기저귀 로봇청소기 에어프라이어 가습기 식기세척기 빔프로젝터
""".split())

def commerce_score(kw: str) -> float:
    toks = set(kw.split())
    hit = len(toks & COMMERCE_TERMS)
    if re.search(r"[A-Za-z].*\d|\d.*[A-Za-z]", kw):  # 모델명류
        hit += 1
    length_bonus = 0.5 if (1 < len(kw.split()) <= 3) else 0.0
    return hit + length_bonus

# ---------- 공통 파이프라인 ----------
def _score_and_rank(candidates: List[Tuple[str,int]], vol_map: Dict[str,float],
                    w_freq: float, w_vol: float, w_com: float,
                    drop_if_bad_commerce: bool=False) -> List[Tuple[str,float,Dict]]:
    if not candidates: return []
    freqs = [f for _,f in candidates]
    f_mean = sum(freqs)/len(freqs)
    f_std  = (sum((x - f_mean)**2 for x in freqs)/max(1,len(freqs)-1))**0.5
    vols = [vol_map.get(kw,0.0) for kw,_ in candidates]
    v_mean = sum(vols)/len(vols) if vols else 0.0
    v_std  = (sum((x - v_mean)**2 for x in vols)/max(1,len(vols)-1))**0.5 if len(vols)>1 else 0.0

    rows = []
    for kw, freq in candidates:
        if _bad_phrase(kw): continue
        fz = _z(freq, f_mean, f_std)
        v_raw = vol_map.get(kw,0.0)
        vz = _z(v_raw, v_mean, v_std) if v_std!=0 else (1.0 if v_raw>0 else 0.0)
        cz = commerce_score(kw)
        if drop_if_bad_commerce and cz <= 0.5:  # 쇼핑 모드에서 약한 키워드 버림
            continue
        score = w_freq*fz + w_vol*vz + w_com*cz
        rows.append((kw, score, {"freq":freq, "vscore":v_raw, "cscore":cz}))
    # 유사어 중복 제거
    best = {}
    for kw, sc, meta in sorted(rows, key=lambda x: x[1], reverse=True):
        k = _uniq(kw)
        if k not in best: best[k] = (kw, sc, meta)
    return list(best.values())

def _write_line_csv(path: str, keywords: List[str]):
    with open(path, "w", encoding="utf-8") as f:
        f.write(",".join(keywords))

def _write_candidates(path: str, ranked: List[Tuple[str,float,Dict]]):
    with open(path,"w",encoding="utf-8",newline="") as f:
        w=csv.writer(f); w.writerow(["keyword","score","freq","volume","commerce_score"])
        for kw, sc, meta in ranked:
            w.writerow([kw, f"{sc:.4f}", meta.get("freq",0), meta.get("vscore",0), meta.get("cscore",0)])

def _write_review(path: str, ranked: List[Tuple[str,float,Dict]], mode:str, top:int,
                  auto_freq:int=2, auto_cscore:float=1.0, auto_take:int=30):
    """
    review CSV: keyword,score,freq,volume,commerce_score,approve
    - auto: 상위 auto_take 안에서 freq>=auto_freq & cscore>=auto_cscore -> approve=1
    - manual: approve=0 (사용자가 체크)
    """
    rows = []
    for i,(kw, sc, meta) in enumerate(ranked[:max(top*5, auto_take)]):
        approve = 1 if (mode=="auto" and meta.get("freq",0)>=auto_freq and meta.get("cscore",0)>=auto_cscore) else 0
        rows.append([kw, f"{sc:.4f}", meta.get("freq",0), meta.get("vscore",0), meta.get("cscore",0), approve])
    with open(path,"w",encoding="utf-8",newline="") as f:
        w=csv.writer(f); w.writerow(["keyword","score","freq","volume","commerce_score","approve"])
        w.writerows(rows)

def _read_approved(path:str) -> List[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8") as f:
        r=csv.DictReader(f)
        for row in r:
            try:
                if str(row.get("approve","0")).strip() in ("1","true","TRUE","yes","Y"):
                    out.append((row.get("keyword","").strip(), float(row.get("score","0") or 0)))
            except Exception:
                continue
    out = [kw for kw,_ in sorted(out, key=lambda x: x[1], reverse=True)]
    return out

# ---------- 메인 ----------
def pipeline(mode:str, k:int, g:int, days:int, min_volume:float, parallel:int) -> Tuple[List[str], List[Tuple[str,float,Dict]], List[Tuple[str,float]]]:
    """
    mode: 'general' or 'shopping'
    """
    # 0) 수집
    lines = fetch_news_candidates(days=days, limit=300, parallel=parallel)
    cand_cnt = extract_candidates(lines)

    # 1) 초기 후보
    prelim = []
    for kw, freq in cand_cnt.most_common(500):
        if _bad_phrase(kw): continue
        # 일반: 너무 짧은 단독 토큰 제거
        if mode=="general" and re.fullmatch(r"[가-힣]{1,2}", kw): continue
        prelim.append((kw, freq))
    if not prelim:
        fallback = ["가전 추천","노트북 추천","스마트폰 추천","카메라 렌즈","무선 이어폰","게이밍 의자","모니터 27인치","에어프라이어","로봇청소기","골프 거리측정기"]
        ranked = [(w, 1.0, {"freq":1,"vscore":0,"cscore":1}) for w in fallback]
        tops  = [w for w,_,_ in ranked[:k]]
        golden= [(tops[0],1.0)]
        return tops, ranked, golden

    # 2) 보조 지표(검색량)
    vol_map = naver_datalab_volumes([kw for kw,_ in prelim[:80]], days=max(14, days*5), parallel=parallel)

    # 3) 가중치(모드별)
    if mode=="general":
        ranked = _score_and_rank(prelim, vol_map, w_freq=0.5, w_vol=0.3, w_com=0.2, drop_if_bad_commerce=False)
    else:  # shopping
        ranked = _score_and_rank(prelim, vol_map, w_freq=0.25, w_vol=0.25, w_com=0.50, drop_if_bad_commerce=True)

    # 4) 탑/골든
    topk = [kw for kw,_,_ in ranked[:k]]
    # 골든: 상위 30 내에서 상업성/검색량을 조금 더 본다
    rg = sorted(ranked[:max(30, g*8)], key=lambda x: (0.35*x[1] + 0.65*(x[2].get("cscore",0) + (1 if x[2].get("vscore",0)>=min_volume else 0))), reverse=True)
    golden = []
    for kw, sc, meta in rg:
        if min_volume>0 and meta.get("vscore",0)<min_volume: continue
        golden.append((kw, sc))
        if len(golden)>=g: break
    if not golden and topk:
        golden=[(topk[0], 0.0)]
    return topk, ranked, golden

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=OUT_TOPK, help="일반(top-k)")
    ap.add_argument("--gold", type=int, default=3, help="일반 황금 개수")
    ap.add_argument("--shop-k", type=int, default=12, help="쇼핑(top-k)")
    ap.add_argument("--shop-gold", type=int, default=3, help="쇼핑 황금 개수")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--min-volume", type=float, default=0.0)
    ap.add_argument("--review", choices=["auto","manual","hybrid"], default="auto",
                    help="auto=자동승인, manual=승인표만 생성, hybrid=승인표의 approve=1 우선, 없으면 자동")
    ap.add_argument("--parallel", type=int, default=6)
    args = ap.parse_args()

    os.makedirs(".cache", exist_ok=True)

    # GENERAL
    g_top, g_ranked, g_golden = pipeline("general", args.k, args.gold, args.days, args.min_volume, args.parallel)
    # REVIEW 처리
    _write_candidates("candidates_general.csv", g_ranked)
    _write_review("review_general.csv", g_ranked, "auto" if args.review=="auto" else "manual",
                  top=args.k, auto_freq=2, auto_cscore=0.8, auto_take=40)
    approved_g = _read_approved("review_general.csv") if args.review in ("manual","hybrid") else []
    if args.review=="manual" and approved_g:
        g_top = approved_g[:args.k]
        g_golden = [(approved_g[0], 0.0)]
    elif args.review=="hybrid" and approved_g:
        g_top = approved_g[:args.k]
        g_golden = [(approved_g[0], 0.0)]

    _write_line_csv("keywords.csv", g_top)
    _write_line_csv("keywords_general.csv", g_top)
    with open("golden_keywords.csv","w",encoding="utf-8",newline="") as f:
        w=csv.writer(f); w.writerow(["keyword","score"])
        for kw,sc in g_golden: w.writerow([kw, f"{sc:.4f}"])

    print(f"[GENERAL] top{len(g_top)} -> keywords.csv")
    print(f"[GENERAL] golden={len(g_golden)} -> golden_keywords.csv")

    # SHOPPING
    s_top, s_ranked, s_golden = pipeline("shopping", args.shop_k, args.shop_gold, args.days, args.min_volume, args.parallel)
    _write_candidates("candidates_shopping.csv", s_ranked)
    _write_review("review_shopping.csv", s_ranked, "auto" if args.review=="auto" else "manual",
                  top=args.shop_k, auto_freq=2, auto_cscore=1.2, auto_take=50)
    approved_s = _read_approved("review_shopping.csv") if args.review in ("manual","hybrid") else []
    if args.review=="manual" and approved_s:
        s_top = approved_s[:args.shop_k]
        s_golden = [(approved_s[0], 0.0)]
    elif args.review=="hybrid" and approved_s:
        s_top = approved_s[:args.shop_k]
        s_golden = [(approved_s[0], 0.0)]

    _write_line_csv("shopping_keywords.csv", s_top)
    with open("golden_shopping_keywords.csv","w",encoding="utf-8",newline="") as f:
        w=csv.writer(f); w.writerow(["keyword","score"])
        for kw,sc in s_golden: w.writerow([kw, f"{sc:.4f}"])

    print(f"[SHOPPING] top{len(s_top)} -> shopping_keywords.csv")
    print(f"[SHOPPING] golden={len(s_golden)} -> golden_shopping_keywords.csv")
    print("[SAMPLE: general]", ", ".join(g_top[:10]))
    print("[SAMPLE: shopping]", ", ".join(s_top[:10]))

if __name__ == "__main__":
    main()
