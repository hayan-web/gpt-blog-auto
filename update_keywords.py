# -*- coding: utf-8 -*-
"""
update_keywords.py
- 네이버 DataLab(검색어 트렌드) + (가능 시) DataLab 쇼핑 인사이트를 활용해
  50개까지 키워드 수집 → 점수화(모멘텀/최근성) → 상위 N개 '황금 키워드' 선별
- 결과 파일
  1) keywords_general.csv         (일반/뉴스성 키워드)
  2) keywords_shopping.csv        (쇼핑성 키워드)
  3) golden_shopping_keywords.csv (쇼핑성 상위 gold개)
- GitHub Actions에서도 멈춤 없이 동작하도록 타임아웃/재시도 최소화

CLI 예:
  python update_keywords.py --k 50 --gold 16 --shop-k 50 --shop-gold 16 --days 7 --parallel 8

환경변수:
  NAVER_CLIENT_ID, NAVER_CLIENT_SECRET (필수)
  KEYWORDS_K           (기본 50)
  BAN_KEYWORDS         (쉼표구분 금지어 부분일치)
  USER_AGENT           (기본 gpt-blog-keywords/1.3)
"""

import os, re, csv, json, time, argparse, random
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Tuple, Iterable
import requests

# ===== ENV =====
NAVER_CLIENT_ID     = (os.getenv("NAVER_CLIENT_ID") or "").strip()
NAVER_CLIENT_SECRET = (os.getenv("NAVER_CLIENT_SECRET") or "").strip()
USER_AGENT          = os.getenv("USER_AGENT") or "gpt-blog-keywords/1.3"

# 출력 파일
OUT_GENERAL  = "keywords_general.csv"
OUT_SHOPPING = "keywords_shopping.csv"
OUT_GOLDEN   = "golden_shopping_keywords.csv"

# 금지어
BAN_KEYWORDS = [w.strip() for w in (os.getenv("BAN_KEYWORDS") or "").split(",") if w.strip()]

# 기본 시드(쇼핑 중심 + 생활잡화) — 필요 시 자유롭게 보강
DEFAULT_SEEDS = [
    "니트", "니트 원피스", "가디건", "스웨터",
    "선풍기", "미니 선풍기", "휴대용 선풍기",
    "가습기", "미니 가습기", "초음파 가습기", "가열식 가습기",
    "청소기", "무선 청소기", "핸디 청소기", "물걸레 청소기",
    "전기포트", "전기 주전자",
    "보조배터리", "무선충전 보조배터리",
    "전기요", "히터", "제습기"
]

# 간단 카테고리 분류(쇼핑/일반)
SHOP_PAT = re.compile(r"(니트|원피스|가디건|스웨터|선풍기|가습기|청소기|전기포트|주전자|보조배터리|전기요|히터|제습기)")

# ===== HTTP =====
def _headers() -> Dict[str, str]:
    return {
        "X-Naver-Client-Id": NAVER_CLIENT_ID,
        "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

def _post_json(url: str, body: dict, timeout=12, retries=2) -> dict:
    """작고 안전한 재시도 래퍼 (429/5xx 시 소폭 대기, 총 시도 retries+1)"""
    for i in range(retries + 1):
        try:
            r = requests.post(url, headers=_headers(), json=body, timeout=timeout)
            # 429는 살짝 쉬었다
            if r.status_code == 429 and i < retries:
                time.sleep(0.8 + 0.3 * i); continue
            r.raise_for_status()
            return r.json()
        except requests.HTTPError as e:
            # 권한/엔드포인트 이슈면 바로 포기
            if r is not None and r.status_code in (400, 401, 403, 404):
                print(f"[NAVER][WARN] {url} -> {r.status_code}: {r.text[:180]}")
                return {}
            if i < retries:
                time.sleep(0.6 + 0.2 * i)
            else:
                print(f"[NAVER][WARN] HTTPError {getattr(r,'status_code',0)}: {e}")
        except Exception as e:
            if i < retries:
                time.sleep(0.4 + 0.2 * i)
            else:
                print(f"[NAVER][WARN] {type(e).__name__}: {e}")
    return {}

# ===== 유틸 =====
def _dedupe(seq: Iterable[str]) -> List[str]:
    seen, out = set(), []
    for s in seq:
        k = s.strip()
        if not k or k in seen: 
            continue
        seen.add(k)
        out.append(k)
    return out

def _normalize_kw(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    # 너무 짧거나 기호/영문숫자만인 것 정리
    if len(s) <= 1: return ""
    return s

def _is_shopping_kw(kw: str) -> bool:
    return bool(SHOP_PAT.search(kw))

def _ban(kw: str) -> bool:
    if not kw: return True
    if len(kw) > 40: return True
    if any(b and (b in kw) for b in BAN_KEYWORDS):
        return True
    # 의미없는 파편/기호 위주
    if not re.search(r"[가-힣]", kw):
        return True
    return False

# ===== 점수 산정 (모멘텀 + 최근성) =====
def _score_from_series(rows: List[Dict]) -> float:
    """
    rows: [{'period':'2025-09-01','ratio':12.3}, ...] (일자 오름차순 가정)
    모멘텀: 최근3일 평균 - 직전3일 평균 (음수면 0)
    최근성: 마지막 값 가중
    """
    if not rows:
        return 0.0
    ratios = [float(x.get("ratio", 0)) for x in rows if x and "ratio" in x]
    if not ratios:
        return 0.0
    n = len(ratios)
    last = ratios[-1]
    # 최근 3 vs 그 전 3
    a = ratios[-3:] if n >= 3 else ratios[-n:]
    b = ratios[-6:-3] if n >= 6 else ratios[:max(0, n-3)]
    m1 = sum(a) / max(1, len(a))
    m0 = sum(b) / max(1, len(b)) if b else 0.0
    momentum = max(0.0, m1 - m0)
    recency = last * 0.3
    return round(momentum + recency, 4)

# ===== DataLab: 검색어 트렌드 =====
def collect_datalab_search(seeds: List[str], days: int = 7) -> Dict[str, float]:
    """
    seed 키워드를 그룹으로 던져 각 키워드의 시계열에서 점수 산정.
    반환: {keyword: score}
    """
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        print("[DATALAB-SEARCH][SKIP] NAVER 키 누락")
        return {}
    end = datetime.now().date()
    start = end - timedelta(days=max(1, days))
    # 그룹 구성: 시드 키워드 하나당 동일 키워드만 묶어 최소 그룹으로
    groups = [{"groupName": s[:20] or "seed", "keywords": [s]} for s in _dedupe(seeds)]
    # DataLab API는 최대 그룹 수 제한(≈20) → 나눠 요청
    chunk_size = 20
    acc: Dict[str, float] = {}
    total_groups = 0
    for i in range(0, len(groups), chunk_size):
        body = {
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": end.strftime("%Y-%m-%d"),
            "timeUnit": "date",
            "keywordGroups": groups[i:i+chunk_size],
            # "device": "", "ages": [], "gender": ""
        }
        url = "https://openapi.naver.com/v1/datalab/search"
        data = _post_json(url, body)
        if not data or "results" not in data:
            continue
        for res in data["results"]:
            kws  = res.get("keywords") or []
            rows = res.get("data") or []
            sc   = _score_from_series(rows)
            for k in kws:
                k = _normalize_kw(k)
                if not _ban(k):
                    acc[k] = max(acc.get(k, 0.0), sc)
        total_groups += len(body["keywordGroups"])
        # 짧게 쉬며 API 부담 완화
        time.sleep(0.15)
    print(f"[DATALAB-SEARCH] groups={total_groups} (momentum keys)")
    return acc

# ===== DataLab: 쇼핑 인사이트 (있으면 사용, 없으면 패스) =====
def collect_datalab_shopping_candidates(days: int = 7) -> Dict[str, float]:
    """
    쇼핑 인사이트에서 '핫' 키워드가 나오는 엔드포인트는 계정/권한에 따라 다를 수 있음.
    접근 실패(404/403/401 등) 시 빈 dict 반환.
    """
    if not (NAVER_CLIENT_ID and NAVER_CLIENT_SECRET):
        return {}
    end = datetime.now().date()
    start = end - timedelta(days=max(1, days))
    # 일부 계정에서 사용 가능한 키워드 엔드포인트 (없을 수 있음)
    url = "https://openapi.naver.com/v1/datalab/shopping/category/keywords"
    body = {
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": end.strftime("%Y-%m-%d"),
        "timeUnit": "date",
        # 카테고리(디폴트: 가전/생활 카테고리들) — 권한 없으면 무시됨
        "category": [{"name": "가전", "param": [50000003]}, {"name": "생활가전", "param": [50000005]}],
        "device": "pc,mobile",
        "gender": "",
        "ages": []
    }
    data = _post_json(url, body)
    acc: Dict[str, float] = {}
    try:
        # 응답 형태가 계정에 따라 다름. 가장 단순한 형태 가정: {'results':[{'keywords':[{'keyword':..., 'ratio':...}, ...]}]}
        for res in data.get("results", []):
            for kv in res.get("keywords", []):
                kw = _normalize_kw(kv.get("keyword") or "")
                if _ban(kw): 
                    continue
                ratio = float(kv.get("ratio") or 0)
                acc[kw] = max(acc.get(kw, 0.0), ratio)
    except Exception:
        # 구조가 다르면 조용히 패스
        return {}
    if acc:
        print(f"[DATALAB-SHOP] candidates={len(acc)}")
    return acc

# ===== 결과 내보내기 =====
def _write_col_csv(path: str, items: List[str]):
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["keyword"])
        for s in items:
            w.writerow([s])

def _rank_and_split(scored: Dict[str, float], k_general: int, k_shop: int) -> Tuple[List[str], List[str]]:
    pairs = sorted(scored.items(), key=lambda x: x[1], reverse=True)
    gens, shops = [], []
    for kw, _ in pairs:
        (shops if _is_shopping_kw(kw) else gens).append(kw)
    return gens[:k_general], shops[:k_shop]

def _pick_golden(shop_list: List[str], gold_n: int) -> List[str]:
    return shop_list[:gold_n]

# ===== 메인 =====
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, default=int(os.getenv("KEYWORDS_K") or 50), help="일반 키워드 최대 개수")
    ap.add_argument("--gold", type=int, default=12, help="황금(일반에서 선별) 개수 — 여기서는 쇼핑이 우선이라 무시됨")
    ap.add_argument("--shop-k", type=int, default=50, help="쇼핑 키워드 최대 개수")
    ap.add_argument("--shop-gold", type=int, default=16, help="황금 쇼핑 키워드 개수")
    ap.add_argument("--days", type=int, default=7, help="DataLab 집계 기간(일)")
    ap.add_argument("--parallel", type=int, default=4, help="미사용(호환용 인자)")
    args = ap.parse_args()

    # 시드 구성: 환경변수 AFF_FALLBACK_KEYWORDS 에 있으면 우선 사용
    env_seeds = [s.strip() for s in (os.getenv("AFF_FALLBACK_KEYWORDS") or "").split(",") if s.strip()]
    seeds = _dedupe(env_seeds + DEFAULT_SEEDS)

    print(f"[KW] collect start (days={args.days}, K_GEN={args.k}, K_SHOP={args.shop_k})")

    # 1) DataLab 검색어 트렌드
    scored = collect_datalab_search(seeds, days=args.days)

    # 2) (옵션) 쇼핑 인사이트 후보 반영 (있으면 가산)
    shop_boost = collect_datalab_shopping_candidates(days=args.days)
    if shop_boost:
        for kw, v in shop_boost.items():
            scored[kw] = max(scored.get(kw, 0.0), v * 0.8)  # 가벼운 가중

    # 필터링 & 정리
    cleaned = {k: v for k, v in scored.items() if not _ban(k)}
    # 최소 확보가 안되면 시드 그대로 보강
    if len(cleaned) < 20:
        for s in seeds:
            if not _ban(s):
                cleaned[s] = max(cleaned.get(s, 0.0), 0.1)

    # 3) 랭크 → 일반/쇼핑 분리
    gen_list, shop_list = _rank_and_split(cleaned, k_general=args.k, k_shop=args.shop_k)

    # 4) 골든(쇼핑 상위) 선별
    golden = _pick_golden(shop_list, args.shop_gold)

    # 5) 파일 저장
    _write_col_csv(OUT_GENERAL,  gen_list)
    _write_col_csv(OUT_SHOPPING, shop_list)
    _write_col_csv(OUT_GOLDEN,   golden)

    # 6) 로그
    def _head(path: str, n=4) -> List[str]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = [r.strip() for r in f.read().splitlines() if r.strip()]
                return rows[1:1+n]
        except Exception:
            return []

    print(f"[GENERAL] {len(gen_list)} → {OUT_GENERAL} (head={_head(OUT_GENERAL)})")
    print(f"[SHOP]    {len(shop_list)} → {OUT_SHOPPING} (gold={len(golden)})")
    if golden:
        print(f"[GOLD]    {golden[:8]} …")

if __name__ == "__main__":
    main()
