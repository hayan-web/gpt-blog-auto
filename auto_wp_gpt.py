import os, sys, json, re, datetime as dt
from urllib.parse import urljoin
import requests
from dotenv import load_dotenv
from slugify import slugify
from openai import OpenAI

load_dotenv()

WP_URL            = os.getenv("WP_URL","").rstrip("/")
WP_USER           = os.getenv("WP_USER","")
WP_APP_PASSWORD   = os.getenv("WP_APP_PASSWORD","")
WP_TLS_VERIFY     = os.getenv("WP_TLS_VERIFY","true").lower() not in ("0","false","no")

OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY","")
OPENAI_MODEL      = os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
OPENAI_MODEL_LONG = os.getenv("OPENAI_MODEL_LONG") or OPENAI_MODEL
MAX_TOKENS_BODY   = int(os.getenv("MAX_TOKENS_BODY", "900"))

DEFAULT_CATEGORY  = os.getenv("DEFAULT_CATEGORY","정보")
DEFAULT_TAGS      = [t for t in (os.getenv("DEFAULT_TAGS") or "").split(",") if t.strip()]
POST_STATUS       = os.getenv("POST_STATUS","future")

KEYWORDS_CSV      = os.getenv("KEYWORDS_CSV","keywords_general.csv")
AD_SHORTCODE      = os.getenv("AD_SHORTCODE","[ads_top]")

# 일상글 H3 전용 CSS (본문 상단 1회 삽입)
DAILY_INLINE_CSS = """<style>
.daily-sub{font-size:1.125rem;line-height:1.6;margin:1.75em 0 .75em 0;padding:.4em .6em;
border-left:4px solid #111;background:linear-gradient(90deg,rgba(0,0,0,.06),rgba(0,0,0,0));}
.diary-hr{border:none;border-top:1px solid rgba(0,0,0,.12);margin:1.25rem 0;}
</style>
"""

client = OpenAI(api_key=OPENAI_API_KEY)

def wp_request(method, path, **kwargs):
    url = urljoin(WP_URL, f"/wp-json/wp/v2/{path.lstrip('/')}")
    auth = (WP_USER, WP_APP_PASSWORD)
    resp = requests.request(method, url, auth=auth, verify=WP_TLS_VERIFY, timeout=30, **kwargs)
    if not resp.ok:
        raise RuntimeError(f"WP {method} {path}: {resp.status_code} {resp.text[:400]}")
    return resp.json()

def ensure_term(kind, name):
    name = name.strip()
    if not name:
        return None
    q = wp_request("GET", f"{kind}", params={"search": name})
    for t in q:
        if t.get("name") == name:
            return t["id"]
    return wp_request("POST", f"{kind}", json={"name": name})["id"]

def ensure_category(name):
    return ensure_term("categories", name)

def ensure_tag(name):
    return ensure_term("tags", name)

def schedule_times_utc(kst_hours=(10,13)):
    # 오늘 KST 기준 특정 시간들 예약 → UTC 변환
    now_utc = dt.datetime.utcnow()
    kst = now_utc + dt.timedelta(hours=9)
    dates = []
    for hh in kst_hours:
        local = kst.replace(hour=hh, minute=0, second=0, microsecond=0)
        if local <= kst:
            local = local + dt.timedelta(days=1)
        utc = local - dt.timedelta(hours=9)
        dates.append(utc)
    return dates

def read_keywords(path):
    keys = []
    if os.path.isfile(path):
        with open(path,"r",encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i==0 and "keyword" in line:
                    continue
                w = line.strip().strip(",")
                if w:
                    keys.append(w)
    if not keys:
        # 폴백
        keys = ["가계부","정리정돈","홈카페","주간 계획","하루 회고","시간 관리","집 정리","오늘의 기록"]
    return keys

PROMPT_DIARY = (
"너는 한국어 블로그 작가. 아래 규칙으로 워드프레스에 바로 붙여넣을 ‘일상글’을 만들어라.\n"
"제목은 20~30자 한 줄. 이어서 부제 1줄. 구분선. 300자 이내 개요. 구분선.\n"
"H3 소제목은 6~8개. 각 섹션은 서로 다른 문체(대화체·서술·묘사 등)로 4~6문장. "
"문장 길이와 단락 길이를 일부러 불규칙하게 섞어 가독성을 높인다. "
"무분별한 과장·미확인 정보 금지. 브랜드 기능·추천 멘트 금지. 감정조작 금지.\n"
"출력은 반드시 아래 형식의 ‘플레인 텍스트’ 스켈레톤을 준수한다. 단, 후처리에서 H3만 변환될 것이다.\n\n"
"# 제목(20~30자)\n\n"
"## 부제 한 줄\n\n"
"---\n\n"
"(개요 300자 이내, 존댓말)\n\n"
"---\n\n"
"### 소제목1\n본문\n---\n\n"
"### 소제목2\n본문\n---\n\n"
"### 소제목3\n본문\n---\n\n"
"### 소제목4\n본문\n---\n\n"
"### 소제목5\n본문\n---\n\n"
"### 소제목6\n본문\n---\n\n"
"### 소제목7\n본문\n---\n\n"
"### 소제목8\n본문\n---\n\n"
"(마지막에 해시태그 2줄: 1줄은 #태그 6개, 다음 줄은 ,로 구분된 키워드 5개)"
)

def llm_generate_diary(keyword):
    sys_prompt = "당신은 블로그 SEO에 맞춰 자연스럽고 사람처럼 쓰는 한국어 작가입니다."
    user_prompt = f"키워드: {keyword}\n위 규칙으로 작성."
    r = client.chat.completions.create(
        model=OPENAI_MODEL_LONG,
        messages=[{"role":"system","content":sys_prompt},{"role":"user","content":PROMPT_DIARY+"\n\n"+user_prompt}],
        temperature=0.8,
        max_tokens=MAX_TOKENS_BODY
    )
    return r.choices[0].message.content.strip()

def decorate_diary_html(text):
    # 상단 광고 숏코드 + CSS
    html = AD_SHORTCODE + "\n" + DAILY_INLINE_CSS

    # H1/H2는 문단 처리, H3는 스타일 클래스 적용
    lines = text.splitlines()
    out = []
    for ln in lines:
        ln = ln.rstrip()
        if ln.startswith("### "):
            title = ln[4:].strip()
            out.append(f'<h3 class="daily-sub">{title}</h3>')
            continue
        if ln.startswith("# "):
            out.append(f"<h1>{ln[2:].strip()}</h1>")
            continue
        if ln.startswith("## "):
            out.append(f"<h2>{ln[3:].strip()}</h2>")
            continue
        if ln.strip() == "---":
            out.append('<hr class="diary-hr" />')
            continue
        # 해시태그 줄이면 p로 감싸되 본문 안쪽 공백 보존 최소화
        if ln.strip():
            out.append(f"<p>{ln}</p>")
        else:
            out.append("")
    html += "\n" + "\n".join([s for s in out if s is not None])
    return html

def create_post(title, content_html, categories=None, tags=None, status="draft", date_gmt=None):
    cat_ids = []
    if categories:
        for c in categories:
            cid = ensure_category(c)
            if cid: cat_ids.append(cid)
    tag_ids = []
    if tags:
        for t in tags:
            tid = ensure_tag(t)
            if tid: tag_ids.append(tid)

    payload = {"title": title, "content": content_html, "status": status}
    if cat_ids: payload["categories"] = cat_ids
    if tag_ids: payload["tags"] = tag_ids
    if date_gmt:
        payload["date_gmt"] = date_gmt.strftime("%Y-%m-%dT%H:%M:%S")

    return wp_request("POST", "posts", json=payload)

def main():
    mode = None
    for a in sys.argv[1:]:
        if a.startswith("--mode="):
            mode = a.split("=",1)[1]

    if mode != "two-posts":
        print("[]")
        return

    keys = read_keywords(KEYWORDS_CSV)
    # 같은 제목 연속 방지용 앞머리 바꿈
    picked = keys[:2] if len(keys) >= 2 else keys * 2

    times_utc = schedule_times_utc((10,13))  # KST 10시/13시
    results = []
    for i, kw in enumerate(picked[:2]):
        raw = llm_generate_diary(kw)
        # 혹시 중복 제목 패턴이면 간단히 치환
        raw = re.sub(r"^#\s*오늘의 기록.*$", "# 가계부에 대해 차분히 정리해 봤어요", raw, flags=re.M)
        html = decorate_diary_html(raw)

        title_match = re.search(r"<h1>(.*?)</h1>", html, flags=re.S)
        title = title_match.group(1).strip() if title_match else f"{kw}에 대해 차분히 정리해 봤어요"

        post = create_post(
            title=title,
            content_html=html,
            categories=[DEFAULT_CATEGORY] if DEFAULT_CATEGORY else None,
            tags=DEFAULT_TAGS or None,
            status=POST_STATUS,
            date_gmt=times_utc[i] if POST_STATUS=="future" and i < len(times_utc) else None
        )
        results.append({"id": post["id"], "title": post["title"]["rendered"], "date_gmt": post.get("date_gmt"), "link": post.get("link")})
    print(json.dumps(results, ensure_ascii=False))

if __name__ == "__main__":
    main()
