# auto_wp_gpt.py  — 2025-09-11
# 핵심 변경
# - choose_category(): 키워드가 뉴스 톤이면 '뉴스'로, 아니면 DEFAULT_CATEGORY로
# - CTA 버튼 URL을 '해당 글에 실제로 지정되는 카테고리' 링크로 생성
# - --category 옵션으로 강제 카테고리 지정 가능
# - 로그에 [CTA] category=..., category_url=... 출력

import os, re, sys, csv, html
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Tuple
import requests
from dotenv import load_dotenv
from openai import OpenAI, BadRequestError
load_dotenv()

# ===== ENV =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"

OPENAI_API_KEY=os.getenv("OPENAI_API_KEY") or None
OPENAI_MODEL=os.getenv("OPENAI_MODEL_LONG") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()

DEFAULT_CATEGORY=os.getenv("DEFAULT_CATEGORY") or "정보"
USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-general/1.4"
USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_FILE=os.path.join(USAGE_DIR,"used_general.txt")

_oai = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
REQ_HEADERS={"User-Agent":USER_AGENT,"Accept":"application/json","Content-Type":"application/json; charset=utf-8"}

def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))

def _wp_future_exists_around(when_gmt_dt: datetime, tol_min: int = 2) -> bool:
    try:
        r=requests.get(f"{WP_URL}/wp-json/wp/v2/posts",
            params={"status":"future","per_page":100,"orderby":"date","order":"asc","context":"edit"},
            headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20)
        r.raise_for_status()
        items=r.json()
    except Exception:
        return False
    tgt=when_gmt_dt.astimezone(timezone.utc); delta=timedelta(minutes=max(1,int(tol_min)))
    lo,hi=tgt-delta,tgt+delta
    for it in items:
        dstr=(it.get("date_gmt") or "").strip()
        if not dstr: continue
        try:
            dt=datetime.fromisoformat(dstr.replace("Z","+00:00"))
            if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
            else: dt=dt.astimezone(timezone.utc)
        except Exception:
            continue
        if lo<=dt<=hi: return True
    return False

def _slot_or_next_day(h:int,m:int=0)->str:
    now=_now_kst()
    target=now.replace(hour=h,minute=m,second=0,microsecond=0)
    if target<=now: target+=timedelta(days=1)
    for _ in range(7):
        if _wp_future_exists_around(target.astimezone(timezone.utc),2):
            print(f"[SLOT] conflict at {target.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')}Z -> +1d")
            target+=timedelta(days=1); continue
        break
    final=target.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[SLOT] scheduled UTC = {final}")
    return final

# === shopping-like 필터(일상글에서 제외) ===
SHOPPING_WORDS=set("추천 리뷰 후기 가격 최저가 세일 특가 쇼핑 쿠폰 할인 핫딜 언박싱 스펙 구매 배송".split())
def is_shopping_like(kw:str)->bool:
    k=kw or ""
    if any(w in k for w in SHOPPING_WORDS): return True
    if re.search(r"[A-Za-z]+[\-\s]?\d{2,}",k): return True
    if re.search(r"(구매|판매|가격|최저가|할인|특가|딜|프로모션|쿠폰|배송)",k): return True
    return False

# === CSV ===
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
    with open(path,"r",encoding="utf-8",newline="") as f: rows=list(csv.reader(f))
    if not rows: return False
    has_header=rows[0] and rows[0][0].strip().lower() in ("keyword","title")
    body=rows[1:] if has_header else rows[:]
    before=len(body)
    body=[r for r in body if (r and r[0].strip()!=kw)]
    if len(body)==before: return False
    new_rows=([rows[0]] if has_header else [])+[[r[0].strip()] for r in body]
    with open(path,"w",encoding="utf-8",newline="") as f: csv.writer(f).writerows(new_rows)
    print(f"[GENERAL] consumed '{kw}' from {path}"); return True

def _consume_line_csv(path:str, kw:str)->bool:
    if not os.path.exists(path): return False
    with open(path,"r",encoding="utf-8") as f: toks=[x.strip() for x in f.readline().split(",") if x.strip()]
    if kw not in toks: return False
    toks=[t for t in toks if t!=kw]
    with open(path,"w",encoding="utf-8") as f: f.write(",".join(toks))
    print(f"[GENERAL] consumed '{kw}' from {path}"); return True

def _consume_from_sources(kw:str):
    if _consume_col_csv("keywords_general.csv",kw): return
    if _consume_line_csv("keywords.csv",kw): return

# === USED LOG ===
def _ensure_usage_dir(): os.makedirs(USAGE_DIR, exist_ok=True)

def _load_used_set(days:int=30)->set:
    _ensure_usage_dir()
    if not os.path.exists(USED_FILE): return set()
    cutoff=datetime.utcnow().date()-timedelta(days=days); used=set()
    with open(USED_FILE,"r",encoding="utf-8",errors="ignore") as f:
        for line in f:
            line=line.strip()
            if not line: continue
            try:
                d_str, kw = line.split("\t",1)
                if datetime.strptime(d_str,"%Y-%m-%d").date()>=cutoff: used.add(kw.strip())
            except Exception:
                used.add(line)
    return used

def _mark_used(kw:str):
    _ensure_usage_dir()
    with open(USED_FILE,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw.strip()}\n")

# === WP taxonomy ===
def _ensure_term(kind:str, name:str)->int:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}", params={"search":name,"per_page":50,"context":"edit"},
                   auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name: return int(it["id"])
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status(); return int(r.json()["id"])

def _category_link(category_name:str)->str:
    try:
        cat_id=_ensure_term("categories", category_name or "정보")
        r=requests.get(f"{WP_URL}/wp-json/wp/v2/categories/{cat_id}",
                       auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
        r.raise_for_status()
        slug=(r.json().get("slug") or "").strip()
    except Exception as e:
        print(f"[WP][WARN] category slug fetch failed: {type(e).__name__}: {e}")
        slug=""
    base=WP_URL.rstrip("/")
    return f"{base}/category/{slug}/" if slug else f"{base}/category/{category_name}/"

# === 카테고리 자동 판별 ===
def choose_category(kw:str)->str:
    forced=(os.getenv("GENERAL_CATEGORY") or "").strip()
    if forced: return forced
    # 뉴스 냄새(대괄호/브리핑/투데이/속보 등)면 '뉴스'
    if re.search(r"(브리핑|속보|단독|리포트|투데이|헤드라인|발표|공식|출시|이슈|뉴스|\])", kw):
        return "뉴스"
    return DEFAULT_CATEGORY

# === OpenAI helpers ===
def _ask_chat(model, system, user, max_tokens, temperature):
    if not _oai: return ""
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
        return (txt or "").strip()
    except Exception as e:
        print(f"[OPENAI][WARN] {type(e).__name__}: {e}"); return ""

BANNED_TITLE=["브리핑","정리","알아보기","대해 알아보기","에 대해 알아보기","해야 할 것","해야할 것","가이드"]
def _bad_title(t:str)->bool:
    t=t.strip(); return any(p in t for p in BANNED_TITLE) or not (14<=len(t)<=26)

def _normalize_title(raw:str)->str:
    s=html.unescape(raw or ""); s=re.sub(r"<[^>]+>","",s)
    s=s.replace("&039;","'").replace("&quot;","\""); s=re.sub(r"\s+"," ",s).strip(" \"'“”‘’")
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
        if cand and not _bad_title(cand): title=cand; break
    if not title:
        title=_normalize_title(f"{kw}, 오늘 시야가 넓어지는 순간")
        if _bad_title(title): title="오늘, 시야가 넓어지는 순간"
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
.cta-wrap{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}
.cta-btn{display:inline-block;padding:10px 14px;border-radius:10px;background:#111;color:#fff;text-decoration:none;font-weight:700}
.cta-btn:hover{opacity:.9}
.ad-wrap{margin:16px 0}
.summary{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;padding:12px 14px;margin:12px 0}
</style>
"""

def _ads_block()->str:
    return """
<div class="ad-wrap">
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-7409421510734308" crossorigin="anonymous"></script>
<!-- 25.06.03 -->
<ins class="adsbygoogle" style="display:block" data-ad-client="ca-pub-7409421510734308" data-ad-slot="9228101213" data-ad-format="auto" data-full-width-responsive="true"></ins>
<script>(adsbygoogle = window.adsbygoogle || []).push({});</script>
</div>
""".strip()

def _cta_btn(url:str, text:str="관련 글 더 보기")->str:
    u=html.escape(url, True); t=html.escape(text)
    return f'<a class="cta-btn" href="{u}">{t}</a>'

def _split_body_into_two(html_body:str)->Tuple[str,str]:
    parts=re.split(r'(?i)(</p>|</li>|</h2>|</h3>)', html_body)
    if len(parts)<4:
        mid=max(1,len(parts)//2)
        return "".join(parts[:mid]),"".join(parts[mid:])
    blocks=[]
    for i in range(0,len(parts),2):
        blk=parts[i]; 
        if i+1<len(parts): blk+=parts[i+1]
        blocks.append(blk)
    total=sum(len(b) for b in blocks); acc=0; cut_idx=1
    for i,b in enumerate(blocks):
        acc+=len(b)
        if acc/total>=0.4: cut_idx=i+1; break
    return "".join(blocks[:cut_idx]),"".join(blocks[cut_idx:])

def gen_body_info(kw:str, category_url:str)->str:
    ad_top=_ads_block()
    btn_top=_cta_btn(category_url,"관련 글 더 보기")
    btn_mid=_cta_btn(category_url,"지금 더 읽기")

    if not _oai:
        summary=f"<div class='summary'><strong>한눈 요약:</strong> {html.escape(kw)} 핵심만 간단히 정리.</div>"
        body1="<p>왜 지금 중요한지, 어떤 변화가 있는지 한 문단으로 짚습니다.</p>"
        body2="<h2>조금 더 깊게</h2><p>배경과 맥락, 사례 비교를 덧붙여 이해를 확장합니다.</p>"
        return _css_block()+f'<div class="post-info">{ad_top}{summary}<div class="cta-wrap">{btn_top}</div>{body1}<div class="cta-wrap">{btn_mid}</div>{_ads_block()}{body2}</div>'

    sys_p="너는 사람스러운 한국어 칼럼니스트다. 광고/구매 표현 없이 지식형 글을 쓴다."
    usr=f"""주제: {kw}
스타일: 정의 → 배경/원리 → 실제 영향/사례 → 관련 연구/수치(개념적) → 비교/표 1개 → 적용 팁 → 정리
요건:
- 도입부 2~3문장 훅
- 소제목 <h2>/<h3> 사용, 불릿/표 포함
- 상업 단어 금지
- 분량: 1000~1200자
- 출력: 순수 HTML만"""
    body=_ask_chat(OPENAI_MODEL, sys_p, usr, max_tokens=950, temperature=0.8) or ""
    body=strip_code_fences(body)
    sum_sys="너는 요약가다. 아래 글의 핵심을 2문장으로 요약하라."
    summary_txt=_ask_chat(OPENAI_MODEL, sum_sys, body, max_tokens=80, temperature=0.4) or "핵심만 간단히 정리했습니다."
    summary_html=f"<div class='summary'><strong>한눈 요약:</strong> {html.escape(summary_txt)}</div>"
    left,right=_split_body_into_two(body)
    return _css_block()+f'<div class="post-info">{ad_top}{summary_html}<div class="cta-wrap">{btn_top}</div>{left}<div class="cta-wrap">{btn_mid}</div>{_ads_block()}{right}</div>'

def post_wp(title:str, html_body:str, when_gmt:str, category:str="정보", tag:str="")->dict:
    cat_id=_ensure_term("categories", category or "정보")
    tag_ids=[]
    if tag:
        try: tag_ids=[_ensure_term("tags", tag)]
        except Exception: pass
    payload={"title":title,"content":html_body,"status":POST_STATUS,"categories":[cat_id],
             "tags":tag_ids,"comment_status":"closed","ping_status":"closed","date_gmt":when_gmt}
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status(); return r.json()

# === 메인 ===
def pick_daily_keywords(n:int=2)->List[str]:
    used=_load_used_set(30); out=[]
    arr1=[k for k in _read_col_csv("keywords_general.csv") if k and (k not in used) and not is_shopping_like(k)]
    arr2=[k for k in _read_line("keywords.csv")       if k and (k not in used) and not is_shopping_like(k)]
    for pool in (arr1,arr2):
        for k in pool:
            out.append(k); 
            if len(out)>=n: break
        if len(out)>=n: break
    while len(out)<n:
        stamp=datetime.utcnow().strftime("%Y%m%d")
        out.append(f"오늘의 작은 통찰 {stamp}-{len(out)}")
    print(f"[GENERAL] picked: {out}")
    return out[:n]

def run_two_posts(force_category:str|None=None):
    kws=pick_daily_keywords(2)
    times=[(10,0),(17,0)]
    for idx,(kw,(h,m)) in enumerate(zip(kws,times)):
        category = force_category or choose_category(kw)
        cta_url  = _category_link(category)
        print(f"[CTA] category='{category}', category_url={cta_url}")
        title    = hook_title(kw)
        html_b   = gen_body_info(kw, cta_url)
        link     = post_wp(title, html_b, _slot_or_next_day(h,m), category=category, tag=kw).get("link")
        print(f"[OK] scheduled ({idx}) '{title}' -> {link}")
        _mark_used(kw); _consume_from_sources(kw)

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    import argparse
    ap=argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["two-posts"], default="two-posts")
    ap.add_argument("--category", help="모드 전체에 강제 카테고리(예: 뉴스)")
    args=ap.parse_args()
    if args.mode=="two-posts": run_two_posts(force_category=args.category)

if __name__=="__main__":
    sys.exit(main())
