# -*- coding: utf-8 -*-
"""
prepare_keyword_lines.py
- keywords.csv 1줄에서 키워드를 읽어 일반/쇼핑 분리
- 출력:
  - keywords_general.csv      (일상용 라인)
  - golden_shopping_keywords.csv  (쿠팡용 상위 N)
  - golden_keywords.csv          (일상용 '황금' 상위 M)
사용:
  python prepare_keyword_lines.py --k 10 --gold 5 --shop-gold 5
"""

import os, re, argparse, csv

DEFAULT_IN = os.getenv("KEYWORDS_CSV") or "keywords.csv"

SHOPPING_WORDS = set("""
추천 리뷰 후기 가격 최저가 세일 특가 쇼핑 쿠폰 할인 핫딜 언박싱 스펙 사용법 베스트
가전 노트북 스마트폰 냉장고 세탁기 에어컨 공기청정기 이어폰 헤드폰 카메라 렌즈 TV 모니터 의자 책상 침대 매트리스
에어프라이어 로봇청소기 가습기 식기세척기 빔프로젝터 유모차 카시트 분유 기저귀 골프 캠핑
""".split())

GENERAL_STOP = set(["브리핑","정리","알아보기","대해 알아보기","해야 할 것","해야할 것","해야할것"])

def read_line_csv(path: str):
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f:
        line=f.readline().strip()
    arr=[x.strip() for x in line.split(",") if x.strip()]
    # 중복 제거(순서 유지)
    seen=set(); out=[]
    for x in arr:
        if x not in seen:
            seen.add(x); out.append(x)
    return out

def is_shopping_like(kw: str) -> bool:
    if any(w in kw for w in SHOPPING_WORDS): return True
    # 모델명/제품형
    if re.search(r"[A-Za-z]+[\-\s]?\d{2,}", kw): return True
    # 상업 신호
    if re.search(r"(추천|리뷰|최저가|세일|특가|할인|구매|가격)", kw): return True
    return False

def info_score(kw: str) -> float:
    # 일반 키워드 '정보성' 점수
    s = 0.0
    L = len(kw)
    s += min(L, 20)/20.0            # 적당한 길이 가점
    s += 0.3 if not any(w in kw for w in GENERAL_STOP) else -0.5
    s += -0.7 if is_shopping_like(kw) else 0.0
    s += 0.2 if re.search(r"[가-힣]{2,}", kw) else 0.0
    return s

def shop_score(kw: str) -> float:
    # 쇼핑 키워드 점수
    s = 0.0
    L = len(kw)
    s += 0.6 if is_shopping_like(kw) else 0.0
    s += 0.2 if any(w in kw for w in SHOPPING_WORDS) else 0.0
    s += min(L, 18)/18.0
    return s

def write_csv_keywords(path: str, keywords):
    # header=keyword 1열 CSV
    with open(path,"w",encoding="utf-8",newline="") as f:
        w=csv.writer(f)
        w.writerow(["keyword"])
        for k in keywords:
            w.writerow([k])

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--input", default=DEFAULT_IN)
    ap.add_argument("--k", type=int, default=10, help="일상 라인 길이")
    ap.add_argument("--gold", type=int, default=5, help="일상 황금 개수")
    ap.add_argument("--shop-gold", type=int, default=5, help="쇼핑 황금 개수")
    args=ap.parse_args()

    base = read_line_csv(args.input)
    if not base:
        print(f"[WARN] no keywords in {args.input}")
        return 0

    general = [kw for kw in base if not is_shopping_like(kw)]
    shopping = [kw for kw in base if is_shopping_like(kw)]

    # 일상 라인: 정보성 높은 순 → 상위 k
    general_ranked = sorted(general, key=info_score, reverse=True)[:args.k]
    # 황금(일반/쇼핑): 각 점수 상위
    golden_general = sorted(general, key=info_score, reverse=True)[:args.gold]
    golden_shop = sorted(shopping, key=shop_score, reverse=True)[:args.shop_gold]

    write_csv_keywords("keywords_general.csv", general_ranked)
    write_csv_keywords("golden_keywords.csv", golden_general)
    write_csv_keywords("golden_shopping_keywords.csv", golden_shop)

    print(f"[OK] wrote keywords_general.csv ({len(general_ranked)})")
    print(f"[OK] wrote golden_keywords.csv ({len(golden_general)})")
    print(f"[OK] wrote golden_shopping_keywords.csv ({len(golden_shop)})")
    return 0

if __name__=="__main__":
    raise SystemExit(main())
