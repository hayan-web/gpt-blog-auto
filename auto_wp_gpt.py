# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상글 2건 예약(10:00 / 17:00 KST)
- keywords_general.csv(열 CSV) 우선, 부족하면 keywords.csv(한 줄)에서 쇼핑스멜 제거
- 후킹형 제목(HTML 엔티티/태그 제거 후 정규화), 정보형 본문(+CSS), 쇼핑 단어 금지
- 최근 30일 사용 키워드 회피 + 성공 시 사용 기록(.usage/used_general.txt)
- 성공 후 소스 CSV에서 해당 키워드 즉시 제거(폐기)
- WP 예약 슬롯 충돌 시 다음날로 이월(최대 7일 재시도)  ← 패치
"""
import os, re, sys, csv, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List
import requests
from dotenv import load_dotenv
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

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-general/1.3"
USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_FILE=os.path.join(USAGE_DIR,"used_general.txt")

REQ_HEADERS={
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ===== TIME =====
def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

# --- 패치: after/before 사용 안 하고, date_gmt(UTC) 직접 비교 ---
def _wp_future_exists_around(when_gmt_dt: datetime, tol_min: int = 2) -> bool:
    """
    워드프레스 예약글(status=future)을 넉넉히 조회한 뒤,
    UTC date_gmt를 기준으로 ±tol_min 분 내 충돌 여부 판단.
    타임존 혼선을 피하기 위해 after/before 파라미터는 사용하지 않는다.
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
                "context": "edit",
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
        # 조회 실패면 보수적으로 '충돌 없음' 처리 (예약을 막지 않음)
        return False

    tgt = when_gmt_dt.astimezone(timezone.utc)
    delta = timedelta(minutes=max(1, int(tol_min)))
    lo, hi = tgt - delta, tgt + delta

    for it in items:
        dstr = (it.get("date_gmt") or "").strip()  # "YYYY-MM-DDTHH:MM:SS"
        if not dstr:
            continue
        try:
            dt = datetime.fromisoformat(dstr.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                dt = dt.astimezone(timezone.utc)
        except Exception:
            continue
        if lo <= dt <= hi:
            return True
    return False

def _slot_or_next_day(h:int, m:int=0)->str:
    """
    - 오늘 KST의 (h:m)을 타깃:
      1) 과거면 +1일
      2) 동일 슬롯에 예약글 있으면 하루씩 밀기(최대 7일)
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
    # 1) keywords_general.csv (열)
    arr1=[k for k in _read_col_csv("keywords_general.csv") if k and (k not in used) and not is_shopping_like(k)]
    # 2) keywords.csv (한 줄)
    arr2=[k for k in _read_line("keywords.csv") if k and (k not in used) and not is_shopping_like(k)]
    for pool in (arr1, arr2):
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

# ===== TITLE / BODY =====
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

def _css_block()->str:
    return """
<style>
.post-info p{line-height:1.86;margin:0 0 14px;color:#222}
.post-info h2{margin:28px 0 12px;font-size:1.42rem;line-height:1.35;border-left:6px solid #22c55e;padding-left:10px}
.post-info h3{margin:22px 0 10px;font-size:1.12rem;color:#0f172a}
.post-info ul{padding-left:22px;margin:10px 0}
.post-info li{margin:6px 0}
.post-info table{border-collapse:collapse;width:100%;margin:16px 0}
.post-info thead th{background:#f1f5f9}
.post-info th,.post-info td{border:1px solid #e2e8f0;padding:8px 10px;text-align:left}
</style>
"""

def gen_body_info(kw:str)->str:
    if _oai.api_key is None:
        html_body=f"""
{_css_block()}
<div class="post-info">
  <h2>{kw} 한눈에 보기</h2>
  <p>핵심 포인트를 빠르게 이해할 수 있도록 간단히 정리했습니다. 최신 사례와 실전 팁을 바탕으로 <strong>바로 적용 가능한 포인트</strong>만 담았습니다.</p>
  <h2>핵심 요약</h2>
  <ol><li>왜 중요한가?</li><li>무엇부터 해야 하는가?</li><li>실패를 줄이는 체크리스트</li></ol>
  <h2>디테일 가이드</h2>
  <ul><li>상황별 선택 옵션</li><li>실제로 써보니 좋았던 방법</li><li>흔한 함정과 피하는 법</li></ul>
  <blockquote><p><strong>TIP</strong> : 작은 반복이 큰 차이를 만듭니다. 오늘 하나만 바로 실행해 보세요.</p></blockquote>
  <h2>정리</h2>
  <p>핵심만 빠르게 실행해 보세요. 나중에 다듬는 것보다 <strong>지금 시작</strong>이 더 중요합니다.</p>
</div>
""".strip()
        return html_body

    sys_p="너는 사람스러운 한국어 칼럼니스트다. 광고/구매 표현 없이 지식형 글을 쓴다."
    usr=f"""주제: {kw}
스타일: 정의 → 배경/원리 → 실제 영향/사례 → 관련 연구/수치(개념적) → 비교/표 1개 → 적용 팁 → 정리
요건:
- 도입부 2~3문장에 '왜 지금 이 주제가 흥미로운지' 훅
- 본문은 3~5문장 단락으로 나누고, 소제목 <h2>/<h3> 사용
- 간단한 2~3행 표 1개 (<table>)
- 불릿 <ul><li> 1세트 포함
- 상업 단어 금지
- 분량: 1000~1300자
- 출력: 순수 HTML만"""
    body=_ask_chat(OPENAI_MODEL, sys_p, usr, max_tokens=950, temperature=0.8)
    if not body:
        return gen_body_info.__wrapped__(kw)  # type: ignore
    return _css_block()+'\n<div class="post-info">\n'+strip_code_fences(body)+"\n</div>"

# ===== WP =====
def _ensure_term(kind:str, name:str)->int:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}", params={"search":name,"per_page":50,"context":"edit"},
                   auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name: return int(it["id"])
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status(); return int(r.json()["id"])

def post_wp(title:str, html_body:str, when_gmt:str, category:str="정보", tag:str="")->dict:
    cat_id=_ensure_term("categories", category or "정보")
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
