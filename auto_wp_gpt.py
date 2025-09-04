# auto_wp_gpt.py — human-like titles & coherent body (no-image)
# - 제목: 반복 어투(브리핑/가이드/핵심 요약 등) 금지, 22~32자 자연어, 다양성 스코어
# - 본문: 키워드 의도 분류 → 뉴스/설명/리뷰 톤으로 구획(서론-핵심-사례/팁-마무리)
# - 키워드 2개 뽑아 10:00 / 17:00 KST 예약(겹치면 다음날 이월)
# - DRY_RUN=true 이면 워드프레스 호출 없이 콘솔만
import os, re, argparse, random, datetime as dt, html, json
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from openai import OpenAI

from utils_cache import cached_call
from budget_guard import log_llm, recommend_models

load_dotenv()
client = OpenAI()

WP_URL = os.getenv("WP_URL","").rstrip("/")
WP_USER = os.getenv("WP_USER","")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD","")
POST_STATUS = os.getenv("POST_STATUS","future")
KEYWORDS_CSV = os.getenv("KEYWORDS_CSV","keywords.csv")
DRY_RUN = os.getenv("DRY_RUN","false").lower()=="true"

# 금지/약화 단어 (제목에 쓰지 않음)
BAN_TITLE_TERMS = set((os.getenv("BAN_TITLE_TERMS","브리핑,가이드,핵심,핵심 요약,핵심 브리핑,총정리,정리,실전").replace(" ","")).split(","))

# ---------- Utils ----------
def kst_now(): return dt.datetime.now(ZoneInfo("Asia/Seoul"))

def cleanup_title(s: str) -> str:
    return re.sub(r"^\s*예약\s*","", (s or "").strip())

def approx_excerpt(body: str, n=150) -> str:
    s = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", body, flags=re.I)
    s = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>"," ", s)
    s = re.sub(r"\s+"," ", s).strip()
    return (s[:n]+"…") if len(s)>n else s

# ---------- WordPress ----------
def wp_auth(): return (WP_USER, WP_APP_PASSWORD)
def wp_post(url, **kw):
    if DRY_RUN:
        print(f"[DRY] POST {url}")
        return {"id":0,"link":"(dry-run)","status":"draft"}
    r = requests.post(url, auth=wp_auth(), timeout=60, **kw)
    r.raise_for_status()
    return r.json()
def wp_get(url, **kw):
    r = requests.get(url, auth=wp_auth(), timeout=60, **kw)
    r.raise_for_status()
    return r.json()

def ensure_categories(cat_names):
    want = set(["전체글"] + [c for c in cat_names if c])
    cats = []; page = 1
    while True:
        url = f"{WP_URL}/wp-json/wp/v2/categories?per_page=100&page={page}"
        r = requests.get(url, auth=wp_auth(), timeout=30)
        if r.status_code == 400: break
        r.raise_for_status()
        arr = r.json()
        if not arr: break
        cats.extend(arr)
        if len(arr)<100: break
        page += 1
    name_to_id = {c.get("name"): c.get("id") for c in cats}
    return [name_to_id[n] for n in want if n in name_to_id]

def ensure_tags(tag_names):
    ids = []
    for name in list(dict.fromkeys([t for t in tag_names if t]))[:10]:
        try:
            url = f"{WP_URL}/wp-json/wp/v2/tags?search={requests.utils.quote(name)}&per_page=1"
            r = requests.get(url, auth=wp_auth(), timeout=20); r.raise_for_status()
            arr = r.json()
            if arr: ids.append(arr[0]["id"])
        except Exception:
            continue
    return ids

def publish_to_wordpress(title, content, categories, tags, schedule_dt=None, status="future"):
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    payload = {
        "title": cleanup_title(title),
        "content": content,
        "status": status,
        "excerpt": approx_excerpt(content),
        "categories": categories or [],
        "tags": tags or []
    }
    if status=="future" and schedule_dt:
        utc = schedule_dt.astimezone(dt.timezone.utc)
        payload["date_gmt"] = utc.strftime("%Y-%m-%dT%H:%M:%S")
    return wp_post(url, json=payload)

# ---------- Rendering ----------
STYLES = """
<style>
.gpt-article{--accent:#2563eb;--line:#e5e7eb;font-size:16px;line-height:1.84;color:#0f172a}
.gpt-article h2{font-size:1.35rem;margin:28px 0 12px;padding:10px 14px;border-left:4px solid var(--accent);background:#f8fafc;border-radius:10px}
.gpt-article h3{font-size:1.12rem;margin:18px 0 8px}
.gpt-article p{margin:10px 0}
.gpt-article ul{margin:8px 0 12px 18px}
@media (max-width:640px){.gpt-article{font-size:15px}.gpt-article h2{font-size:1.22rem}}
</style>
"""
def assemble_html(body_html: str) -> str:
    return f"{STYLES}\n<div class='gpt-article'>\n{body_html}\n</div>"

# ---------- LLM ----------
def ask_openai(model: str, messages, max_tokens=600):
    def _call(model, messages, max_tokens=600):
        kwargs = {"model": model, "messages": messages, "n": 1}
        if max_tokens is not None:
            kwargs["max_completion_tokens"] = max_tokens
        resp = client.chat.completions.create(**kwargs)
        text = resp.choices[0].message.content
        log_llm(model, json.dumps(messages, ensure_ascii=False), text)
        return {"text": text}
    return cached_call(_call, model=model, messages=messages, max_tokens=max_tokens)

def classify_intent(keyword: str) -> str:
    """news / explain / review 중 하나"""
    k = keyword.lower()
    if any(x in k for x in ["속보","뉴스","브리핑","발표","실적","주가","매출","정책","사건","리그","경기","이적"]):
        return "news"
    if any(x in k for x in ["후기","리뷰","사용기","추천","비교"]):
        return "review"
    return "explain"

# 제목 후보 생성 → 금지어 제거 → 점수화
BAN_PATTERN = re.compile("|".join([re.escape(x) for x in BAN_TITLE_TERMS if x]) or r"$^")


def _history_path()->str:
    base = os.getenv("USAGE_DIR") or ".usage"
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, "title_history.json")

def _load_history()->list[str]:
    try:
        with open(_history_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [str(x) for x in data][-400:]
    except Exception:
        pass
    return []

def _save_history(history:list[str]):
    try:
        with open(_history_path(), "w", encoding="utf-8") as f:
            json.dump(history[-400:], f, ensure_ascii=False, indent=0)
    except Exception:
        pass

# 제목 후보 생성 → 금지어 제거 → 점수화(+최근 사용 패턴 페널티)
BAN_PATTERN = re.compile("|".join([re.escape(x) for x in BAN_TITLE_TERMS if x]) or r"$^")

def _ngram_set(t, n=3):
    toks = re.findall(r"[가-힣A-Za-z0-9]+", t)
    return {" ".join(toks[i:i+n]) for i in range(len(toks)-n+1)} if len(toks) >= n else set()

def generate_titles(keyword: str, model_short: str):
    history = _load_history()
    hist_ngrams = [(_ngram_set(h), h) for h in history]
    sys = "너는 사람 같은 한국어 블로그 에디터다. 자연스럽고 구체적인 제목을 쓴다."
    usr = (f"아래 키워드로 한국어 블로그 글 제목 12개.\n"
           f"- 평이한 일상 한국어, 과장/기교/이모지/대괄호/말줄임표 금지\n"
           f"- 22~32자, 문장형 어투, 키워드 맥락을 자연스럽게 반영\n"
           f"- '브리핑','가이드','핵심','총정리','정리','실전' 같은 상투어 금지\n"
           f"- 어투를 다양화: 의문/단정/비교/경고/제안/관찰 등 고르게\n"
           f"- 한 줄에 하나씩\n\n키워드: {keyword}")
    raw = ask_openai(model_short, [{"role":"system","content":sys},{"role":"user","content":usr}], max_tokens=220)["text"]
    cands = [re.sub(r'^[\'"“”‘’《「(]+|[\'"“”‘’》」)]+$','',x).strip() for x in raw.splitlines() if x.strip()]
    cands = [t for t in cands if t and not BAN_PATTERN.search(t)]
    # 점수: 길이 근접 + 키워드 포함 + 다양성(어휘 고유 토큰 수) - 최근 패턴 유사도 페널티
    def score(t):
        L = len(t)
        s = max(0, 12 - abs(27 - L))
        s += 6 if keyword.replace(" ","") in t.replace(" ","") else 0
        s += len(set(re.findall(r"[가-힣A-Za-z0-9]{2,}", t))) * 0.2
        ng = _ngram_set(t)
        # 최근 200개 제목과 3-그램 교집합 최대치에 따라 페널티
        if hist_ngrams:
            overlap = max((len(ng & hn) for hn,_ in hist_ngrams), default=0)
            s -= overlap * 1.5
        return s
    cands.sort(key=score, reverse=True)
    top = cands[:6] or [f"{keyword}에 대해 알아보기"]
    # 히스토리 기록
    history.extend(top[:2])
    _save_history(history)
    return top


def render_body(keyword: str, intent: str, model_long: str, max_tokens_body: int) -> str:
    # 톤과 구조 템플릿
    if intent=="news":
        guide = "사실 중심/배경→영향/전망→주의할 점 순. 주장 과장 금지."
    elif intent=="review":
        guide = "사용 맥락→장점/아쉬움→비교 포인트→추천 대상 순. 과장/광고톤 금지."
    else:
        guide = "개념→핵심 원리→실전 팁/예시→마무리 요약 순. 교재체 금지, 일상어 위주."
    sys = "너는 자연스러운 한국어 블로그 라이터다. 군더더기 없이 맥락을 맞춘다."
    usr = (f"키워드: {keyword}\n"
           f"톤/구조 가이드: {guide}\n\n"
           "아래 조건으로 순수 HTML을 작성:\n"
           " - <h2>/<h3>/<p>/<ul>/<li>/<table>만 사용 (마크다운 금지)\n"
           " - 서론 2~3문장으로 맥락 연결, 본문은 소제목으로 나누기\n"
           " - 중간에 비교/체크 표 1개 포함 (<table><thead><tbody>)\n"
           " - 끝에 <h2>마무리</h2>로 2~3문장 요약\n"
           " - 과장/클릭베이트/이모지 금지\n")
    body = ask_openai(model_long, [{"role":"system","content":sys},{"role":"user","content":usr}], max_tokens=max_tokens_body)["text"]
    # fence/엔티티 정리
    body = body.replace("```html","").replace("```HTML","").replace("```","")
    body = html.unescape(body).strip()
    # 제목 태그 보정
    body = re.sub(r'^\s*###\s+(.+)$', r'<h3>\1</h3>', body, flags=re.M)
    body = re.sub(r'^\s*##\s+(.+)$',  r'<h2>\1</h2>', body, flags=re.M)
    return assemble_html(body)

# ---------- Scheduling ----------
def has_future_post_at(target_kst: dt.datetime, tol_min:int=5)->bool:
    try:
        arr = wp_get(f"{WP_URL}/wp-json/wp/v2/posts?status=future&per_page=100&orderby=date&order=asc")
    except Exception:
        return False
    tgt_utc = target_kst.astimezone(dt.timezone.utc)
    for p in arr:
        dgmt = p.get("date_gmt"); 
        if not dgmt: continue
        try:
            post_utc = dt.datetime.fromisoformat(dgmt.replace("Z","+00:00")) if dgmt.endswith("Z") else dt.datetime.fromisoformat(dgmt + "+00:00")
        except Exception:
            continue
        if abs((post_utc - tgt_utc).total_seconds())/60.0 <= tol_min:
            return True
    return False

def pick_slot(idx:int):
    now = kst_now(); base = now.date()
    hour = 10 if idx==0 else 17
    cand = dt.datetime(base.year, base.month, base.day, hour, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    if now >= cand: cand += dt.timedelta(days=1)
    safety=0
    while has_future_post_at(cand, 5):
        cand += dt.timedelta(days=1); safety += 1
        if safety>60: break
    return cand

# ---------- Keywords ----------
def read_keywords_all():
    words=[]
    if os.path.exists(KEYWORDS_CSV):
        with open(KEYWORDS_CSV,"r",encoding="utf-8") as f:
            for row in f:
                parts=[x.strip() for x in row.strip().split(",") if x.strip()]
                words.extend(parts)
    # unique
    seen=set(); out=[]
    for w in words:
        if w not in seen:
            seen.add(w); out.append(w)
    return out

def pick_two_keywords():
    uniq = read_keywords_all()
    if len(uniq) >= 2:
        return random.sample(uniq, k=2)
    if not uniq: uniq = ["일상 키워드 1","일상 키워드 2"]
    while len(uniq)<2: uniq.append(f"일반 키워드 {len(uniq)+1}")
    return uniq[:2]

# ---------- Compose ----------
def create_posts():
    models = recommend_models()
    M_SHORT = (models.get("short") or "gpt-5-nano").strip()
    M_LONG  = (models.get("long")  or "gpt-4o-mini").strip()
    MAX_BODY = int(models.get("max_tokens_body", 900))
    keywords = pick_two_keywords()
    results=[]
    for kw in keywords:
        intent = classify_intent(kw)
        title_candidates = generate_titles(kw, M_SHORT)
        title = title_candidates[0]
        body  = render_body(kw, intent, M_LONG, MAX_BODY)
        results.append({"keyword":kw, "title":title, "body":body, "intent":intent})
    return results

def auto_category(keyword:str)->str:
    k=keyword.lower()
    if any(x in k for x in ["뉴스","속보","실적","브리핑","발표","주가","매출"]): return "뉴스"
    if any(x in k for x in ["후기","리뷰","비교","추천"]): return "쇼핑"
    return "전체글"

def derive_tags(keyword:str, max_n=8):
    tags=[]; kw=(keyword or "").strip()
    if kw: tags.append(kw)
    for tok in re.findall(r"[A-Za-z가-힣0-9]{2,12}", kw):
        if tok not in tags: tags.append(tok)
        if len(tags)>=max_n: break
    return tags[:max_n]

def create_and_schedule_two_posts():
    posts = create_posts()
    for idx, post in enumerate(posts):
        kw = post["keyword"]
        title = cleanup_title(post["title"])
        cat_ids = ensure_categories([auto_category(kw)])
        tag_ids = ensure_tags(derive_tags(kw,8))
        sched = pick_slot(idx)
        res = publish_to_wordpress(
            title=title,
            content=post["body"],
            categories=cat_ids,
            tags=tag_ids,
            schedule_dt=sched,
            status=POST_STATUS
        )
        print(f"[OK] scheduled ({idx}) '{title}' -> {res.get('link')}")

# ---------- main ----------
def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요(.env/Secrets).")
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="two-posts")
    _ = parser.parse_args()
    create_and_schedule_two_posts()

if __name__ == "__main__":
    main()
