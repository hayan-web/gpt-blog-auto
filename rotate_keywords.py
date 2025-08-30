# rotate_keywords.py : 실행마다 첫 줄 키워드를 맨 아래로 보냅니다.
import os, io
PATH = os.getenv("KEYWORDS_CSV", "keywords.csv")
def rotate():
    if not os.path.exists(PATH):
        return
    with io.open(PATH, "r", encoding="utf-8") as f:
        lines = [ln.strip() for ln in f if ln.strip()]
    if len(lines) <= 1:
        return
    first = lines.pop(0)
    lines.append(first)
    with io.open(PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")
if __name__ == "__main__":
    rotate()
