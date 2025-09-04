# auto_wp_gpt.py — humanized daily posts (2/day) + WP TLS verify toggle
# - 사람 느낌 강화 / 순수 HTML / 제목 후킹 / 10·17시 예약 중복 회피
# - DRY_RUN=true 시 워드프레스 호출 없이 콘솔만
# - NEW: WP_TLS_VERIFY 지원 (자체서명 SSL 환경 대응)

import os, re, argparse, random, datetime as dt, html, requests
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
from openai import OpenAI
from slugify import slugify

from utils_cache import cached_call
from budget_guard import log_llm, recommend_models

load_dotenv()
client = OpenAI()

# =========================
# Env
# =========================
WP_URL = os.getenv("WP_URL", "").rstrip("/")
WP_USER = os.getenv("WP_USER", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
WP_TLS_VERIFY = os.getenv("WP_TLS_VERIFY", "true").lower() != "false"  # <- NEW
POST_STATUS = os.getenv("POST_STATUS", "future")

KEYWORDS_CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# =========================
# Utils
# =========================
def kst_now(): return dt.datetime.now(ZoneInfo("Asia/Seoul"))

def approx_excerpt(body: str, n=160) -> str:
    s = body or ""
    s = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", s, flags=re.I)
    s = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:n] + "…") if len(s) > n else s

# =========================
# Body CSS + sanitizers
# =========================
STYLES_CSS = """
<style>
.gpt-article{--accent:#2563eb;--line:#e5e7eb;font-size:16px;line-height:1.85;color:#0f172a}
.gpt-article h2{font-size:1.375rem;margin:28px 0 12px;padding:10px 14px;border-left:4px solid var(--accent);background:#f8fafc;border-radius:10px}
.gpt-article h3{font-size:1.125rem;margin:18px 0 8px;color:#0b1440}
.gpt-article p{margin:10px 0}
.gpt-article ul, .gpt-article ol{margin:8px 0 12px 18px}
.gpt-article .table-wrap{overflow-x:auto;margin:12px 0}
.gpt-article table{width:100%;border-collapse:separate;border-spacing:0;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.gpt-article thead th{background:#f3f4f6;font-weight:600;padding:10px;text-align:left;border-bottom:1px solid var(--line)}
.gpt-article tbody td{padding:10px;border-top:1px solid #f1f5f9}
@media (max-width:640px){.gpt-article{font-size:15px}.gpt-article h2{font-size:1.25rem}.gpt-article h3{font-size:1.05rem}}
</style>
"""

AIY_PHRASES = [
    "결론적으로","요약하자면","정리하자면","전반적으로","본 글에서는","이번 글에서는",
    "AI","인공지능 모델로서","독자 여러분","마무리하면","한편으로는"
]
AIY_REPLACEMENTS = [
    "한 줄로 말하면","핵심만 집어보면","짧게 정리하면","실전에서는","","",
    "","","","덧붙이면",""
]

def _md_headings_to_html(txt: str) -> str:
    txt = re.sub(r'^\s*####\s+(.+)$', r'<h4>\1</h4>', txt, flags=re.M)
    txt = re.sub(r'^\s*###\s+(.+)$', r'<h3>\1</h3>', txt, flags=re.M)
    txt = re.sub(r'^\s*##\s+(.+)$',  r'<h2>\1</h2>', txt, flags=re.M)
    txt = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', txt)
    return txt

def _sanitize_llm_html(raw: str) -> str:
    if not raw: return ""
    s = raw
    s = re.sub(r"```(?:html|HTML)?\s*([\s\S]*?)```", r"\1", s)
    s = re.sub(r"[\"“”]```(?:html|HTML)?\s*([\s\S]*?)```[\"“”]", r"\1", s)
    s = s.replace("```html","").replace("```HTML","").replace("```","")
    s = html.unescape(s)
    return s.strip()

def _humanize_text(html_text: str) -> str:
    s = html_text
    for a, b in zip(AIY_PHRASES, AIY_REPLACEMENTS):
        s = re.sub(rf"\b{re.escape(a)}\b", b, s)
    s = re.sub(r"([^.?!])\s{1}([가-힣A-Za-z])", r"\1 \2", s)
    return s

def process_body_html_or_md(body: str) -> str:
    body = _sanitize_llm_html(body or "")
    body = _md_headings_to_html(body)
    body = body.replace("<table", "<div class=\"table-wrap\"><table").replace("</table>", "</table></div>")
    body = _humanize_text(body)
    return body

# =========================
# LLM
# =========================
def ask_openai(model: str, prompt: str, max_tokens=500, temperature=None):
    def _call(model, prompt, max_tokens=500, temperature=None):
        messages = [
            {"role":"system","content":"너는 한국어 블로거. 군더더기 없이 명료하되 건조하지 않게 쓴다. 과장/광고 금지. 구체 팁과 사례."},
            {"role":"user","content":prompt},
        ]
        kwargs = {"model":model, "messages":messages, "n":1}
        if max_tokens is not None:
            kwargs["max_completion_tokens"] = max_tokens
        resp = client.chat.completions.create(**kwargs)
        text = resp.choices[0].message.content
        log_llm(model, prompt, text)
        return {"text": text}
    return cached_call(_call, model=model, prompt=prompt, max_tokens=max_tokens, temperature=temperature, namespace="openai")

# =========================
# Title
# =========================
def normalize_title(s:str)->str:
    s = (s or "").strip()
    s = re.sub(r'^[\'"“”‘’《「(]+','',s); s=re.sub(r'[\'"“”‘’》」)]+$','',s)
    return re.sub(r'\s+',' ',s)

HOOK_BENEFIT_TERMS=["체크리스트","한눈에","실전","가이드","꿀팁","바로 써먹는","문제해결","요령","리스트","핵심"]
def _score_title(t,kw):
    L=len(t)
    return max(0,10-abs(26-L)) + (5 if any(ch.isdigit() for ch in t) else 0) + \
           min(sum(1 for w in HOOK_BENEFIT_TERMS if w in t),6) + \
           (6 if kw.replace(" ","") in t.replace(" ","") else -4)

def build_title(keyword:str,candidate:str)->str:
    t = normalize_title(candidate)
    if len(t)<6: t=f"{keyword} 실전 체크리스트"
    if len(t)>64: t=t[:64].rstrip()
    return t

def generate_hook_title(keyword, model_short):
    p=(f"키워드 '{keyword}'로 24~32자 한국어 블로그 제목 8개. "
       "광고/쿠팡/과장/감탄사/이모지 금지. 실전형 단어 활용. 한 줄에 하나씩.")
    raw=ask_openai(model_short,p,max_tokens=220)["text"]
    cands=[normalize_title(x) for x in raw.splitlines() if x.strip()]
    if len(cands)<3:
        fb=ask_openai(model_short,f"'{keyword}' 핵심 24~28자 제목 3개만",max_tokens=120)["text"]
        cands+=[normalize_title(x) for x in fb.splitlines() if x.strip()]
    best=sorted(cands,key=lambda t:_score_title(t,keyword),reverse=True)[0] if cands else f"{keyword} 실전 가이드"
    return build_title(keyword,best)

# =========================
# Keywords/Category/Tags
# =========================
def read_keywords_random(need=2):
    words=[]
    if os.path.exists(KEYWORDS_CSV):
        with open(KEYWORDS_CSV,"r",encoding="utf-8") as f:
            for row in f:
                parts=[x.strip() for x in row.strip().split(",") if x.strip()]
                words.extend(parts)
    uniq=[]; seen=set()
    for w in words:
        b=w.strip()
        if b and b not in seen:
            seen.add(b); uniq.append(b)
    if len(uniq)>=need: return random.sample(uniq,k=need)
    while len(uniq)<need: uniq.append(f"일반 키워드 {len(uniq)+1}")
    return uniq[:need]

def auto_category(keyword:str)->str:
    k=keyword.lower()
    if any(x in k for x in ["뉴스","속보","브리핑"]): return "뉴스"
    if any(x in k for x in ["쇼핑","추천","리뷰","제품"]): return "쇼핑"
    return "전체글"

def derive_tags_from_keyword(keyword:str,max_n=8):
    tags=[]; kw=(keyword or "").strip()
    if kw: tags.append(kw)
    for tok in re.findall(r"[A-Za-z가-힣0-9]{2,12}", kw):
        if tok not in tags: tags.append(tok)
        if len(tags)>=max_n: break
    return tags[:max_n]

# =========================
# WordPress API
# =========================
def wp_auth(): return (WP_USER, WP_APP_PASSWORD)
def wp_post(url,**kw):
    if DRY_RUN:
        print(f"[DRY] POST {url}"); return {"id":0,"link":"(dry-run)"}
    r=requests.post(url,auth=wp_auth(),timeout=60,verify=WP_TLS_VERIFY,**kw)  # <- NEW
    r.raise_for_status(); return r.json()
def wp_get(url,**kw):
    r=requests.get(url,auth=wp_auth(),timeout=60,verify=WP_TLS_VERIFY,**kw)  # <- NEW
    r.raise_for_status(); return r.json()

def _has_category(name:str)->bool:
    try:
        url=f"{WP_URL}/wp-json/wp/v2/categories?search={requests.utils.quote(name)}&per_page=10"
        arr=requests.get(url,auth=wp_auth(),timeout=20,verify=WP_TLS_VERIFY).json()  # <- NEW
        return any(x.get("name")==name for x in arr)
    except Exception:
        return False

def ensure_categories(cat_names):
    want=set([c for c in cat_names if c]); cats=[]; page=1
    while True:
        url=f"{WP_URL}/wp-json/wp/v2/categories?per_page=100&page={page}"
        r=requests.get(url,auth=wp_auth(),timeout=30,verify=WP_TLS_VERIFY)  # <- NEW
        if r.status_code==400: break
        r.raise_for_status(); arr=r.json()
        if not arr: break
        cats.extend(arr)
        if len(arr)<100: break
        page+=1
    name_to_id={c.get("name"):c.get("id") for c in cats}
    ids=[]
    if "전체글" in name_to_id: ids.append(name_to_id["전체글"])
    for n in want:
        if n in name_to_id and name_to_id[n] not in ids:
            ids.append(name_to_id[n])
    return ids

def ensure_tags(tag_names):
    want=set([t for t in tag_names if t]); ids=[]
    for name in list(want)[:10]:
        try:
            url=f"{WP_URL}/wp-json/wp/v2/tags?search={requests.utils.quote(name)}&per_page=1"
            r=requests.get(url,auth=wp_auth(),timeout=20,verify=WP_TLS_VERIFY)  # <- NEW
            r.raise_for_status()
            arr=r.json()
            if arr: ids.append(arr[0]["id"])
        except Exception: continue
    return ids

def publish_to_wordpress(title, content, categories, tags, schedule_dt=None, status="future"):
    url=f"{WP_URL}/wp-json/wp/v2/posts"
    payload={"title":title,"content":content,"status":status,
             "excerpt":approx_excerpt(content),
             "categories":categories or [],"tags":tags or []}
    try:
        payload["slug"]=slugify(title, separator="-")
    except Exception:
        pass
    if status=="future" and schedule_dt:
        utc=schedule_dt.astimezone(dt.timezone.utc)
        payload["date_gmt"]=utc.strftime("%Y-%m-%dT%H:%M:%S")
    return wp_post(url,json=payload)

# =========================
# Scheduling (10/17 + rollover)
# =========================
def _has_future_post_around(target_kst: dt.datetime, tolerance_min: int = 5) -> bool:
    try:
        arr = wp_get(f"{WP_URL}/wp-json/wp/v2/posts?status=future&per_page=100&orderby=date&order=asc")
    except Exception:
        return False
    tgt_utc = target_kst.astimezone(dt.timezone.utc)
    for p in arr:
        dgmt = p.get("date_gmt")
        if not dgmt: continue
        try:
            post_utc = dt.datetime.fromisoformat(dgmt.replace("Z","+00:00")) if dgmt.endswith("Z") else dt.datetime.fromisoformat(dgmt + "+00:00")
        except Exception:
            continue
        delta_min = abs((post_utc - tgt_utc).total_seconds())/60.0
        if delta_min <= tolerance_min:
            return True
    return False

def pick_slot(idx:int):
    now = kst_now()
    base = now.date()
    hour = 10 if idx==0 else 17
    cand = dt.datetime(base.year, base.month, base.day, hour, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    if now >= cand: cand += dt.timedelta(days=1)
    safety=0
    while _has_future_post_around(cand,5):
        cand += dt.timedelta(days=1); safety+=1
        if safety>60: break
    return cand

# =========================
# Compose & Post
# =========================
def assemble_content(body:str):
    cleaned = process_body_html_or_md(body)
    html_doc = f"{STYLES_CSS}\n<div class='gpt-article'>\n{cleaned}\n</div>"
    ad_method=os.getenv("AD_METHOD","shortcode"); ad_sc=os.getenv("AD_SHORTCODE","[ads_top]")
    ad_mid = os.getenv("AD_INSERT_MIDDLE","true").lower()=="true"
    if ad_method!="shortcode" or not ad_sc: return html_doc
    return html_doc.replace("</style>", f"</style>\n{ad_sc}\n", 1) + (f"\n\n{ad_sc}\n\n" if ad_mid else "")

HUMAN_BODY_INSTR = (
    "아래 요구를 만족하는 '순수 HTML' 본문을 작성하라.\n"
    "형식: 섹션은 <h2>, 소제목은 <h3>, 문단은 <p>, 리스트는 <ul>/<ol>, 표는 <table><thead><tbody>만 사용.\n"
    "톤: 친근하지만 담백. 과장/광고 문구 금지. 구체 예시/실전 팁/자주 하는 실수/체크리스트 포함.\n"
    "AI처럼 보이는 문구 금지. 첫 부분 오프닝 2~3문장 → <p><strong>한 줄 요약</strong></p> 포함.\n"
    "마지막은 <h2>마무리</h2> 섹션으로 실천 요점 3~5개 리스트."
)

def generate_two_posts(keywords_today):
    models = recommend_models()
    M_SHORT = (models.get("short") or "").strip() or "gpt-5-nano"
    M_LONG  = (models.get("long")  or "").strip() or "gpt-4o-mini"
    MAX_BODY = models.get("max_tokens_body", 950)

    ctx_all = {}
    for kw in keywords_today[:2]:
        ctx_prompt = (
            f"키워드: {kw}\n"
            "사람이 좋아할 구성의 개요를 만들자. 다음 항목을 목록으로:\n"
            "- 핵심 문제/관찰 포인트 3개\n"
            "- 실전 팁 3개(바로 적용 가능)\n"
            "- 자주 하는 실수 3개(피하는 요령 포함)\n"
            "- 비교/선택 기준 3개(표로 만들 수 있게 간단 문구)\n"
        )
        ctx_all[kw] = ask_openai(M_SHORT, ctx_prompt, max_tokens=350)["text"]

    posts=[]
    for kw in keywords_today[:2]:
        body_prompt = (
            HUMAN_BODY_INSTR +
            f"\n[키워드] {kw}\n[개요]\n{ctx_all[kw]}\n"
            "표가 들어갈 경우 1개만 넣고, 3~5행으로 간단히.\n"
        )
        body_html = ask_openai(M_LONG, body_prompt, max_tokens=MAX_BODY)["text"]
        body_html = _sanitize_llm_html(body_html)
        title = generate_hook_title(kw, M_SHORT)
        posts.append({"keyword": kw, "title": title, "body": body_html})
    return posts

def create_and_schedule_two_posts():
    words=[]
    if os.path.exists(KEYWORDS_CSV):
        with open(KEYWORDS_CSV,"r",encoding="utf-8") as f:
            for row in f:
                parts=[x.strip() for x in row.strip().split(",") if x.strip()]
                words.extend(parts)
    uniq=[]; seen=set()
    for w in words:
        b=w.strip()
        if b and b not in seen:
            seen.add(b); uniq.append(b)
    if len(uniq)<2: uniq += ["일반 키워드 1","일반 키워드 2"]
    keywords_today = random.sample(uniq, k=2)

    posts = generate_two_posts(keywords_today)
    for idx, post in enumerate(posts):
        kw = post["keyword"]
        final_title = build_title(kw, post["title"])
        cat_name = auto_category(kw)
        cat_ids = ensure_categories([cat_name])
        tag_ids = ensure_tags(derive_tags_from_keyword(kw,8))
        sched = pick_slot(idx)
        content = assemble_content(post["body"])
        res = publish_to_wordpress(
            title=final_title,
            content=content,
            categories=cat_ids,
            tags=tag_ids,
            schedule_dt=sched,
            status=POST_STATUS
        )
        print(f"[OK] scheduled ({idx}) '{final_title}' -> {res.get('link')}")

# =========================
# main
# =========================
def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 환경변수 필요(.env/Secrets).")
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="two-posts")
    _ = parser.parse_args()
    create_and_schedule_two_posts()

if __name__ == "__main__":
    main()
