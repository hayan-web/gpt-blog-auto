# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 자동 워드프레스 포스팅 (일상글 2건)
- 일반(일상) 글은 매 플로우마다 '완전 새로운 키워드'로 작성
- keywords_general.csv가 비어도 폴백 풀/ENV로 반드시 2건 생성
- 최근 사용/당일 중복 방지(.usage/used_general.txt)
- 공백 제외 MIN_DIARY_CHARS(기본 1500) 이상 보장
"""

import os, csv, json, html, re, random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List
import requests
from dotenv import load_dotenv

load_dotenv()

# ===== ENV =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
VERIFY_TLS=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"

POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()
DEFAULT_CATEGORY=(os.getenv("DEFAULT_CATEGORY") or "정보").strip() or "정보"
EXISTING_CATEGORIES=[c.strip() for c in (os.getenv("EXISTING_CATEGORIES") or "").split(",") if c.strip()]

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-auto/diary-2.2"
USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_GENERAL=os.path.join(USAGE_DIR,"used_general.txt")

NO_REPEAT_TODAY=(os.getenv("NO_REPEAT_TODAY") or "1").lower() in ("1","true","y","yes","on")
GEN_USED_BLOCK_DAYS=int(os.getenv("AFF_USED_BLOCK_DAYS") or "30")

KEYWORDS_CSV=(os.getenv("KEYWORDS_CSV") or "keywords_general.csv").strip()
MIN_DIARY_CHARS=int(os.getenv("MIN_DIARY_CHARS") or "1500")

REQ_HEADERS={"User-Agent":USER_AGENT,"Accept":"application/json","Content-Type":"application/json; charset=utf-8"}

# ===== WP helpers =====
def _ensure_term(kind:str, name:str)->int:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}",
                   params={"search":name,"per_page":50,"context":"edit"},
                   auth=(WP_USER,WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name:
            return int(it["id"])
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
                    auth=(WP_USER,WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    return int(r.json()["id"])

def post_wp(title:str, content:str, when_gmt:str, category:str)->dict:
    cat_id=_ensure_term("categories", category or DEFAULT_CATEGORY)
    payload={
        "title": title,
        "content": content,
        "status": POST_STATUS,
        "categories": [cat_id],
        "comment_status": "closed",
        "ping_status": "closed",
        "date_gmt": when_gmt
    }
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                    auth=(WP_USER,WP_APP_PASSWORD), verify=VERIFY_TLS, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status()
    return r.json()

# ===== usage =====
def _ensure_usage(): os.makedirs(USAGE_DIR,exist_ok=True)

def _load_used(days:int)->set:
    _ensure_usage()
    if not os.path.exists(USED_GENERAL): return set()
    cutoff=datetime.utcnow().date()-timedelta(days=days)
    out=set()
    for ln in open(USED_GENERAL,"r",encoding="utf-8",errors="ignore"):
        ln=ln.strip()
        if not ln: continue
        try:
            d_str,kw=ln.split("\t",1)
            if datetime.strptime(d_str,"%Y-%m-%d").date()>=cutoff:
                out.add(kw.strip())
        except Exception:
            out.add(ln)
    return out

def _mark_used(kw:str):
    _ensure_usage()
    with open(USED_GENERAL,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw}\n")

# ===== keywords =====
def _read_col_csv(path:str)->List[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8",newline="") as f:
        rd=csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and row[0].strip().lower() in ("keyword","title"): continue
            s=row[0].strip()
            if s: out.append(s)
    return out

def _fresh_general_keyword() -> str:
    used_block=_load_used(GEN_USED_BLOCK_DAYS)
    used_today=_load_used(1) if NO_REPEAT_TODAY else set()

    pool=_read_col_csv(KEYWORDS_CSV)
    if not pool:
        base=[w.strip() for w in (os.getenv("GENERAL_FALLBACK_KEYWORDS") or "").split(",") if w.strip()] or [
            "가계부","정리정돈","시간관리","운동 루틴","독서 메모","주간 계획","월간 회고","홈카페",
            "집안일 팁","디지털 정리","습관 기록","작은 실험","배운 점","루틴 점검","하루 요약",
        ]
        pool = [*base]

    random.shuffle(pool)
    for kw in pool:
        if kw in used_block or kw in used_today:
            continue
        return kw

    seed=datetime.utcnow().strftime("%Y%m%d%H%M")
    return f"{random.choice(pool)} {seed}"

# ===== content =====
def _css():
    return """
<style>
.diary-wrap{line-height:1.85}
.diary-cta{display:flex;justify-content:center;margin:18px 0}
.diary-btn{display:inline-block;padding:14px 22px;border-radius:999px;background:#0ea5e9;color:#fff;text-decoration:none;font-weight:800}
.diary-card{padding:14px 16px;border:1px solid #e5e7eb;border-radius:12px;background:#fafafa;margin:14px 0}
.diary-h2{margin:10px 0 6px;font-size:1.25rem;color:#334155}
.diary-hr{border:0;border-top:1px solid #e5e7eb;margin:16px 0}
.diary-small{font-size:.95rem;color:#64748b}
</style>
""".strip()

def _chars_no_space(s: str) -> int:
    return len(re.sub(r"\s+", "", s or ""))

def _build_title(kw:str)->str:
    tpl=[
        f"{kw} — 오늘 한 줄 회고",
        f"오늘의 {kw} 기록",
        f"{kw} 메모: 하루 요약",
        f"{kw} 점검 노트",
    ]
    random.shuffle(tpl)
    return tpl[0]

def _section(title:str, body_html:str)->str:
    return f'<div class="diary-card"><strong class="diary-h2">{html.escape(title)}</strong>{body_html}</div>'

def _p(text:str)->str:
    return f"<p>{html.escape(text)}</p>"

def _ul(items:List[str])->str:
    xs=[i for i in items if i and i.strip()]
    if not xs: return ""
    return "<ul>" + "".join(f"<li>{html.escape(i)}</li>" for i in xs) + "</ul>"

def _ensure_min_diary_chars(body_html:str, min_chars:int=MIN_DIARY_CHARS)->str:
    """공백 제외 글자수 보강: 섹션을 단계적으로 추가해 자연스럽게 1500자 이상을 보장."""
    if _chars_no_space(body_html) >= min_chars:
        return body_html

    fillers = [
        _section("하루 에피소드",
                 _p("오늘의 핵심 장면을 5W1H(누가, 언제, 어디서, 무엇을, 왜, 어떻게)로 짧게 써서 사건의 뼈대를 세웁니다.") +
                 _p("그 장면이 주제와 연결되는 이유를 한 문장으로 정리해요.")),
        _section("실행 로그",
                 _ul(["시작 시간/중단 시간 기록", "집중을 방해한 요인 1가지 제거", "끝나고 3줄 회고 작성"]) +
                 _p("결과보다 과정을 데이터로 남기면 다음 개선점이 또렷해집니다.")),
        _section("관련 자료",
                 _ul(["참고한 글/도구/영상 1~2개", "대체 선택지 1개와 포기 이유"]) +
                 _p("근거를 남겨두면 이후 재사용성이 생깁니다.")),
        _section("내일을 위한 질문",
                 _ul(["무엇을 계속할 것인가?", "무엇을 줄일 것인가?", "무엇을 시작할 것인가?"]) +
                 _p("질문이 다음 행동을 당깁니다.")),
    ]
    buf = body_html
    for ex in fillers:
        if _chars_no_space(buf) >= min_chars: break
        buf += "\n" + ex

    # 그래도 부족하면 가벼운 베이스 문단 추가(중복 표현 최소화)
    base = ("기록은 선택을 선명하게 만듭니다. 오늘의 선택이 왜 합리적이었는지 근거를 남기면 "
            "내일은 더 빠르게 같은 품질의 결정을 내릴 수 있습니다. 작은 반복이 누적되어 변화를 만듭니다.")
    i = 0
    while _chars_no_space(buf) < min_chars and i < 6:
        buf += "\n" + _p(base)
        i += 1
    return buf

def _render_diary_long(kw:str, category:str)->str:
    cat=html.escape(category or DEFAULT_CATEGORY)
    kw_e=html.escape(kw)

    parts=[_css(), '<div class="diary-wrap">']
    parts.append(_p("하루를 짧게 넘기지 않기 위해 핵심만 또렷하게 남깁니다. 요약 → 배운 점 → 실행 계획 → 지표/체크리스트 순서로 정리해요."))

    parts.append(_section("요약",
        _p(f"오늘의 주제는 ‘{kw_e}’였습니다. 가장 의미 있었던 한 장면을 떠올려 핵심 전환점을 문장 하나로 남깁니다.") +
        _p("왜 이런 결과가 나왔는지, 운과 실력, 시스템 중 어디의 영향이 컸는지 구분합니다.")
    ))

    parts.append(_section("하이라이트 3",
        _ul(["가장 잘한 선택 1가지","의외의 장애물 1가지","내일도 반복하고 싶은 습관 1가지"])
    ))

    parts.append(_section("배운 점",
        _p("사실/해석/감정이 섞이지 않도록, 관찰한 사실 → 해석 → 선택지 순으로 나눠 적습니다.") +
        _p("같은 상황이 오면 어떤 기준으로 빠르게 결정할지 한 문장으로 정의합니다.")
    ))

    parts.append(_section("실행 계획",
        _ul([
            "내일 당장 적용: 5분 이내로 시작할 수 있는 첫 행동",
            "중기 계획: 2주 동안 지켜볼 지표 하나",
            "차단 규칙: 하지 않을 것 1가지"
        ])
    ))

    parts.append(_section("지표/체크리스트",
        _ul(["집중 시간(분)", "피드백 횟수", "완료/보류 항목", "수면/수분/운동"]) +
        _p("숫자는 솔직하게 적고, 비교는 어제의 나와만 합니다.")
    ))

    parts.append(_section("회고 문단",
        _p("오늘은 작은 진동이 누적된 날이었어요. 즉각적 성과는 크지 않아도 방향이 올바르면 다음 선택이 쉬워집니다.") +
        _p("기록은 기억의 편집점을 만들어 줍니다. 다음 번 같은 갈림길에서 망설임을 줄이기 위해 문장을 남깁니다.")
    ))

    # 하단 CTA (카테고리 이동)
    parts.append('<hr class="diary-hr"/>')
    parts.append(f'<div class="diary-cta"><a class="diary-btn" href="{WP_URL}/category/{cat}" aria-label="카테고리 보기">카테고리 보기</a></div>')
    parts.append('</div>')  # .diary-wrap

    body="\n".join(parts)

    # 길이 보정(공백 제외)
    if _chars_no_space(body) < MIN_DIARY_CHARS:
        body = _ensure_min_diary_chars(body, MIN_DIARY_CHARS)

    return body

# ===== scheduling =====
def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))

def _slot_kst(hour:int, minute:int)->str:
    now=_now_kst()
    tgt=now.replace(hour=hour,minute=minute,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def _two_slots()->list[str]:
    # 10:00, 17:00 KST
    return [_slot_kst(10,0), _slot_kst(17,0)]

# ===== main modes =====
def _post_one_diary():
    kw=_fresh_general_keyword()
    title=sanitize_title(_build_title(kw))
    body=_render_diary_long(kw, DEFAULT_CATEGORY)
    when_gmt=_slot_kst(10,0)
    res=post_wp(title, body, when_gmt, DEFAULT_CATEGORY)
    print(json.dumps({"id":res.get("id"),"title":title,"date_gmt":res.get("date_gmt"),"link":res.get("link")}, ensure_ascii=False))
    _mark_used(kw)

def _post_two_diaries():
    slots=_two_slots()
    for when in slots:
        kw=_fresh_general_keyword()
        title=_build_title(kw)
        body=_render_diary_long(kw, DEFAULT_CATEGORY)
        res=post_wp(title, body, when, DEFAULT_CATEGORY)
        print(json.dumps({"id":res.get("id"),"title":title,"date_gmt":res.get("date_gmt"),"link":res.get("link")}, ensure_ascii=False))
        _mark_used(kw)

def main():
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["one","two-posts"], default="two-posts")
    args=ap.parse_args()

    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    if args.mode=="one":
        _post_one_diary()
    else:
        _post_two_diaries()

if __name__=="__main__":
    main()


def sanitize_title(title: str) -> str:
    # remove leading markers like "예약", "예약17", etc.
    t = re.sub(r'^\s*예약\d*\s*[:：-]\s*', '', title)
    t = re.sub(r'^\s*예약\d*\s*', '', t)
    t = t.replace("예약 ", "").strip()
    return t
