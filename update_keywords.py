# update_keywords.py
# 키워드 5~20개 수집 + 랜덤 셔플 (경량 버전: 로컬 CSV 유지 보수 위주)

import os, random

CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")

SEED_KEYWORDS = [
    "AI 최신 동향", "스마트폰 신제품", "전기차 배터리 이슈", "건강 관리 팁",
    "여행 준비 체크리스트", "워드프레스 최적화", "쿠팡 인기 상품", "나만의 재테크",
    "간단 레시피 모음", "업무 자동화", "노트북 추천", "가성비 모니터",
]

def ensure_csv():
    if not os.path.exists(CSV):
        # 기본 키워드 파일 생성
        lines = []
        random.shuffle(SEED_KEYWORDS)
        for i in range(0, len(SEED_KEYWORDS), 2):
            lines.append(", ".join(SEED_KEYWORDS[i:i+2]))
        with open(CSV, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
        print(f"[OK] created {CSV} with seed keywords.")
        return

    # 파일 존재: 맨 윗줄이 비어있으면 보충
    with open(CSV, "r", encoding="utf-8") as f:
        rows = [r.strip() for r in f if r.strip()]
    if not rows:
        random.shuffle(SEED_KEYWORDS)
        with open(CSV, "w", encoding="utf-8") as f:
            for i in range(0, len(SEED_KEYWORDS), 2):
                f.write(", ".join(SEED_KEYWORDS[i:i+2]) + "\n")
        print(f"[OK] refilled {CSV} with seed keywords.")
        return

    # 상단 키워드가 너무 유사하면 간단 셔플(가벼운 중복 완화)
    rows = list(dict.fromkeys(rows))  # 중복 라인 제거
    random.shuffle(rows)
    with open(CSV, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(r + "\n")
    print(f"[OK] refreshed {CSV} (shuffled).")

if __name__ == "__main__":
    ensure_csv()
