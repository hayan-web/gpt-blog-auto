# -*- coding: utf-8 -*-
"""
build_products_seed.py
- golden_shopping_keywords.csv의 상위 키워드로 씨드 CSV(products_seed.csv) 생성
- REQUIRE_COUPANG_API=1 이고 키/채널 설정이 있으면 coupang_api.deeplink_for_query()로 딥링크 생성
- 실패 시 Coupang 검색 URL로 폴백하여 행을 채워, 이후 단계가 끊기지 않게 보장
- 출력 스키마: product_name,raw_url,pros,cons,keyword,title,url,image
"""

from __future__ import annotations
import os, csv, sys, html, traceback
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

# ===== ENV =====
PRODUCTS_SEED_CSV = os.getenv("PRODUCTS_SEED_CSV", "products_seed.csv")
P_GOLD = "golden_shopping_keywords.csv"

REQUIRE_COUPANG_API = (os.getenv("REQUIRE_COUPANG_API") or "0").strip().lower() in ("1", "true", "yes", "on")
COUNT = int(os.getenv("SEED_COUNT") or "12")  # 한 번에 뽑을 키워드 수 (원하면 .env에서 조정)
COUPANG_DEBUG = (os.getenv("COUPANG_DEBUG") or "0").strip().lower() in ("1", "true", "yes", "on")

# ===== Optional import (API 사용 조건일 때만) =====
DEEPLINK_AVAILABLE = False
if REQUIRE_COUPANG_API:
    try:
        from coupang_api import deeplink_for_query  # 우리가 만든 모듈
        DEEPLINK_AVAILABLE = True
    except Exception:
        DEEPLINK_AVAILABLE = False

def _read_col_csv(path: str) -> List[str]:
    if not os.path.exists(path):
        return []
    out: List[str] = []
    with open(path, "r", encoding="utf-8", newline="") as f:
        rd = csv.reader(f)
        for i, row in enumerate(rd):
            if not row:
                continue
            if i == 0 and row[0].strip().lower() in ("keyword", "title"):
                continue
            s = row[0].strip()
            if s:
                out.append(s)
    return out

def _coupang_search_url(q: str) -> str:
    from urllib.parse import quote_plus
    return f"https://search.shopping.coupang.com/search?component=&q={quote_plus(q)}&channel=rel"

def _safe_deeplink(q: str) -> str:
    """
    딥링크를 우선 시도하고, 실패 시 검색 URL 반환.
    """
    if REQUIRE_COUPANG_API and DEEPLINK_AVAILABLE:
        try:
            return deeplink_for_query(q)
        except Exception as e:
            if COUPANG_DEBUG:
                print(f"[build_products_seed] deeplink error: {type(e).__name__}: {e}", file=sys.stderr)
                traceback.print_exc()
    return _coupang_search_url(q)

def _ensure_header(path: str):
    # 항상 헤더부터 씀(덮어쓰기). 다운스트림에서 헤더를 기대하므로 명시적으로 재작성.
    with open(path, "w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["product_name", "raw_url", "pros", "cons", "keyword", "title", "url", "image"])

def _append_row(path: str, row: List[str]):
    with open(path, "a", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(row)

def main():
    pool = _read_col_csv(P_GOLD)
    if not pool:
        print("[build_products_seed] WARN: golden_shopping_keywords.csv 비어 있음(헤더만 있거나 파일 없음)")
        _ensure_header(PRODUCTS_SEED_CSV)
        print("[build_products_seed] 생성된 행이 없습니다.")
        return

    # 뽑을 수 만큼 슬라이스(맨 위부터)
    picks = pool[:max(1, COUNT)]

    _ensure_header(PRODUCTS_SEED_CSV)
    inserted = 0

    for kw in picks:
        # 우선 딥링크 → 실패 시 검색 URL
        url = _safe_deeplink(kw)

        # seed CSV 스키마에 맞춰 채우기
        product_name = kw
        raw_url = _coupang_search_url(kw)  # 원본 검색 URL(딥링크 전)
        pros = ""   # 나중에 풍부화 가능
        cons = ""   # 나중에 풍부화 가능
        title = kw  # 기본 타이틀(본문 단계에서 대체됨)
        image = ""  # 이미지 선택 로직이 없으므로 빈칸

        _append_row(PRODUCTS_SEED_CSV, [
            product_name, raw_url, pros, cons, kw, title, url, image
        ])
        inserted += 1

    print(f"[build_products_seed] OK: {inserted} rows -> {PRODUCTS_SEED_CSV}")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[build_products_seed] FATAL: {type(e).__name__}: {e}", file=sys.stderr)
        if os.getenv("CI"):
            # CI에서는 non-zero로 실패해도 워크플로 전체를 끊지 않게 상위 스텝에서 || true 처리 권장
            sys.exit(1)
        raise
