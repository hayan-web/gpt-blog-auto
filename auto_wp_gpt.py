# auto_wp_gpt.py
# 월 10달러 이하 모드 통합본

import os, csv, re, json, argparse, random, datetime as dt
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from openai import OpenAI

from utils_cache import cached_call
from budget_guard import log_llm, log_image, recommend_models, allowed_images
from thumbgen import make_thumb

load_dotenv()
client = OpenAI()

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

def _size_tuple(s: str):
    try:
        w, h = s.lower().split("x")
        return (int(w), int(h))
    except Exception:
        return (768, 768)

def kst_now():
    return dt.datetime.now(ZoneInfo("Asia/Seoul"))

def cleanup_title(s: str) -> str:
    return re.sub(r"^\s*예약\s*", "", s or "").strip()

def approx_excerpt(body: str, n=140) -> str:
    txt = re.sub(r"<[^>]+>", " ", body or "")
    txt = re.sub(r"\s+", " ", txt).strip()
    return (txt[:n] + "…") if len(txt) > n else txt

def ask_openai(model: str, prompt: str, max_tokens=500, temperature=0.3):
    def _call(model, prompt, max_tokens=500, temperature=0.3):
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system",
                 "content": "너는 간결한 한국어 SEO 라이터다. 군더더기 최소화, 사실 우선. 표절 금지."},
                {"role": "user", "content": prompt}
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            n=1,
        )
        text = resp.choices[0].message.content
        log_llm(model, prompt, text)
        return {"text": text}
    return cached_call(_call, model=model, prompt=prompt, max_tokens=max_tokens, temperature=temperature)

def read_top_keywords(need=2):
    if not os.path.exists(KEYWORDS_CSV):
        raise FileNotFoundError(f"{KEYWORDS_CSV} 가 없습니다.")
    with open(KEYWORDS_CSV, "r", encoding="utf-8") as f:
        rows = [r.strip() for r in f if r.strip()]
    out = []
    for row in rows:
        for w in [x.strip() for x in row.split(",") if x.strip()]:
            if w not in out:
                out.append(w)
            if len(out) >= need:
                return out[:need]
    while len(out) < need:
        out.append(f"일반 키워드 {len(out)+1}")
    return out[:need]

def auto_category(keyword: str) -> str:
    k = keyword.lower()
    if any(x in k for x in ["뉴스", "속보", "브리핑"]):
        return "뉴스"
    if any(x in k for x in ["쇼핑", "추천", "리뷰", "제품"]):
        return "쇼핑"
    return "정보"

def auto_tags(keyword: str, body: str):
    tags = set()
    for t in re.split(r"[,\s/|]+", keyword):
        if 2 <= len(t) <= 15:
            tags.add(t)
    toks = re.findall(r"[A-Za-z가-힣0-9]{2,8}", body or "")
    random.shuffle(toks)
    for t in toks[:8]:
        tags.add(t)
        if len(tags) >= 10:
            break
    return list(tags)

def wp_auth():
    return (WP_USER, WP_APP_PASSWORD)

def wp_post(url, **kw):
    r = requests.post(url, auth=wp_auth(), timeout=60, **kw)
    r.raise_for_status()
    return r.json()

def ensure_categories(cat_names):
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
        utc_dt = schedule_dt.astimezone(dt.timezone.utc)
        payload["date_gmt"] = utc_dt.strftime("%Y-%m-%dT%H:%M:%S")
    return wp_post(url, json=payload)

def assemble_content(body: str, media_ids):
    ad_method = os.getenv("AD_METHOD", "shortcode")
    ad_sc = os.getenv("AD_SHORTCODE", "[ads_top]")
    ad_middle = os.getenv("AD_INSERT_MIDDLE", "true").lower() == "true"
    parts = []
    if ad_method == "shortcode" and ad_sc:
        parts.append(ad_sc)
    parts.append(body)
    if ad_method == "shortcode" and ad_sc and ad_middle:
        parts.append("\n\n" + ad_sc + "\n\n")
    return "\n\n".join(parts)

def make_images_or_template(title: str, category: str):
    num_allowed = allowed_images(NUM_IMAGES_DEFAULT)
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

def pick_slot(idx: int):
    now = kst_now()
    base = now.date()
    hour = 10 if idx == 0 else 17
    target = dt.datetime(base.year, base.month, base.day, hour, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    if now >= target:
        target = target + dt.timedelta(days=1)
    return target

def generate_two_posts(keywords_today):
    models = recommend_models()
    M_SHORT = models["short"]
    M_LONG = models["long"]
    MAX_BODY = models["max_tokens_body"]
    context_prompt = f"""아래 2개 키워드 각각에 대해 SEO용 소제목 5개와 한줄요약을 간단히 제시하라.
- {keywords_today[0]}
- {keywords_today[1]}
각 항목은 300자 이내."""
    context = ask_openai(M_SHORT, context_prompt, max_tokens=600)["text"]
    posts = []
    for kw in keywords_today[:2]:
        body_prompt = (
            "다음 개요를 바탕으로 1200자 전후의 본문을 작성하라. "
            "표는 2~3컬럼만 허용하고 불필요한 군더더기는 금지. "
            "FAQ/체크리스트는 생략. 소제목은 간결하게.\n\n"
            f"[키워드] {kw}\n[개요]\n{context}"
        )
        body = ask_openai(M_LONG, body_prompt, max_tokens=MAX_BODY)["text"]
        title_prompt = f"키워드 '{kw}'로 클릭을 유도하는 24~28자 제목 1개만 출력하라. 특수문자·이모지 금지."
        title = ask_openai(M_SHORT, title_prompt, max_tokens=60)["text"].strip()
        posts.append({"keyword": kw, "title": title, "body": body})
    return posts

def create_and_schedule_two_posts():
    keywords_today = read_top_keywords(need=2)
    posts = generate_two_posts(keywords_today)
    for idx, post in enumerate(posts):
        kw = post["keyword"]
        cat_name = auto_category(kw)
        cat_ids = ensure_categories([cat_name])
        t_ids = ensure_tags(auto_tags(kw, post["body"]))
        media_ids = make_images_or_template(post["title"], category=cat_name)
        schedule_time = pick_slot(idx)
        res = publish_to_wordpress(
            title=cleanup_title(post["title"]),
            content=assemble_content(post["body"], media_ids),
            categories=cat_ids,
            tags=t_ids,
            featured_media=media_ids[0] if media_ids else None,
            schedule_dt=schedule_time,
            status=POST_STATUS
        )
        link = res.get("link")
        print(f"[OK] scheduled ({idx}) '{cleanup_title(post['title'])}' -> {link}")

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
