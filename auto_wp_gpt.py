# auto_wp_gpt.py
# 일상글 2건 자동 예약(10:00, 17:00 KST) + 슬롯 충돌 시 최대 7일 이월
# - WordPress date_gmt(UTC) 기준 ±2분 충돌 감지 (context=edit, status=future 대량 조회 후 직접 비교)
# - 키워드 파이프라인: keywords_general.csv 우선, 부족 시 keywords.csv 보충
# - 성공 시 소스 CSV에서 키워드 제거, .usage/used_general.txt에 "YYYY-MM-DD,keyword" 기록
# - 이미지 전부 제거 (텍스트 전용)
# - 제목에 '예약' 같은 접두어 삽입 없음
# - 실행 예: python auto_wp_gpt.py --mode=two-posts

from __future__ import annotations
import argparse
import csv
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple

import requests

# --- 환경변수 로딩 -----------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

WP_URL = os.getenv("WP_URL", "").rstrip("/")
WP_USER = os.getenv("WP_USER", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
WP_TLS_VERIFY = os.getenv("WP_TLS_VERIFY", "1") not in ("0", "false", "False", "FALSE")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_MODEL_LONG = os.getenv("OPENAI_MODEL_LONG", OPENAI_MODEL)
MAX_TOKENS_BODY = int(os.getenv("MAX_TOKENS_BODY", "1400"))
POST_STATUS = os.getenv("POST_STATUS", "future")  # 'future' 권장 (예약발행)

# 사용 기록 보관/차단
GENERAL_USED_BLOCK_DAYS = int(os.getenv("GENERAL_USED_BLOCK_DAYS", "30"))

# 경로 상수
CSV_GENERAL_MAIN = "keywords_general.csv"
CSV_GENERAL_FALLBACK = "keywords.csv"
USAGE_DIR = ".usage"
USAGE_GENERAL = os.path.join(USAGE_DIR, "used_general.txt")

# 요청 공통 헤더
REQ_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json",
}

# --- 시간/타임존 유틸 -------------------------------------------------------
KST = timezone(timedelta(hours=9))

def _now_kst() -> datetime:
    return datetime.now(tz=KST)

def _ensure_dirs():
    os.makedirs(USAGE_DIR, exist_ok=True)

# --- WP 예약 충돌 감지(쿠팡글과 동일 방식) -----------------------------------
def _wp_future_exists_around(when_gmt_dt: datetime, tol_min: int = 2) -> bool:
    """
    워드프레스 예약글(status=future)을 넉넉히 조회한 뒤,
    UTC date_gmt를 기준으로 ±tol_min 분 내에 충돌이 있는지 직접 판단.
    after/before 파라미터를 사용하지 않아 타임존 혼선을 제거.
    """
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    try:
        r = requests.get(
            url,
            params={
                "status": "future",
                "per_page": 100,
                "orderby": "date",
                "order": "asc",
                "context": "edit",  # 인증 필요
            },
            headers=REQ_HEADERS,
            auth=(WP_USER, WP_APP_PASSWORD),
            verify=WP_TLS_VERIFY,
            timeout=20,
        )
        r.raise_for_status()
        items = r.json()
    except Exception as e:
        print(f"[WP][WARN] future list fetch failed: {type(e).__name__}: {e}")
        return False  # 조회 실패 시 보수적으로 '충돌 없음' 처리

    tgt = when_gmt_dt
    if tgt.tzinfo is None:
        tgt = tgt.replace(tzinfo=timezone.utc)
    else:
        tgt = tgt.astimezone(timezone.utc)

    delta = timedelta(minutes=max(1, int(tol_min)))
    lo, hi = tgt - delta, tgt + delta

    for it in items:
        dstr = (it.get("date_gmt") or "").strip()  # "YYYY-MM-DDTHH:MM:SS"
        if not dstr:
            continue
        try:
            dt = datetime.fromisoformat(dstr)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if lo <= dt <= hi:
            return True
    return False

def _slot_or_next_day(h: int, m: int = 0) -> str:
    """
    Asia/Seoul 기준 (h:m) 슬롯을 우선 시도.
    - 현재 시각이 지나있으면 다음날로 기본 이월
    - 동일 슬롯이 이미 예약돼 있으면 1일씩 밀면서 빈 날을 찾음(최대 7일)
    반환: WP date_gmt(UTC) 문자열 "YYYY-MM-DDTHH:MM:SS"
    """
    now_kst = _now_kst()
    target_kst = now_kst.replace(hour=h, minute=m, second=0, microsecond=0)
    if target_kst <= now_kst:
        target_kst += timedelta(days=1)

    for _ in range(7):
        when_gmt_dt = target_kst.astimezone(timezone.utc)
        if _wp_future_exists_around(when_gmt_dt, tol_min=2):
            print(f"[SLOT] conflict at {when_gmt_dt.strftime('%Y-%m-%dT%H:%M:%S')}Z -> push +1d")
            target_kst += timedelta(days=1)
            continue
        break

    final = target_kst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[SLOT] scheduled UTC = {final}")
    return final

# --- 키워드 파이프라인 -------------------------------------------------------
def _read_usage_block_dict(path: str, block_days: int) -> dict:
    """
    used_general.txt → {"keyword": last_used_date(YYYY-MM-DD)}
    block_days 이내 사용한 키워드는 1차 제외 용도로 사용
    """
    d = {}
    if not os.path.isfile(path):
        return d
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = [p.strip() for p in line.split(",", 1)]
                if len(parts) != 2:
                    continue
                dt_str, kw = parts
                d[kw] = dt_str
    except Exception:
        pass
    # 블록 기간 필터링
    keep = {}
    today = _now_kst().date()
    for kw, dstr in d.items():
        try:
            used_dt = datetime.strptime(dstr, "%Y-%m-%d").date()
            if (today - used_dt).days <= block_days:
                keep[kw] = dstr
        except Exception:
            # 파싱 실패 시 보수적으로 제외 유지
            keep[kw] = dstr
    return keep

def _load_csv_list(path: str) -> List[str]:
    if not os.path.isfile(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as f:
        rdr = csv.reader(f)
        for row in rdr:
            if not row:
                continue
            # 한 줄 1키워드 형식과, 여러열 중 첫번째 열을 모두 허용
            kw = (row[0] or "").strip()
            if kw:
                out.append(kw)
    return out

def _save_csv_list(path: str, items: List[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        for kw in items:
            w.writerow([kw])

def _pick_keywords_for_two_posts() -> Tuple[str, str]:
    """
    1) keywords_general.csv에서 2개 선발(최근 사용 block_days 이내 사용 키워드는 1차 제외)
    2) 부족하면 keywords.csv에서 보충
    3) 그래도 부족하면 남은 것 중 순서대로
    """
    usage_block = _read_usage_block_dict(USAGE_GENERAL, GENERAL_USED_BLOCK_DAYS)

    gen_main = _load_csv_list(CSV_GENERAL_MAIN)
    gen_fb = _load_csv_list(CSV_GENERAL_FALLBACK)

    # 1차 후보(최근 사용 안 한 것 우선)
    main_filtered = [k for k in gen_main if k not in usage_block]
    selected: List[str] = []

    for src in (main_filtered, gen_main, gen_fb):
        for k in src:
            if k not in selected:
                selected.append(k)
            if len(selected) >= 2:
                break
        if len(selected) >= 2:
            break

    # 보정: 혹시 2개 미만이면 중복 허용해서라도 채움
    while len(selected) < 2:
        if gen_main:
            selected.append(gen_main[0])
        elif gen_fb:
            selected.append(gen_fb[0])
        else:
            selected.append(f"일상 아카이브 {_now_kst().strftime('%Y%m%d%H%M')}")

    return selected[0], selected[1]

def _remove_used_keyword(kw: str) -> None:
    """소스 CSV들에서 해당 키워드를 한 번만 제거"""
    changed = False
    for path in (CSV_GENERAL_MAIN, CSV_GENERAL_FALLBACK):
        lst = _load_csv_list(path)
        if kw in lst:
            lst.remove(kw)
            _save_csv_list(path, lst)
            changed = True
            break
    if not changed:
        # 소스에 없었을 수도 있으므로 조용히 통과
        pass

def _append_usage(kw: str) -> None:
    _ensure_dirs()
    today = _now_kst().strftime("%Y-%m-%d")
    with open(USAGE_GENERAL, "a", encoding="utf-8") as f:
        f.write(f"{today},{kw}\n")

# --- 본문/제목 생성 (이미지 없음) -------------------------------------------
def _make_title_from_keyword(kw: str) -> str:
    """
    '예약' 같은 접두어 넣지 않음. 키워드 기반 자연스러운 제목 생성.
    (간단 규칙; 실제 환경에서는 OpenAI 호출로 더 자연스러운 문장 생성 권장)
    """
    return f"{kw}: 요즘 사람들이 진짜 궁금해하는 포인트 정리"

def _openai_generate_body(kw: str) -> str:
    """
    OpenAI 호출 없이도 동작하도록 기본 텍스트 작성.
    OPENAI_API_KEY가 세팅돼 있으면 OpenAI를 사용해 더 풍부하게 생성.
    """
    base = [
        f"## {kw} 한눈에 보기",
        "",
        "- 핵심 포인트를 빠르게 이해할 수 있도록 간단히 정리했습니다.",
        "- 최신 사례와 실전 팁을 바탕으로 **바로 적용 가능한 포인트**만 담았습니다.",
        "",
        "## 핵심 요약",
        "1) 왜 중요한가?",
        "2) 무엇부터 해야 하는가?",
        "3) 실패를 줄이는 체크리스트",
        "",
        "## 디테일 가이드",
        "- 상황별로 선택해야 할 옵션",
        "- 실제로 써보니 좋았던 방법",
        "- 흔한 함정과 피하는 법",
        "",
        "## 마무리",
        "핵심만 빠르게 실행해보세요. 작은 반복이 큰 차이를 만듭니다.",
    ]
    body = "\n".join(base)

    if not OPENAI_API_KEY:
        return body

    # OpenAI 사용 (옵션)
    try:
        # 최신 Python SDK 기준
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        sys_prompt = (
            "너는 한국어 글쓰기 전문가야. 아래 키워드를 중심으로 블로그 '일상/정보' 글을 작성해줘. "
            "이미지는 쓰지 말고 마크다운만 사용하며, 과장 없이 실용적 요약과 체크리스트를 포함해. "
            "제목에 '예약' 같은 접두어를 절대 넣지 마."
        )
        user_prompt = (
            f"키워드: {kw}\n"
            f"- 섹션: 한눈에 보기/핵심 요약/디테일 가이드/마무리\n"
            f"- 톤: 친절하고 간결, 실전 팁 중심\n"
            f"- 마크다운 소제목 사용, 글자 수 900~1,400자"
        )

        resp = client.chat.completions.create(
            model=OPENAI_MODEL_LONG or OPENAI_MODEL,
            messages=[
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
            max_tokens=MAX_TOKENS_BODY,
        )
        content = resp.choices[0].message.content.strip()
        if content:
            return content
    except Exception as e:
        print(f"[OPENAI][WARN] fallback to base body: {type(e).__name__}: {e}")

    return body

# --- 워드프레스 포스트 생성 ---------------------------------------------------
def _wp_create_post(date_gmt_str: str, title: str, content_md: str) -> dict:
    """
    워드프레스 글 생성. 상태는 POST_STATUS (기본 future).
    date_gmt는 'YYYY-MM-DDTHH:MM:SS' (UTC) 형식.
    """
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    payload = {
        "title": title,
        "content": content_md,
        "status": POST_STATUS,
        "date_gmt": date_gmt_str,
        # 필요 시 categories, tags 등 확장 가능
    }
    r = requests.post(
        url,
        json=payload,
        headers=REQ_HEADERS,
        auth=(WP_USER, WP_APP_PASSWORD),
        verify=WP_TLS_VERIFY,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()

# --- 실행 플로우 -------------------------------------------------------------
def _schedule_one(h: int, m: int, kw: str) -> dict:
    date_gmt = _slot_or_next_day(h, m)
    title = _make_title_from_keyword(kw)
    body = _openai_generate_body(kw)
    res = _wp_create_post(date_gmt, title, body)
    # 성공 시 회전/기록
    _remove_used_keyword(kw)
    _append_usage(kw)
    return {
        "post_id": res.get("id"),
        "link": res.get("link"),
        "status": res.get("status"),
        "date_gmt": res.get("date_gmt"),
        "title": res.get("title", {}).get("rendered"),
        "keyword": kw,
    }

def run_two_posts():
    kw1, kw2 = _pick_keywords_for_two_posts()
    out1 = _schedule_one(10, 0, kw1)   # 10:00 KST
    print(out1)
    out2 = _schedule_one(17, 0, kw2)   # 17:00 KST
    print(out2)

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="two-posts", help="two-posts (default)")
    return ap.parse_args()

def _check_env():
    missing = []
    for k, v in {
        "WP_URL": WP_URL,
        "WP_USER": WP_USER,
        "WP_APP_PASSWORD": WP_APP_PASSWORD,
    }.items():
        if not v:
            missing.append(k)
    if missing:
        print(f"[FATAL] missing env: {', '.join(missing)}")
        sys.exit(2)

def main():
    _check_env()
    args = parse_args()
    mode = args.mode.lower().strip()

    if mode == "two-posts":
        run_two_posts()
    else:
        print(f"[WARN] Unknown mode: {mode}. Running 'two-posts' by default.")
        run_two_posts()

if __name__ == "__main__":
    main()
