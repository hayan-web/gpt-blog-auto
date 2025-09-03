# rotate_keywords.py
# 사용한 키워드를 CSV 맨 아래로 이동 (1줄 단위)

import os

CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")

def rotate_once():
    if not os.path.exists(CSV):
        print(f"[warn] {CSV} not found; skip rotate.")
        return
    with open(CSV, "r", encoding="utf-8") as f:
        rows = [line.rstrip("\n") for line in f if line.strip()]
    if len(rows) <= 1:
        print("[info] nothing to rotate")
        return
    first = rows.pop(0)
    rows.append(first)
    with open(CSV, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(r + "\n")
    print("[OK] rotated keywords: moved first line to bottom.")

if __name__ == "__main__":
    rotate_once()
