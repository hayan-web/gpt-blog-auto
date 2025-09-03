# auto_wp_gpt.py
# under10 모드 + 카테고리별 이미지 프롬프트 + OpenAI 사진형 썸네일 + 가독 CSS 본문
# - 텍스트: max_completion_tokens 사용(temperature 미전달)
# - 제목: SERP 후킹형 자동 생성(22~32자)
# - 본문: 순수 HTML(h2/h3/p/table) + 스타일 주입(콜아웃/표 반응형)
# - 키워드: keywords.csv 전체에서 무작위 2개 선택
# - 태그: 키워드 기반만 사용
# - 이미지: IMAGE_SOURCE=openai → OpenAI 이미지 생성(문자 절대 금지), 아니면 thumbgen 로컬
# - 이미지 size 보정: 768 등 비지원 값은 API 1024로 호출 후 저장 크기로 다운스케일
# - 예산 85%↑: 본문 모델 nano 전환 + 이미지 0장

import os, re, argparse, random, datetime as dt, io, base64
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image

from utils_cache import cached_call
from budget_guard import log_llm, log_image, recommend_models, allowed_images
from thumbgen import make_thumb

load_dotenv()
client = OpenAI()

# =========================
# 환경변수
# =========================
WP_URL = os.getenv("WP_URL", "").rstrip("/")
WP_USER = os.getenv("WP_USER", "")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD", "")
POST_STATUS = os.getenv("POST_STATUS", "future")

KEYWORDS_CSV = os.getenv("KEYWORDS_CSV", "keywords.csv")
EXISTING_CATEGORIES = [x.strip() for x in os.getenv(
    "EXISTING_CATEGORIES", "뉴스,비공개,쇼핑,전체글,게시글,정보,취미"
).split(",") if x.strip()]

NUM_IMAGES_DEFAULT = int(os.getenv("NUM_IMAGES", "1"))
IMAGE_SOURCE = os.getenv("IMAGE_SOURCE", "openai").lower()  # openai | local
IMAGE_STYLE  = os.getenv("IMAGE_STYLE", "photo").lower()    # photo | illustration | flat | 3d
IMAGE_SIZE = os.getenv("IMAGE_SIZE", "1024x1024")           # 기본 1024 (768도 입력 가능: API 1024로 보정)
IMAGE_QUALITY_WEBP = int(os.getenv("IMAGE_QUALITY_WEBP", "75"))
LOW_COST_MODE = os.getenv("LOW_COST_MODE", "true").lower() == "true"

# =========================
# 유틸
# =========================
def _size_tuple(s: str):
    try:
        w, h = s.lower().split("x")
        return (int(w), int(h))
    except Exception:
        return (1024, 1024)

def kst_now():
    return dt.datetime.now(ZoneInfo("Asia/Seoul"))

def cleanup_title(s: str) -> str:
    return re.sub(r"^\s*예약\s*", "", s or "").strip()

def approx_excerpt(body: str, n=140) -> str:
    txt = re.sub(r"<[^>]+>", " ", body or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return (txt[:n] + "…") if len(txt) > n else txt

# --- OpenAI Image size helpers ---
ALLOWED_API_SIZES = {"1024x1024", "1024x1536", "1536x1024", "auto"}

def _normalize_api_size(size_str: str) -> str:
    """
    OpenAI 이미지 API가 지원하는 size로 보정.
    - 768x768 등 비지원 값을 넣으면 1024x1024로 자동 대체
    """
    s = (size_str or "").lower().strip()
    if s in ALLOWED_API_SIZES:
        return s
    # 흔한 소형/정사각 요청은 1024 정사각으로
    if any(x in s for x in ["768", "800", "512", "square"]):
        return "1024x1024"
    # 1536 힌트가 있으면 가로/세로 추정
    if "1536" in s:
        return "1536x1024" if s.startswith("1536x") else "1024x1536"
    return "1024x1024"

def _api_width(api_size: str) -> int:
    if api_size == "1536x1024":
        return 1536
    # auto나 그 외는 1024로 가정
    return 1024

# =========================
# 제목(후킹형)
# =========================
def normalize_title(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r'^[\'"“”‘’《「(]+', '', s)
    s = re.sub(r'[\'"“”‘’》」)]+$', '', s)
    s = re.sub(r'\s+', ' ', s)
    return s

def build_title(keyword: str, candidate: str) -> str:
    t = cleanup_title(normalize_title(candidate))
    if len(t) < 5:
        t = f"{keyword} 한눈에 정리"
    if len(t) > 60:
        t = t[:60].rstrip()
    return t

HOOK_BENEFIT_TERMS = [
    "총정리","가이드","방법","비법","체크리스트","꿀팁","가격","비교","추천","리뷰",
    "정리","필수","초보","전문가","실전","한눈에","업데이트","최신","무료","혜택",
    "주의","함정","핵심","요약","A부터 Z","비교표","차이"
]
HOOK_STOP_TERMS = ["제목 없음","예약","테스트","Test","sample"]

def _score_title(t: str, keyword: str) -> float:
    s = (t or "").strip()
    L = len(s)
    len_score = max(0, 10 - abs(26 - L))
    num_score = 6 if any(ch.isdigit() for ch in s) else 0
    hook_score = min(sum(1 for w in HOOK_BENEFIT_TERMS if w in s), 6)
    kw_score = 6 if keyword.replace(" ", "") in s.replace(" ", "") else -10
    dup_penalty = -4 if s.count(keyword) >= 2 else 0
    bad_penalty = -8 if any(b in s for b in HOOK_STOP_TERMS) else 0
    if any(c in s for c in ["★","☆","❤","🔥","?", "!", "…"]):
        bad_penalty -= 4
    return len_score + num_score + hook_score + kw_score + dup_penalty + bad_penalty

def generate_hook_title(keyword: str, model_short: str) -> str:
    prompt = (
        f"키워드 '{keyword}'로 한국어 SEO 제목 8개를 생성하라.\n"
        "- 각 제목은 22~32자\n"
        "- 키워드를 자연스럽게 1회 포함\n"
        "- 숫자(예: 7가지, 2025)나 후킹 단어(가이드, 총정리, 체크리스트, 추천 등) 활용\n"
        "- 따옴표/이모지/특수문자 금지, 마침표 금지, 콜론/대괄호 금지\n"
        "- 번호 매기지 말고, 각 제목을 한 줄에 하나씩 출력"
    )
    raw = ask_openai(model_short, prompt, max_tokens=200)["text"]
    cands = [normalize_title(x) for x in raw.splitlines() if x.strip()]
    if len(cands) < 3:
        fb = ask_openai(model_short, f"'{keyword}' 핵심을 담은 24~28자 제목 3개만 한 줄씩.", max_tokens=120)["text"]
        cands += [normalize_title(x) for x in fb.splitlines() if x.strip()]
    ranked = sorted(cands, key=lambda t: _score_title(t, keyword), reverse=True)
    best = ranked[0] if ranked else f"{keyword} 한눈에 정리"
    return build_title(keyword, best)

# =========================
# 본문 정리 & CSS
# =========================
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
    txt = re.sub(r'^\s*####\s+(.+)$', r'<h4>\1</h4>', txt, flags=re.M)
    txt = re.sub(r'^\s*###\s+(.+)$', r'<h3>\1</h3>', txt, flags=re.M)
    txt = re.sub(r'^\s*##\s+(.+)$',  r'<h2>\1</h2>', txt, flags=re.M)
    txt = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', txt)
    return txt

def _auto_callouts(txt: str) -> str:
    pat = r'^\s*(핵심|주의|TIP|참고)\s*[:：]\s*(.+)$'
    return re.sub(pat, r'<div class="callout"><strong>\1</strong> \2</div>', txt, flags=re.M)

def _wrap_tables(txt: str) -> str:
    return txt.replace('<table', '<div class="table-wrap"><table') \
              .replace('</table>', '</table></div>')

def process_body_html_or_md(body: str) -> str:
    body2 = _md_headings_to_html(body or "")
    body2 = _auto_callouts(body2)
    body2 = _wrap_tables(body2)
    return body2

# =========================
# OpenAI 텍스트 (캐시+로깅)
# =========================
def ask_openai(model: str, prompt: str, max_tokens=500, temperature=None):
    def _call(model, prompt, max_tokens=500, temperature=None):
        messages = [
            {"role": "system",
             "content": "너는 간결한 한국어 SEO 라이터다. 군더더기 최소화, 사실 우선. 표절 금지."},
            {"role": "user", "content": prompt}
        ]
        create_kwargs = {"model": model, "messages": messages, "n": 1}
        if max_tokens is not None:
            create_kwargs["max_completion_tokens"] = max_tokens
        resp = client.chat.completions.create(**create_kwargs)
        text = resp.choices[0].message.content
        log_llm(model, prompt, text)
        return {"text": text}
    return cached_call(_call, model=model, prompt=prompt,
                       max_tokens=max_tokens, temperature=temperature)

# =========================
# 키워드 (무작위 선택)
# =========================
def read_keywords_random(need=2):
    """
    keywords.csv의 모든 줄을 읽어 쉼표로 분해한 뒤,
    중복 제거 후 무작위로 need개 반환.
    (파일이 1줄만 있어도 '첫 두 개'가 아니라 '무작위 2개'를 고름)
    """
    words = []
    if os.path.exists(KEYWORDS_CSV):
        with open(KEYWORDS_CSV, "r", encoding="utf-8") as f:
            for row in f:
                row = row.strip()
                if not row: continue
                parts = [x.strip() for x in row.split(",") if x.strip()]
                words.extend(parts)
    # 중복 제거
    uniq = []
    seen = set()
    for w in words:
        base = w.strip()
        if base and base not in seen:
            seen.add(base)
            uniq.append(base)
    if len(uniq) >= need:
        return random.sample(uniq, k=need)
    # 부족하면 시드 보충
    while len(uniq) < need:
        uniq.append(f"일반 키워드 {len(uniq)+1}")
    return uniq[:need]

# =========================
# 카테고리/태그
# =========================
def auto_category(keyword: str) -> str:
    k = keyword.lower()
    if any(x in k for x in ["뉴스", "속보", "브리핑"]): return "뉴스"
    if any(x in k for x in ["쇼핑", "추천", "리뷰", "제품"]): return "쇼핑"
    return "정보"

def derive_tags_from_keyword(keyword: str, max_n=8):
    """
    키워드 문구에서만 태그를 생성.
    - 전체 문구 1개 + 토큰화된 단어들(2~12자) 위주
    - 본문에서 무작위 추출하지 않음
    """
    tags = []
    kw = (keyword or "").strip()
    if kw:
        tags.append(kw)  # 전체 구문도 태그로
    for tok in re.findall(r"[A-Za-z가-힣0-9]{2,12}", kw):
        if tok not in tags:
            tags.append(tok)
        if len(tags) >= max_n:
            break
    return tags[:max_n]

# =========================
# WP API
# =========================
def wp_auth(): return (WP_USER, WP_APP_PASSWORD)

def wp_post(url, **kw):
    r = requests.post(url, auth=wp_auth(), timeout=60, **kw)
    r.raise_for_status()
    return r.json()

def ensure_categories(cat_names):
    want = set(["전체글"] + [c for c in cat_names if c])
    cats, page = [], 1
    while True:
        url = f"{WP_URL}/wp-json/wp/v2/categories?per_page=100&page={page}"
        r = requests.get(url, auth=wp_auth(), timeout=30)
        if r.status_code == 400: break
        r.raise_for_status()
        arr = r.json()
        if not arr: break
        cats.extend(arr)
        if len(arr) < 100: break
        page += 1
    name_to_id = {c.get("name"): c.get("id") for c in cats}
    ids = [name_to_id[n] for n in want if n in name_to_id]
    return ids

def ensure_tags(tag_names):
    want = set([t for t in tag_names if t]); ids = []
    for name in list(want)[:10]:
        try:
            url = f"{WP_URL}/wp-json/wp/v2/tags?search={requests.utils.quote(name)}&per_page=1"
            r = requests.get(url, auth=wp_auth(), timeout=20)
            r.raise_for_status()
            arr = r.json()
            if arr: ids.append(arr[0]["id"])
        except Exception:
            continue
    return ids

def _mime_from_ext(path: str):
    ext = os.path.splitext(path.lower())[1]
    return {
        ".webp": "image/webp",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg"
    }.get(ext, "application/octet-stream")

def upload_media_to_wp(path: str):
    filename = os.path.basename(path)
    url = f"{WP_URL}/wp-json/wp/v2/media"
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Type": _mime_from_ext(filename),
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
        utc_dt = schedule_dt.astimezone(dt.timezone.utc)
        payload["date_gmt"] = utc_dt.strftime("%Y-%m-%dT%H:%M:%S")
    return wp_post(url, json=payload)

# =========================
# 컨텐츠 조립
# =========================
def assemble_content(body: str, media_ids):
    cleaned = process_body_html_or_md(body)
    article_html = f"{STYLES_CSS}\n<div class='gpt-article'>\n{cleaned}\n</div>"
    ad_method = os.getenv("AD_METHOD", "shortcode")
    ad_sc = os.getenv("AD_SHORTCODE", "[ads_top]")
    ad_middle = os.getenv("AD_INSERT_MIDDLE", "true").lower() == "true"
    if ad_method != "shortcode" or not ad_sc:
        return article_html
    return article_html.replace("</style>", f"</style>\n{ad_sc}\n", 1) + \
           (f"\n\n{ad_sc}\n\n" if ad_middle else "")

# =========================
# 이미지 생성 (OpenAI / Local)  —— 카테고리별 주제 힌트 + 스타일 + 사이즈 보정 + '문자 절대 금지'
# =========================
def _category_subject_hint(category: str, title: str) -> str:
    c = (category or "").strip()
    if "뉴스" in c:
        return (
            "Photojournalistic scene representing the issue (location/objects: city street, conference room, "
            "microphone stand, meeting table, screen with charts). Natural light, candid feel. "
            "Avoid recognizable faces. No logos."
        )
    if "쇼핑" in c:
        return (
            "Hero product close-up on a clean neutral background. Soft daylight, subtle reflections, "
            "emphasize materials and textures, minimal props. Studio look. No logos."
        )
    return (
        "Contextual objects or workspace scene symbolizing the topic (desk with notebook/laptop/tools/graph). "
        "Shallow depth of field, clean composition. No logos."
    )

def _image_prompt(title: str, category: str) -> str:
    base = f"Topic: {title}. Category: {category}."
    hint = _category_subject_hint(category, title)

    if IMAGE_STYLE == "photo":
        style = "Photorealistic editorial stock photo, high detail, natural lighting, shallow depth of field."
    elif IMAGE_STYLE in ("3d", "isometric"):
        style = "Clean realistic 3D isometric render, soft global illumination, physically based materials."
    elif IMAGE_STYLE == "illustration":
        style = "Modern vector illustration with clean shapes and subtle gradients."
    else:  # flat
        style = "Flat minimal graphic with soft gradient background."

    # ★ 텍스트 절대 금지(한글/영문/숫자/간판/표지판/라벨/워터마크 등)
    no_text = ("Absolutely no text of any language (no Korean Hangul, no English letters, no numbers), "
               "no captions, no typography, no signage, no labels, no UI, no watermarks; "
               "surfaces must be plain without any readable characters.")
    return f"{style} {hint} {no_text} Square composition. {base}"

def _gen_openai_image(title: str, category: str, size="1024x1024", out="thumb.webp", quality=75):
    # 1) API 호출용 size 보정
    api_size = _normalize_api_size(size)
    # 2) 프롬프트
    prompt = _image_prompt(title, category)
    # 3) 생성
    resp = client.images.generate(model="gpt-image-1", prompt=prompt, size=api_size, n=1)
    b64 = resp.data[0].b64_json
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    # 4) 저장 크기(환경 요청)로 리사이즈
    save_w, save_h = _size_tuple(size)
    if (img.width, img.height) != (save_w, save_h):
        try:
            img = img.resize((save_w, save_h), Image.LANCZOS)
        except Exception:
            img = img.resize((save_w, save_h))
    # 5) 저장 + 비용 로깅(과금은 API size 기준)
    img.save(out, "WEBP", quality=quality)
    log_image(size_px=_api_width(api_size))
    print(f"[image] OpenAI api_size={api_size} save_size={save_w}x{save_h}")
    return out

def make_images_or_template(title: str, category: str):
    num_allowed = allowed_images(NUM_IMAGES_DEFAULT)

    if IMAGE_SOURCE == "openai" and num_allowed > 0:
        print(f"[image] OpenAI ({IMAGE_STYLE}, size={IMAGE_SIZE})")
        path = _gen_openai_image(
            title=cleanup_title(title),
            category=category,
            size=IMAGE_SIZE,
            out="thumb.webp",
            quality=IMAGE_QUALITY_WEBP
        )
        media_id = upload_media_to_wp(path)
        return [media_id]

    print(f"[image] LOCAL thumbgen (fallback) size={IMAGE_SIZE}")
    path = make_thumb(
        title=cleanup_title(title),
        cat=category,
        size=_size_tuple(IMAGE_SIZE),
        out="thumb.webp",
        quality=IMAGE_QUALITY_WEBP
    )
    media_id = upload_media_to_wp(path)
    return [media_id]

# =========================
# 스케줄 (10:00 / 17:00 KST)
# =========================
def pick_slot(idx: int):
    now = kst_now()
    base = now.date()
    hour = 10 if idx == 0 else 17
    target = dt.datetime(base.year, base.month, base.day, hour, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    if now >= target:
        target = target + dt.timedelta(days=1)
    return target

# =========================
# 포스트 생성
# =========================
def generate_two_posts(keywords_today):
    models = recommend_models()
    M_SHORT = (models.get("short") or "").strip() or "gpt-5-nano"
    M_LONG  = (models.get("long")  or "").strip() or "gpt-4o-mini"
    MAX_BODY = models.get("max_tokens_body", 900)

    context_prompt = f"""아래 2개 키워드를 각각 5개의 소제목과 한줄요약(각 120자 이내)으로 정리.
- {keywords_today[0]}
- {keywords_today[1]}
간결하고 중복 없이."""
    context = ask_openai(M_SHORT, context_prompt, max_tokens=500)["text"]

    posts = []
    for kw in keywords_today[:2]:
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
        title = generate_hook_title(kw, M_SHORT)
        posts.append({"keyword": kw, "title": title, "body": body_html})
    return posts

def create_and_schedule_two_posts():
    # 무작위 2개 키워드 선택
    keywords_today = read_keywords_random(need=2)
    posts = generate_two_posts(keywords_today)

    for idx, post in enumerate(posts):
        kw = post["keyword"]
        final_title = build_title(kw, post["title"])

        cat_name = auto_category(kw)
        cat_ids = ensure_categories([cat_name])  # "전체글"은 존재 시 포함

        # 태그는 '키워드 기반'만
        tags = derive_tags_from_keyword(kw, max_n=8)
        t_ids = ensure_tags(tags)

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
        print(f"[OK] scheduled ({idx}) '{final_title}' -> {res.get('link')}")

# =========================
# main
# =========================
def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 환경변수를 확인하세요 (.env/GitHub Secrets).")

    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="two-posts", help="two-posts (default)")
    args = parser.parse_args()

    create_and_schedule_two_posts()

if __name__ == "__main__":
    main()
