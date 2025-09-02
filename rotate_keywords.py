# -*- coding: utf-8 -*-
"""
rotate_keywords.py
- keywords.csv 의 첫 줄(방금 사용한 키워드)을 파일 맨 아래로 이동
- 안전 처리: 파일 부재, 빈 파일, 1개만 있는 경우, 공백 라인 정리
- 로깅 추가
"""

import os
import csv
import logging
from dotenv import load_dotenv

load_dotenv()

KEYWORDS_CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("rotate_keywords")


def read_keywords(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    rows: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            kw = (row[0] or "").strip()
            if kw:
                rows.append(kw)
    return rows


def write_keywords(path: str, items: list[str]) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for it in items:
            w.writerow([it])


def main():
    kws = read_keywords(KEYWORDS_CSV)
    if not kws:
        log.warning("keywords.csv is empty or missing. Nothing to rotate.")
        return

    if len(kws) == 1:
        # 하나뿐이면 그대로 유지
        write_keywords(KEYWORDS_CSV, kws)
        log.info("Single keyword only. Rotation skipped.")
        return

    first = kws.pop(0)
    kws.append(first)
    write_keywords(KEYWORDS_CSV, kws)
    log.info(f"✅ Rotated. New first: {kws[0]} (moved '{first}' to bottom)")


if __name__ == "__main__":
    main()
