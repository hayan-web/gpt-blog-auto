# auto_wp_gpt.py
# - Excerpt: style/script 제거 후 텍스트만
# - Image: photo 스타일 기본, 저정보량 자동 재생성(최대 2회), 소품 강제, 빈 배경 금지
#          제목 오버레이 박스 화면 내 중앙 고정(폭/높이 초과 시 자동 축소/재래핑)
# - 본문: 코드펜스/엔티티 정리 → 순수 HTML, h2/h3 변환 + CSS
# - 예약: 10/17시, 충돌 시 다음날 이월
# - 테스트: DRY_RUN=true면 API/WP 미호출(비용 0)

import os, re, argparse, random, datetime as dt, io, base64, glob, html
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image, ImageDraw, ImageFont, ImageStat

from utils_cache import cached_call
from budget_guard import log_llm, log_image, recommend_models

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
IMAGE_STYLE  = os.getenv("IMAGE_STYLE", "photo").lower()          # ← 기본 photo
IMAGE_SIZE = os.getenv("IMAGE_SIZE", "1024x1024")
IMAGE_QUALITY_WEBP = int(os.getenv("IMAGE_QUALITY_WEBP", "80"))
NUM_IMAGES_DEFAULT = 1
LOW_COST_MODE = os.getenv("LOW_COST_MODE", "true").lower() == "true"
DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

# =========================
# Utils
# =========================
def kst_now(): return dt.datetime.now(ZoneInfo("Asia/Seoul"))

def _size_tuple(s: str):
    try:
        w, h = s.lower().split("x")
        return (int(w), int(h))
    except Exception:
        return (1024, 1024)

def cleanup_title(s: str) -> str:
    return re.sub(r"^\s*예약\s*", "", s or "").strip()

def approx_excerpt(body: str, n=140) -> str:
    """요약 생성: style/script 제거 → 태그 제거 → 공백 정리"""
    s = body or ""
    s = re.sub(r"<style[^>]*>[\s\S]*?</style>", "", s, flags=re.I)
    s = re.sub(r"<script[^>]*>[\s\S]*?</script>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return (s[:n] + "…") if len(s) > n else s

# --- OpenAI image size helpers ---
ALLOWED_API_SIZES = {"1024x1024", "1024x1536", "1536x1024", "auto"}
def _normalize_api_size(size_str: str) -> str:
    s = (size_str or "").lower().strip()
    if s in ALLOWED_API_SIZES: return s
    if any(x in s for x in ["768","800","512","square"]): return "1024x1024"
    if "1536" in s: return "1536x1024" if s.startswith("1536x") else "1024x1536"
    return "1024x1024"
def _api_width(api_size: str) -> int:
    return 1536 if api_size == "1536x1024" else 1024

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
    # 코드펜스/엔티티 정리
    if not raw: return ""
    s = raw
    s = re.sub(r"```(?:html|HTML)?\s*([\s\S]*?)```", r"\1", s)          # ```html ... ```
    s = re.sub(r"[\"“”]```(?:html|HTML)?\s*([\s\S]*?)```[\"“”]", r"\1", s)  # “```html ... ```”
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
# Title
# =========================
def normalize_title(s:str)->str:
    s = (s or "").strip()
    s = re.sub(r'^[\'"“”‘’《「(]+','',s); s=re.sub(r'[\'"“”‘’》」)]+$','',s)
    return re.sub(r'\s+',' ',s)
def build_title(keyword:str,candidate:str)->str:
    t = cleanup_title(normalize_title(candidate))
    if len(t)<5: t=f"{keyword} 한눈에 정리"
    if len(t)>60: t=t[:60].rstrip()
    return t
HOOK_BENEFIT_TERMS=["총정리","가이드","방법","체크리스트","추천","리뷰","한눈에","최신","가격","비교","요약","핵심"]
def _score_title(t,kw):
    L=len(t)
    return max(0,10-abs(26-L)) + (6 if any(ch.isdigit() for ch in t) else 0) + \
           min(sum(1 for w in HOOK_BENEFIT_TERMS if w in t),6) + \
           (6 if kw.replace(" ","") in t.replace(" ","") else -6)
def generate_hook_title(keyword, model_short):
    p=(f"키워드 '{keyword}'로 22~32자 한국어 SEO 제목 8개. 숫자/후킹단어 활용. "
       "따옴표·이모지·대괄호·마침표 금지. 한 줄에 하나씩.")
    raw=ask_openai(model_short,p,max_tokens=200)["text"]
    cands=[normalize_title(x) for x in raw.splitlines() if x.strip()]
    if len(cands)<3:
        fb=ask_openai(model_short,f"'{keyword}' 핵심 24~28자 제목 3개만",max_tokens=120)["text"]
        cands+=[normalize_title(x) for x in fb.splitlines() if x.strip()]
    best=sorted(cands,key=lambda t:_score_title(t,keyword),reverse=True)[0] if cands else f"{keyword} 한눈에 정리"
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
    return "정보"

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

def _mime_from_ext(path:str):
    ext=os.path.splitext(path.lower())[1]
    return {".webp":"image/webp",".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg"}.get(ext,"application/octet-stream")

def upload_media_to_wp(path:str):
    if DRY_RUN:
        print(f"[DRY] upload_media {path}"); return 0
    url=f"{WP_URL}/wp-json/wp/v2/media"; fn=os.path.basename(path)
    headers={"Content-Disposition":f'attachment; filename="{fn}"',"Content-Type":_mime_from_ext(fn)}
    with open(path,"rb") as f:
        r=requests.post(url,headers=headers,data=f,auth=wp_auth(),timeout=120)
    r.raise_for_status(); return r.json().get("id")

def publish_to_wordpress(title, content, categories, tags, featured_media=None, schedule_dt=None, status="future"):
    url=f"{WP_URL}/wp-json/wp/v2/posts"
    payload={"title":cleanup_title(title),"content":content,"status":status,
             "excerpt":approx_excerpt(content),"categories":categories or [],"tags":tags or []}
    if featured_media: payload["featured_media"]=featured_media
    if status=="future" and schedule_dt:
        utc=schedule_dt.astimezone(dt.timezone.utc)
        payload["date_gmt"]=utc.strftime("%Y-%m-%dT%H:%M:%S")
    return wp_post(url,json=payload)

# =========================
# Thumbnail generation (photo default) + variance check + centered overlay
# =========================
def _category_subject_hint(category:str,title:str)->str:
    c=(category or "").strip()
    if "뉴스" in c:
        return ("Press/briefing ambience with microphones, notepad, camera on tripod; "
                "include 3–5 props; cinematic light; shallow depth of field; avoid plain background.")
    if "쇼핑" in c:
        return ("Unbranded hero product close-up on neutral background; soft daylight; "
                "materials and textures emphasized; include 2–3 subtle props; avoid blank background.")
    return ("Desk/workspace scene: laptop keyboard corner, blank notebook pages, pen, coffee mug; "
            "include 3–5 props; realistic lighting; avoid large empty areas.")

def _image_prompt(title:str, category:str)->str:
    negative = "no text, no typography, no logos, no watermarks"
    if IMAGE_STYLE == "photo":
        style = "Photorealistic photo, clear central subject, rich textures, cinematic lighting."
    elif IMAGE_STYLE in ("3d","isometric"):
        style = "Realistic 3D render, soft global illumination, physically based materials."
    else:
        style = "Modern vector illustration with soft gradients and rich detail."
    comp = "Balanced composition, avoid large empty areas; detailed background with natural shadows/highlights."
    return f"{style} {_category_subject_hint(category,title)} {comp}, {negative}. Square composition."

def _find_kr_font():
    patterns = [
        "/usr/share/fonts/**/NotoSansCJK*",
        "/usr/share/fonts/**/NotoSansKR*",
        "/usr/share/fonts/**/NanumGothic*",
        "/System/Library/Fonts/**/AppleSDGothicNeo*",
        "**/NotoSansCJK*","**/NotoSansKR*","**/NanumGothic*",
    ]
    for pat in patterns:
        for p in glob.glob(pat, recursive=True):
            if p.lower().endswith((".ttf",".otf",".ttc")) and os.path.exists(p):
                return p
    return None

def _wrap_kr(draw, text, font, max_width, max_lines=2):
    words = text.split()
    lines=[]
    if len(words)>1:
        cur=""
        for w in words:
            t = f"{cur} {w}".strip()
            box = draw.textbbox((0,0), t, font=font, stroke_width=0)
            if box[2]-box[0] <= max_width: cur=t
            else:
                if cur: lines.append(cur); cur=w
            if len(lines)>=max_lines: break
        if cur and len(lines)<max_lines: lines.append(cur)
    else:
        cur=""
        for ch in text:
            t=cur+ch
            box=draw.textbbox((0,0), t, font=font, stroke_width=0)
            if box[2]-box[0] <= max_width: cur=t
            else:
                lines.append(cur); cur=ch
                if len(lines)>=max_lines: break
        if cur and len(lines)<max_lines: lines.append(cur)
    return lines[:max_lines]

def _overlay_title_centered(img: Image.Image, title: str)->Image.Image:
    """박스/텍스트가 무조건 화면 안에서 중앙 정렬되도록 크기 자동 조절"""
    title = cleanup_title(title)
    W,H = img.size

    # 1) 폰트 확보(없으면 PIL 기본 폰트)
    font_path = _find_kr_font()
    use_default = False
    if font_path:
        def get_font(sz): return ImageFont.truetype(font_path, sz)
    else:
        use_default = True
        def get_font(sz): return ImageFont.load_default()

    draw = ImageDraw.Draw(img)
    max_w = int(W*0.94)
    max_h = int(H*0.70)
    font_size = int(W*0.12)

    for _ in range(60):   # 축소 루프
        font = get_font(max(font_size, 14))
        lines=_wrap_kr(draw, title, font, max_w, max_lines=2) or [title]
        widths=[]; heights=[]
        for t in lines:
            box=draw.textbbox((0,0), t, font=font, stroke_width=3)
            widths.append(box[2]-box[0]); heights.append(box[3]-box[1])
        pad_x=int(font_size*0.7); pad_y=int(font_size*0.5)
        box_w=max(widths)+pad_x*2
        box_h=sum(heights)+pad_y*2

        if box_w <= max_w and box_h <= max_h:
            # 중앙 배치 후 그리기
            x=(W-box_w)//2; y=(H-box_h)//2
            try:
                draw.rounded_rectangle([x,y,x+box_w,y+box_h], radius=int(font_size*0.6), fill=(0,0,0,200))
            except Exception:
                draw.rectangle([x,y,x+box_w,y+box_h], fill=(0,0,0,200))
            ty=y+pad_y
            for t in lines:
                box=draw.textbbox((0,0), t, font=font, stroke_width=3)
                tw=box[2]-box[0]
                tx=x+(box_w-tw)//2
                draw.text((tx,ty), t, font=font, fill="white", stroke_width=3, stroke_fill="black")
                ty+= (box[3]-box[1])
            return img
        font_size -= 2
        if font_size < 14: break

    # 최후: 작은 박스라도 중앙에
    small = get_font(14)
    box=draw.textbbox((0,0), title, font=small, stroke_width=2)
    tw, th = box[2]-box[0], box[3]-box[1]
    pad=10
    bw, bh = min(tw+pad*2, max_w), min(th+pad*2, max_h)
    x=(W-bw)//2; y=(H-bh)//2
    draw.rectangle([x,y,x+bw,y+bh], fill=(0,0,0,200))
    draw.text((x+(bw-tw)//2, y+(bh-th)//2), title, font=small, fill="white", stroke_width=2, stroke_fill="black")
    return img

def _gen_openai_image(title: str, category: str, size="1024x1024", out="thumb.webp", quality=80):
    # DRY-RUN: 비용 0으로 확인
    if DRY_RUN:
        img = Image.new("RGB", _size_tuple(size), (245,248,252))
        img = _overlay_title_centered(img, title)
        img.save(out, "WEBP", quality=quality)
        print("[DRY] generated placeholder image.")
        return out

    api_size = _normalize_api_size(size)
    base_prompt = _image_prompt(title, category)

    # 최대 2회까지 재생성
    attempt = 0
    while True:
        attempt += 1
        resp = client.images.generate(model="gpt-image-1", prompt=base_prompt, size=api_size, n=1)
        b64 = resp.data[0].b64_json
        img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")

        stdv = ImageStat.Stat(img.convert("L")).stddev[0]
        if stdv >= 18 or attempt >= 2:
            break
        base_prompt += " Highly detailed background, more props and textures, higher contrast, avoid minimalism."

    # 저장 크기 맞추고 중앙 오버레이
    save_w, save_h = _size_tuple(size)
    if (img.width, img.height) != (save_w, save_h):
        try: img = img.resize((save_w, save_h), Image.LANCZOS)
        except Exception: img = img.resize((save_w, save_h))
    img = _overlay_title_centered(img, title)

    img.save(out, "WEBP", quality=quality)
    log_image(size_px=_api_width(api_size))
    print(f"[image] OpenAI api_size={api_size} save_size={save_w}x{save_h}")
    return out

def make_images_or_template(title: str, category: str):
    print(f"[image] OpenAI ({IMAGE_STYLE}, size={IMAGE_SIZE})")
    path = _gen_openai_image(
        title=cleanup_title(title),
        category=category,
        size=IMAGE_SIZE,
        out="thumb.webp",
        quality=IMAGE_QUALITY_WEBP,
    )
    media_id = upload_media_to_wp(path)
    return [media_id]

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
def assemble_content(body:str, media_ids):
    cleaned = process_body_html_or_md(body)
    html_doc = f"{STYLES_CSS}\n<div class='gpt-article'>\n{cleaned}\n</div>"
    ad_method=os.getenv("AD_METHOD","shortcode"); ad_sc=os.getenv("AD_SHORTCODE","[ads_top]")
    ad_mid = os.getenv("AD_INSERT_MIDDLE","true").lower()=="true"
    if ad_method!="shortcode" or not ad_sc: return html_doc
    return html_doc.replace("</style>", f"</style>\n{ad_sc}\n", 1) + (f"\n\n{ad_sc}\n\n" if ad_mid else "")

def generate_two_posts(keywords_today):
    models = recommend_models()
    M_SHORT = (models.get("short") or "").strip() or "gpt-5-nano"
    M_LONG  = (models.get("long")  or "").strip() or "gpt-4o-mini"
    MAX_BODY = models.get("max_tokens_body", 900)

    ctx = ask_openai(M_SHORT,
        f"아래 2개 키워드 각각 5개 소제목과 한줄요약(각 120자 이내)만 목록으로.\n- {keywords_today[0]}\n- {keywords_today[1]}",
        max_tokens=500)["text"]

    posts=[]
    for kw in keywords_today[:2]:
        body_prompt = (
            "다음 개요를 바탕으로 약 1000~1300자 본문을 '순수 HTML'로 작성하라. "
            "섹션 <h2>, 소소제목 <h3>, 단락 <p>만 사용. "
            "중간에 비교 표 1개(<table><thead><tbody>) 포함. "
            "마크다운(##, ``` 등) 금지. 과한 인라인 스타일 금지. "
            "마지막에 <h2>결론</h2> 포함.\n\n"
            f"[키워드] {kw}\n[개요]\n{ctx}"
        )
        body_html = ask_openai(M_LONG, body_prompt, max_tokens=MAX_BODY)["text"]
        body_html = _sanitize_llm_html(body_html)  # ← 코드펜스/엔티티 제거
        title = generate_hook_title(kw, M_SHORT)
        posts.append({"keyword": kw, "title": title, "body": body_html})
    return posts

def create_and_schedule_two_posts():
    # 키워드 2개 랜덤
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
        media_ids = make_images_or_template(final_title, category=cat_name)
        sched = pick_slot(idx)
        res = publish_to_wordpress(
            title=final_title,
            content=assemble_content(post["body"], media_ids),
            categories=cat_ids,
            tags=tag_ids,
            featured_media=media_ids[0] if media_ids else None,
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
