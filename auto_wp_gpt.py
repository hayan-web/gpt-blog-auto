# auto_wp_gpt.py — daily posts (fixed per-keyword outline + human titles)
# - 각 키워드마다 개요/제목 별도 생성 (공유 ctx 버그 제거)
# - 주제에 맞는 형식(뉴스/해설/가이드/리뷰/생활팁) 자동 선택 → 제목 품질 강화
# - 어색한 꼬리(예약됨 등) 제거 필터
# - 순수 HTML 본문(h2/h3/p + 1개 표), 상단/중간 숏코드 삽입 그대로 유지
# - 예약: 10시/17시, 겹치면 익일로 자동 이월

import os, re, argparse, random, datetime as dt, html
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from openai import OpenAI

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
POST_STATUS = os.getenv("POST_STATUS", "future")

KEYWORDS_CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# =========================
# Utils
# =========================
def kst_now(): return dt.datetime.now(ZoneInfo("Asia/Seoul"))

def cleanup_title(s: str) -> str:
    # 워드프레스 플러그인 자동 접두어/이상 문자열 제거
    s = (s or "")
    s = re.sub(r"\b예약됨\b", "", s)
    return re.sub(r"\s{2,}", " ", s).strip()

def approx_excerpt(body: str, n=140) -> str:
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
.gpt-article{--accent:#2563eb;--line:#e5e7eb;font-size:16px;line-height:1.8;color:#0f172a}
.gpt-article h2{font-size:1.375rem;margin:28px 0 12px;padding:10px 14px;border-left:4px solid var(--accent);background:#f8fafc;border-radius:10px}
.gpt-article h3{font-size:1.125rem;margin:20px 0 8px;color:#0b1440}
.gpt-article p{margin:10px 0}
.gpt-article .table-wrap{overflow-x:auto;margin:12px 0}
.gpt-article table{width:100%;border-collapse:separate;border-spacing:0;border:1px solid var(--line);border-radius:12px;overflow:hidden}
.gpt-article thead th{background:#f3f4f6;font-weight:600;padding:10px;text-align:left;border-bottom:1px solid var(--line)}
.gpt-article tbody td{padding:10px;border-top:1px solid #f1f5f9}
@media (max-width:640px){.gpt-article{font-size:15px}.gpt-article h2{font-size:1.25rem}.gpt-article h3{font-size:1.05rem}}
</style>
"""

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

def process_body_html_or_md(body: str) -> str:
    body = _sanitize_llm_html(body or "")
    body = _md_headings_to_html(body)
    body = body.replace("<table", "<div class=\"table-wrap\"><table").replace("</table>", "</table></div>")
    return body

# =========================
# LLM
# =========================
def ask_openai(model: str, prompt: str, max_tokens=500, temperature=None):
    def _call(model, prompt, max_tokens=500, temperature=None):
        messages = [
            {"role": "system","content":"너는 간결한 한국어 SEO 라이터다. 군더더기 최소화, 사실 우선."},
            {"role":"user","content":prompt},
        ]
        kwargs = {"model":model, "messages":messages, "n":1}
        if max_tokens is not None:
            kwargs["max_completion_tokens"] = max_tokens
        resp = client.chat.completions.create(**kwargs)
        text = resp.choices[0].message.content
        log_llm(model, prompt, text)
        return {"text": text}
    return cached_call(_call, model=model, prompt=prompt, max_tokens=max_tokens, temperature=temperature)

# =========================
# Title (humanized)
# =========================
BAN_TITLE_TOKENS = {"예약됨","테스트","임시","클릭","여기","바로가기"}
HOOK_BENEFIT_TERMS=["총정리","가이드","방법","체크리스트","리뷰","한눈에","핵심","요약","브리핑","분석"]

def normalize_title(s:str)->str:
    s = (s or "").strip()
    s = re.sub(r'^[\'"“”‘’《「(]+','',s); s=re.sub(r'[\'"“”‘’》」)]+$','',s)
    s = re.sub(r'\s+',' ',s)
    for bad in BAN_TITLE_TOKENS:
        s = re.sub(bad, "", s)
    return re.sub(r'\s{2,}',' ',s).strip()

def _score_title(t,kw):
    L=len(t)
    score = max(0,10-abs(26-L))
    score += 6 if any(ch.isdigit() for ch in t) else 0
    score += min(sum(1 for w in HOOK_BENEFIT_TERMS if w in t),6)
    score += 6 if kw.replace(" ","") in t.replace(" ","") else -4
    if any(bad in t for bad in BAN_TITLE_TOKENS): score -= 8
    return score

def generate_hook_title(keyword, model_short):
    prompt = (
        "다음 키워드로 가장 적절한 형식을 고르고(뉴스 브리핑/해설/가이드/리뷰/생활팁 중 1개), "
        "그 형식에 맞는 한국어 제목 8개를 22~32자로 제시하라.\n"
        "- 키워드 의미와 맥락을 유지\n"
        "- 사람 말투, 과장/낚시 금지\n"
        "- 따옴표·이모지·대괄호·마침표 금지\n"
        f"[키워드] {keyword}\n"
        "한 줄에 하나씩 출력"
    )
    raw=ask_openai(model_short,prompt,max_tokens=220)["text"]
    cands=[normalize_title(x) for x in raw.splitlines() if x.strip()]
    # 보완 샘플
    if len(cands)<4:
        fb=ask_openai(model_short,f"'{keyword}' 주제로 24~28자 자연스러운 제목 4개",max_tokens=120)["text"]
        cands+=[normalize_title(x) for x in fb.splitlines() if x.strip()]
    # 점수화
    cands=[t for t in cands if 8<=len(t)<=40 and all(b not in t for b in BAN_TITLE_TOKENS)]
    best=sorted(set(cands), key=lambda t:_score_title(t,keyword), reverse=True)[:1]
    return best[0] if best else f"{keyword} 핵심 브리핑"

# =========================
# Keywords/Category/Tags
# =========================
def read_keywords_list():
    words=[]
    if os.path.exists(KEYWORDS_CSV):
        with open(KEYWORDS_CSV,"r",encoding="utf-8") as f:
            raw=f.read().strip()
    else:
        raw=""
    if raw and "\n" not in raw and "," in raw:
        for w in raw.split(","):
            w=w.strip()
            if w: words.append(w)
        return words
    if os.path.exists(KEYWORDS_CSV):
        with open(KEYWORDS_CSV,"r",encoding="utf-8") as f:
            for row in f:
                parts=[x.strip() for x in row.strip().split(",") if x.strip()]
                words.extend(parts)
    # unique preserve order
    seen=set(); uniq=[]
    for w in words:
        if w not in seen:
            seen.add(w); uniq.append(w)
    return uniq

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
    r=requests.post(url,auth=wp_auth(),timeout=60,**kw); r.raise_for_status(); return r.json()
def wp_get(url,**kw):
    r=requests.get(url,auth=wp_auth(),timeout=60,**kw); r.raise_for_status(); return r.json()

def ensure_categories(cat_names):
    want=set(["전체글"]+[c for c in cat_names if c]); cats=[]; page=1
    while True:
        url=f"{WP_URL}/wp-json/wp/v2/categories?per_page=100&page={page}"
        r=requests.get(url,auth=wp_auth(),timeout=30)
        if r.status_code==400: break
        r.raise_for_status(); arr=r.json()
        if not arr: break
        cats.extend(arr)
        if len(arr)<100: break
        page+=1
    name_to_id={c.get("name"):c.get("id") for c in cats}
    return [name_to_id[n] for n in want if n in name_to_id]

def ensure_tags(tag_names):
    want=set([t for t in tag_names if t]); ids=[]
    for name in list(want)[:10]:
        try:
            url=f"{WP_URL}/wp-json/wp/v2/tags?search={requests.utils.quote(name)}&per_page=1"
            r=requests.get(url,auth=wp_auth(),timeout=20); r.raise_for_status()
            arr=r.json()
            if arr: ids.append(arr[0]["id"])
        except Exception: continue
    return ids

def publish_to_wordpress(title, content, categories, tags, schedule_dt=None, status="future"):
    url=f"{WP_URL}/wp-json/wp/v2/posts"
    payload={"title":cleanup_title(title),"content":content,"status":status,
             "excerpt":approx_excerpt(content),"categories":categories or [],"tags":tags or []}
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

def make_outline_and_titles(kw:str, model_short:str):
    prompt = (
        "아래 키워드에 가장 적절한 형식을 고르고(뉴스 브리핑/해설/가이드/리뷰/생활팁 중 1개), "
        "그 형식에 맞춰 5개 소제목과 한줄 인트로(각 110자 이내)를 제시하라. "
        "이어 같은 형식에 맞는 제목 8개를 22~32자로 제시하라. "
        "출력 형식은 다음과 같다:\n"
        "==intro==\n(한줄 인트로)\n==outline==\n- 소제목1\n- 소제목2\n- 소제목3\n- 소제목4\n- 소제목5\n==titles==\n제목1\n제목2\n...\n제목8\n"
        f"[키워드] {kw}\n"
        "금지: 예약됨/임시/클릭/여기/바로가기/이모지/대괄호/따옴표"
    )
    txt = ask_openai(model_short, prompt, max_tokens=420)["text"]
    intro = ""
    outline = []
    titles = []
    sec = None
    for line in txt.splitlines():
        s = line.strip()
        if s == "==intro==": sec="intro"; continue
        if s == "==outline==": sec="outline"; continue
        if s == "==titles==": sec="titles"; continue
        if not s: continue
        if sec=="intro": intro += (s+" ")
        elif sec=="outline":
            s = re.sub(r"^[-•]\s*","",s)
            if s: outline.append(s)
        elif sec=="titles":
            titles.append(normalize_title(s))
    if not outline: outline = [f"{kw} 핵심 정리", "핵심 포인트", "장단점과 유의점", "활용 사례", "마무리"]
    titles=[t for t in titles if t and all(b not in t for b in BAN_TITLE_TOKENS)]
    return intro.strip(), outline[:5], titles[:12]

def build_body_from_outline(kw:str, intro:str, outline:list, model_long:str, max_tokens:int):
    # 표는 한 개만, 순수 HTML
    body_prompt = (
        "아래 개요를 바탕으로 약 1100~1400자 본문을 '순수 HTML'로 작성하라. "
        "섹션 <h2>, 소소제목 <h3>, 단락 <p>만 사용. "
        "중간에 비교/요약 표 1개(<table><thead><tbody>) 포함. "
        "마크다운(##, ``` 등) 금지. 과한 인라인 스타일 금지. "
        "마지막에 <h2>결론</h2> 포함.\n\n"
        f"[키워드] {kw}\n[인트로] {intro}\n[소제목]\n- " + "\n- ".join(outline)
    )
    return ask_openai(model_long, body_prompt, max_tokens=max_tokens)["text"]

def generate_post_for_keyword(kw:str, models:dict):
    M_SHORT = (models.get("short") or "").strip() or "gpt-5-nano"
    M_LONG  = (models.get("long")  or "").strip() or "gpt-4o-mini"
    MAX_BODY = models.get("max_tokens_body", 900)

    intro, outline, titles = make_outline_and_titles(kw, M_SHORT)
    cand_title = generate_hook_title(kw, M_SHORT) if not titles else \
                 sorted(set(titles), key=lambda t:_score_title(t,kw), reverse=True)[0]
    body_html = build_body_from_outline(kw, intro, outline, M_LONG, MAX_BODY)
    body_html = _sanitize_llm_html(body_html)
    return {"keyword": kw, "title": cand_title, "body": body_html}

def create_and_schedule_two_posts():
    # 키워드 2개 랜덤(중복 방지)
    words = read_keywords_list()
    uniq=[]; seen=set()
    for w in words:
        b=w.strip()
        if b and b not in seen:
            seen.add(b); uniq.append(b)
    if len(uniq)<2: uniq += ["일반 키워드 1","일반 키워드 2"]
    keywords_today = random.sample(uniq, k=2) if len(uniq)>=2 else uniq[:2]

    models = recommend_models()
    posts=[]
    for kw in keywords_today[:2]:
        posts.append(generate_post_for_keyword(kw, models))

    for idx, post in enumerate(posts):
        kw = post["keyword"]
        final_title = cleanup_title(post["title"])
        cat_name = "전체글"
        cat_ids = ensure_categories([cat_name])
        tag_ids = ensure_tags(derive_tags_from_keyword(kw,8))
        sched = pick_slot(idx)
        res = publish_to_wordpress(
            title=final_title,
            content=assemble_content(post["body"]),
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
