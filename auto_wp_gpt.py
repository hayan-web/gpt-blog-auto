# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상글 자동 발행 (기사형)
- 섹션: 요약 → 하이라이트 → 배운 점 → 실행 계획 → 지표/체크리스트 → 회고 (+ 내부광고 상/중간)
- 박스/카드형 마크업 제거, 일반 글 스타일(H2/H3/UL/TABLE)만 사용
- 공백 제외 1500자 이상 자동 보강
- 예약: 기본 10:00 / 17:00 (KST)
- 키워드 CSV(환경변수 KEYWORDS_CSV, 기본 keywords_general.csv)에서 맨 윗줄 사용 후 사용 로그 기록
"""

from __future__ import annotations
import os, csv, json, re, html, argparse
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional, List, Dict
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
AD_SHORTCODE=os.getenv("AD_SHORTCODE") or ""

USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_GENERAL=os.path.join(USAGE_DIR, "used_general.txt")

KEYWORDS_CSV=(os.getenv("KEYWORDS_CSV") or "keywords_general.csv").strip()
MIN_CHARS=int(os.getenv("MIN_DIARY_CHARS") or "1500")

REQ_HEADERS={
    "User-Agent": os.getenv("USER_AGENT") or "gpt-blog-auto/diary-2.1",
    "Accept":"application/json",
    "Content-Type":"application/json; charset=utf-8"
}

# ===== 공통 유틸 =====
def _esc(s: Optional[str])->str:
    return html.escape((s or "").strip())

def _strip_tags(s:str)->str:
    return re.sub(r"<[^>]+>","", s or "")

def _nchars_no_space(html_text:str)->int:
    return len(re.sub(r"\s+","", _strip_tags(html_text)))

def _ensure_usage():
    os.makedirs(USAGE_DIR, exist_ok=True)

def _mark_used(topic:str):
    _ensure_usage()
    with open(USED_GENERAL, "a", encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{topic}\n")

def _read_col_csv(path:str)->List[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8",newline="") as f:
        rd=csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and row[0].strip().lower() in ("keyword","title"):
                continue
            s=row[0].strip()
            if s: out.append(s)
    return out

def _pick_topic()->str:
    pool=_read_col_csv(KEYWORDS_CSV)
    return pool[0] if pool else "하루 회고"

def _ensure_min_chars(body_html:str, min_chars:int)->str:
    if _nchars_no_space(body_html) >= min_chars:
        return body_html
    fillers = [
        "<h3>메모: 기준 재확인</h3><p>사실·해석·감정을 구분해 적고, 해석은 검증 가능한 근거와 분리합니다. "
        "다음 행동은 5분 안에 시작할 수 있을 만큼 작게 정의합니다.</p>",
        "<h3>작은 습관의 힘</h3><p>완벽보다 빈도가 중요합니다. 미세한 개선이라도 누적되면 다음 선택의 난이도가 낮아집니다.</p>",
        "<h3>반복 점검 문장</h3><p>오늘의 선택이 1주 후에도 같은 결정을 돕는가? 재사용 가능한 문장을 한 줄로 남깁니다.</p>",
    ]
    buf=body_html
    for add in fillers:
        if _nchars_no_space(buf) >= min_chars: break
        buf += "\n" + add
    base = ("핵심 한 가지에 에너지를 모읍니다. 측정 가능한 지표를 정하고, "
            "다음 행동을 캘린더에 바로 배치하면 실행률이 올라갑니다.")
    while _nchars_no_space(buf) < min_chars:
        buf += f"\n<p class='note'>{base}</p>"
    return buf

# ===== 시간대 =====
def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

def _slot_to_utc(kst_hm:str)->str:
    hh,mm=[int(x) for x in kst_hm.split(":")]
    now=_now_kst()
    tgt=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ===== WordPress =====
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

# ===== 본문 생성 (기사형) =====
def build_diary_content(topic:str)->str:
    ad = AD_SHORTCODE or ""
    t = _esc(topic)

    # 1) 상단 내부광고
    sec1 = ad

    # 2) 요약
    sec2 = (
        "<h2>요약</h2>"
        "<p>하루를 짧게 넘기지 않기 위해 핵심만 적습니다. "
        "오늘의 주제는 ‘{t}’였습니다. 가장 의미 있었던 한 장면을 떠올려 핵심 전환점을 문장 하나로 남깁니다.</p>"
    ).format(t=t)

    # 3) 하이라이트
    sec3 = (
        "<h2>하이라이트 3</h2>"
        "<ul>"
        "<li>가장 잘한 선택 1가지</li>"
        "<li>의외의 장애물 1가지</li>"
        "<li>내일도 반복하고 싶은 습관 1가지</li>"
        "</ul>"
    )

    # 4) 배운 점
    sec4 = (
        "<h2>배운 점</h2>"
        "<p>사실과 해석, 감정을 섞지 않도록 주의합니다. 같은 상황이 오면 어떤 기준으로 더 빠르게 "
        "결정할지 문장으로 정의합니다.</p>"
    )

    # 5) 중간 내부광고
    sec5 = ad

    # 6) 실행 계획
    sec6 = (
        "<h2>실행 계획</h2>"
        "<ul>"
        "<li>내일 당장 착수: 5분 이내 시작할 수 있는 첫 행동</li>"
        "<li>중기 계획: 2주 동안 지켜볼 지표 하나</li>"
        "<li>차단 규칙: 하지 않을 것 1가지</li>"
        "</ul>"
    )

    # 7) 지표/체크리스트
    table = (
        "<table>"
        "<thead><tr><th>지표</th><th>기록 단위</th><th>비고</th></tr></thead>"
        "<tbody>"
        "<tr><td>집중 시간</td><td>분</td><td>깊은 작업</td></tr>"
        "<tr><td>피드백 횟수</td><td>건</td><td>상호작용</td></tr>"
        "<tr><td>완료/보류</td><td>개</td><td>진행률</td></tr>"
        "<tr><td>수면/운동</td><td>시간/분</td><td>컨디션</td></tr>"
        "</tbody></table>"
    )
    sec7 = "<h2>지표/체크리스트</h2>" + table

    # 8) 회고
    sec8 = (
        "<h2>회고 문단</h2>"
        "<p>작은 진동이 누적되면 방향이 잡힙니다. 완벽한 날보다 흐름이 끊기지 않는 하루가 더 강력합니다. "
        "내일도 같은 결정을 쉽게 만들 문장을 한 줄 남깁니다.</p>"
    )

    body = "\n\n".join([sec1, sec2, sec3, sec4, sec5, sec6, sec7, sec8])
    return _ensure_min_chars(body, MIN_CHARS)

# ===== 작성 & 예약 =====
def _sanitize_title(t:str)->str:
    # 혹시 생성 흐름에서 '예약' 같은 접두가 들어오면 제거
    t = t.strip()
    return re.sub(r"^\s*예약[:\-\s]+", "", t)

def create_diary_post(slot_kst:str, topic:str)->dict:
    when_gmt=_slot_to_utc(slot_kst)
    title=_sanitize_title(f"{topic} 메모: 하루 요약")
    content=build_diary_content(topic)
    return post_wp(title, content, when_gmt, DEFAULT_CATEGORY)

# ===== CLI =====
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--mode", default="two-posts")
    args=parser.parse_args()

    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")

    # 토픽 선정 & 사용 로그
    topic=_pick_topic()
    _mark_used(topic)

    if args.mode=="two-posts":
        slots=["10:00","17:00"]  # KST
    else:
        slots=["10:00"]

    results=[]
    for s in slots:
        res=create_diary_post(s, topic)
        results.append({"id":res.get("id"), "title":res.get("title",{}).get("rendered") if isinstance(res.get("title"),dict) else topic,
                        "date_gmt":res.get("date_gmt"), "link":res.get("link")})

    for r in results:
        print(json.dumps(r, ensure_ascii=False))

if __name__=="__main__":
    main()
