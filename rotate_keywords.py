# rotate_keywords.py
# - 지원 모드
#   1) 줄 단위 목록 (여러 줄)  → 기본: 첫 줄을 맨 아래로 이동
#   2) 한 줄 콤마 리스트(예: "키워드1, 키워드2, 키워드3") → 기본: 첫 토큰을 맨 뒤로 이동
# - 옵션
#   --used "키워드"   : 해당 키워드를 찾아 맨 아래(또는 맨 뒤)로 이동 (없으면 기본 동작)
#   --count N         : 맨 앞에서 N개를 순차 이동 (기본 1). used 미지정일 때만 적용
#   --delimiter ";"   : 콤마 리스트 모드에서 구분자 지정 (기본 ",")
#   --mode auto|lines|list : 강제 모드 지정(기본 auto)
#   --dry-run         : 파일을 수정하지 않고 결과만 출력
#   --backup          : 수정 전 백업 파일 생성(예: keywords.csv.bak)
#
# Env:
#   KEYWORDS_CSV (기본 "keywords.csv")
#   USED_KEYWORD (선택: --used 미지정 시 사용)

import os
import argparse
from typing import List

CSV_PATH = os.getenv("KEYWORDS_CSV", "keywords.csv")

def read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

def write_text(path: str, text: str, backup: bool = False):
    if backup and os.path.exists(path):
        import shutil
        shutil.copy2(path, path + ".bak")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)

def detect_mode(text: str) -> str:
    # 여러 줄이면 lines, 한 줄인데 구분자(,;| 등)가 있으면 list, 아니면 lines 로 간주
    if "\n" in text.strip():
        return "lines"
    # 한 줄짜리인 경우
    return "list" if ("," in text or ";" in text or "|" in text) else "lines"

def rotate_list(items: List[str], count: int = 1) -> List[str]:
    if not items:
        return items
    n = len(items)
    k = count % n
    return items[k:] + items[:k] if k else items

def move_used_to_end(items: List[str], used: str) -> List[str]:
    if not used:
        return items
    used = used.strip()
    if not used:
        return items
    if used not in items:
        return items
    out = [x for x in items if x != used]
    out.append(used)
    return out

def rotate_lines_mode(text: str, used: str = "", count: int = 1) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 1:
        return "\n".join(lines) + ("\n" if lines else "")
    if used:
        lines = move_used_to_end(lines, used)
    else:
        lines = rotate_list(lines, count=count)
    return "\n".join(lines) + "\n"

def rotate_list_mode(text: str, delimiter: str = ",", used: str = "", count: int = 1) -> str:
    # 한 줄에서 delimiter 로 분리
    items = [x.strip() for x in text.strip().split(delimiter) if x.strip()]
    if len(items) <= 1:
        return delimiter.join(items)
    if used:
        items = move_used_to_end(items, used)
    else:
        items = rotate_list(items, count=count)
    return delimiter.join(items)

def main():
    ap = argparse.ArgumentParser(description="Rotate keywords in keywords.csv")
    ap.add_argument("--used", default=os.getenv("USED_KEYWORD", ""), help="이번에 사용한 키워드(우선 이동)")
    ap.add_argument("--count", type=int, default=1, help="맨 앞에서 N개 이동 (used 미지정 시 적용)")
    ap.add_argument("--delimiter", default=",", help="리스트 모드 구분자 (기본 ,)")
    ap.add_argument("--mode", choices=["auto","lines","list"], default="auto", help="강제 모드 지정 (기본 auto)")
    ap.add_argument("--path", default=CSV_PATH, help="키워드 파일 경로 (기본 KEYWORDS_CSV)")
    ap.add_argument("--dry-run", action="store_true", help="파일을 수정하지 않고 결과만 출력")
    ap.add_argument("--backup", action="store_true", help="수정 전 .bak 백업 생성")
    args = ap.parse_args()

    if not os.path.exists(args.path):
        print(f"[warn] {args.path} not found; skip rotate.")
        return 0

    raw = read_text(args.path)
    mode = args.mode if args.mode != "auto" else detect_mode(raw)

    if mode == "lines":
        new_text = rotate_lines_mode(raw, used=args.used, count=args.count)
    else:  # list
        new_one_line = rotate_list_mode(raw, delimiter=args.delimiter, used=args.used, count=args.count)
        new_text = new_one_line if new_one_line.endswith("\n") else new_one_line + "\n"

    if args.dry_run:
        print("[dry-run] mode:", mode)
        print(new_text, end="")
        return 0

    write_text(args.path, new_text, backup=args.backup)
    moved = f"(used='{args.used}')" if args.used else f"(count={args.count})"
    print(f"[OK] rotated {args.path} mode={mode} {moved}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
