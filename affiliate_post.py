# -*- coding: utf-8 -*-
"""
affiliate_post.py — Coupang Partners 글 자동 포스팅 (요구 레이아웃 반영판)
- 구조:
  [고지문] → 1. 내부광고 → 2. 요약글 → 3. 버튼(2개) →
  4. 본문1(짧게: 소개/분석) → 5. 버튼(2개) → 6. 내부광고 → 7. 본문2(장단점·결론)
- 버튼: '제품 보기', '쇼핑 글 모아보기' (가운데 정렬, 세로, 간격=버튼 높이, 호버 효과)
- URL 없을 때 쿠팡 검색 페이지 폴백
- 골든키워드 회전/사용로그/예약 충돌 회피
- 제목: 스토리 톤 → (LLM 선택) → 템플릿 폴백
"""

import os, re, csv, json, html, random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Tuple
from pathlib import Path
import requests
from dotenv import load_dotenv
from urllib.parse import quote, quote_plus

load_dotenv()

def _adsense_block():
    """내부 광고 블록. AD_SHORTCODE 값이 있으면 그대로 삽입(없으면 미삽입)."""
    shortcode = os.getenv("AD_SHORTCODE", "").strip()
    if shortcode:
        return f'<div class="ads-wrap" style="margin:16px 0;">{shortcode}</div>'
    return ""

# ===== OpenAI (optional) =====
try:
    from openai import OpenAI, BadRequestError
except Exception:
    OpenAI = None
    BadRequestError = Exception

_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_OPENAI_MODEL = (os.getenv("OPENAI_MODEL_LONG") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini")
_oai = OpenAI(api_key=_OPENAI_API_KEY) if (_OPENAI_API_KEY and OpenAI) else None

# ===== TITLE config =====
AFF_TITLE_MIN = int(os.getenv("AFF_TITLE_MIN", "22"))
AFF_TITLE_MAX = int(os.getenv("AFF_TITLE_MAX", "42"))
AFF_TITLE_MODE = (os.getenv("AFF_TITLE_MODE") or "story-then-template").lower()
AFF_BANNED_PHRASES = (
    "제대로 써보고 알게 된 포인트","써보고 알게 된 포인트","총정리 가이드","사용기","리뷰","후기","광고","테스트","예약됨",
)

# ===== ENV / WP =====
WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()

DEFAULT_CATEGORY=(os.getenv("AFFILIATE_CATEGORY") or "쇼핑").strip() or "쇼핑"
DEFAULT_TAGS=(os.getenv("AFFILIATE_TAGS") or "").strip()
DISCLOSURE_TEXT=(os.getenv("DISCLOSURE_TEXT") or "이 포스팅은 쿠팡 파트너스 활동의 일환으로, 이에 따른 일정액의 수수료를 제공합니다.").strip()

# 버튼 라벨 (요구 기본값 고정, 환경변수로 바꾸고 싶으면 오버라이드 가능)
BTN_MAIN_TEXT = (os.getenv("BUTTON_TEXT") or "제품 보기").strip()
BTN_LIST_TEXT = "쇼핑 글 모아보기"

USE_IMAGE=((os.getenv("USE_IMAGE") or "").strip().lower() in ("1","true","y","yes","on"))
AFFILIATE_TIME_KST=(os.getenv("AFFILIATE_TIME_KST") or "13:00").strip()

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-affiliate/2.1"
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

# ===== Utilities =====
def _normalize_title(s: str) -> str:
    s = (s or "").strip()
    s = html.unescape(s).replace("“","").replace("”","").replace("‘","").replace("’","").strip('"\' ')
    return re.sub(r"\s+", " ", s)

def _sanitize_title_text(s: str) -> str:
    s = _normalize_title(s)
    for ban in AFF_BANNED_PHRASES:
        s = s.replace(ban, "")
    return re.sub(r"\s+", " ", s).strip(" ,.-·")

def _bad_aff_title(t: str) -> bool:
    if not t: return True
    if not (AFF_TITLE_MIN <= len(t) <= AFF_TITLE_MAX): return True
    if any(p in t for p in AFF_BANNED_PHRASES): return True
    if any(x in t for x in ("최저가","역대급","무조건","100%","클릭","필구","대박")): return True
    return False

# ===== 조사 보정 =====
def _has_jong(ch: str) -> bool:
    code = ord(ch) - 0xAC00
    return 0 <= code <= 11171 and (code % 28) != 0
def _josa(word: str, pair=("이","가")) -> str:
    return pair[0] if word and _has_jong(word[-1]) else pair[1]
def _iraseo(word: str) -> str:
    return "이라서" if word and _has_jong(word[-1]) else "라서"

# ===== 핵심 키워드 압축 =====
_COLOR_WORDS = {"화이트","블랙","아이보리","핑크","레드","블루","네이비","브라운","베이지","하늘색","그레이","회색","카키","민트"}
_DROP_TOKENS = {"여성","남성","남녀","3컬러","2컬러","25fw","fw","ss","가을니트","겨울니트","여름","봄","가을겨울","신상","인기","베스트","새상품","정품"}
_KEEP_ADJ={"가을","겨울","간절기","울","캐시미어","브이넥","라운드","오버핏","루즈핏","크롭","롱","퍼프","반팔","폴라","반목","하이넥","레이스","케이블",
           "경량","무선","가열식","초음파","미니"}
_CATS = ["니트","스웨터","가디건","케이프","숄","머플러","가습기","전기포트","주전자","선풍기","청소기","보조배터리","전기요","히터","제습기","원피스"]

def _tokenize_ko(s: str) -> List[str]:
    s = re.sub(r"[^\w가-힣\s\-]", " ", s)
    return [t for t in s.replace("  "," ").strip().split() if t]

def _compress_keyword(keyword: str) -> Tuple[str, str]:
    toks = _tokenize_ko(keyword)
    kept_adj, cat = [], None
    for t in toks:
        if t in _COLOR_WORDS or t in _DROP_TOKENS: continue
        if any(c.isdigit() for c in t): continue
        if t in _KEEP_ADJ and len(kept_adj) < 2: kept_adj.append(t); continue
        if (t in _CATS) and not cat: cat = t
    if not cat:
        for c in _CATS:
            if c in keyword: cat = c; break
    core = (" ".join(kept_adj + ([cat] if cat else [])) or " ".join(toks[:3])).strip()
    core = re.sub(r"\s+", " ", core).strip()
    if len(core) < 4 and "니트" in keyword: core = "가을 니트"
    return core, " ".join(toks)

# ===== 카테고리 감지/문구 =====
def _detect_category_from_text(text: str) -> str:
    s = text
    if ("물걸레" in s) or ("습건식" in s): return "cleaner_mop"
    if "청소기" in s: return "cleaner_mini"
    if "가습기" in s: return "humidifier"
    if ("포트" in s) or ("주전자" in s): return "kettle"
    if "원피스" in s: return "knit_dress"
    if ("니트" in s) or ("스웨터" in s) or ("가디건" in s): return "knit"
    return "general"

def _core_phrase_by_cat(cat: str, src: str) -> str:
    if cat == "cleaner_mop":  return "물걸레 청소기"
    if cat == "cleaner_mini": return "무선 미니 청소기" if ("미니" in src or "핸디" in src) else "핸디 청소기"
    if cat == "humidifier":   return "미니 가습기" if "미니" in src else "가습기"
    if cat == "kettle":       return "전기포트"
    if cat == "knit_dress":   return "니트 원피스"
    if cat == "knit":         return "니트"
    return _compress_keyword(src)[0] or "아이템"

TIME_PHRASES = {
    "cleaner_mop":["저녁마다","주말엔","요즘"], "cleaner_mini":["퇴근하고","아침마다","요즘"],
    "humidifier":["밤새","아침마다","요즘"], "kettle":["아침마다","주말 브런치에","요즘"],
    "knit":["아침마다","출근길에","요즘"], "knit_dress":["하루 종일","약속 있는 날엔","요즘"],
    "general":["요즘","아침마다","하루 종일"],
}
SITCH = {
    "cleaner_mop":["바닥 끈적임이 신경 쓰여서"], "cleaner_mini":["책상 위 먼지가 계속 보여서"],
    "humidifier":["자꾸 목이 칼칼해서"], "kettle":["티타임을 자주 해서"],
    "knit":["아침 코디가 고민돼서"], "knit_dress":["코디가 번거로워서"], "general":["자잘한 불편이 쌓여서"],
}
BENEFITS = {
    "cleaner_mop":["물자국이 안 남아요"], "cleaner_mini":["틈새 먼지가 금방 사라져요"],
    "humidifier":["공기가 부드러워져요"], "kettle":["홈카페가 쉬워졌어요"],
    "knit":["핏이 단정하게 떨어져요"], "knit_dress":["코디가 5분 만에 끝나요"],
    "general":["손이 자꾸 가요"],
}
TAILS = ["그래서 계속 손이 가요","이젠 이걸로 정착했어요","한 번 써보면 이유를 알게 돼요","돌려보면 차이가 나요"]

def _story_candidates(core: str, cat: str) -> List[str]:
    t  = TIME_PHRASES.get(cat, TIME_PHRASES["general"])[0]
    s  = SITCH.get(cat, SITCH["general"])[0]
    b  = BENEFITS.get(cat, BENEFITS["general"])[0]
    tail = TAILS[0]
    core_eunneun = core + _josa(core, ("은","는"))
    core_iga     = core + _josa(core, ("이","가"))
    core_iraseo  = core + " " + _iraseo(core)
    cands = [
        f"{t} {s} {core} 쓰니 {b}",
        f"{s} {core_eunneun} {b}",
        f"{t} {core_iraseo} {b}",
        f"{core} 켜두면 {b}",
        f"한 번 써보면 {core_iga} 왜 편한지 알게 돼요",
        f"{b}, 그래서 {core}로 갈아탔어요",
        f"{t} {core_eunneun} {b}, 그래서 계속 손이 가요",
        f"{t} {core_eunneun} {b}, {tail}",
    ]
    out=[]
    for s in cands:
        s = _sanitize_title_text(s)
        if len(s) < AFF_TITLE_MIN - 2: s += " 좋아요"
        if len(s) > AFF_TITLE_MAX: s = s[:AFF_TITLE_MAX-1].rstrip()+"…"
        out.append(s)
    return out

def _aff_title_from_story(keyword: str) -> str:
    src = _sanitize_title_text(keyword)
    cat = _detect_category_from_text(src)
    core = _core_phrase_by_cat(cat, src)
    pool = _story_candidates(core, cat)
    seen=set()
    for cand in pool:
        cand = _sanitize_title_text(cand)
        if cand in seen: continue
        seen.add(cand)
        if not _bad_aff_title(cand): return cand
    return ""

def _aff_title_from_templates(core: str, kw: str) -> str:
    rnd = random.Random(abs(hash(f"{core}|{kw}|{datetime.utcnow().date()}")) % (2**32))
    AFF_TITLE_TEMPLATES = [
        "{core}, 한 장이면 끝","가을엔 역시 {core}","{core} 이렇게 입어요","{core} 포근함을 더하다",
        "출근룩은 {core}로","오늘은 {core}","부드럽게, {core}","{core} 깔끔한 데일리","{core} 선택 가이드","{core} 고민 끝!",
        "가볍게 챙기는 {core}","지금 딱, {core}","센스 완성 {core}","이유 있는 선택, {core}","따뜻함 한 장, {core}",
        "{core} 핵심만 쏙","편안함의 기준, {core}","꾸안꾸의 정석 {core}","레이어드 맛집 {core}","포인트 주기 좋은 {core}",
    ]
    for tpl in rnd.sample(AFF_TITLE_TEMPLATES, k=min(6, len(AFF_TITLE_TEMPLATES))):
        cand = _sanitize_title_text(tpl.format(core=core))
        if _bad_aff_title(cand): continue
        a = re.sub(r"[^\w가-힣]","",cand); b = re.sub(r"[^\w가-힣]","",kw)
        if a == b: continue
        return cand
    fallback = _sanitize_title_text(f"{core} 핵심만 쏙")
    return fallback if not _bad_aff_title(fallback) else _sanitize_title_text(core)[:AFF_TITLE_MAX]

def _aff_title_from_llm(core: str, kw: str) -> str:
    if not _oai: return ""
    try:
        sys_p = "너는 한국어 카피라이터다. 쇼핑 포스트용 모바일 최적 제목을 1개만 출력한다."
        styles = "후킹형, 상황형, 하우투형, 혜택·해결형, 담백한 문장형"
        usr = f"""핵심 키워드(core): {core}
원문 키워드(raw): {kw}
요청:
- {AFF_TITLE_MIN}~{AFF_TITLE_MAX}자, 말맛 있는 1줄
- 제품명 그대로 쓰지 말고, {styles} 중 하나로 변주
- 금지문구: {", ".join(AFF_BANNED_PHRASES)}
- 과장/낚시 금지(최저가/역대 등)
- 출력은 제목 1줄(순수 텍스트)만"""
        r = _oai.chat.completions.create(
            model=_OPENAI_MODEL,
            messages=[{"role":"system","content":sys_p},{"role":"user","content":usr}],
            temperature=0.9, max_tokens=60,
        )
        cand = _sanitize_title_text(r.choices[0].message.content or "")
        return "" if _bad_aff_title(cand) else cand
    except BadRequestError:
        return ""
    except Exception as e:
        print(f"[AFF-TITLE][WARN] {type(e).__name__}: {e}")
        return ""

def hook_aff_title(keyword: str) -> str:
    core, _ = _compress_keyword(keyword)
    if AFF_TITLE_MODE in ("story","story-first","story-then-template","story-then-llm"):
        t = _aff_title_from_story(keyword)
        if t: return t
    if AFF_TITLE_MODE in ("llm","llm-then-template","story-then-llm"):
        t = _aff_title_from_llm(core, keyword)
        if t: return t
    return _aff_title_from_templates(core, keyword)

# ===== TIME / SLOT =====
def _now_kst(): return datetime.now(ZoneInfo("Asia/Seoul"))

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
        if lo <= dt <= hi: return True
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

# ===== USED LOG =====
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

def _read_recent_used(n:int=8)->list[str]:
    try:
        p = Path(f"{USAGE_DIR}/used_shopping.txt")
        if not p.exists(): return []
        lines = [line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
        recent = [ln.split("\t",1)[1] for ln in lines][-n:]
        return list(reversed(recent))
    except Exception:
        return []

def _mark_used(kw:str):
    _ensure_usage_dir()
    with open(USED_FILE,"a",encoding="utf-8") as f:
        f.write(f"{datetime.utcnow().date():%Y-%m-%d}\t{kw.strip()}\n")

# ===== CSV =====
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

# ===== KEYWORD / URL =====
def pick_affiliate_keyword()->str:
    used_today = _load_used_set(1) if NO_REPEAT_TODAY else set()
    used_block = _load_used_set(AFF_USED_BLOCK_DAYS)
    gold=_read_col_csv("golden_shopping_keywords.csv")
    shop=_read_col_csv("keywords_shopping.csv")
    pool=[k for k in gold+shop if k and (k not in used_block)]
    if NO_REPEAT_TODAY: pool=[k for k in pool if k not in used_today]
    recent = set(_read_recent_used(8))
    pool=[k for k in pool if k not in recent]
    if pool: return pool[0].strip()
    fb=[x.strip() for x in (os.getenv("AFF_FALLBACK_KEYWORDS") or "").split(",") if x.strip()]
    return fb[0] if fb else "휴대용 선풍기"

def resolve_product_url(keyword:str)->str:
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
    return f"https://www.coupang.com/np/search?q={quote_plus(keyword)}"

# ===== WP =====
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
    try:
        r = requests.get(
            f"{WP_URL}/wp-json/wp/v2/categories",
            params={"search": name, "per_page": 50, "context":"view"},
            headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=12
        ); r.raise_for_status()
        for it in r.json():
            if (it.get("name") or "").strip() == name:
                link = (it.get("link") or "").strip()
                if link: return link
    except Exception as e:
        print(f"[CAT][WARN] fallback category url for '{name}': {type(e).__name__}: {e}")
    return f"{WP_URL}/category/{quote(name)}/"

def post_wp(title:str, html_body:str, when_gmt:str, category:str, tag:str)->dict:
    cat_id=_ensure_term("categories", category or DEFAULT_CATEGORY)
    tag_ids=[]
    if tag:
        try: tag_ids=[_ensure_term("tags", tag)]
        except Exception: pass
    payload={
        "title": title, "content": html_body, "status": POST_STATUS,
        "categories": [cat_id], "tags": tag_ids,
        "comment_status": "closed", "ping_status": "closed", "date_gmt": when_gmt
    }
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status(); return r.json()

# ===== TEMPLATE / CSS =====
def _css_block()->str:
    return """
<style>
/* wrap */
.aff-wrap{font-family:inherit}

/* 고지문 */
.aff-disclosure{margin:0 0 16px;padding:12px 14px;border:2px solid #e5e7eb;background:#f8fafc;color:#0f172a;border-radius:10px;font-size:.95rem}
.aff-disclosure strong{color:#334155}

/* CTA 버튼: 세로, 가운데, 간격=버튼 높이 */
.aff-cta{ --btn-h: 62px;
  display:flex; flex-direction:column; align-items:center; justify-content:center;
  gap:var(--btn-h); width:100%; margin:22px 0;
}
.aff-cta a{
  display:inline-flex !important; justify-content:center; align-items:center;
  width:clamp(280px, 78%, 460px) !important; min-height:var(--btn-h);
  padding:14px 24px; border-radius:9999px; font-weight:800; letter-spacing:-.2px;
  text-decoration:none; line-height:1.1; box-shadow:0 6px 16px rgba(0,0,0,.08);
  transition: transform .18s ease, box-shadow .18s ease, filter .18s ease, opacity .18s ease;
}
.aff-cta a:hover{ transform:translateY(-2px) scale(1.01); box-shadow:0 10px 22px rgba(0,0,0,.12); filter:brightness(1.03) }

/* 색상 */
.aff-cta .btn-main{ background:#16a34a; color:#fff }
.aff-cta .btn-main:hover{ background:#149E46 }
.aff-cta .btn-list{ background:#0f172a; color:#fff }
.aff-cta .btn-list:hover{ background:#0c1220 }

/* 본문 요소 */
.aff-wrap h2{margin:20px 0 10px;padding-top:6px;border-top:1px solid #e5e7eb}
.aff-wrap h3{margin:12px 0 8px}
.aff-table{width:100%;border-collapse:collapse;margin:8px 0 16px}
.aff-table th,.aff-table td{border:1px solid #e5e7eb;padding:8px 10px;text-align:left}
.aff-table thead th{background:#f1f5f9}
@media(max-width:480px){ .aff-cta a{ width:90% } }
</style>
"""

def _cta_html(url_main:str, category_url:str, category_name:str)->str:
    btn1 = html.escape(BTN_MAIN_TEXT or "제품 보기")
    btn2 = html.escape(BTN_LIST_TEXT)
    u1 = html.escape(url_main or "#")
    uc = html.escape(category_url or "#")
    return f"""
  <div class="aff-cta">
    <a class="btn-main" href="{u1}" target="_blank" rel="nofollow sponsored noopener" aria-label="{btn1}">{btn1}</a>
    <a class="btn-list" href="{uc}" aria-label="{btn2}">{btn2}</a>
  </div>
""".rstrip()

# ===== 요약글 생성(짧게) =====
def _summary_block(keyword:str)->str:
    core, _ = _compress_keyword(keyword)
    cat = _detect_category_from_text(keyword)
    time_phrase = TIME_PHRASES.get(cat, TIME_PHRASES["general"])[0]
    benefit = BENEFITS.get(cat, BENEFITS["general"])[0]
    # 2~3줄 요약
    return f"""
  <h2>요약글</h2>
  <p><em>{time_phrase}</em> <strong>{core}</strong> 중심으로 간단히 정리했어요. 핵심은 “{benefit}”입니다. 아래 본문1에서 짧게 특징을 훑고, 본문2에서 장단점과 결론을 정리합니다.</p>
""".strip()

# ===== 본문 렌더 =====
def render_affiliate_html(keyword:str, url:str, image:str="", category_name:str="쇼핑")->str:
    disc = html.escape(DISCLOSURE_TEXT)
    kw_esc = html.escape(keyword)
    category_url = _category_url_for(category_name)

    img_html = ""
    if image and USE_IMAGE:
        img_html = f'<figure style="margin:0 0 18px"><img src="{html.escape(image)}" alt="{kw_esc}" loading="lazy" decoding="async" style="max-width:100%;height:auto;border-radius:12px"></figure>'

    # 섹션들 조립 (요구 순서)
    top_disclosure = f'<p class="aff-disclosure"><strong>{disc}</strong></p>'
    ad_top = _adsense_block()
    summary = _summary_block(keyword)
    cta_top = _cta_html(url, category_url, category_name)

    # 본문1: 짧은 소개/분석
    body1 = f"""
  <h2>본문 1 — 소개 &amp; 핵심 분석</h2>
  <p>{kw_esc} 관련해 바로 적용 가능한 포인트만 간단히 정리합니다.</p>
  <ul>
    <li>사용성: 휴대/보관이 쉽고, 필요한 기능 위주로 가볍게 시작</li>
    <li>스펙: 과투자 방지 — 내 용도에 필요한 최소 요건만 체크</li>
    <li>관리: 세척·보관·소모품 주기를 미리 고려</li>
  </ul>
  <h3>가격/가성비 한 줄 가이드</h3>
  <table class="aff-table">
    <thead><tr><th>체크</th><th>포인트</th></tr></thead>
    <tbody>
      <tr><td>성능</td><td>공간/목적 대비 충분한지</td></tr>
      <tr><td>관리</td><td>세척·보관·소모품 비용/난도</td></tr>
      <tr><td>비용</td><td>구매가 + 유지비, 시즌 특가</td></tr>
    </tbody>
  </table>
""".strip()

    cta_mid = _cta_html(url, category_url, category_name)
    ad_mid = _adsense_block()

    # 본문2: 장단점/결론
    body2 = f"""
  <h2>본문 2 — 장단점 &amp; 결론</h2>
  <h3>장점</h3>
  <ul>
    <li>가벼운 난도: 어디서든 간편하게 시작</li>
    <li>합리 선택: 필요 기능 위주로 고르면 경제적</li>
    <li>확장성: 모드·거치·액세서리로 활용 폭↑</li>
  </ul>
  <h3>단점</h3>
  <ul>
    <li>배터리/소모품 교체 주기 고려</li>
    <li>상위급 대비 세밀한 성능 한계</li>
  </ul>
  <h3>결론</h3>
  <p>과투자만 피하면 실사용 만족도가 높습니다. <strong>{kw_esc}</strong>은(는) 입문/서브 용도로 특히 무난하며, 시즌/프로모션 타이밍을 잡아 구매하면 체감 만족이 커집니다.</p>
""".strip()

    return f"""
{_css_block()}
<div class="aff-wrap aff-section">
  {top_disclosure}
  {ad_top}
  {summary}
  {cta_top}
  {img_html}

  {body1}
  {cta_mid}
  {ad_mid}
  {body2}
</div>
""".strip()

# ===== TITLE ENTRY POINT =====
def build_title(keyword:str)->str:
    t = hook_aff_title(keyword)
    return _sanitize_title_text(t)[:AFF_TITLE_MAX]

# ===== ROTATE & RUN =====
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
    title = build_title(kw)
    body = render_affiliate_html(kw, url, image="", category_name=DEFAULT_CATEGORY)
    res = post_wp(title, body, when_gmt, category=DEFAULT_CATEGORY, tag=kw)
    print(json.dumps({
        "post_id":res.get("id") or res.get("post") or 0,
        "link": res.get("link"), "status":res.get("status"), "date_gmt":res.get("date_gmt"),
        "title": title, "keyword": kw
    }, ensure_ascii=False))
    _mark_used(kw)
    rotate_sources(kw)

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    run_once()

if __name__=="__main__":
    main()
