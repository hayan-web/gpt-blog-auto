# -*- coding: utf-8 -*-
"""
affiliate_post.py — Coupang Partners 자동 포스팅 (SEO 구조/광고/버튼 반영)
- 상단 고지문 강조 + 상단 가로 CTA(2) + 내부광고(상단)
- 본문: H2(부제목) → 요약(300자 이내) → H3 섹션 6~8개(구분선 <hr> 포함, 표 1개 이상) → 내부광고(중간) → 결론/추천
- 하단 가로 CTA(2) + 카테고리 모아보기
- 중간 CTA는 삭감(요청사항 반영)
- 버튼: 가로 2개, 가운데 정렬, 반응형(모바일은 세로 스택), 호버 효과
- 내부광고: AD_SHORTCODE 값이 있을 때만 그대로 삽입(스크립트 포함)
"""

import os, re, csv, json, html, random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Tuple
import requests
from dotenv import load_dotenv
from urllib.parse import quote, quote_plus

load_dotenv()

try:
    from openai import OpenAI, BadRequestError
except Exception:
    OpenAI = None
    BadRequestError = Exception

# ====== ENV / WP ======
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()

DEFAULT_CATEGORY=(os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip() or "쇼핑"
DEFAULT_TAGS=(os.getenv("AFFILIATE_TAGS") or "").strip()
DISCLOSURE_TEXT=(os.getenv("DISCLOSURE_TEXT") or "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공합니다.").strip()

# 버튼 라벨
BUTTON_PRIMARY=(os.getenv("BUTTON_TEXT") or "제품 보기").strip()
BUTTON_SECONDARY=(os.getenv("BUTTON2_TEXT") or "쇼핑 글 모아보기").strip()
BUTTON2_URL=(os.getenv("BUTTON2_URL") or "").strip()

USE_IMAGE=((os.getenv("USE_IMAGE") or "").strip().lower() in ("1","true","y","yes","on"))
AFFILIATE_TIME_KST=(os.getenv("AFFILIATE_TIME_KST") or "13:00").strip()

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-affiliate/3.0"
USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_FILE=os.path.join(USAGE_DIR,"used_shopping.txt")

NO_REPEAT_TODAY=(os.getenv("NO_REPEAT_TODAY") or "1").lower() in ("1","true","y","yes","on")
AFF_USED_BLOCK_DAYS=int(os.getenv("AFF_USED_BLOCK_DAYS") or "30")

PRODUCTS_SEED_CSV=(os.getenv("PRODUCTS_SEED_CSV") or "products_seed.csv")

REQ_HEADERS={
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ====== OpenAI (제목·문장 톤 보강, 없어도 동작) ======
_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_OPENAI_MODEL = (os.getenv("OPENAI_MODEL_LONG") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini")
_oai = OpenAI(api_key=_OPENAI_API_KEY) if (_OPENAI_API_KEY and OpenAI) else None

AFF_TITLE_MIN = int(os.getenv("AFF_TITLE_MIN", "22"))
AFF_TITLE_MAX = int(os.getenv("AFF_TITLE_MAX", "42"))
AFF_TITLE_MODE = (os.getenv("AFF_TITLE_MODE") or "story-then-template").lower()
AFF_BANNED_PHRASES = ("사용기","리뷰","후기","광고","테스트","예약됨","최저가","역대급","무조건","필구","대박")

# ====== 광고 블록 ======
def _adsense_block()->str:
    sc = (os.getenv("AD_SHORTCODE") or "").strip()
    if not sc:
        return ""
    # 숏코드/스크립트 그대로 박아 넣기 (WP가 렌더링)
    return f'<div class="ads-wrap" style="margin:16px 0">{sc}</div>'

# ====== 유틸 ======
def _normalize_title(s:str)->str:
    s=(s or "").strip()
    s=html.unescape(s)
    s=s.replace("“","").replace("”","").replace("‘","").replace("’","").strip('"\' ')
    s=re.sub(r"\s+"," ",s)
    return s

def _sanitize_title_text(s:str)->str:
    s=_normalize_title(s)
    for ban in AFF_BANNED_PHRASES:
        s=s.replace(ban,"")
    s=re.sub(r"\s+"," ",s).strip(" ,.-·")
    return s

def _bad_aff_title(t:str)->bool:
    if not t: return True
    if not (AFF_TITLE_MIN <= len(t) <= AFF_TITLE_MAX): return True
    if any(p in t for p in AFF_BANNED_PHRASES): return True
    return False

def _has_jong(ch:str)->bool:
    code=ord(ch)-0xAC00
    return 0<=code<=11171 and (code%28)!=0

def _josa(word:str, pair=("이","가"))->str:
    return pair[0] if word and _has_jong(word[-1]) else pair[1]

# 핵심 키워드 추출(간단)
_CATS=["니트","스웨터","가디건","가습기","전기포트","선풍기","청소기","보조배터리","제습기","히터"]
def _compress_keyword(keyword:str)->Tuple[str,str]:
    toks=[t for t in re.sub(r"[^\w가-힣\s]"," ",keyword).split() if t]
    cat=None
    for c in _CATS:
        if c in keyword: cat=c; break
    core = cat or "아이템"
    return core," ".join(toks)

# ====== 제목 생성 ======
def _aff_title_from_story(keyword:str)->str:
    core,_=_compress_keyword(keyword)
    seed=abs(hash(f"story|{core}|{keyword}|{datetime.utcnow().date()}"))%(2**32)
    rnd=random.Random(seed)
    subject = f"{core}{_josa(core,('은','는'))}"
    pool=[
        f"요즘 {subject} 확실히 편해졌어요, 돌려보면 차이가 나요",
        f"아침마다 {subject} 손이 자꾸 가요, 써보면 이유를 알게 돼요",
        f"{subject} 홈카페가 쉬워졌어요, 그래서 계속 쓰게 돼요",
        f"한 번 써보면 {subject} 왜 편한지 알게 돼요"
    ]
    rnd.shuffle(pool)
    for cand in pool:
        cand=_sanitize_title_text(cand)
        if not _bad_aff_title(cand):
            return cand
    return ""

def _aff_title_from_llm(core:str, kw:str)->str:
    if not _oai: return ""
    try:
        r=_oai.chat.completions.create(
            model=_OPENAI_MODEL,
            temperature=0.8,
            max_tokens=60,
            messages=[
                {"role":"system","content":"너는 한국어 카피라이터다. 과장/낚시 없이 모바일 친화 한 줄 제목만 출력."},
                {"role":"user","content":f"핵심:{core}\n원문:{kw}\n길이:{AFF_TITLE_MIN}~{AFF_TITLE_MAX}자, 과장·금지어 배제, 자연어 한 줄"}
            ]
        )
        cand=_sanitize_title_text(r.choices[0].message.content or "")
        return "" if _bad_aff_title(cand) else cand
    except Exception:
        return ""

TEMPLATES=["{core} 이렇게 쓰니 편해요","지금 딱 {core}","한 번 쓰면 계속 찾는 {core}","가볍게 챙기는 {core}"]
def _aff_title_from_templates(core:str, kw:str)->str:
    for tpl in TEMPLATES:
        cand=_sanitize_title_text(tpl.format(core=core))
        if not _bad_aff_title(cand): return cand
    return _sanitize_title_text(core)

def build_title(keyword:str)->str:
    core,_=_compress_keyword(keyword)
    for fn in (_aff_title_from_story, lambda k:_aff_title_from_llm(core,k), lambda k:_aff_title_from_templates(core,k)):
        t=fn(keyword)
        if t: return t[:AFF_TITLE_MAX]
    return _sanitize_title_text(keyword)[:AFF_TITLE_MAX]

# ====== 슬롯 ======
def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))

def _wp_future_exists_around(when_gmt_dt:datetime, tol_min:int=2)->bool:
    url=f"{WP_URL}/wp-json/wp/v2/posts"
    try:
        r=requests.get(url, params={"status":"future","per_page":100,"orderby":"date","order":"asc","context":"edit"},
                       headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20)
        r.raise_for_status()
        items=r.json()
    except Exception:
        return False
    tgt=when_gmt_dt.astimezone(timezone.utc)
    win=timedelta(minutes=max(1,int(tol_min)))
    lo,hi=tgt-win,tgt+win
    for it in items:
        d=(it.get("date_gmt") or "").strip()
        if not d: continue
        try:
            dt=datetime.fromisoformat(d.replace("Z","+00:00"))
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except Exception:
            continue
        if lo<=dt<=hi: return True
    return False

def _slot_affiliate()->str:
    hh,mm=[int(x) for x in (AFFILIATE_TIME_KST.split(":")+["0"])[:2]]
    now=_now_kst()
    tgt=now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    for _ in range(7):
        utc=tgt.astimezone(timezone.utc)
        if _wp_future_exists_around(utc,2):
            tgt+=timedelta(days=1); continue
        break
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

# ====== 사용 로그 ======
def _ensure_usage_dir(): os.makedirs(USAGE_DIR,exist_ok=True)

def _load_used_set(days:int=30)->set:
    _ensure_usage_dir()
    if not os.path.exists(USED_FILE): return set()
    cutoff=datetime.utcnow().date()-timedelta(days=days)
    used=set()
    with open(USED_FILE,"r",encoding="utf-8",errors="ignore") as f:
        for line in f:
            line=line.strip()
            if not line: continue
            try:
                d_str,kw=line.split("\t",1)
                if datetime.strptime(d_str,"%Y-%m-%d").date()>=cutoff:
                    used.add(kw.strip())
            except Exception:
                used.add(line)
    return used

def _read_recent_used(n:int=8)->list[str]:
    try:
        p=os.path.join(USAGE_DIR,"used_shopping.txt")
        if not os.path.exists(p): return []
        lines=[ln.strip() for ln in open(p,"r",encoding="utf-8").read().splitlines() if ln.strip()]
        body=[ln.split("\t",1)[1] if "\t" in ln else ln for ln in lines]
        return list(reversed(body[-n:]))
    except Exception:
        return []

def _mark_used(kw:str):
    _ensure_usage_dir()
    with open(USED_FILE,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw.strip()}\n")

# ====== CSV ======
def _read_col_csv(path:str)->List[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8",newline="") as f:
        rd=csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and (row[0].strip().lower() in ("keyword","title")): continue
            if row[0].strip(): out.append(row[0].strip())
    return out

def _consume_col_csv(path:str, kw:str)->bool:
    if not os.path.exists(path): return False
    rows=list(csv.reader(open(path,"r",encoding="utf-8",newline="")))
    if not rows: return False
    has_header=rows[0] and rows[0][0].strip().lower() in ("keyword","title")
    body=rows[1:] if has_header else rows[:]
    before=len(body)
    body=[r for r in body if (r and r[0].strip()!=kw)]
    if len(body)==before: return False
    new_rows=([rows[0]] if has_header else [])+[[r[0].strip()] for r in body]
    csv.writer(open(path,"w",encoding="utf-8",newline="")).writerows(new_rows)
    return True

# ====== 키워드 선택 / URL ======
def pick_affiliate_keyword()->str:
    used_today=_load_used_set(1) if NO_REPEAT_TODAY else set()
    used_block=_load_used_set(AFF_USED_BLOCK_DAYS)
    gold=_read_col_csv("golden_shopping_keywords.csv")
    shop=_read_col_csv("keywords_shopping.csv")
    pool=[k for k in gold+shop if k and (k not in used_block)]
    if NO_REPEAT_TODAY:
        pool=[k for k in pool if k not in used_today]
    recent=set(_read_recent_used(8))
    pool=[k for k in pool if k not in recent]
    if pool: return pool[0].strip()
    fb=[x.strip() for x in (os.getenv("AFF_FALLBACK_KEYWORDS") or "").split(",") if x.strip()]
    return fb[0] if fb else "미니 선풍기"

def resolve_product_url(keyword:str)->str:
    if os.path.exists(PRODUCTS_SEED_CSV):
        try:
            rd=csv.DictReader(open(PRODUCTS_SEED_CSV,"r",encoding="utf-8"))
            for r in rd:
                if (r.get("keyword") or "").strip()==keyword and (r.get("url") or "").strip():
                    return r["url"].strip()
                if (r.get("product_name") or "").strip()==keyword and (r.get("url") or "").strip():
                    return r["url"].strip()
                if (r.get("raw_url") or "").strip() and (r.get("product_name") or "").strip()==keyword:
                    return r["raw_url"].strip()
        except Exception:
            pass
    return f"https://www.coupang.com/np/search?q={quote_plus(keyword)}"

# ====== 워드프레스 ======
def _ensure_term(kind:str, name:str)->int:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}",
                   params={"search":name,"per_page":50,"context":"edit"},
                   auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name: return int(it["id"])
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status(); return int(r.json()["id"])

def _category_url_for(name:str)->str:
    try:
        r=requests.get(f"{WP_URL}/wp-json/wp/v2/categories",
                       params={"search":name,"per_page":50,"context":"view"},
                       headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=12)
        r.raise_for_status()
        for it in r.json():
            if (it.get("name") or "").strip()==name:
                link=(it.get("link") or "").strip()
                if link: return link
    except Exception:
        pass
    return f"{WP_URL}/category/{quote(name)}/"

def post_wp(title:str, html_body:str, when_gmt:str, category:str, tag:str)->dict:
    cat_id=_ensure_term("categories", category or DEFAULT_CATEGORY)
    tag_ids=[]
    if tag:
        try:
            tid=_ensure_term("tags", tag); tag_ids=[tid]
        except Exception:
            pass
    payload={
        "title": title,
        "content": html_body,
        "status": POST_STATUS,
        "categories": [cat_id],
        "tags": tag_ids,
        "comment_status": "closed",
        "ping_status": "closed",
        "date_gmt": when_gmt
    }
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status(); return r.json()

# ====== 스타일/CSS & 컴포넌트 ======
def _css_block()->str:
    return """
<style>
.aff-wrap{font-family:inherit;line-height:1.65}
.aff-disclosure{margin:0 0 16px;padding:12px 14px;border:2px solid #334155;background:#f1f5f9;color:#0f172a;border-radius:12px;font-size:.96rem}
.aff-disclosure strong{color:#0f172a}

.aff-sub{margin:10px 0 6px;font-size:1.2rem;color:#334155}
.aff-hr{border:0;border-top:1px solid #e5e7eb;margin:16px 0}

.aff-cta-row{display:flex;flex-wrap:wrap;gap:12px;justify-content:center;margin:18px auto}
.aff-btn{display:inline-flex;align-items:center;justify-content:center;padding:14px 22px;border-radius:999px;text-decoration:none;font-weight:700;min-width:220px}
.aff-btn--primary{background:#0ea5e9;color:#fff}
.aff-btn--secondary{background:#0f172a;color:#fff}
.aff-btn:hover{transform:translateY(-1px);box-shadow:0 6px 16px rgba(0,0,0,.12)}
@media (max-width:540px){.aff-btn{width:100%}}

.aff-table{width:100%;border-collapse:collapse;margin:8px 0 14px}
.aff-table th,.aff-table td{border:1px solid #e5e7eb;padding:8px 10px;text-align:left}
.aff-table thead th{background:#f8fafc}
.aff-wrap h2{margin:18px 0 6px}
.aff-wrap h3{margin:16px 0 6px}
</style>
"""

def _cta_row(url_main:str, url_cat:str, primary_label:str, secondary_label:str)->str:
    u1=html.escape(url_main or "#")
    u2=html.escape(url_cat or "#")
    l1=html.escape(primary_label)
    l2=html.escape(secondary_label)
    return f"""
<div class="aff-cta-row">
  <a class="aff-btn aff-btn--primary" href="{u1}" target="_blank" rel="nofollow sponsored noopener" aria-label="{l1}">{l1}</a>
  <a class="aff-btn aff-btn--secondary" href="{u2}" aria-label="{l2}">{l2}</a>
</div>
""".strip()

# ====== 본문 렌더 ======
def render_affiliate_html(keyword:str, url:str, image:str="", category_name:str="쇼핑")->str:
    disc=html.escape(DISCLOSURE_TEXT)
    kw_esc=html.escape(keyword)
    cat_url=_category_url_for(category_name)
    url_alt = url  # 버튼은 2개: 제품 보기 / 쇼핑 글 모아보기

    # 서브제목 & 요약
    subtitle=f"{kw_esc} 한 눈에 보기"
    summary=(
        f"{kw_esc}를 중심으로 핵심만 간단히 정리했어요. 과장 없이 실제 사용 맥락을 바탕으로 선택 기준과 활용 팁을 담았습니다. "
        f"읽고 바로 비교·결정할 수 있도록 요약-분석-가격/가성비-장단점-추천 순서로 구성했습니다."
    )

    # 가격/가성비 표(3x3)
    table_html=f"""
<table class="aff-table">
  <thead><tr><th>항목</th><th>확인 포인트</th><th>비고</th></tr></thead>
  <tbody>
    <tr><td>성능</td><td>공간/목적 대비 충분한지</td><td>필요 이상 과투자 방지</td></tr>
    <tr><td>관리</td><td>세척·보관·소모품</td><td>난도/주기 체크</td></tr>
    <tr><td>비용</td><td>구매가 + 유지비</td><td>시즌 특가/묶음 혜택</td></tr>
  </tbody>
</table>
""".strip()

    # 이미지(선택)
    img_html=""
    if image and USE_IMAGE:
        img_html=f'<figure style="margin:0 0 18px"><img src="{html.escape(image)}" alt="{kw_esc}" loading="lazy" decoding="async" style="max-width:100%;height:auto;border-radius:12px"></figure>'

    # 중간 광고
    mid_ads=_adsense_block()

    body=f"""
{_css_block()}
<div class="aff-wrap">
  <p class="aff-disclosure"><strong>{disc}</strong></p>
  {_adsense_block()}
  {img_html}

  <h2 class="aff-sub">{subtitle}</h2>
  <p>{summary}</p>
  <hr class="aff-hr">

  {_cta_row(url, _category_url_for(category_name), BUTTON_PRIMARY, BUTTON_SECONDARY)}

  <h3>왜 이 제품을 찾게 되었나</h3>
  <p>생활 동선에서 자잘한 불편이 반복될 때 가장 먼저 손이 가는 도구가 됩니다. {kw_esc}도 마찬가지예요. 사용 환경을 먼저 정리하면 스펙을 과감하게 덜어낼 수 있고, 핵심은 오히려 또렷해집니다.</p>
  <hr class="aff-hr">

  <h3>핵심 기능만 딱 추리기</h3>
  <p>모든 기능을 챙기기보다 자주 쓰는 두세 가지만 선명하게. 전원 방식, 휴대성, 활용 모드처럼 “매일 만지는 요소”가 사용자 경험을 좌우합니다.</p>
  <hr class="aff-hr">

  <h3>선택 기준 3가지</h3>
  <p>공간/목적, 관리 난도, 총비용. 이 세 가지 기준을 표로 정리해 두면 다른 모델과도 바로 비교가 됩니다.</p>
  {table_html}
  <hr class="aff-hr">

  <h3>실전 사용 팁</h3>
  <p>환경 소음·바람길·전원 위치 같은 사소한 변수만 다듬어도 체감 만족도가 크게 달라집니다. 기본은 가볍게, 필요할 때만 모드를 올리세요.</p>
  <hr class="aff-hr">

  {mid_ads}

  <h3>장점</h3>
  <p>간편한 접근성, 부담 없는 유지비, 상황별 확장성. 한 번 익숙해지면 밖에서도 같은 사용 리듬을 이어가기 쉬워집니다.</p>
  <hr class="aff-hr">

  <h3>단점</h3>
  <p>배터리·소모품 주기, 상위급 대비 세밀한 성능 한계. 사용 목적을 확실히 좁히면 체감되는 단점은 줄어듭니다.</p>
  <hr class="aff-hr">

  <h3>이런 분께 추천</h3>
  <p>여행·서브·선물용으로 무난한 선택지를 찾는 분, 가볍게 시작해 보고 필요하면 단계 업그레이드를 생각하는 분께 특히 잘 맞습니다.</p>

  {_cta_row(url_alt, cat_url, BUTTON_PRIMARY, BUTTON_SECONDARY)}
</div>
""".strip()

    return body

# ====== 회전 & 실행 ======
def rotate_sources(kw:str):
    changed=False
    if _consume_col_csv("golden_shopping_keywords.csv",kw):
        print(f"[ROTATE] removed '{kw}' from golden_shopping_keywords.csv"); changed=True
    if _consume_col_csv("keywords_shopping.csv",kw):
        print(f"[ROTATE] removed '{kw}' from keywords_shopping.csv"); changed=True
    if not changed:
        print("[ROTATE] nothing removed (maybe already rotated)")

def run_once():
    print(f"[USAGE] NO_REPEAT_TODAY={NO_REPEAT_TODAY}, AFF_USED_BLOCK_DAYS={AFF_USED_BLOCK_DAYS}")
    kw = pick_affiliate_keyword()
    url = resolve_product_url(kw)
    when_gmt = _slot_affiliate()
    title = build_title(kw)
    body = render_affiliate_html(kw, url, image="", category_name=DEFAULT_CATEGORY)
    res = post_wp(title, body, when_gmt, category=DEFAULT_CATEGORY, tag=kw)
    print(json.dumps({"post_id":res.get("id") or 0,"link":res.get("link"),"status":res.get("status"),
                      "date_gmt":res.get("date_gmt"),"title":title,"keyword":kw}, ensure_ascii=False))
    _mark_used(kw)
    rotate_sources(kw)

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    run_once()

if __name__=="__main__":
    main()
