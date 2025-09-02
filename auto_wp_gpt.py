# auto_wp_gpt.py : 글 1개 자동 발행 (다음날 자동 이월 스케줄링 + UI 패치)
# - [예약:HOUR] 마커 유지(워크플로 Guard와 호환)
# - 오늘 해당 회차 예약이 있으면 **내일 같은 회차**로 자동 이월, 오늘/내일 모두 있으면 스킵
# - 소제목 스타일 단일화(H2/H3), 본문 CSS 유틸 강화, 이미지 캡션 제거
# - 이미지: WebP 우선, 실패 시 PNG 폴백 + MIME 자동
# - 디버그 로그 강화

import os, csv, re, io, base64, time, json
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from PIL import Image
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ── 필수 설정 ─────────────────────────────────────────────
WP_URL            = os.getenv("WP_URL", "").rstrip("/")
WP_USER           = os.getenv("WP_USER")
WP_APP_PASSWORD   = os.getenv("WP_APP_PASSWORD")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
MODEL             = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
if not (WP_URL and WP_USER and WP_APP_PASSWORD and OPENAI_API_KEY):
    raise RuntimeError("'.env'의 WP_URL, WP_USER, WP_APP_PASSWORD, OPENAI_API_KEY 를 확인하세요.")

client = OpenAI(api_key=OPENAI_API_KEY)
auth   = HTTPBasicAuth(WP_USER, WP_APP_PASSWORD)

# ── 게시/분류/키워드 ─────────────────────────────────────
POST_STATUS       = os.getenv("POST_STATUS", "future")   # publish | draft | future
SCHEDULE_KST_HOUR = int(os.getenv("SCHEDULE_KST_HOUR", "10"))
# (선택) Guard 단계에서 계산해 넣어줄 수 있음. 없으면 스크립트가 자동 판단.
SCHEDULE_SHIFT_DAYS_ENV = os.getenv("SCHEDULE_SHIFT_DAYS")
KEYWORDS_CSV      = os.getenv("KEYWORDS_CSV","keywords.csv")
EXISTING_CATEGORIES = [s.strip() for s in os.getenv(
    "EXISTING_CATEGORIES", "뉴스,비공개,쇼핑,전체글,게시글,정보,취미"
).split(",") if s.strip()]
ALLOW_CREATE_TERMS  = os.getenv("ALLOW_CREATE_TERMS","false").lower()=="true"
TAGS_BASE           = [s.strip() for s in os.getenv("TAGS","").split(",") if s.strip()]

# ── 광고 설정(.env) ─────────────────────────────────────
AD_METHOD       = os.getenv("AD_METHOD", "shortcode").lower()   # shortcode | raw
AD_SHORTCODE    = os.getenv("AD_SHORTCODE", "[ads_top]").strip()
AD_HTML         = os.getenv("AD_HTML", "").encode("utf-8", "ignore").decode("utf-8")
AD_HTML_FILE    = os.getenv("AD_HTML_FILE", "").strip()
AD_INSERT_MIDDLE= os.getenv("AD_INSERT_MIDDLE", "true").lower()=="true"  # 중간 광고 삽입 여부

# ── 이미지 옵션 (2장 고정) ──────────────────────────────
NUM_IMAGES      = 2
IMAGE_SIZE      = os.getenv("IMAGE_SIZE", "1024x1024")
IMAGE_QUALITY_WEBP  = int(os.getenv("IMAGE_QUALITY_WEBP","82"))
IMAGE_PROMPT_STYLE  = "중립적 다큐 사진, 자연스러운 색감, 텍스트/워터마크 없음, 과도한 인물 클로즈업 피함, 폭력/성적/범죄/정치 선동 배제"

# ── 글로벌 스타일(CSS) ──────────────────────────────────
STYLE_GLOBAL = """
<style>
/* ===== Typography & Layout ===== */
.post-body{line-height:1.85;font-size:17px;color:#1f2937;word-break:keep-all}
.post-body h1{font-size:28px;margin:0 0 16px}
.post-body h2,.post-body h3{margin:22px 0 12px}
.post-body p{margin:0 0 14px}
.post-body hr.soft{border:0;border-top:1px solid #e5e7eb;margin:22px 0}

/* ===== Unified Headings (단일 스타일) ===== */
.h2-unified{display:inline-block;padding-bottom:8px;border-bottom:3px solid #6366f1}
.h3-unified{padding-left:12px;border-left:4px solid #22c55e}

/* ===== Figures & Tables ===== */
.post-body figure{margin:16px 0;text-align:center}
.post-body figure img{max-width:100%;height:auto;border-radius:12px;border:1px solid #e5e7eb}
.post-body figure figcaption{display:none !important} /* 과거 글 캡션도 숨김 */
.post-body table{width:100%;border-collapse:collapse;margin:16px 0;border:1px solid #e5e7eb}
.post-body thead th{background:#f8fafc;font-weight:700}
.post-body td,.post-body th{padding:10px;border:1px solid #e5e7eb;text-align:left}
.post-body table.zebra tbody tr:nth-child(odd){background:#fafafa}

/* ===== Lists & Blockquotes ===== */
.post-body ul{margin:12px 0 12px 20px}
.post-body ol{margin:12px 0 12px 22px}
.post-body li{margin:6px 0}
.post-body blockquote{
  margin:16px 0;padding:14px 16px;border-left:4px solid #9ca3af;
  background:#f8fafc;border-radius:10px;color:#374151
}

/* ===== Emphasis & Code ===== */
.post-body .lead{font-size:18px;color:#111827}
.post-body .muted{color:#6b7280}
.post-body .kbd{font-family:ui-monospace,monospace;border:1px solid #e5e7eb;border-bottom-width:2px;border-radius:6px;padding:2px 6px;background:#f8fafc}
.post-body code{font-family:ui-monospace,monospace;background:#f3f4f6;padding:2px 6px;border-radius:6px}
.post-body pre{overflow:auto;background:#0b1020;color:#e5e7eb;padding:12px 14px;border-radius:12px}
.post-body mark{background:#fef08a;padding:0 4px;border-radius:4px}

/* ===== Cards & Grid ===== */
.post-body .card{background:#fff;border:1px solid #e5e7eb;border-radius:14px;padding:16px;box-shadow:0 4px 10px rgba(2,6,23,.04);margin:14px 0}
.post-body .grid{display:grid;gap:12px}
.post-body .grid.cols-2{grid-template-columns:repeat(2,1fr)}
.post-body .grid.cols-3{grid-template-columns:repeat(3,1fr)}
@media(max-width:720px){.post-body .grid.cols-2,.post-body .grid.cols-3{grid-template-columns:1fr}}

/* ===== Buttons & Links ===== */
.post-body .btn{display:inline-block;padding:10px 14px;border-radius:10px;border:1px solid #e5e7eb;background:#f8fafc}
.post-body .btn.primary{background:#4f46e5;color:#fff;border-color:#4338ca}
.post-body a{color:#2563eb;text-decoration:none}
.post-body a:hover{text-decoration:underline}

/* ===== Summary & Ads ===== */
.post-body .summary{background:#f8fafc;border-left:4px solid #3b82f6;padding:14px 16px;border-radius:10px;margin:16px 0}
.post-body .ad{margin:18px 0}

/* ===== Placeholder ===== */
.placeholder{height:180px;border-radius:12px;background:linear-gradient(135deg,#f1f5f9,#e2e8f0);border:1px dashed #cbd5e1;display:flex;align-items:center;justify-content:center;color:#475569}

/* ===== Mobile ===== */
@media (max-width:640px){ .post-body{font-size:16px} .post-body h1{font-size:24px} }
</style>
""".strip()

# ── 본문 내 스타일 스니펫(톤 정리) ─────────────────────
STYLE_VARIANT_A = """
<style>
.callout-a{background:#eef2ff;border-left:5px solid #6366f1;padding:14px 16px;border-radius:12px;margin:18px 0}
.stat-card{display:flex;gap:12px;align-items:center;background:#ffffff;border:1px solid #e5e7eb;padding:14px;border-radius:12px;box-shadow:0 4px 10px rgba(2,6,23,.04)}
.stat-card .dot{width:10px;height:10px;border-radius:50%;background:#22c55e;margin-top:2px}
.timeline{position:relative;margin:18px 0 6px 0;padding-left:14px}
.timeline::before{content:"";position:absolute;left:6px;top:0;bottom:0;width:2px;background:#e2e8f0}
.timeline .t-item{position:relative;margin:10px 0 10px 10px}
.timeline .t-item .dot{position:absolute;left:-14px;top:4px;width:10px;height:10px;border-radius:50%;background:#60a5fa}
</style>
""".strip()

STYLE_VARIANT_B = """
<style>
.tip-box{background:#ecfeff;border:1px solid #bae6fd;color:#0c4a6e;padding:14px;border-radius:12px;margin:18px 0}
.quote-box{background:#fff7ed;border-left:5px solid #f59e0b;padding:14px;border-radius:12px;margin:18px 0}
.key-card{background:#ffffff;border:1px solid #e2e7f0;border-radius:12px;padding:16px;box-shadow:0 4px 10px rgba(2,6,23,.04)}
</style>
""".strip()

# ── 유틸 ───────────────────────────────────────────────
def strip_bom(s: str) -> str:
    return s.lstrip("\ufeff").strip()

def slugify_ascii(text:str, fallback:str="image") -> str:
    base = re.sub(r"[^\w-]+", "-", strip_bom(text)).strip("-").lower()
    return base[:80] if base else fallback

def slugify_ko(text:str)->str:
    text = re.sub(r"<.*?>", "", text or "")
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", "-", text.strip())
    return text.lower()[:120] if text else ""

def load_keyword(path:str)->str:
    if not os.path.exists(path): return "오늘의 이슈"
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if row and row[0].strip():
                return strip_bom(row[0])
    return "오늘의 이슈"

def tidy_text(html: str) -> str:
    html = re.sub(r"\n{3,}", "\n\n", html)
    lines = html.splitlines()
    out, prev = [], None
    for ln in lines:
        if prev is None or ln.strip() != prev.strip():
            out.append(ln)
        prev = ln
    return "\n".join(out)

def excerpt_from_html(html: str) -> str:
    txt = re.sub(r"<style.*?</style>", " ", html, flags=re.DOTALL|re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\[/?[^\]]+\]", " ", txt)     # 쇼트코드 제거
    txt = re.sub(r"\s+", " ", txt).strip()
    return (txt[:150] + "...") if len(txt) > 150 else txt

# ── 카테고리 자동 분류 ─────────────────────────────────
SHOP_WORDS  = {"쇼핑","특가","할인","쿠폰","리뷰","언박싱","구매","최저가"}
NEWS_WORDS  = {"속보","브리핑","발표","논란","사건","쟁점","분석","현황","여론","선거","정책"}
HOBBY_WORDS = {"게임","여행","캠핑","등산","사진","요리","낚시","운동","영화","음악"}
INFO_WORDS  = {"가이드","방법","설명","정리","팁","노하우","튜토리얼","설치","세팅","문제해결"}

def choose_categories(keyword: str, plain_text: str) -> list[str]:
    text = (keyword + " " + plain_text).lower()
    cats = []
    if any(w.lower() in text for w in SHOP_WORDS):  cats.append("쇼핑")
    if any(w.lower() in text for w in NEWS_WORDS):  cats.append("뉴스")
    if any(w.lower() in text for w in HOBBY_WORDS): cats.append("취미")
    if any(w.lower() in text for w in INFO_WORDS):  cats.append("정보")
    if not cats: cats = ["정보"]
    return [c for c in cats if c in EXISTING_CATEGORIES] or (["전체글"] if "전체글" in EXISTING_CATEGORIES else ["정보"])

# ── OpenAI: 제목/본문 ─────────────────────────────────
TITLE_GUIDE = """
한국어 블로그 H1 제목 한 줄만 출력하세요.
[조건] 22~28자, 키워드와 강한 연관(가능하면 포함), 과장/낚시 금지, 자연스러운 말투, 따옴표·괄호·이모지 금지
"""

BODY1_GUIDE = """
HTML 조각만 출력하세요(워드프레스 본문용). <h1>은 출력하지 않습니다.
필수 포함:
- <h2> 1개(설명형/질문형)
- <div class="summary"><p>요약 300자 이내, 존댓말</p></div>
- <section id="body1"> 3~5개 짧은 문단(총 400~600자)</section>
마크다운/지침 금지.
"""

BODY2_GUIDE = """
HTML 조각만 출력하세요(워드프레스 본문용). <h1>은 출력하지 않습니다.
필수 포함:
- <hr> 로 시작
- <section id="body2"> 최소 1200~1600자 분량, 다양한 <h3> + <p>
- 섹션 내부에 실제 <table><thead><tr><th>…</th></tr></thead><tbody>…</tbody></table> 1개 포함(2x2, 3x3, 4x5 중 임의)
- 맺음말 포함
마크다운/지침 금지.
"""

IMG_PROMPT_GUIDE = """
아래 제목과 키워드에 맞춰 블로그용 이미지 설명 N개를 JSON 배열로만 반환하세요.
각 설명은 한국어 15~25자, 중립적·비인물·비논쟁적 콘셉트(서류, 책상, 도시 전경, 그래프, 자연 풍경 등), 텍스트/워터마크 금지.
반환 예: ["설명1","설명2",...]
"""

def gen_title(keyword: str, hour: str) -> str:
    print(f"[1/10] 제목 생성… ({keyword})")
    try:
        r = client.chat.completions.create(
            model=MODEL,
            messages=[{"role":"user","content": TITLE_GUIDE + f"\n키워드: {keyword}"}]
        )
        title = (r.choices[0].message.content or "").strip()
    except Exception as e:
        print(f"[경고] 제목 생성 실패: {e} → 기본 제목 사용")
        title = f"{keyword} 핵심정리와 실전 가이드"

    title = re.sub(r"[\"“”‘’'<>]", "", title)
    title = re.sub(r"\s+", " ", title).strip()
    if len(title) < 20:
        title = f"{keyword} 핵심정리와 실전 가이드"
    return f"[예약:{hour}] {title[:30]}"

def gen_body1(keyword: str, title: str) -> str:
    print("[2/10] 요약/본문1 생성…")
    up = BODY1_GUIDE + f"\n제목: {title}\n키워드: {keyword}\n"
    r = client.chat.completions.create(model=MODEL, messages=[{"role":"user","content":up}])
    return tidy_text(r.choices[0].message.content or "")

def gen_body2(keyword: str, title: str) -> str:
    print("[3/10] 본문2 생성…")
    up = BODY2_GUIDE + f"\n제목: {title}\n키워드: {keyword}\n"
    r = client.chat.completions.create(model=MODEL, messages=[{"role":"user","content":up}])
    return tidy_text(r.choices[0].message.content or "")

def gen_image_captions(keyword:str, title:str, n:int) -> list[str]:
    print("[4/10] 이미지 캡션(설명 텍스트) 생성…")
    r = client.chat.completions.create(model=MODEL, messages=[{"role":"user","content": IMG_PROMPT_GUIDE + f"\n제목:{title}\n키워드:{keyword}\n개수:{n}"}])
    txt = (r.choices[0].message.content or "").strip()
    try:
        arr = json.loads(txt)
        arr = [str(x) for x in arr][:n]
        while len(arr) < n: arr.append("중립적 배경 이미지")
        return arr
    except Exception:
        return ["중립적 배경 이미지" for _ in range(n)]

# ── 이미지: 생성/압축(폴백)/업로드 ───────────────────────
def openai_generate_image_bytes(prompt:str, safe_retry=False) -> bytes:
    p = prompt if not safe_retry else f"중립적 개념 이미지: 책상 위 서류, 그래프 화면, 도시 풍경. 텍스트/로고/워터마크/인물 전면 없음. {IMAGE_PROMPT_STYLE}"
    r = client.images.generate(model="gpt-image-1", prompt=f"{p}, {IMAGE_PROMPT_STYLE}", size=IMAGE_SIZE)
    b64 = r.data[0].b64_json
    return base64.b64decode(b64)

def encode_image_bytes(image_bytes: bytes, quality:int=82) -> tuple[bytes, str]:
    """WebP 저장 시도 → 실패하면 PNG 폴백. (bytes, ext) 반환"""
    im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    try:
        buf = io.BytesIO()
        im.save(buf, format="WEBP", quality=quality, method=6)
        return buf.getvalue(), ".webp"
    except Exception as e:
        print(f"[DBG] WebP 저장 실패 → PNG 폴백: {e}")
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue(), ".png"

def safe_ascii_filename(title:str, idx:int, ext:str=".webp") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    base  = slugify_ascii(title or "image", "image")
    return f"{base}-{stamp}-{idx:02d}{ext}"

def wp_upload_media(filename:str, image_bytes:bytes, alt_text:str) -> dict:
    media_url = f"{WP_URL}/wp-json/wp/v2/media"
    ext = filename.lower().rsplit(".",1)[-1]
    mime = "image/webp" if ext == "webp" else "image/png"
    files = {"file": (filename, image_bytes, mime)}
    data  = {"title": os.path.splitext(filename)[0]}
    r = requests.post(media_url, auth=auth, files=files, data=data, timeout=120)
    print(f"[DBG] Media upload code={r.status_code}, mime={mime}, name={filename}")
    print(f"[DBG] Media body: {r.text[:300]}")
    r.raise_for_status()
    media = r.json()
    try:
        r2 = requests.post(f"{media_url}/{media['id']}", auth=auth, json={"alt_text": alt_text}, timeout=30)
        print(f"[DBG] Media alt update code={r2.status_code}")
    except Exception as e:
        print(f"[DBG] Media alt update skip: {e}")
    return media

# ⬇︎ 캡션 없는 그림만 출력
def build_img_figure(src:str, alt:str):
    return f'<figure><img loading="lazy" decoding="async" src="{src}" alt="{alt}"></figure>'

def placeholder_figure(text:str):
    return f'<div class="placeholder">{text}</div>'

# ── 광고 블록 로더 ─────────────────────────────────────
def load_ad_block() -> str:
    if AD_METHOD == "shortcode" and AD_SHORTCODE:
        return f'<div class="ad">{AD_SHORTCODE}</div>'
    if AD_METHOD == "raw":
        if AD_HTML_FILE and os.path.exists(AD_HTML_FILE):
            try:
                with open(AD_HTML_FILE, "r", encoding="utf-8") as f:
                    return f'<div class="ad">{f.read()}</div>'
            except Exception as e:
                print("[DBG] AD_HTML_FILE read fail:", e)
        if AD_HTML:
            raw = AD_HTML.replace("\\n", "\n")
            return f'<div class="ad">{raw}</div>'
    return '<div class="ad"><!-- 광고 영역 --></div>'

# ── 소제목 스타일 “단일화” 주입 ─────────────────────────
def _inject_class(tag_open:str, cls:str) -> str:
    if re.search(r'class\s*=\s*"', tag_open, flags=re.IGNORECASE):
        return re.sub(r'(class\s*=\s*")', r'\1'+cls+' ', tag_open, count=1, flags=re.IGNORECASE)
    return re.sub(r"(<h[23])", r'\1 class="'+cls+'"', tag_open, count=1, flags=re.IGNORECASE)

def stylize_headings(html:str)->str:
    # H2 → .h2-unified, H3 → .h3-unified
    html = re.sub(r"<h2[^>]*>", lambda m: _inject_class(m.group(0), "h2-unified"), html, flags=re.IGNORECASE)
    html = re.sub(r"<h3[^>]*>", lambda m: _inject_class(m.group(0), "h3-unified"), html, flags=re.IGNORECASE)
    return html

# ── 본문용 리치 모듈 ───────────────────────────────────
def rich_modules(title:str, keyword:str) -> tuple[str,str]:
    mod_a = f'''
{STYLE_VARIANT_A}
<div class="callout-a"><p>핵심: "{keyword}" 주제를 일상에 적용하려면 오늘 하나만 바꿔도 충분합니다. 작게 시작해도 꾸준하면 커집니다.</p></div>
<div class="stat-card"><span class="dot"></span><div><p>집중 포인트: 환경 정리 → 루틴 고정 → 방해요인 차단</p></div></div>
<div class="timeline">
  <div class="t-item"><span class="dot"></span><div class="t-body"><p>Step 1: 오늘 책상 위 3가지만 남겨두기</p></div></div>
  <div class="t-item"><span class="dot"></span><div class="t-body"><p>Step 2: 자주 쓰는 도구는 한 팔 내로 배치</p></div></div>
  <div class="t-item"><span class="dot"></span><div class="t-body"><p>Step 3: 끝나면 2분 정리, 사진으로 상태 기록</p></div></div>
</div>
'''.strip()

    mod_b = f'''
{STYLE_VARIANT_B}
<div class="tip-box"><p>작은 팁: 타이머 25분에 알림을 맞추고, 끝나면 자리에서 꼭 일어나 스트레칭하세요. 리셋이 집중을 지켜줍니다.</p></div>
<div class="quote-box"><p>"꾸준함은 의지보다 시스템에서 나온다."</p></div>
<div class="key-card"><p>정리: 제목 "{title}" 에서 말하는 핵심은 '꾸준히 유지 가능한 구조'입니다. 과하지 않게, 그러나 매일.</p></div>
'''.strip()
    return mod_a, mod_b

# ── 레이아웃 조립 ─────────────────────────────────────
def assemble_post(title:str, body1_html:str, body2_html:str, figures_top:list[str], figures_mid:list[str], keyword:str) -> str:
    ad_top = load_ad_block()
    mod_a, mod_b = rich_modules(title, keyword)
    parts = []
    parts.append(f"<h1>{title}</h1>")
    parts.append(ad_top)
    parts.append(body1_html)
    parts.append(mod_a)
    if figures_top: parts.append("\n".join(figures_top))  # 상단 이미지 2장
    parts.append("<hr class='soft'>")
    if AD_INSERT_MIDDLE:
        parts.append(load_ad_block())
    parts.append(mod_b)
    parts.append(body2_html)
    html = "\n".join(parts)
    html = re.sub(r"<hr\s*/?>", '<hr class="soft">', html, flags=re.IGNORECASE)
    html = stylize_headings(html)
    return STYLE_GLOBAL + f'\n<div class="post-body">\n{html}\n</div>'

# ── WP 용어(term) 유틸 ─────────────────────────────────
def wp_search_terms(kind:str, search:str):
    url = f"{WP_URL}/wp-json/wp/v2/{kind}"
    r = requests.get(url, auth=auth, params={"search":search, "per_page":100}, timeout=30)
    print(f"[DBG] Search {kind} '{search}' code={r.status_code}")
    print(f"[DBG] Search body: {r.text[:200]}")
    r.raise_for_status()
    return r.json()

def get_term_id(kind:str, name:str)->int|None:
    for it in wp_search_terms(kind, name):
        if it.get("name") == name:
            return it["id"]
    return None

def ensure_term(kind:str, name:str)->int|None:
    tid = get_term_id(kind, name)
    if tid is not None: return tid
    if not ALLOW_CREATE_TERMS: return None
    r = requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", auth=auth, json={"name":name}, timeout=30)
    print(f"[DBG] Create {kind} '{name}' code={r.status_code}")
    print(f"[DBG] Create body: {r.text[:200]}")
    r.raise_for_status()
    return r.json()["id"]

# ── 회차별 예약 존재 여부 조회 ──────────────────────────
def _exists_for_date(hour: str, base_date: date) -> bool:
    try:
        kst = ZoneInfo("Asia/Seoul")
        after  = datetime(base_date.year, base_date.month, base_date.day, 0,0,0, tzinfo=kst).isoformat()
        before = datetime(base_date.year, base_date.month, base_date.day, 23,59,59, tzinfo=kst).isoformat()
        url = f"{WP_URL}/wp-json/wp/v2/posts"
        params = {"status":"future", "after":after, "before":before, "per_page":100}
        r = requests.get(url, auth=auth, params=params, timeout=30)
        print(f"[DBG] Exists-check {base_date} code={r.status_code}")
        print(f"[DBG] Exists body: {r.text[:200]}")
        r.raise_for_status()
        posts = r.json()
        return any(f"[예약:{hour}]" in (p.get("title",{}).get("rendered","")) for p in posts)
    except Exception as e:
        print(f"[DBG] Exists-check error: {e} (treat as not existing)")
        return False

def _compute_shift_days(hour: str) -> int | None:
    """
    반환:
      0 => 오늘 예약
      1 => 내일로 이월
      None => 오늘/내일 모두 존재 → 스킵
    우선순위: 환경변수 SCHEDULE_SHIFT_DAYS > 자동판단
    """
    if SCHEDULE_SHIFT_DAYS_ENV is not None:
        try:
            v = int(SCHEDULE_SHIFT_DAYS_ENV)
            return max(0, min(1, v))
        except Exception:
            pass

    kst = ZoneInfo("Asia/Seoul")
    today = datetime.now(kst).date()
    tomorrow = today + timedelta(days=1)

    today_has = _exists_for_date(hour, today)
    if not today_has:
        return 0
    # 오늘 이미 있음 → 내일 확인
    tomorrow_has = _exists_for_date(hour, tomorrow)
    if not tomorrow_has:
        return 1
    # 오늘/내일 모두 존재 → 스킵
    return None

# ── 포스팅 ─────────────────────────────────────────────
def create_post(title, content_html, cat_ids, tag_ids, featured_media_id=None, shift_days: int = 0):
    payload = {
        "title": title,
        "content": content_html,
        "excerpt": excerpt_from_html(content_html),
        "slug": slugify_ko(title),
        "status": POST_STATUS,
        "categories": cat_ids,
        "tags": tag_ids
    }
    if featured_media_id:
        payload["featured_media"] = featured_media_id
    if POST_STATUS == "future":
        kst = ZoneInfo("Asia/Seoul")
        base = datetime.now(kst).date() + timedelta(days=shift_days)
        schedule_kst = datetime(base.year, base.month, base.day, SCHEDULE_KST_HOUR, 0, 0, tzinfo=kst)
        payload["date_gmt"] = schedule_kst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    print("[DBG] Posting payload summary:",
          {"status": payload["status"], "len_content": len(payload["content"]), "cats": cat_ids, "tags": tag_ids,
           "has_feat": bool(featured_media_id), "shift_days": shift_days, "hour": SCHEDULE_KST_HOUR})

    r = requests.post(f"{WP_URL}/wp-json/wp/v2/posts", auth=auth, json=payload, timeout=120)
    print("[DBG] WP response code:", r.status_code)
    print("[DBG] WP response body:", r.text[:500])
    r.raise_for_status()
    return r.json()

# ── 메인(글 1개) ───────────────────────────────────────
def main():
    hour = str(SCHEDULE_KST_HOUR)

    # 목표 날짜(오늘/내일) 계산
    shift = _compute_shift_days(hour)
    if shift is None:
        print(f"[가드] 오늘과 내일 모두 {hour}KST 예약글이 존재합니다. 스킵합니다.")
        raise SystemExit(0)

    keyword = load_keyword(KEYWORDS_CSV)
    print(f"[0/10] 대상 키워드: {keyword} / 회차: {hour}KST / shift_days={shift}")

    title  = gen_title(keyword, hour)   # 마커 포함
    body1  = gen_body1(keyword, title)
    body2  = gen_body2(keyword, title)

    captions = gen_image_captions(keyword, title, NUM_IMAGES)
    figures, media_ids = [], []
    for idx, cap in enumerate(captions, start=1):
        try:
            print(f"[5/10] 이미지 생성 {idx}/{len(captions)} …")
            try:
                raw = openai_generate_image_bytes(cap, safe_retry=False)
            except Exception as e:
                print(f"[참고] 1차 생성 실패 → 중립 재시도: {e}")
                time.sleep(1)
                raw = openai_generate_image_bytes(cap, safe_retry=True)

            enc, ext = encode_image_bytes(raw, IMAGE_QUALITY_WEBP)
            fn = safe_ascii_filename(title, idx, ext=ext)
            media = wp_upload_media(fn, enc, alt_text=f"{title} - {cap}")
            media_ids.append(media.get("id"))
            # ⬇︎ 캡션 없이 그림만
            figures.append(build_img_figure(media.get("source_url",""), f"{title} - {cap}"))
        except Exception as e:
            print(f"[경고] 이미지 {idx} 실패: {e}")
            figures.append(placeholder_figure("이미지 준비 중"))

    featured_id = next((m for m in media_ids if m), None)
    figures_top, _ = figures[:2], []  # 2장 고정

    html   = assemble_post(title, body1, body2, figures_top, [], keyword)

    plain  = re.sub(r"<[^>]+>", " ", html)
    cat_names = choose_categories(keyword, plain)
    print("[DBG] category guess:", cat_names)

    # 항상 '전체글' 포함
    if "전체글" in EXISTING_CATEGORIES and "전체글" not in cat_names:
        cat_names.append("전체글")

    cat_ids = []
    for name in cat_names:
        cid = ensure_term("categories", name) if ALLOW_CREATE_TERMS else get_term_id("categories", name)
        if cid: cat_ids.append(cid)
    if not cat_ids:
        fallback = "전체글" if "전체글" in EXISTING_CATEGORIES else "정보"
        fid = get_term_id("categories", fallback)
        if fid: cat_ids = [fid]
    print("[DBG] category ids:", cat_ids)

    tag_ids = []
    for t in TAGS_BASE:
        tid = ensure_term("tags", t) if ALLOW_CREATE_TERMS else get_term_id("tags", t)
        if tid: tag_ids.append(tid)
    kw_tag = get_term_id("tags", keyword) or (ensure_term("tags", keyword) if ALLOW_CREATE_TERMS else None)
    if kw_tag: tag_ids.append(kw_tag)
    print("[DBG] tag ids:", tag_ids)

    print(f"[8/10] 워드프레스 업로드… | 카테고리={cat_names} | 이미지={sum(1 for x in media_ids if x)}장")
    post = create_post(title, html, cat_ids, tag_ids, featured_media_id=featured_id, shift_days=shift)
    print(f"[10/10] 완료 :", post.get('link'))

if __name__ == "__main__":
    main()
