# -*- coding: utf-8 -*-
"""
affiliate_post.py — Coupang Partners 글 자동 포스팅 (상단 고지문/CTA x2, 하단 CTA x2, 템플릿 고정)
- 상단 고지문(굵게/강조) + 상단 CTA 2개 + 카테고리 이동 버튼 + 내부광고(상단)
- 본문 섹션: 고려요소 → 주요 특징 → 가격/가성비 → (내부광고) → 장단점 → 이런 분께 추천
- 하단 CTA 2개 + 카테고리 이동 버튼
- URL 없을 때 쿠팡 검색 페이지 폴백
- 골든키워드 회전/사용로그/예약 충돌 회피(기존 유지)
- NEW: 제목 생성 개선 (고정 문구 제거, 길이/가독성 최적화, LLM+템플릿), 제품명 요약
"""
import os, re, csv, json, html, random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List
import requests
from dotenv import load_dotenv
from urllib.parse import quote, quote_plus  # 카테고리/검색 폴백 URL용
load_dotenv()

# ========= OpenAI (optional) =========
try:
    from openai import OpenAI, BadRequestError
except Exception:
    OpenAI = None
    BadRequestError = Exception

_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_OPENAI_MODEL = (os.getenv("OPENAI_MODEL_LONG") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini")
_oai = OpenAI(api_key=_OPENAI_API_KEY) if (_OPENAI_API_KEY and OpenAI) else None

# ===== Affiliate title options =====
AFF_TITLE_MIN = int(os.getenv("AFF_TITLE_MIN", "14"))
AFF_TITLE_MAX = int(os.getenv("AFF_TITLE_MAX", "26"))
# llm-then-template | template | llm
AFF_TITLE_MODE = (os.getenv("AFF_TITLE_MODE") or "llm-then-template").lower()
AFF_BANNED_PHRASES = ("제대로 써보고 알게 된 포인트","써보고 알게 된 포인트")

AFF_TITLE_TEMPLATES = [
    "{name}, 한눈에 핵심만",
    "{name} 장단점 총정리",
    "{name} 7일 써본 결론",
    "{name} 실사용 팁 모음",
    "{name} 이렇게 쓰니 편해요",
    "{name} 쓰고 달라진 점",
    "{name} 이런 분께 맞아요",
    "{name} 아쉬운 점까지 솔직히",
    "{name} 구매 전 체크리스트",
    "{name} 첫인상부터 실전까지",
    "{name} 놓치기 쉬운 포인트",
    "{name} 핵심만 쏙 정리",
]

def _normalize_title(s: str) -> str:
    s = (s or "").strip()
    s = html.unescape(s)
    s = s.replace("“","").replace("”","").replace("‘","").replace("’","").strip('"\' ')
    s = re.sub(r"\s+"," ",s)
    return s

def _bad_aff_title(t: str) -> bool:
    if not t: return True
    if not (AFF_TITLE_MIN <= len(t) <= AFF_TITLE_MAX): return True
    if any(p in t for p in AFF_BANNED_PHRASES): return True
    if any(x in t for x in ("최저가","역대급","무조건","100%","클릭")): return True
    return False

def _aff_title_from_templates(name: str, kw: str) -> str:
    # 하루 단위로 씨드 고정 → 같은 상품이라도 매일 다른 제목
    seed = abs(hash(f"{name}|{kw}|{datetime.utcnow().date()}")) % (2**32)
    random.seed(seed)
    for _ in range(6):
        cand = _normalize_title(random.choice(AFF_TITLE_TEMPLATES).format(name=name.strip()))
        if not _bad_aff_title(cand):
            return cand
    # 최후의 보루
    fallback = _normalize_title(f"{name.strip()} 핵심 체크")
    return fallback if not _bad_aff_title(fallback) else _normalize_title(name.strip())

def _aff_title_from_llm(name: str, kw: str) -> str:
    if not _oai:
        return ""
    try:
        sys_p = "너는 한국어 카피라이터다. 쇼핑 포스트용 짧고 담백한 제목 1개만 출력한다."
        usr = f"""상품명(요약): {name}
원 키워드: {kw}
조건:
- 길이 {AFF_TITLE_MIN}~{AFF_TITLE_MAX}자
- 금지문구: {", ".join(AFF_BANNED_PHRASES)}
- 과장/낚시 금지(최저가/역대급 등)
- 반복 패턴 금지
- 출력은 제목 1줄(순수 텍스트)"""
        r = _oai.chat.completions.create(
            model=_OPENAI_MODEL,
            messages=[{"role":"system","content":sys_p},{"role":"user","content":usr}],
            temperature=0.9,
            max_tokens=60,
        )
        cand = _normalize_title(r.choices[0].message.content or "")
        return "" if _bad_aff_title(cand) else cand
    except BadRequestError:
        return ""
    except Exception as e:
        print(f"[AFF-TITLE][WARN] {type(e).__name__}: {e}")
        return ""

def hook_aff_title(product_name_short: str, keyword: str) -> str:
    title = ""
    mode_used = "template"
    if AFF_TITLE_MODE in ("llm","llm-then-template"):
        title = _aff_title_from_llm(product_name_short, keyword)
        mode_used = "llm"
    if not title:
        t2 = _aff_title_from_templates(product_name_short, keyword)
        if t2: title = t2; mode_used = "template"
    print(f"[AFF-TITLE] mode={mode_used}, name='{product_name_short}', title='{title}'")
    return title

# ========= ENV =========
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()

DEFAULT_CATEGORY=(os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip() or "쇼핑"
DEFAULT_TAGS=(os.getenv("AFFILIATE_TAGS") or "").strip()
DISCLOSURE_TEXT=(os.getenv("DISCLOSURE_TEXT") or "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공합니다.").strip()

BUTTON_TEXT=(os.getenv("BUTTON_TEXT") or "쿠팡에서 최저가 확인하기").strip()
BUTTON2_TEXT=(os.getenv("BUTTON2_TEXT") or "제품 보러가기").strip()
BUTTON2_URL=(os.getenv("BUTTON2_URL") or "").strip()

USE_IMAGE=((os.getenv("USE_IMAGE") or "").strip().lower() in ("1","true","y","yes","on"))
AFFILIATE_TIME_KST=(os.getenv("AFFILIATE_TIME_KST") or "13:00").strip()

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-affiliate/1.9"
USAGE_DIR=os.getenv("USAGE_DIR") or ".usage"
USED_FILE=os.path.join(USAGE_DIR,"used_shopping.txt")

NO_REPEAT_TODAY=(os.getenv("NO_REPEAT_TODAY") or "1").lower() in ("1","true","y","yes","on")
AFF_USED_BLOCK_DAYS=int(os.getenv("AFF_USED_BLOCK_DAYS") or "30")

PRODUCTS_SEED_CSV=(os.getenv("PRODUCTS_SEED_CSV") or "products_seed.csv")

REQ_HEADERS={
    "User-Agent": USER_AGENT,
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
}

# ========= TIME / SLOT =========
def _now_kst():
    return datetime.now(ZoneInfo("Asia/Seoul"))

def _wp_future_exists_around(when_gmt_dt: datetime, tol_min: int = 2) -> bool:
    url = f"{WP_URL}/wp-json/wp/v2/posts"
    try:
        r = requests.get(
            url, params={"status":"future","per_page":100,"orderby":"date","order":"asc","context":"edit"},
            headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20
        ); r.raise_for_status()
        items = r.json()
    except Exception as e:
        print(f"[WP][WARN] future list fetch failed: {type(e).__name__}: {e}")
        return False
    tgt = when_gmt_dt.astimezone(timezone.utc)
    win = timedelta(minutes=max(1,int(tol_min)))
    lo, hi = tgt - win, tgt + win
    for it in items:
        d=(it.get("date_gmt") or "").strip()
        if not d: continue
        try:
            dt=datetime.fromisoformat(d.replace("Z","+00:00"))
            dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except Exception:
            continue
        if lo <= dt <= hi:
            return True
    return False

def _slot_affiliate()->str:
    hh, mm = [int(x) for x in (AFFILIATE_TIME_KST.split(":")+["0"])[:2]]
    now = _now_kst()
    tgt = now.replace(hour=hh,minute=mm,second=0,microsecond=0)
    if tgt <= now: tgt += timedelta(days=1)
    for _ in range(7):
        utc = tgt.astimezone(timezone.utc)
        if _wp_future_exists_around(utc, tol_min=2):
            print(f"[SLOT] conflict at {utc.strftime('%Y-%m-%dT%H:%M:%S')}Z -> push +1d")
            tgt += timedelta(days=1); continue
        break
    final = tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[SLOT] scheduled UTC = {final}")
    return final

# ========= USED LOG =========
def _ensure_usage_dir(): os.makedirs(USAGE_DIR, exist_ok=True)

def _load_used_set(days:int=30)->set:
    _ensure_usage_dir()
    if not os.path.exists(USED_FILE): return set()
    cutoff=datetime.utcnow().date()-timedelta(days=days)
    used=set()
    with open(USED_FILE,"r",encoding="utf-8",errors="ignore") as f:
        for line in f:
            line=line.strip()
            if not line: continue
            try:
                d_str, kw = line.split("\t",1)
                if datetime.strptime(d_str,"%Y-%m-%d").date()>=cutoff:
                    used.add(kw.strip())
            except Exception:
                used.add(line)
    return used

def _mark_used(kw:str):
    _ensure_usage_dir()
    with open(USED_FILE,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw.strip()}\n")

# ========= CSV =========
def _read_col_csv(path:str)->List[str]:
    if not os.path.exists(path): return []
    out=[]
    with open(path,"r",encoding="utf-8",newline="") as f:
        rd=csv.reader(f)
        for i,row in enumerate(rd):
            if not row: continue
            if i==0 and (row[0].strip().lower() in ("keyword","title")): continue
            if row[0].strip(): out.append(row[0].strip())
    return out

def _consume_col_csv(path:str, kw:str)->bool:
    if not os.path.exists(path): return False
    with open(path,"r",encoding="utf-8",newline="") as f:
        rows=list(csv.reader(f))
    if not rows: return False
    has_header=rows[0] and rows[0][0].strip().lower() in ("keyword","title")
    body=rows[1:] if has_header else rows[:]
    before=len(body)
    body=[r for r in body if (r and r[0].strip()!=kw)]
    if len(body)==before: return False
    new_rows=([rows[0]] if has_header else [])+[[r[0].strip()] for r in body]
    with open(path,"w",encoding="utf-8",newline="") as f:
        csv.writer(f).writerows(new_rows)
    return True

# ========= KEYWORD / URL =========
def pick_affiliate_keyword()->str:
    NO_REPEAT = (_load_used_set(1) if NO_REPEAT_TODAY else set())
    used_block = _load_used_set(AFF_USED_BLOCK_DAYS)
    gold=_read_col_csv("golden_shopping_keywords.csv")
    shop=_read_col_csv("keywords_shopping.csv")
    pool=[k for k in gold+shop if k and (k not in used_block)]
    if NO_REPEAT_TODAY:
        pool=[k for k in pool if k not in NO_REPEAT]
    if pool: return pool[0].strip()
    fb=[x.strip() for x in (os.getenv("AFF_FALLBACK_KEYWORDS") or "").split(",") if x.strip()]
    return fb[0] if fb else "휴대용 선풍기"

def resolve_product_url(keyword:str)->str:
    # 1) products_seed.csv 우선
    if os.path.exists(PRODUCTS_SEED_CSV):
        try:
            with open(PRODUCTS_SEED_CSV,"r",encoding="utf-8") as f:
                rd=csv.DictReader(f)
                for r in rd:
                    if (r.get("keyword") or "").strip()==keyword and (r.get("url") or "").strip():
                        return r["url"].strip()
                    if (r.get("product_name") or "").strip()==keyword and (r.get("url") or "").strip():
                        return r["url"].strip()
                    if (r.get("raw_url") or "").strip() and (r.get("product_name") or "").strip()==keyword:
                        return r["raw_url"].strip()
        except Exception as e:
            print(f"[SEED][WARN] read error: {e}")
    # 2) 안전 폴백: 쿠팡 검색
    return f"https://www.coupang.com/np/search?q={quote_plus(keyword)}"

# ========= WP =========
def _ensure_term(kind:str, name:str)->int:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}", params={"search":name,"per_page":50,"context":"edit"},
                   auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name: return int(it["id"])
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status(); return int(r.json()["id"])

def _category_url_for(name:str)->str:
    """카테고리 링크를 WP API에서 찾고, 실패 시 /category/<이름>/ 로 폴백."""
    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/categories",
            params={"search": name, "per_page": 50, "context":"view"},
            headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=12
        )
        r.raise_for_status()
        items = r.json()
        for it in items:
            if (it.get("name") or "").strip() == name:
                link = (it.get("link") or "").strip()
                if link: return link
        if items and (items[0].get("link") or "").strip():
            return items[0]["link"].strip()
    except Exception as e:
        print(f"[CAT][WARN] fallback category url for '{name}': {type(e).__name__}: {e}")
    return f"{WP_URL}/category/{quote(name)}/"

def post_wp(title:str, html_body:str, when_gmt:str, category:str, tag:str)->dict:
    cat_id=_ensure_term("categories", category or DEFAULT_CATEGORY)
    tag_ids=[]
    if tag:
        try:
            tid=_ensure_term("tags", tag); tag_ids=[tid]
        except Exception:
            pass
    payload={
        "title": title,
        "content": html_body,
        "status": POST_STATUS,
        "categories": [cat_id],
        "tags": tag_ids,
        "comment_status": "closed",
        "ping_status": "closed",
        "date_gmt": when_gmt
    }
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status(); return r.json()

# ========= TEMPLATE =========
def _css_block()->str:
    return """
<style>
.aff-wrap{font-family:inherit}
.aff-disclosure{margin:0 0 16px;padding:12px 14px;border:2px solid #ef4444;background:#fff1f2;color:#991b1b;font-weight:700;border-radius:10px}
.aff-cta{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0 14px}
.aff-cta a{display:inline-block;padding:12px 18px;border-radius:999px;text-decoration:none;font-weight:700}
.aff-cta a.btn-primary{background:#2563eb;color:#fff}
.aff-cta a.btn-primary:hover{opacity:.95}
.aff-cta a.btn-secondary{background:#fff;color:#2563eb;border:2px solid #2563eb}
.aff-cta a.btn-secondary:hover{background:#eff6ff}
.aff-cta a.btn-tertiary{background:#0f172a;color:#fff;border:0}
.aff-cta a.btn-tertiary:hover{opacity:.92}
.aff-section h2{margin:28px 0 12px;font-size:1.42rem;line-height:1.35;border-left:6px solid #22c55e;padding-left:10px}
.aff-section h3{margin:18px 0 10px;font-size:1.12rem}
.aff-section p{line-height:1.9;margin:0 0 14px;color:#222}
.aff-section ul{padding-left:22px;margin:10px 0}
.aff-section li{margin:6px 0}
.aff-table{border-collapse:collapse;width:100%;margin:16px 0}
.aff-table th,.aff-table td{border:1px solid #e2e8f0;padding:10px;text-align:left}
.aff-table thead th{background:#f1f5f9}
.aff-note{font-style:italic;color:#334155;margin-top:6px}
.aff-ad{margin:12px 0 22px}
</style>
""".strip()

def _adsense_block()->str:
    return """
<div class="aff-ad">
<script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-7409421510734308"
     crossorigin="anonymous"></script>
<!-- 25.06.03 -->
<ins class="adsbygoogle"
     style="display:block"
     data-ad-client="ca-pub-7409421510734308"
     data-ad-slot="9228101213"
     data-ad-format="auto"
     data-full-width-responsive="true"></ins>
<script>
     (adsbygoogle = window.adsbygoogle || []).push({});
</script>
</div>
""".strip()

def _cta_html(url_main:str, url_alt:str, category_url:str, category_name:str)->str:
    btn1 = html.escape(BUTTON_TEXT or "쿠팡에서 최저가 확인하기")
    btn2 = html.escape(BUTTON2_TEXT or "제품 보러가기")
    btn3 = html.escape(f"{category_name} 글 모아보기")
    u1 = html.escape(url_main or "#")
    u2 = html.escape(url_alt or url_main or "#")
    uc = html.escape(category_url or "#")
    return f"""
  <div class="aff-cta">
    <a class="btn-primary" href="{u1}" target="_blank" rel="nofollow sponsored noopener" aria-label="{btn1}">{btn1}</a>
    <a class="btn-secondary" href="{u2}" target="_blank" rel="nofollow sponsored noopener" aria-label="{btn2}">{btn2}</a>
    <a class="btn-tertiary" href="{uc}" aria-label="{btn3}">{btn3}</a>
  </div>
""".rstrip()

def render_affiliate_html(keyword:str, url:str, image:str="", category_name:str="쇼핑")->str:
    disc = html.escape(DISCLOSURE_TEXT)
    kw_esc = html.escape(keyword)
    url_alt = BUTTON2_URL if BUTTON2_URL else url
    category_url = _category_url_for(category_name)

    img_html = ""
    if image and USE_IMAGE:
        img_html = f'<figure style="margin:0 0 18px"><img src="{html.escape(image)}" alt="{kw_esc}" loading="lazy" decoding="async" style="max-width:100%;height:auto;border-radius:12px"></figure>'

    top_block = f"""
  <p class="aff-disclosure"><strong>{disc}</strong></p>
  {_adsense_block()}
  {_cta_html(url, url_alt, category_url, category_name)}
  {img_html}
""".rstrip()

    mid_ads = _adsense_block()

    return f"""
{_css_block()}
<div class="aff-wrap aff-section">
  {top_block}

  <h2>{kw_esc} 선택 시 고려해야 할 요소</h2>
  <p>{kw_esc}를(을) 선택할 때는 용도·공간·소음·관리 편의·예산의 균형을 먼저 잡아야 합니다. 이하 1분 체크리스트로 빠르게 감만 잡고 상세 섹션에서 구체화하세요.</p>
  <ul>
    <li>필요 환경: 어느 공간/누구용인지</li>
    <li>핵심 스펙: 성능 대비 과투자 방지</li>
    <li>관리 난도: 세척·보관·소모품</li>
    <li>총비용: 구매가 + 유지비</li>
  </ul>

  <h2>주요 특징</h2>
  <ul>
    <li>간편한 사용성과 휴대/이동성</li>
    <li>상황별 풍속/모드 조절(있다면 자동/타이머 활용)</li>
    <li>USB/무선 등 전원 옵션과 호환성</li>
    <li>거치대/스트랩 등 액세서리로 활용성 확대</li>
  </ul>

  <h2>가격/가성비</h2>
  <p>동급 제품의 가격대는 시즌·재고·프로모션에 따라 크게 변동합니다. 아래 기준으로 합리 범위를 먼저 잡아보세요.</p>
  <table class="aff-table">
    <thead><tr><th>체크</th><th>포인트</th></tr></thead>
    <tbody>
      <tr><td>성능</td><td>공간/목적 대비 충분한지</td></tr>
      <tr><td>관리</td><td>세척·보관·소모품 비용/난도</td></tr>
      <tr><td>비용</td><td>구매가 + 유지비, 시즌 특가</td></tr>
    </tbody>
  </table>
  <p class="aff-note">* 시즌 아이템은 타이밍이 가성비를 좌우합니다.</p>

  {mid_ads}

  <h2>장단점</h2>
  <h3>장점</h3>
  <ul>
    <li>가벼운 사용 난도, 어디서든 간편</li>
    <li>필요 기능 위주 선택 시 경제적</li>
    <li>모드·거치 옵션 등 확장성</li>
  </ul>
  <h3>단점</h3>
  <ul>
    <li>배터리/소모품 교체 주기 고려</li>
    <li>상위급 대비 세밀한 성능 한계</li>
  </ul>

  <h2>이런 분께 추천</h2>
  <ul>
    <li>여행/야외/서브 용도로 간편한 제품이 필요한 분</li>
    <li>가볍게 시작해보고 이후 업그레이드 계획인 분</li>
    <li>선물/비상용 등 무난한 선택지를 찾는 분</li>
  </ul>

  {_cta_html(url, url_alt, category_url, category_name)}
</div>
""".strip()

# ========= NAME SHORTENER (new) =========
_STOP = set("""
가을 겨울 봄 여름 간절기 데일리 여성 남성 유니섹스 오버 루즈 레귤러 클래식 베이직
부드러운 도톰 얇은 따뜻한 가벼운 경량 프리미엄 심플 트렌디 인기 신상 기본
단추 버튼 라운드 브이넥 반목 반폴라 폴라 목터틀 7부 반소매 긴팔 반팔
화이트 아이보리 아이보리색 블랙 네이비 그레이 베이지 브라운 카키
미니 소형 대형 100 1000 2000 M L XL XXL 55 66 77 S
""".split())
# 핵심 명사 우선순위(있으면 최대 2개까지 유지)
_CORE_NOUNS = ["니트","가디건","전기포트","전기주전자","가습기","케이프","판초","숄","티","후드","코트","자켓","점퍼","청소기","제습기","선풍기"]

_SEASON_CODES = re.compile(r"(?i)\b(?:\d{2,4}(?:fw|ss)|fw|f\/w|s\/s)\b")
_NUMLIKE = re.compile(r"^\d+[a-z가-힣]*$", re.I)

def _tokenize(s: str) -> List[str]:
    toks = re.split(r"[,\s/+\-·\|]+", (s or "").strip())
    return [t for t in toks if t]

def shorten_keyword_for_title(kw: str) -> str:
    # 0) 시즌 코드 제거
    s = _SEASON_CODES.sub("", kw)
    toks = _tokenize(s)

    # 1) 불용/색상/숫자성 토큰 제거
    cleaned=[]
    for t in toks:
        if t in _STOP: continue
        if _NUMLIKE.match(t): continue
        if len(t) <= 1: continue
        cleaned.append(t)

    # 2) 핵심명사/브랜드 선별
    core=[t for t in cleaned if any(n in t for n in _CORE_NOUNS)]
    brand=""
    for t in cleaned:
        if t in core: continue
        # 브랜드 후보: 한글 2~6자 또는 영문/영문+한글 조합, 숫자 미포함
        if not any(ch.isdigit() for ch in t):
            brand = t
            break

    # 3) 조합 규칙: [브랜드] + 핵심명사(최대2) or 핵심명사만
    out=[]
    if brand: out.append(brand)
    if core:
        # 중복 제거 & 길이 짧은 순
        seen=set()
        for t in core:
            base = next((n for n in _CORE_NOUNS if n in t), t)
            if base in seen: continue
            seen.add(base)
            out.append(base)
            if len(out) >= 3: break

    # 4) 최후 보루: 불용어 제거된 앞에서 3~4개
    if not out:
        out = cleaned[:3] if cleaned else _tokenize(kw)[:2]

    # 5) 길이 제한 (너무 길면 뒤에서 컷)
    name = " ".join(out).strip()
    if len(name) > 18:
        # 뒤에서부터 토막
        cut = []
        acc = 0
        for t in out:
            if acc + len(t) + (1 if cut else 0) > 18: break
            cut.append(t); acc += len(t) + (1 if cut else 0)
        name = " ".join(cut) if cut else out[0][:18]

    return re.sub(r"\s+"," ",name).strip()

# ========= RUN =========
def rotate_sources(kw:str):
    changed=False
    if _consume_col_csv("golden_shopping_keywords.csv",kw):
        print(f"[ROTATE] removed '{kw}' from golden_shopping_keywords.csv"); changed=True
    if _consume_col_csv("keywords_shopping.csv",kw):
        print(f"[ROTATE] removed '{kw}' from keywords_shopping.csv"); changed=True
    if not changed:
        print("[ROTATE] nothing removed (maybe already rotated)")

def run_once():
    print(f"[USAGE] NO_REPEAT_TODAY={NO_REPEAT_TODAY}, AFF_USED_BLOCK_DAYS={AFF_USED_BLOCK_DAYS}")
    kw = pick_affiliate_keyword()
    url = resolve_product_url(kw)
    when_gmt = _slot_affiliate()

    # NEW: 제품명 요약 + 동적 제목
    short_name = shorten_keyword_for_title(kw)
    title = hook_aff_title(product_name_short=short_name, keyword=kw)

    body = render_affiliate_html(kw, url, image="", category_name=DEFAULT_CATEGORY)
    res = post_wp(title, body, when_gmt, category=DEFAULT_CATEGORY, tag=kw)
    link = res.get("link")
    print(json.dumps({
        "post_id":res.get("id") or res.get("post") or 0,
        "link": link,
        "status":res.get("status"),
        "date_gmt":res.get("date_gmt"),
        "title": title,
        "keyword": kw,
        "short_name": short_name
    }, ensure_ascii=False))
    _mark_used(kw)
    rotate_sources(kw)

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    run_once()

if __name__=="__main__":
    main()
