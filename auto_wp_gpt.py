# auto_wp_gpt.py : 글 1개 자동 발행
# 주요 변경점:
# - 이미지 저장 시 WebP 실패하면 PNG로 폴백
# - safe_ascii_filename이 확장자 인자를 받아 PNG/WebP 자동 처리
# - 업로드 시 MIME도 확장자에 맞게 자동 지정

import os, csv, re, io, base64, time, json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from PIL import Image
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

# ── 필수 설정 ──────────────────────────────
WP_URL            = os.getenv("WP_URL", "").rstrip("/")
WP_USER           = os.getenv("WP_USER")
WP_APP_PASSWORD   = os.getenv("WP_APP_PASSWORD")
OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY")
MODEL             = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
if not (WP_URL and WP_USER and WP_APP_PASSWORD and OPENAI_API_KEY):
    raise RuntimeError("'.env'의 WP_URL, WP_USER, WP_APP_PASSWORD, OPENAI_API_KEY 를 확인하세요.")

# ── 게시/분류/키워드 ──────────────────────
POST_STATUS       = os.getenv("POST_STATUS", "publish")
SCHEDULE_KST_HOUR = int(os.getenv("SCHEDULE_KST_HOUR", "9"))
KEYWORDS_CSV      = os.getenv("KEYWORDS_CSV","keywords.csv")
EXISTING_CATEGORIES = [s.strip() for s in os.getenv(
    "EXISTING_CATEGORIES", "뉴스,비공개,쇼핑,전체글,게시글,정보,취미"
).split(",") if s.strip()]
ALLOW_CREATE_TERMS  = os.getenv("ALLOW_CREATE_TERMS","false").lower()=="true"
TAGS_BASE           = [s.strip() for s in os.getenv("TAGS","").split(",") if s.strip()]

# ── 광고 설정 ─────────────────────────────
AD_METHOD       = os.getenv("AD_METHOD", "shortcode").lower()
AD_SHORTCODE    = os.getenv("AD_SHORTCODE", "[ads_top]").strip()
AD_HTML         = os.getenv("AD_HTML", "").encode("utf-8", "ignore").decode("utf-8")
AD_HTML_FILE    = os.getenv("AD_HTML_FILE", "").strip()
AD_INSERT_MIDDLE= os.getenv("AD_INSERT_MIDDLE", "true").lower()=="true"

# ── 이미지 옵션 ───────────────────────────
NUM_IMAGES      = 3
IMAGE_SIZE      = os.getenv("IMAGE_SIZE", "1024x1024")
IMAGE_QUALITY_WEBP  = int(os.getenv("IMAGE_QUALITY_WEBP","82"))
IMAGE_PROMPT_STYLE  = "중립적 다큐 사진, 자연스러운 색감, 텍스트/워터마크 없음, 과도한 인물 클로즈업 피함, 폭력/성적/범죄/정치 선동 배제"

client = OpenAI(api_key=OPENAI_API_KEY)
auth   = HTTPBasicAuth(WP_USER, WP_APP_PASSWORD)

# ── 유틸 ─────────────────────────────────
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

# ── 이미지 관련 (WebP → PNG 폴백) ────────────────────
def encode_image_bytes(image_bytes: bytes, quality:int=82) -> tuple[bytes, str]:
    """
    WebP 저장을 시도하다 실패하면 PNG로 폴백.
    반환: (이미지 바이트, 확장자 ".webp"|".png")
    """
    im = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    try:
        buf = io.BytesIO()
        im.save(buf, format="WEBP", quality=quality, method=6)
        return buf.getvalue(), ".webp"
    except Exception:
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
    r.raise_for_status()
    media = r.json()
    try:
        requests.post(f"{media_url}/{media['id']}", auth=auth, json={"alt_text": alt_text}, timeout=30)
    except Exception:
        pass
    return media

# (중략: 나머지 본문 생성/레이아웃/포스팅 함수는 원본 그대로 두시면 됩니다)

# ── 메인(글 1개) ───────────────────────────────
def main():
    keyword = load_keyword(KEYWORDS_CSV)
    print(f"[0/10] 대상 키워드: {keyword}")

    title  = gen_title(keyword)
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
            figures.append(build_img_figure(media.get("source_url",""), f"{title} - {cap}", cap))
        except Exception as e:
            print(f"[경고] 이미지 {idx} 실패: {e}")
            figures.append(placeholder_figure("이미지 준비 중"))

    # (이하 워드프레스 업로드/카테고리/태그 지정 로직은 원본과 동일)
    ...
