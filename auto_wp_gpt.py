# auto_wp_gpt.py
# 일상글 2건 자동 예약(10:00, 17:00 KST) + 슬롯 충돌 시 최대 7일 이월
# 본문은 마크다운이 아닌 HTML로 전송하여 워드프레스 기본 스타일 유지
# (h2/h3/ul/ol/blockquote/table 표준 태그 사용)
# 폴백 키워드("일상 아카이브 …")는 제목/본문/프롬프트에 노출하지 않음

from __future__ import annotations
import argparse, csv, os, sys, requests
from datetime import datetime, timedelta, timezone
from typing import List, Tuple

# --- env ----------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

WP_URL = os.getenv("WP_URL", "").rstrip("/")
WP_USER = os.getenv("WP_USER", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
WP_TLS_VERIFY = os.getenv("WP_TLS_VERIFY", "1") not in ("0","false","False","FALSE")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_MODEL_LONG = os.getenv("OPENAI_MODEL_LONG", OPENAI_MODEL)
MAX_TOKENS_BODY = int(os.getenv("MAX_TOKENS_BODY", "1400"))
POST_STATUS = os.getenv("POST_STATUS", "future")

GENERAL_USED_BLOCK_DAYS = int(os.getenv("GENERAL_USED_BLOCK_DAYS", "30"))

CSV_GENERAL_MAIN = "keywords_general.csv"
CSV_GENERAL_FALLBACK = "keywords.csv"
USAGE_DIR = ".usage"
USAGE_GENERAL = os.path.join(USAGE_DIR, "used_general.txt")

REQ_HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
    "Accept": "application/json",
}

KST = timezone(timedelta(hours=9))
def _now_kst(): return datetime.now(tz=KST)
def _ensure_dirs(): os.makedirs(USAGE_DIR, exist_ok=True)

# --- WP future conflict (UTC date_gmt ±2m) ------------------
def _wp_future_exists_around(when_gmt_dt: datetime, tol_min: int = 2) -> bool:
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    try:
        r = requests.get(
            url,
            params={"status":"future","per_page":100,"orderby":"date","order":"asc","context":"edit"},
            headers=REQ_HEADERS, auth=(WP_USER, WP_APP_PASSWORD),
            verify=WP_TLS_VERIFY, timeout=20,
        )
        r.raise_for_status()
        items = r.json()
    except Exception as e:
        print(f"[WP][WARN] future list fetch failed: {type(e).__name__}: {e}")
        return False
    tgt = when_gmt_dt.astimezone(timezone.utc)
    delta = timedelta(minutes=max(1,int(tol_min)))
    lo, hi = tgt - delta, tgt + delta
    for it in items:
        dstr = (it.get("date_gmt") or "").strip()
        if not dstr: continue
        try:
            dt = datetime.fromisoformat(dstr)
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if lo <= dt <= hi:
            return True
    return False

def _slot_or_next_day(h:int,m:int=0)->str:
    now_kst = _now_kst()
    target_kst = now_kst.replace(hour=h, minute=m, second=0, microsecond=0)
    if target_kst <= now_kst: target_kst += timedelta(days=1)
    for _ in range(7):
        when_gmt_dt = target_kst.astimezone(timezone.utc)
        if _wp_future_exists_around(when_gmt_dt, tol_min=2):
            print(f"[SLOT] conflict at {when_gmt_dt.strftime('%Y-%m-%dT%H:%M:%S')}Z -> +1d")
            target_kst += timedelta(days=1); continue
        break
    final = target_kst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[SLOT] scheduled UTC = {final}")
    return final

# --- Keywords -----------------------------------------------
def _read_usage_block_dict(path: str, block_days: int) -> dict:
    d = {}
    if os.path.isfile(path):
        with open(path,"r",encoding="utf-8") as f:
            for line in f:
                line=line.strip()
                if not line or line.startswith("#"): continue
                parts=[p.strip() for p in line.split(",",1)]
                if len(parts)==2: d[parts[1]]=parts[0]
    keep={}
    today=_now_kst().date()
    for kw,dstr in d.items():
        try:
            used=datetime.strptime(dstr,"%Y-%m-%d").date()
            if (today-used).days<=block_days: keep[kw]=dstr
        except Exception:
            keep[kw]=dstr
    return keep

def _load_csv_list(path:str)->List[str]:
    if not os.path.isfile(path): return []
    out=[]
    with open(path,"r",encoding="utf-8") as f:
        rdr=csv.reader(f)
        for row in rdr:
            if not row: continue
            kw=(row[0] or "").strip()
            if kw: out.append(kw)
    return out

def _save_csv_list(path:str,items:List[str])->None:
    with open(path,"w",encoding="utf-8",newline="") as f:
        w=csv.writer(f)
        for kw in items: w.writerow([kw])

def _pick_keywords_for_two_posts()->Tuple[str,str]:
    usage_block=_read_usage_block_dict(USAGE_GENERAL, GENERAL_USED_BLOCK_DAYS)
    main=_load_csv_list(CSV_GENERAL_MAIN)
    fb=_load_csv_list(CSV_GENERAL_FALLBACK)
    main_filtered=[k for k in main if k not in usage_block]
    selected=[]
    for src in (main_filtered, main, fb):
        for k in src:
            if k not in selected: selected.append(k)
            if len(selected)>=2: break
        if len(selected)>=2: break
    while len(selected)<2:
        if main: selected.append(main[0])
        elif fb: selected.append(fb[0])
        else: selected.append(f"일상 아카이브 {_now_kst().strftime('%Y%m%d%H%M')}")
    return selected[0], selected[1]

def _remove_used_keyword(kw:str)->None:
    for path in (CSV_GENERAL_MAIN, CSV_GENERAL_FALLBACK):
        lst=_load_csv_list(path)
        if kw in lst:
            lst.remove(kw); _save_csv_list(path,lst); break

def _append_usage(kw:str)->None:
    _ensure_dirs()
    today=_now_kst().strftime("%Y-%m-%d")
    with open(USAGE_GENERAL,"a",encoding="utf-8") as f:
        f.write(f"{today},{kw}\n")

# --- Fallback detection -------------------------------------
def _is_fallback_kw(kw: str) -> bool:
    return kw.startswith("일상 아카이브 ")

# --- Title / Body (HTML) ------------------------------------
def _make_title_from_keyword(kw:str)->str:
    """
    폴백 키워드(일상 아카이브 …)는 사용자에게 노출하지 않음.
    """
    if _is_fallback_kw(kw):
        return "요즘 사람들이 진짜 궁금해하는 포인트 정리"
    return f"{kw} : 요즘 사람들이 진짜 궁금해하는 포인트 정리"

def _build_static_html_body(kw:str)->str:
    """마크다운 X, 표준 HTML. 테마/구텐베르크 스타일을 그대로 받는다."""
    # TOC 플러그인/테마가 있으면 [toc] 인식, 없으면 텍스트로 노출되지만 무해.
    heading = "" if _is_fallback_kw(kw) else f"<h2>{kw} 한눈에 보기</h2>"
    intro = (
        "<p>핵심 포인트를 빠르게 이해할 수 있도록 간단히 정리했습니다. "
        "최신 사례와 실전 팁을 바탕으로 <strong>바로 적용 가능한 포인트</strong>만 담았습니다.</p>"
    )
    return f"""
<p>[toc]</p>

{heading}
{intro}

<h2>핵심 요약</h2>
<ol>
  <li>왜 중요한가?</li>
  <li>무엇부터 해야 하는가?</li>
  <li>실패를 줄이는 체크리스트</li>
</ol>

<h2>디테일 가이드</h2>
<ul>
  <li>상황별로 선택해야 할 옵션</li>
  <li>실제로 써보니 좋았던 방법</li>
  <li>흔한 함정과 피하는 법</li>
</ul>

<blockquote>
  <p><strong>TIP</strong> : 작은 반복이 큰 차이를 만듭니다. 오늘 하나만 바로 실행해 보세요.</p>
</blockquote>

<h2>정리</h2>
<p>핵심만 빠르게 실행해 보세요. 나중에 다듬는 것보다 <strong>지금 시작하는 것</strong>이 더 중요합니다.</p>
""".strip()

def _openai_generate_html_body(kw:str)->str:
    """OPENAI_API_KEY가 있으면 HTML로 직접 생성하도록 지시."""
    if not OPENAI_API_KEY:
        return _build_static_html_body(kw)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=OPENAI_API_KEY)

        # 폴백이면 키워드를 프롬프트에 넣지 않음(노출 방지)
        prompt_kw = "" if _is_fallback_kw(kw) else f"키워드: {kw}\n"

        sys_prompt = (
            "너는 한국어 글쓰기 전문가다. 워드프레스에 그대로 붙여넣을 "
            "<h2>/<h3>/<p>/<ul>/<ol>/<blockquote>/<table> 등 HTML만 출력해라. "
            "마크다운(##, -, **) 금지. 인라인 스타일 최소화. 제목에 '예약' 같은 접두어 금지. "
            "본문 맨 위에 [toc] 단축코드를 한 단락으로 포함해라."
        )
        user_prompt = (
            f"{prompt_kw}"
            f"- 섹션: 한눈에 보기 / 핵심 요약 / 디테일 가이드 / 정리\n"
            f"- 톤: 간결하고 실용적, 소제목은 h2, 필요시 h3\n"
            f"- 글자수: 900~1400자\n"
        )
        resp = client.chat.completions.create(
            model = OPENAI_MODEL_LONG or OPENAI_MODEL,
            messages=[
                {"role":"system","content":sys_prompt},
                {"role":"user","content":user_prompt},
            ],
            temperature=0.5,
            max_tokens=MAX_TOKENS_BODY,
        )
        content = (resp.choices[0].message.content or "").strip()
        if content: return content
    except Exception as e:
        print(f"[OPENAI][WARN] fallback to static html: {type(e).__name__}: {e}")
    return _build_static_html_body(kw)

# --- WP create ----------------------------------------------
def _wp_create_post(date_gmt_str:str, title:str, content_html:str)->dict:
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    payload = {
        "title": title,
        "content": content_html,   # HTML 그대로 전송
        "status": POST_STATUS,
        "date_gmt": date_gmt_str,
    }
    r = requests.post(
        url, json=payload, headers=REQ_HEADERS,
        auth=(WP_USER, WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=30,
    )
    r.raise_for_status()
    return r.json()

# --- Flow ----------------------------------------------------
def _schedule_one(h:int, m:int, kw:str)->dict:
    date_gmt = _slot_or_next_day(h,m)
    title = _make_title_from_keyword(kw)
    body_html = _openai_generate_html_body(kw)
    res = _wp_create_post(date_gmt, title, body_html)
    _remove_used_keyword(kw); _append_usage(kw)
    return {
        "post_id": res.get("id"),
        "link": res.get("link"),
        "status": res.get("status"),
        "date_gmt": res.get("date_gmt"),
        "title": res.get("title",{}).get("rendered"),
        "keyword": kw,
    }

def run_two_posts():
    kw1, kw2 = _pick_keywords_for_two_posts()
    print(_schedule_one(10,0,kw1))
    print(_schedule_one(17,0,kw2))

def parse_args():
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode", default="two-posts")
    return ap.parse_args()

def _check_env():
    miss=[]
    for k,v in {"WP_URL":WP_URL,"WP_USER":WP_USER,"WP_APP_PASSWORD":WP_APP_PASSWORD}.items():
        if not v: miss.append(k)
    if miss:
        print(f"[FATAL] missing env: {', '.join(miss)}"); sys.exit(2)

def main():
    _check_env()
    args=parse_args()
    if args.mode.lower().strip()=="two-posts":
        run_two_posts()
    else:
        run_two_posts()

if __name__=="__main__":
    main()
