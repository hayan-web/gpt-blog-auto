# update_keywords.py
# 키워드 5~20개 수집 + 랜덤 셔플 (경량 버전: 로컬 CSV 유지 보수 위주)

import os, random

CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")

SEED_KEYWORDS = [
    "AI 최신 동향", "스마트폰 신제품", "전기차 배터리 이슈", "건강 관리 팁",
    "여행 준비 체크리스트", "워드프레스 최적화", "쿠팡 인기 상품", "나만의 재테크",
    "간단 레시피 모음", "업무 자동화", "노트북 추천", "가성비 모니터",
    "글쓰기 생산성", "사진 보정 팁", "무료 폰트 모음", "유용한 크롬 확장",
    "윈도우 최적화", "맥북 활용법", "프리랜서 세무", "블로그 수익화",
]

def ensure_csv():
    if not os.path.exists(CSV):
        lines = []
        pool = SEED_KEYWORDS[:]
        random.shuffle(pool)
        for i in range(0, len(pool), 2):
            lines.append(", ".join(pool[i:i+2]))
        with open(CSV, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        print(f"[OK] created {CSV} with seed keywords.")
        return

    with open(CSV, "r", encoding="utf-8") as f:
        rows = [r.strip() for r in f if r.strip()]

    if not rows:
        pool = SEED_KEYWORDS[:]
        random.shuffle(pool)
        with open(CSV, "w", encoding="utf-8") as f:
            for i in range(0, len(pool), 2):
                f.write(", ".join(pool[i:i+2]) + "\n")
        print(f"[OK] refilled {CSV} with seed keywords.")
        return

    # 중복 라인 제거 + 셔플
    rows = list(dict.fromkeys(rows))
    random.shuffle(rows)
    with open(CSV, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(r + "\n")
    print(f"[OK] refreshed {CSV} (shuffled).")

if __name__ == "__main__":
    ensure_csv()
