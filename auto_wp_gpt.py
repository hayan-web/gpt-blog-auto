# auto_wp_gpt.py
# 월 10달러 이하 모드 (under10) 통합본 - 1일 2포스팅
# - OpenAI: max_completion_tokens 사용, temperature 미전달
# - 모델 공백 시 안전 기본값(coalesce)
# - 제목: 후킹형(SERP용) 자동 생성 + 보강
# - 본문: 순수 HTML(h2/h3/p/table) + 읽기 좋은 CSS 주입
# - 표: thead/tbody 구조 + 지브라/라운드 + 반응형
# - 콜아웃: "핵심:, 주의:, TIP:" 자동 스타일 박스
# - 이미지: 로컬 캐리커처 썸네일(thumbgen.py) 업로드(0$)
# - 카테고리: "전체글" 항상 포함(존재할 때), 태그 자동

import os, re, json, argparse, random, datetime as dt
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from openai import OpenAI

from utils_cache import cached_call
from budget_guard import log_llm, recommend_models, allowed_images
from thumbgen import make_thumb

load_dotenv()
client = OpenAI()

# ---------------------------
# 환경변수
# ---------------------------
WP_URL = os.getenv("WP_URL", "").rstrip("/")
WP_USER = os.getenv("WP_USER", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
POST_STATUS = os.getenv("POST_STATUS", "future")

KEYWORDS_CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")
EXISTING_CATEGORIES = [x.strip() for x in os.getenv(
    "EXISTING_CATEGORIES", "뉴스,비공개,쇼핑,전체글,게시글,정보,취미"
).split(",") if x.strip()]

NUM_IMAGES_DEFAULT = int(os.getenv("NUM_IMAGES", "1"))
IMAGE_SIZE = os.getenv("IMAGE_SIZE", "768x768")
IMAGE_QUALITY_WEBP = int(os.getenv("IMAGE_QUALITY_WEBP", "75"))
LOW_COST_MODE = os.getenv("LOW_COST_MODE", "true").lower() == "true"

# ---------------------------
# 공용 유틸
# ---------------------------
def _size_tuple(s: str):
    try:
        w, h = s.lower().split("x")
        return (int(w), int(h))
    except Exception:
        return (768, 768)

def kst_now():
    return dt.datetime.now(ZoneInfo("Asia/Seoul"))

def cleanup_title(s: str) -> str:
    # 제목에 "예약" 접두가 들어오지 않도록 보정
    return re.sub(r"^\s*예약\s*", "", s or "").strip()

def approx_excerpt(body: str, n=140) -> str:
    txt = re.sub(r"<[^>]+>", " ", body or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return (txt[:n] + "…") if len(txt) > n else txt

# ---------------------------
# 제목 보강/후킹 타이틀 생성
# ---------------------------
def normalize_title(s: str) -> str:
    s = (s or "").strip()
    # 양끝 따옴표/괄호 제거
    s = re.sub(r'^[\'"“”‘’《「(]+', '', s)
    s = re.sub(r'[\'"“”‘’》」)]+$', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s

def build_title(keyword: str, candidate: str) -> str:
    t = cleanup_title(normalize_title(candidate))
    # 너무 짧으면 안전 기본값
    if len(t) < 5:
        t = f"{keyword} 한눈에 정리"
    # 너무 길면 잘라내기(테마/SEO 안전선)
    if len(t) > 60:
        t = t[:60].rstrip()
    return t

# === Hook 타이틀 생성기 ===
HOOK_BENEFIT_TERMS = [
    "총정리","가이드","방법","비법","체크리스트","꿀팁","가격","비교","추천","리뷰",
    "정리","필수","초보","전문가","실전","한눈에","업데이트","최신","무료","혜택",
    "주의","함정","핵심","요약","A부터 Z","비교표","차이"
]
HOOK_STOP_TERMS = ["제목 없음","예약","테스트","Test","sample"]

def _score_title(t: str, keyword: str) -> float:
    s = (t or "").strip()
    # 길이: 26자 근처 가산 (22~32 허용)
    L = len(s)
    len_score = max(0, 10 - abs(26 - L))
    # 숫자(리스트형/연도) 가산
    num_score = 6 if any(ch.isdigit() for ch in s) else 0
    # 이득/후킹 단어 가산
    hook_score = sum(1 for w in HOOK_BENEFIT_TERMS if w in s)
    hook_score = min(hook_score, 6)
    # 키워드 포함 (필수) + 중복 과다 페널티
    kw_score = 6 if keyword.replace(" ", "") in s.replace(" ", "") else -10
    dup_penalty = -4 if s.count(keyword) >= 2 else 0
    # 금지어/이모지/특수문자 과다 페널티
    bad_penalty = -8 if any(b in s for b in HOOK_STOP_TERMS) else 0
    if any(c in s for c in ["★","☆","❤","🔥","?", "!", "…"]):
        bad_penalty -= 4
    return len_score + num_score + hook_score + kw_score + dup_penalty + bad_penalty

def generate_hook_title(keyword: str, model_short: str) -> str:
    # 모델에게 후보 다수 생성 (줄바꿈 구분)
    prompt = (
        f"키워드 '{keyword}'로 한국어 SEO 제목 8개를 생성하라.\n"
        "- 각 제목은 22~32자\n"
        "- 키워드를 자연스럽게 1회 포함\n"
        "- 숫자(예: 7가지, 2025)나 후킹 단어(가이드, 총정리, 체크리스트, 추천 등) 활용\n"
        "- 따옴표/이모지/특수문자(!? … ★ ☆ ❤) 금지, 마침표 금지\n"
        "- 콜론/대괄호도 사용하지 말 것\n"
        "- 번호 매기지 말고, 각 제목을 한 줄에 하나씩 출력"
    )
    raw = ask_openai(model_short, prompt, max_tokens=200)["text"]
    cands = [normalize_title(x) for x in raw.splitlines() if x.strip()]
    # 후보 보강
    if len(cands) < 3:
        fb = ask_openai(model_short, f"'{keyword}' 핵심을 담은 24~28자 제목 3개만 한 줄씩.", max_tokens=120)["text"]
        cands += [normalize_title(x) for x in fb.splitlines() if x.strip()]
    ranked = sorted(cands, key=lambda t: _score_title(t, keyword), reverse=True)
    best = ranked[0] if ranked else f"{keyword} 한눈에 정리"
    return build_title(keyword, best)

# ---------------------------
# Readable CSS & HTML processors
# ---------------------------
STYLES_CSS = """
<style>
.gpt-article{--accent:#2563eb;--muted:#475569;--line:#e5e7eb;--soft:#f8fafc;
  font-size:16px; line-height:1.8; color:#0f172a;}
.gpt-article h2{font-size:1.375rem;margin:28px 0 12px;padding:10px 14px;
  border-left:4px solid var(--accent);background:#f8fafc;border-radius:10px;}
.gpt-article h3{font-size:1.125rem;margin:20px 0 8px;color:#0b1440;}
.gpt-article p{margin:10px 0;}
.gpt-article ul,.gpt-article ol{margin:10px 0 10px 1.25rem;}
.gpt-article .callout{margin:16px 0;padding:12px 14px;background:#eff6ff;
  border-left:4px solid var(--accent);border-radius:10px;}
.gpt-article .table-wrap{overflow-x:auto;margin:12px 0;}
.gpt-article table{width:100%;border-collapse:separate;border-spacing:0;
  border:1px solid var(--line);border-radius:12px;overflow:hidden;}
.gpt-article thead th{background:#f3f4f6;font-weight:600;padding:10px;text-align:left;
  border-bottom:1px solid var(--line);}
.gpt-article tbody td{padding:10px;border-top:1px solid #f1f5f9;}
.gpt-article tbody tr:nth-child(even){background:#fbfdff;}
.gpt-article .mark{background:linear-gradient(transparent 65%, #ffe9a8 65%);}
@media (max-width:640px){
  .gpt-article{font-size:15px;}
  .gpt-article h2{font-size:1.25rem;}
  .gpt-article h3{font-size:1.05rem;}
}
</style>
"""

def _md_headings_to_html(txt: str) -> str:
    # ###, ##, #### → <h3>/<h2>/<h4> (긴 패턴 우선)
    txt = re.sub(r'^\s*####\s+(.+)$', r'<h4>\1</h4>', txt, flags=re.M)
    txt = re.sub(r'^\s*###\s+(.+)$', r'<h3>\1</h3>', txt, flags=re.M)
    txt = re.sub(r'^\s*##\s+(.+)$',  r'<h2>\1</h2>', txt, flags=re.M)
    # **bold** → <strong>
    txt = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', txt)
    return txt

def _auto_callouts(txt: str) -> str:
    # "핵심: ..." / "주의: ..." / "TIP: ..." / "참고: ..." 라인 → 콜아웃
    pat = r'^\s*(핵심|주의|TIP|참고)\s*[:：]\s*(.+)$'
    return re.sub(pat, r'<div class="callout"><strong>\1</strong> \2</div>', txt, flags=re.M)

def _wrap_tables(txt: str) -> str:
    # <table> 반응형 래퍼
    return txt.replace('<table', '<div class="table-wrap"><table') \
              .replace('</table>', '</table></div>')

def process_body_html_or_md(body: str) -> str:
    """모델이 HTML을 내놓든, 실수로 마크다운 헤더를 내놓든 가볍게 정리."""
    body2 = _md_headings_to_html(body or "")
    body2 = _auto_callouts(body2)
    body2 = _wrap_tables(body2)
    return body2

# ---------------------------
# OpenAI 래퍼 (캐시 + 로깅)  ★ max_completion_tokens / no temperature ★
# ---------------------------
def ask_openai(model: str, prompt: str, max_tokens=500, temperature=None):
    """
    gpt-5-nano / gpt-4o-mini 호환:
    - max_tokens -> max_completion_tokens 로 변환
    - temperature는 이 모델군에서 커스텀 불가 → API 호출에 전달하지 않음
    """
    def _call(model, prompt, max_tokens=500, temperature=None):
        messages = [
            {"role": "system",
             "content": "너는 간결한 한국어 SEO 라이터다. 군더더기 최소화, 사실 우선. 표절 금지."},
            {"role": "user", "content": prompt}
        ]
        create_kwargs = {
            "model": model,
            "messages": messages,
            "n": 1,
        }
        if max_tokens is not None:
            create_kwargs["max_completion_tokens"] = max_tokens

        resp = client.chat.completions.create(**create_kwargs)
        text = resp.choices[0].message.content
        log_llm(model, prompt, text)  # 비용 로깅(근사)
        return {"text": text}

    # cached_call 키에는 여전히 max_tokens/temperature 포함되어도 무방
    return cached_call(_call, model=model, prompt=prompt,
                       max_tokens=max_tokens, temperature=temperature)

# ---------------------------
# 키워드
# ---------------------------
def read_top_keywords(need=2):
    if not os.path.exists(KEYWORDS_CSV):
        raise FileNotFoundError(f"{KEYWORDS_CSV} 가 없습니다.")
    with open(KEYWORDS_CSV, "r", encoding="utf-8") as f:
        rows = [r.strip() for r in f if r.strip()]
    # 첫 줄부터 차례로 수집
    out = []
    for row in rows:
        for w in [x.strip() for x in row.split(",") if x.strip()]:
            if w not in out:
                out.append(w)
            if len(out) >= need:
                return out[:need]
    # 부족하면 임시 보충
    while len(out) < need:
        out.append(f"일반 키워드 {len(out)+1}")
    return out[:need]

# ---------------------------
# 카테고리/태그
# ---------------------------
def auto_category(keyword: str) -> str:
    k = keyword.lower()
    if any(x in k for x in ["뉴스", "속보", "브리핑"]):
        return "뉴스"
    if any(x in k for x in ["쇼핑", "추천", "리뷰", "제품"]):
        return "쇼핑"
    return "정보"

def auto_tags(keyword: str, body: str):
    # 키워드 단어 + 본문 토큰 일부 (과도하게 긴 토큰 방지)
    tags = set()
    for t in re.split(r"[,\s/|]+", keyword):
        t = t.strip()
        if 2 <= len(t) <= 15:
            tags.add(t)

    toks = re.findall(r"[A-Za-z가-힣0-9]{2,12}", re.sub(r"<[^>]+>", " ", body or ""))
    random.shuffle(toks)
    for t in toks:
        if 2 <= len(t) <= 12:
            tags.add(t)
        if len(tags) >= 10:
            break
    return list(tags)

# ---------------------------
# 워드프레스 API
# ---------------------------
def wp_auth():
    return (WP_USER, WP_APP_PASSWORD)

def wp_post(url, **kw):
    r = requests.post(url, auth=wp_auth(), timeout=60, **kw)
    r.raise_for_status()
    return r.json()

def ensure_categories(cat_names):
    """
    워드프레스에서 이름→ID 매핑. "전체글"은 항상 포함(존재하는 경우).
    """
    want = set(["전체글"] + [c for c in cat_names if c])
    cats = []
    page = 1
    while True:
        url = f"{WP_URL}/wp-json/wp/v2/categories?per_page=100&page={page}"
        r = requests.get(url, auth=wp_auth(), timeout=30)
        if r.status_code == 400:
            break
        r.raise_for_status()
        arr = r.json()
        if not arr:
            break
        cats.extend(arr)
        if len(arr) < 100:
            break
        page += 1
    name_to_id = {c.get("name"): c.get("id") for c in cats}
    ids = []
    for name in want:
        if name in name_to_id:
            ids.append(name_to_id[name])
    return ids

def ensure_tags(tag_names):
    """
    태그는 생성하지 않고, 존재하는 것만 매핑(과호출 방지).
    """
    want = set([t for t in tag_names if t])
    ids = []
    for name in list(want)[:10]:
        try:
            url = f"{WP_URL}/wp-json/wp/v2/tags?search={requests.utils.quote(name)}&per_page=1"
            r = requests.get(url, auth=wp_auth(), timeout=20)
            r.raise_for_status()
            arr = r.json()
            if arr:
                ids.append(arr[0]["id"])
        except Exception:
            continue
    return ids

def upload_media_to_wp(path: str):
    filename = os.path.basename(path)
    url = f"{WP_URL}/wp-json/wp/v2/media"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": "image/webp",
    }
    with open(path, "rb") as f:
        r = requests.post(url, headers=headers, data=f, auth=wp_auth(), timeout=120)
    r.raise_for_status()
    j = r.json()
    return j.get("id")

def publish_to_wordpress(title: str, content: str, categories, tags,
                         featured_media=None, schedule_dt=None, status="future"):
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    payload = {
        "title": cleanup_title(title),
        "content": content,
        "status": status,
        "excerpt": approx_excerpt(content),
        "categories": categories or [],
        "tags": tags or [],
    }
    if featured_media:
        payload["featured_media"] = featured_media
    if status == "future" and schedule_dt:
        # WP는 UTC 기준 date_gmt 요구
        utc_dt = schedule_dt.astimezone(dt.timezone.utc)
        payload["date_gmt"] = utc_dt.strftime("%Y-%m-%dT%H:%M:%S")
    return wp_post(url, json=payload)

# ---------------------------
# 컨텐츠 조립 (CSS + 정리 + 광고)
# ---------------------------
def assemble_content(body: str, media_ids):
    # 1) 본문 정리(헤더/표/콜아웃)
    cleaned = process_body_html_or_md(body)

    # 2) 스타일 + 본문 컨테이너
    article_html = f"{STYLES_CSS}\n<div class='gpt-article'>\n{cleaned}\n</div>"

    # 3) 광고 삽입(옵션)
    ad_method = os.getenv("AD_METHOD", "shortcode")
    ad_sc = os.getenv("AD_SHORTCODE", "[ads_top]")
    ad_middle = os.getenv("AD_INSERT_MIDDLE", "true").lower() == "true"

    if ad_method != "shortcode" or not ad_sc:
        return article_html

    # 스타일 직후 상단 광고 1회, 문서 끝에 추가 1회(옵션)
    return article_html.replace("</style>", f"</style>\n{ad_sc}\n", 1) + \
           (f"\n\n{ad_sc}\n\n" if ad_middle else "")

# ---------------------------
# 썸네일/이미지 (로컬 캐리커처 1장 권장)
# ---------------------------
def make_images_or_template(title: str, category: str):
    num_allowed = allowed_images(NUM_IMAGES_DEFAULT)

    # 로컬 캐리커처 생성(비용 0)
    path = make_thumb(
        title=cleanup_title(title),
        cat=category,
        size=_size_tuple(IMAGE_SIZE),
        out="thumb.webp",
        quality=IMAGE_QUALITY_WEBP
    )
    media_id = upload_media_to_wp(path)

    if num_allowed <= 0:
        return [media_id]

    return [media_id]

# ---------------------------
# 스케줄 계산 (10:00 / 17:00 KST)
# ---------------------------
def pick_slot(idx: int):
    """
    idx=0 -> 오늘 10:00, 이미 지났으면 내일 10:00
    idx=1 -> 오늘 17:00, 이미 지났으면 내일 17:00
    """
    now = kst_now()
    base = now.date()
    hour = 10 if idx == 0 else 17
    target = dt.datetime(base.year, base.month, base.day, hour, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    if now >= target:
        target = target + dt.timedelta(days=1)
    return target

# ---------------------------
# 포스트 생성 로직
# ---------------------------
def generate_two_posts(keywords_today):
    models = recommend_models()
    # ✅ 모델 값이 비었으면 안전 기본값으로 보강
    M_SHORT = (models.get("short") or "").strip() or "gpt-5-nano"
    M_LONG  = (models.get("long")  or "").strip() or "gpt-4o-mini"
    MAX_BODY = models.get("max_tokens_body", 900)

    # 공통 개요 1회 (간단)
    context_prompt = f"""아래 2개 키워드를 각각 5개의 소제목과 한줄요약(각 120자 이내)으로 정리.
- {keywords_today[0]}
- {keywords_today[1]}
간결하고 중복 없이."""
    context = ask_openai(M_SHORT, context_prompt, max_tokens=500)["text"]

    posts = []
    for kw in keywords_today[:2]:
        # 본문: 반드시 순수 HTML. h2/h3/p + 표 포함, 결론 섹션 필수
        body_prompt = (
            "다음 개요를 바탕으로 약 1200자 본문을 '순수 HTML'로 작성하라. "
            "마크다운(##, ###, 코드블럭, 백틱) 사용 금지. "
            "섹션 제목은 <h2> / 소소제목은 <h3>, 단락은 <p>로만 구성. "
            "중간에 1개의 비교 표를 <table><thead><tbody> 구조로 포함. "
            "표는 3~5열, 3~6행으로 간결하게. "
            "핵심 문구는 <strong>으로 강조. "
            "특수한 클래스나 인라인 style 속성은 넣지 말 것. "
            "마지막에 <h2>결론</h2> 섹션 포함.\n\n"
            f"[키워드] {kw}\n[개요]\n{context}"
        )
        body_html = ask_openai(M_LONG, body_prompt, max_tokens=MAX_BODY)["text"]

        # 제목: 후킹형(SERP용) 생성기 사용
        title = generate_hook_title(kw, M_SHORT)

        posts.append({"keyword": kw, "title": title, "body": body_html})
    return posts

def create_and_schedule_two_posts():
    # 키워드 2개 확보
    keywords_today = read_top_keywords(need=2)
    posts = generate_two_posts(keywords_today)

    for idx, post in enumerate(posts):
        kw = post["keyword"]
        # 최종 제목 보강(이중 안전장치)
        final_title = build_title(kw, post["title"])

        cat_name = auto_category(kw)
        cat_ids = ensure_categories([cat_name])  # "전체글" 포함(존재 시)
        t_ids = ensure_tags(auto_tags(kw, post["body"]))
        media_ids = make_images_or_template(final_title, category=cat_name)
        schedule_time = pick_slot(idx)

        res = publish_to_wordpress(
            title=final_title,
            content=assemble_content(post["body"], media_ids),
            categories=cat_ids,
            tags=t_ids,
            featured_media=media_ids[0] if media_ids else None,
            schedule_dt=schedule_time,
            status=POST_STATUS
        )
        link = res.get("link")
        print(f"[OK] scheduled ({idx}) '{final_title}' -> {link}")

# ---------------------------
# main
# ---------------------------
def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 환경변수를 확인하세요 (.env/GitHub Secrets).")

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="two-posts", help="two-posts (default)")
    args = parser.parse_args()

    if args.mode == "two-posts":
        create_and_schedule_two_posts()
    else:
        create_and_schedule_two_posts()

if __name__ == "__main__":
    main()
