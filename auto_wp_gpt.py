# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상글 2건 예약(10:00 / 17:00 KST)

업데이트 사항(일상글 템플릿/광고 최적화):
- 구조: [내부광고] → [요약글] → [버튼] → [본문1(짧게)] → [버튼] → [내부광고] → [본문2(나머지)]
- 썸네일은 사용하지 않음(완전 스킵)
- 상단/중간 AdSense 블록 자동 삽입(요청 코드 기본값, 단 AD_METHOD=shortcode 이면 AD_SHORTCODE 사용)
- CTA 버튼(상단/중간) 자동 삽입: 기본은 사이트 내 검색(/?s=키워드)로 내부 회유
- golden_keywords.csv 우선 사용(+소스 소비), 30일 내 사용 회피
- 제목 후킹/정규화, 쇼핑어 필터, WP 예약 슬롯 충돌 시 최대 7일 이월 유지
"""
import os, re, sys, csv, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Tuple
import requests
from dotenv import load_dotenv
from slugify import slugify
load_dotenv()

# === OpenAI (옵션) ===
from openai import OpenAI, BadRequestError
_oai = OpenAI(api_key=os.getenv("OPENAI_API_KEY") or None)

# ===== ENV =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
OPENAI_MODEL=os.getenv("OPENAI_MODEL_LONG") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()
MAX_TOKENS_BODY=int(os.getenv("MAX_TOKENS_BODY") or 900)

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-general/1.4"
USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_FILE=os.path.join(USAGE_DIR,"used_general.txt")

# AdSense / 버튼
AD_METHOD=(os.getenv("AD_METHOD") or "").strip().lower()          # "shortcode" 면 AD_SHORTCODE 사용
AD_SHORTCODE=(os.getenv("AD_SHORTCODE") or "").strip()
BUTTON_TEXT=(os.getenv("BUTTON_TEXT") or "관련 글 모아보기").strip()

REQ_HEADERS={
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ===== TIME =====
def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

# --- 견고한 예약 충돌 감지(UTC date_gmt 직접 비교) ---
def _wp_future_exists_around(when_gmt_dt: datetime, tol_min: int = 2) -> bool:
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    try:
        r = requests.get(
            url,
            params={"status":"future","per_page":100,"orderby":"date","order":"asc","context":"edit"},
            headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20,
        )
        r.raise_for_status()
        items = r.json()
    except Exception as e:
        print(f"[WP][WARN] future list fetch failed: {type(e).__name__}: {e}")
        return False

    tgt = when_gmt_dt.astimezone(timezone.utc)
    delta = timedelta(minutes=max(1, int(tol_min)))
    lo, hi = tgt - delta, tgt + delta
    for it in items:
        dstr = (it.get("date_gmt") or "").strip()
        if not dstr: 
            continue
        try:
            dt = datetime.fromisoformat(dstr.replace("Z","+00:00"))
            if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
            else: dt = dt.astimezone(timezone.utc)
        except Exception:
            continue
        if lo <= dt <= hi:
            return True
    return False

def _slot_or_next_day(h:int, m:int=0)->str:
    """
    - 오늘 KST의 (h:m) 기준:
      1) 과거면 +1일
      2) 충돌 시 하루씩 밀기(최대 7일)
    - 반환: UTC ISO8601 (YYYY-MM-DDTHH:MM:SS)
    """
    now=_now_kst()
    target_kst = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target_kst <= now:
        target_kst += timedelta(days=1)

    for _ in range(7):
        when_gmt_dt = target_kst.astimezone(timezone.utc)
        if _wp_future_exists_around(when_gmt_dt, tol_min=2):
            print(f"[SLOT] conflict at {when_gmt_dt.strftime('%Y-%m-%dT%H:%M:%S')}Z -> +1d")
            target_kst += timedelta(days=1)
            continue
        break

    final = target_kst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[SLOT] scheduled UTC = {final}")
    return final

# ===== SHOPPING FILTER =====
SHOPPING_WORDS=set("추천 리뷰 후기 가격 최저가 세일 특가 쇼핑 쿠폰 할인 핫딜 언박싱 스펙 구매 배송".split())
def is_shopping_like(kw:str)->bool:
    k=kw or ""
    if any(w in k for w in SHOPPING_WORDS): return True
    if re.search(r"[A-Za-z]+[\-\s]?\d{2,}",k): return True
    if re.search(r"(구매|판매|가격|최저가|할인|특가|딜|프로모션|쿠폰|배송)",k): return True
    return False

# ===== CSV IO =====
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

def _read_line(path:str)->List[str]:
    if not os.path.exists(path): return []
    with open(path,"r",encoding="utf-8") as f:
        return [x.strip() for x in f.readline().split(",") if x.strip()]

def _consume_col_csv(path:str, kw:str)->bool:
    if not os.path.exists(path): return False
    with open(path,"r",encoding="utf-8",newline="") as f:
        rows=list(csv.reader(f))
    if not rows: return False
    has_header=rows[0] and rows[0][0].strip().lower() in ("keyword","title")
    body=rows[1:] if has_header else rows[:]
    before=len(body)
    body=[r for r in body if (r and r[0].strip()!=kw)]
    if len(body)==before: return False
    new_rows=([rows[0]] if has_header else [])+[[r[0].strip()] for r in body]
    with open(path,"w",encoding="utf-8",newline="") as f:
        csv.writer(f).writerows(new_rows)
    print(f"[GENERAL] consumed '{kw}' from {path}")
    return True

def _consume_line_csv(path:str, kw:str)->bool:
    if not os.path.exists(path): return False
    with open(path,"r",encoding="utf-8") as f:
        toks=[x.strip() for x in f.readline().split(",") if x.strip()]
    if kw not in toks: return False
    toks=[t for t in toks if t!=kw]
    with open(path,"w",encoding="utf-8") as f:
        f.write(",".join(toks))
    print(f"[GENERAL] consumed '{kw}' from {path}")
    return True

def _consume_from_sources(kw:str):
    # golden 우선 소비
    if _consume_col_csv("golden_keywords.csv",kw): return
    if _consume_col_csv("keywords_general.csv",kw): return
    if _consume_line_csv("keywords.csv",kw): return

# ===== USED LOG =====
def _ensure_usage_dir(): os.makedirs(USAGE_DIR, exist_ok=True)

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
                d_str, kw = line.split("\t",1)
                if datetime.strptime(d_str,"%Y-%m-%d").date()>=cutoff:
                    used.add(kw.strip())
            except Exception:
                used.add(line)
    return used

def _mark_used(kw:str):
    _ensure_usage_dir()
    with open(USED_FILE,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw.strip()}\n")

# ===== KEYWORDS =====
def pick_daily_keywords(n:int=2)->List[str]:
    used=_load_used_set(30)
    out=[]
    # 0) golden_keywords.csv (열)
    arr0=[k for k in _read_col_csv("golden_keywords.csv") if k and (k not in used) and not is_shopping_like(k)]
    # 1) keywords_general.csv (열)
    arr1=[k for k in _read_col_csv("keywords_general.csv") if k and (k not in used) and not is_shopping_like(k)]
    # 2) keywords.csv (한 줄)
    arr2=[k for k in _read_line("keywords.csv") if k and (k not in used) and not is_shopping_like(k)]

    for pool in (arr0, arr1, arr2):
        for k in pool:
            out.append(k)
            if len(out)>=n: break
        if len(out)>=n: break

    # 3) 부족하면 안전한 기본 키워드(폴백)
    i=0
    bases=["오늘의 작은 통찰","생각이 자라는 순간","일상을 바꾸는 관찰","시야가 넓어지는 한 줄"]
    while len(out)<n:
        stamp=datetime.utcnow().strftime("%Y%m%d")
        out.append(f"{bases[i%len(bases)]} {stamp}-{i}")
        i+=1
    print(f"[GENERAL] picked: {out}")
    return out[:n]

# ===== OpenAI helper =====
def _ask_chat(model, system, user, max_tokens, temperature):
    if _oai.api_key is None:
        return ""
    try:
        r=_oai.chat.completions.create(
            model=model,
            messages=[{"role":"system","content":system},{"role":"user","content":user}],
            temperature=temperature, max_tokens=max_tokens
        )
        return (r.choices[0].message.content or "").strip()
    except BadRequestError:
        rr=_oai.responses.create(model=model, input=f"[시스템]\n{system}\n\n[사용자]\n{user}", max_output_tokens=max_tokens)
        txt=getattr(rr,"output_text",None)
        if isinstance(txt,str) and txt.strip(): return txt.strip()
        return ""
    except Exception as e:
        print(f"[OPENAI][WARN] {type(e).__name__}: {e}")
        return ""

# ===== VIEW HELPERS (광고/버튼/CSS) =====
def _adsense_block()->str:
    """AD_METHOD=shortcode + AD_SHORTCODE 우선, 아니면 지정된 스크립트 블록 사용"""
    if AD_METHOD=="shortcode" and AD_SHORTCODE:
        return AD_SHORTCODE
    # 기본값(사용자 제공 코드)
    return (
        '<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-7409421510734308" crossorigin="anonymous"></script>\n'
        '<!-- 25.06.03 -->\n'
        '<ins class="adsbygoogle" style="display:block" data-ad-client="ca-pub-7409421510734308" data-ad-slot="9228101213" data-ad-format="auto" data-full-width-responsive="true"></ins>\n'
        '<script>(adsbygoogle = window.adsbygoogle || []).push({});</script>'
    )

def _cta_button(kw:str, text:str|None=None)->str:
    """내부 회유용 CTA: 기본은 사이트 검색(/?s=키워드)"""
    label = (text or BUTTON_TEXT or "관련 글 모아보기").strip()
    base = WP_URL or ""
    href = f"{base}/?s="+requests.utils.quote(kw) if base else "#"
    return f'<div class="cta-wrap"><a class="btn-cta" href="{href}" rel="noopener">{html.escape(label)}</a></div>'

def _css_block()->str:
    # 테마와 충돌 최소화 + 버튼/광고 마진
    return """
<style>
.post-info p{line-height:1.86;margin:0 0 14px;color:#1f2937}
.post-info h2{margin:28px 0 12px;font-size:1.42rem;line-height:1.35;border-left:6px solid #22c55e;padding-left:10px}
.post-info h3{margin:22px 0 10px;font-size:1.12rem;color:#0f172a}
.post-info ul{padding-left:22px;margin:10px 0}
.post-info li{margin:6px 0}
.post-info table{border-collapse:collapse;width:100%;margin:16px 0}
.post-info thead th{background:#f1f5f9}
.post-info th,.post-info td{border:1px solid #e2e8f0;padding:8px 10px;text-align:left}
.post-info .ad-slot{margin:18px 0}
.post-info .summary{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px;margin:12px 0}
.post-info .cta-wrap{text-align:center;margin:14px 0}
.post-info .btn-cta{display:inline-block;padding:12px 18px;border-radius:999px;background:#0ea5e9;color:#fff;text-decoration:none;font-weight:700}
.post-info .btn-cta:hover{filter:brightness(0.95)}
.post-info .muted{color:#475569}
</style>
"""

# ===== TITLE =====
BANNED_TITLE=["브리핑","정리","알아보기","대해 알아보기","에 대해 알아보기","해야 할 것","해야할 것","해야할것","가이드"]
def _bad_title(t:str)->bool:
    t=t.strip()
    return any(p in t for p in BANNED_TITLE) or not (14<=len(t)<=26)

def _normalize_title(raw:str)->str:
    s=html.unescape(raw or "")
    s=re.sub(r"<[^>]+>","",s)
    s=s.replace("&039;","'").replace("&quot;","\"")
    s=re.sub(r"\s+"," ",s).strip(" \"'“”‘’")
    return s

def hook_title(kw:str)->str:
    sys_p="너는 한국어 카피라이터다. 클릭을 부르는 짧고 강한 제목만 출력."
    usr=f"""키워드: {kw}
조건:
- 14~26자
- 금지어: {", ".join(BANNED_TITLE)}
- 쇼핑/구매/할인/최저가/쿠폰/딜/가격 단어 금지
- '리뷰/가이드/사용기' 표지어 지양
- 출력은 제목 1줄(순수 텍스트)"""
    title=""
    for _ in range(3):
        cand=_ask_chat(OPENAI_MODEL, sys_p, usr, max_tokens=60, temperature=0.9) or ""
        cand=_normalize_title(cand)
        if cand and not _bad_title(cand):
            title=cand; break
    if not title:
        title=_normalize_title(f"{kw}, 오늘 시야가 넓어지는 순간")
        if _bad_title(title):
            title="오늘, 시야가 넓어지는 순간"
    return title

def strip_code_fences(s:str)->str:
    return re.sub(r"```(?:\w+)?","",s).replace("```","").strip().strip("“”\"'")

# ===== BODY (일상글 템플릿) =====
def _split_sections(raw:str)->Tuple[str,str,str]:
    """
    모델 출력에서 <!--SUMMARY-->, <!--BODY1-->, <!--BODY2--> 마커로 3개 섹션 분리.
    실패 시 안전한 폴백 본문 생성.
    """
    text = strip_code_fences(raw or "")
    m1=re.search(r"<!--SUMMARY-->(.*?)<!--BODY1-->", text, flags=re.DOTALL)
    m2=re.search(r"<!--BODY1-->(.*?)<!--BODY2-->", text, flags=re.DOTALL)
    m3=re.search(r"<!--BODY2-->(.*)$",             text, flags=re.DOTALL)
    if m1 and m2 and m3:
        return m1.group(1).strip(), m2.group(1).strip(), m3.group(1).strip()

    # 폴백
    sum_html="<ul><li>핵심만 빠르게 이해</li><li>실전 적용 포인트 3가지</li><li>흔한 실수와 피하는 법</li></ul>"
    body1="<p>핵심만 먼저 짧게 짚습니다. 오늘 바로 적용해 볼 수 있는 한 가지를 골라 실행해 보세요. 반복이 힘이고, 작은 변화가 가장 큰 결과를 만듭니다.</p>"
    body2=(
        "<h2>맥락과 원리</h2><p>주제를 이해하려면 배경과 원리를 간단히 짚는 것이 도움이 됩니다. "
        "핵심 변수와 상호작용을 파악하고, 내 상황에 맞게 최소 단위부터 시도해 보세요.</p>"
        "<h2>실전 팁</h2><ul><li>작게 시작해서 빠르게 피드백 받기</li><li>기록으로 패턴 찾기</li>"
        "<li>한 번에 하나만 바꾸기</li></ul><p class='muted'>정답보다 적합함을 찾는 과정이 더 중요합니다.</p>"
    )
    return sum_html, body1, body2

def gen_body_info(kw:str)->str:
    # --- LLM 사용 경로 ---
    if _oai.api_key is not None:
        sys_p = (
            "너는 사람스러운 한국어 칼럼니스트다. 광고/구매 표현 없이 지식형 글을 쓴다. "
            "출력은 반드시 HTML 조각만 포함하고, 스크립트/스타일은 포함하지 않는다."
        )
        usr = f"""주제: {kw}
요건:
- 3개 섹션을 '정확히' 이 순서/마커로 출력:
  <!--SUMMARY-->
  (요약: 3~5개 불릿, <ul><li> 사용, 문장형)
  <!--BODY1-->
  (본문1: 180~260자, 2~4문장, 도입 훅)
  <!--BODY2-->
  (본문2: 700~1000자, 소제목 <h2>/<h3> 포함, 불릿/표 중 1개 포함 가능)
- 쇼핑/구매/가격/할인 등의 상업 단어 금지
- 표가 있으면 2~3행의 간단 표만
- 오직 HTML만 반환(마커 포함)"""
        raw=_ask_chat(OPENAI_MODEL, sys_p, usr, max_tokens=MAX_TOKENS_BODY, temperature=0.8)
        s_sum, s_b1, s_b2 = _split_sections(raw or "")
    else:
        # --- 키 없이도 안정 출력 ---
        s_sum, s_b1, s_b2 = _split_sections("")

    # --- 조립(광고/버튼 포함) ---
    parts = [
        _css_block(),
        '<div class="post-info">',
        # 1) 내부광고(상단)
        '<div class="ad-slot">'+_adsense_block()+'</div>',
        # 2) 요약글
        '<div class="summary">'+s_sum+'</div>',
        # 3) 버튼(상단 CTA)
        _cta_button(kw),
        # 4) 본문1 (짧게)
        '<div class="body-short">'+s_b1+'</div>',
        # 5) 썸네일: 사용하지 않음 (스킵)
        # 6) 버튼(중간 CTA)
        _cta_button(kw),
        # 7) 내부광고(중간)
        '<div class="ad-slot">'+_adsense_block()+'</div>',
        # 8) 본문2 (나머지)
        '<div class="body-long">'+s_b2+'</div>',
        '</div>'
    ]
    return "\n".join(parts)

# ===== WP =====
def _ensure_term(kind:str, name:str)->Tuple[int,str]:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}", params={"search":name,"per_page":50,"context":"edit"},
                   auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name:
            return int(it["id"]), (it.get("slug") or "")
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status(); j=r.json()
    return int(j["id"]), (j.get("slug") or "")

def post_wp(title:str, html_body:str, when_gmt:str, category:str="정보", tag:str="")->dict:
    cat_id,_=_ensure_term("categories", category or "정보")
    tag_ids=[]
    if tag:
        try:
            tid,_=_ensure_term("tags", tag); tag_ids=[tid]
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

# ===== RUNNERS =====
def run_two_posts():
    kws=pick_daily_keywords(2)
    times=[(10,0),(17,0)]
    for idx,(kw,(h,m)) in enumerate(zip(kws,times)):
        title=hook_title(kw)
        html_body=gen_body_info(kw)
        link=post_wp(title, html_body, _slot_or_next_day(h,m), category="정보", tag=kw).get("link")
        print(f"[OK] scheduled ({idx}) '{title}' -> {link}")
        _mark_used(kw)
        _consume_from_sources(kw)

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["two-posts"], default="two-posts")
    args=ap.parse_args()
    if args.mode=="two-posts": run_two_posts()

if __name__=="__main__":
    sys.exit(main())
