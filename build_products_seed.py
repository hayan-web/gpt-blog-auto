# -*- coding: utf-8 -*-
"""
build_products_seed.py — 자동으로 products_seed.csv 채우기
- keywords.csv 의 상위 키워드 1~2개를 사용
- Coupang Partners "products/search"로 최대 10개씩 가져와서 CSV로 저장
- ENV:
    COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY  (필수)
    PRODUCTS_SEED_CSV=products_seed.csv     (기본값)
    MAX_PER_KEYWORD=8
    USE_KEYWORDS_N=2
"""
import os, csv, sys
from typing import List, Dict
from coupang_search import search_products

CSV_PATH = os.getenv("PRODUCTS_SEED_CSV","products_seed.csv")
MAX_PER_KEYWORD = int(os.getenv("MAX_PER_KEYWORD","8"))
USE_KEYWORDS_N = int(os.getenv("USE_KEYWORDS_N","2"))
ACCESS = os.getenv("COUPANG_ACCESS_KEY","").strip()
SECRET = os.getenv("COUPANG_SECRET_KEY","").strip()

def read_keywords(path="keywords.csv", n=2) -> List[str]:
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        line = f.readline().strip()
    parts = [x.strip() for x in line.split(",") if x.strip()]
    return parts[:max(1,n)]

def build_rows(keyword: str) -> List[Dict[str,str]]:
    items = search_products(keyword, ACCESS, SECRET, limit=MAX_PER_KEYWORD, sort="salesVolume")
    rows = []
    for it in items[:MAX_PER_KEYWORD]:
        rows.append({
            "keyword": keyword,
            "product_name": it["productName"],
            "raw_url": it["productUrl"],  # 딥링크는 affiliate_post 단계에서 처리
            "pros": "",
            "cons": ""
        })
    return rows

def main()->int:
    if not (ACCESS and SECRET):
        print("[build_products_seed] SKIP: COUPANG_ACCESS_KEY/SECRET_KEY 가 비어있음")
        return 0
    kws = read_keywords("keywords.csv", n=USE_KEYWORDS_N)
    if not kws:
        print("[build_products_seed] SKIP: keywords.csv 가 비어있음")
        return 0
    all_rows: List[Dict[str,str]] = []
    for kw in kws:
        try:
            rows = build_rows(kw)
            print(f"[build_products_seed] '{kw}' -> {len(rows)}개")
            all_rows.extend(rows)
        except Exception as e:
            print(f"[build_products_seed] WARN: '{kw}' 실패: {e}")
    if not all_rows:
        print("[build_products_seed] 생성된 행이 없습니다.")
        return 0
    # 헤더 쓰기
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["keyword","product_name","raw_url","pros","cons"])
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"[build_products_seed] 저장: {CSV_PATH} ({len(all_rows)}행)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
