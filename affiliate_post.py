# -*- coding: utf-8 -*-
"""
affiliate_post.py — Coupang Partners 글 자동 포스팅
- 상단 고지문(굵게/강조) + 상단 CTA 2개 + 카테고리 이동 버튼 + 내부광고(상단)
- 본문 섹션: 고려요소 → 주요 특징 → 가격/가성비 → (내부광고) → 장단점 → 이런 분께 추천
- 하단 CTA 2개 + 카테고리 이동 버튼
- URL 없을 때 쿠팡 검색 페이지 폴백
- 골든키워드 회전/사용로그/예약 충돌 회피
- ✨ 제목 생성 로직: 사람 말투 '미니 스토리' → (LLM) → 템플릿 폴백
"""

def _adsense_block():
    """
    내부 광고 블록. 환경변수 AD_SHORTCODE 값이 있으면 그대로 삽입.
    값이 비어있으면 아무 것도 넣지 않음(레이아웃 영향 X).
    """
    shortcode = os.getenv("AD_SHORTCODE", "").strip()
    if shortcode:
        return f'<div class="ads-wrap" style="margin:16px 0;">{shortcode}</div>'
    return ""

import os, re, csv, json, html, random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import List, Tuple
import requests
from dotenv import load_dotenv
from urllib.parse import quote, quote_plus

load_dotenv()

try:
    from openai import OpenAI, BadRequestError
except Exception:
    OpenAI = None
    BadRequestError = Exception

# ===== Title config =====
AFF_TITLE_MIN = int(os.getenv("AFF_TITLE_MIN", "22"))
AFF_TITLE_MAX = int(os.getenv("AFF_TITLE_MAX", "42"))
AFF_TITLE_MODE = (os.getenv("AFF_TITLE_MODE") or "story-then-template").lower()

AFF_BANNED_PHRASES = (
    "제대로 써보고 알게 된 포인트",
    "써보고 알게 된 포인트",
    "총정리 가이드",
    "사용기", "리뷰", "후기",
    "광고", "테스트", "예약됨",
)

_OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
_OPENAI_MODEL = (os.getenv("OPENAI_MODEL_LONG") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini")
_oai = OpenAI(api_key=_OPENAI_API_KEY) if (_OPENAI_API_KEY and OpenAI) else None

# ===== ENV / WP =====
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

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-affiliate/2.0"
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
    s = html.unescape(s)
    s = s.replace("“","").replace("”","").replace("‘","").replace("’","").strip('"\' ')
    s = re.sub(r"\s+", " ", s)
    return s

def _sanitize_title_text(s: str) -> str:
    s = _normalize_title(s)
    for ban in AFF_BANNED_PHRASES:
        s = s.replace(ban, "")
    s = re.sub(r"\s+", " ", s).strip(" ,.-·")
    return s

def _bad_aff_title(t: str) -> bool:
    if not t:
        return True
    if not (AFF_TITLE_MIN <= len(t) <= AFF_TITLE_MAX):
        return True
    if any(p in t for p in AFF_BANNED_PHRASES):
        return True
    if any(x in t for x in ("최저가","역대급","무조건","100%","클릭","필구","대박")):
        return True
    return False

# ===== 한글 조사 보정 =====
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
_KEEP_ADJ = {"가을","겨울","간절기","울","캐시미어","브이넥","라운드","오버핏","루즈핏","크롭","롱","퍼프","반팔","폴라","반목","하이넥","레이스","케이블","아가일","데일리","포근","경량","무선","가열식","초음파","미니"}
_CATS = ["니트","스웨터","가디건","케이프","숄","머플러","가습기","전기포트","주전자","선풍기","청소기","보조배터리","전기요","히터","제습기","원피스"]

def _tokenize_ko(s: str) -> List[str]:
    s = re.sub(r"[^\w가-힣\s\-]", " ", s)
    s = s.replace("  ", " ")
    toks = [t for t in s.strip().split() if t]
    return toks

def _compress_keyword(keyword: str) -> Tuple[str, str]:
    toks = _tokenize_ko(keyword)
    kept_adj, cat = [], None
    for t in toks:
        if t in _COLOR_WORDS or t in _DROP_TOKENS:
            continue
        if any(c.isdigit() for c in t):
            continue
        if t in _KEEP_ADJ and len(kept_adj) < 2:
            kept_adj.append(t); continue
        if (t in _CATS) and not cat:
            cat = t
    if not cat:
        for c in _CATS:
            if c in keyword:
                cat = c; break
    if cat:
        core = " ".join(kept_adj + [cat]).strip()
    else:
        core = " ".join(toks[:3])
    core = re.sub(r"\s+", " ", core).strip()
    if len(core) < 4 and "니트" in keyword:
        core = "가을 니트"
    return core, " ".join(toks)

# ===== 카테고리 감지 =====
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

# ===== 스토리 톤 풀 =====
TIME_PHRASES = {
    "cleaner_mop":  ["저녁마다", "주말엔", "요즘"],
    "cleaner_mini": ["퇴근하고", "아침마다", "요즘"],
    "humidifier":   ["밤새", "아침마다", "요즘"],
    "kettle":       ["아침마다", "주말 브런치에", "요즘"],
    "knit":         ["아침마다", "출근길에", "요즘"],
    "knit_dress":   ["하루 종일", "약속 있는 날엔", "요즘"],
    "general":      ["요즘", "아침마다", "하루 종일"],
}
SITCH = {
    "cleaner_mop":  ["바닥 끈적임이 신경 쓰여서", "거실 물자국이 성가셔서", "주방 바닥이 거칠어서"],
    "cleaner_mini": ["책상 위 먼지가 계속 보여서", "차 안이 금방 지저분해져서", "원룸이라 금세 쌓여서"],
    "humidifier":   ["자꾸 목이 칼칼해서", "아침에 코가 건조해서", "방 공기가 텁텁해서"],
    "kettle":       ["티타임을 자주 해서", "물 데우는 게 번거로워서", "라면이 자주 땡겨서"],
    "knit":         ["아침 코디가 고민돼서", "큰 옷은 답답해서", "겉돌지 않는 걸 찾다 보니"],
    "knit_dress":   ["코디가 번거로워서", "밋밋해 보여서", "라인이 무너져서"],
    "general":      ["자잘한 불편이 쌓여서", "정리가 필요해서", "바로 쓰고 싶어서"],
}
BENEFITS = {
    "cleaner_mop":  ["물자국이 안 남아요", "끈적임이 싹 사라져요", "발바닥이 보송해요"],
    "cleaner_mini": ["틈새 먼지가 금방 사라져요", "차 안 청소가 쉬워졌어요", "책상 주변이 단정해져요"],
    "humidifier":   ["아침에 목이 편해요", "공기가 부드러워져요", "밤새 촉촉하더라고요"],
    "kettle":       ["티타임이 빨라져요", "라면 준비가 금방이에요", "홈카페가 쉬워졌어요"],
    "knit":         ["핏이 단정하게 떨어져요", "가볍게 따뜻하더라고요", "아침이 덜 바빠요"],
    "knit_dress":   ["라인이 예쁘게 살아나요", "코디가 5분 만에 끝나요", "움직일 때 실루엣이 예뻐요"],
    "general":      ["손이 자꾸 가요", "확실히 편해졌어요", "쓰면 이유를 알아요"],
}
TAILS = [
    "그래서 계속 손이 가요",
    "이젠 이걸로 정착했어요",
    "한 번 써보면 이유를 알게 돼요",
    "돌려보면 차이가 나요",
]

# ===== 템플릿(폴백용) =====
AFF_TITLE_TEMPLATES = [
    "{core}, 한 장이면 끝",
    "가을엔 역시 {core}",
    "{core} 이렇게 입어요",
    "{core} 포근함을 더하다",
    "출근룩은 {core}로",
    "오늘은 {core}",
    "부드럽게, {core}",
    "{core} 깔끔한 데일리",
    "{core} 선택 가이드",
    "{core} 고민 끝!",
    "가볍게 챙기는 {core}",
    "지금 딱, {core}",
    "센스 완성 {core}",
    "이유 있는 선택, {core}",
    "따뜻함 한 장, {core}",
    "{core} 핵심만 쏙",
    "편안함의 기준, {core}",
    "꾸안꾸의 정석 {core}",
    "레이어드 맛집 {core}",
    "포인트 주기 좋은 {core}",
]

def _story_candidates(core: str, cat: str) -> List[str]:
    t  = random.choice(TIME_PHRASES.get(cat, TIME_PHRASES["general"]))
    s  = random.choice(SITCH.get(cat, SITCH["general"]))
    b  = random.choice(BENEFITS.get(cat, BENEFITS["general"]))
    tail = random.choice(TAILS)

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
        if len(s) < AFF_TITLE_MIN - 2:
            s += " 좋아요"
        if len(s) > AFF_TITLE_MAX:
            s = s[:AFF_TITLE_MAX-1].rstrip()+"…"
        out.append(s)
    return out

def _aff_title_from_story(keyword: str) -> str:
    src = _sanitize_title_text(keyword)
    cat = _detect_category_from_text(src)
    core = _core_phrase_by_cat(cat, src)
    seed = abs(hash(f"story|{core}|{src}|{datetime.utcnow().date()}")) % (2**32)
    rnd = random.Random(seed)

    pool = _story_candidates(core, cat)
    rnd.shuffle(pool)

    seen=set()
    for cand in pool:
        cand = _sanitize_title_text(cand)
        if not cand or cand in seen:
            continue
        seen.add(cand)
        if not _bad_aff_title(cand):
            return cand
    return ""

def _aff_title_from_templates(core: str, kw: str) -> str:
    seed = abs(hash(f"{core}|{kw}|{datetime.utcnow().date()}")) % (2**32)
    rnd = random.Random(seed)
    cands = rnd.sample(AFF_TITLE_TEMPLATES, k=min(6, len(AFF_TITLE_TEMPLATES)))
    for cand_tpl in cands:
        cand = _sanitize_title_text(cand_tpl.format(core=core))
        if _bad_aff_title(cand):
            continue
        a = re.sub(r"[^\w가-힣]", "", cand)
        b = re.sub(r"[^\w가-힣]", "", kw)
        if a == b:
            continue
        return cand
    fallback = _sanitize_title_text(f"{core} 핵심만 쏙")
    if not _bad_aff_title(fallback):
        return fallback
    return _sanitize_title_text(core)[:AFF_TITLE_MAX]

def _aff_title_from_llm(core: str, kw: str) -> str:
    if not _oai:
        return ""
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
            temperature=0.9,
            max_tokens=60,
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

    # 1) 스토리형(사람 말투)
    if AFF_TITLE_MODE in ("story","story-first","story-then-template","story-then-llm"):
        t = _aff_title_from_story(keyword)
        if t:
            return t

    # 2) LLM (선택)
    if AFF_TITLE_MODE in ("llm","llm-then-template","story-then-llm"):
        t = _aff_title_from_llm(core, keyword)
        if t:
            return t

    # 3) 템플릿 폴백
    return _aff_title_from_templates(core, keyword)

# ===== TIME / SLOT =====
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
    """Return the last n used shopping keywords (most recent first)."""
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
    if NO_REPEAT_TODAY:
        pool=[k for k in pool if k not in used_today]
    # avoid most recent items (extra guard)
    recent = set(_read_recent_used(8))
    pool=[k for k in pool if k not in recent]
    if pool: return pool[0].strip()
    fb=[x.strip() for x in (os.getenv("AFF_FALLBACK_KEYWORDS") or "").split(",") if x.strip()]
    if fb: return fb[0]
    return "휴대용 선풍기"
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

# ===== TEMPLATE (본문) =====

def _css_block()->str:
    return """
<style>
/* Keep existing wraps/fonts */
.aff-wrap{font-family:inherit}

/* --- Disclosure (unchanged) --- */
.aff-disclosure{margin:0 0 16px;padding:12px 14px;border:2px solid #e5e7eb;background:#f8fafc;color:#0f172a;border-radius:10px;font-size:.95rem}
.aff-disclosure strong{color:#334155}

/* --- CTA: vertical, centered, generous --- */
.aff-cta{display:flex;flex-direction:column;align-items:center;gap:16px;margin:20px auto 14px;max-width:100%}
.aff-cta a{display:block;width:clamp(260px,60%,420px);padding:16px 24px;border-radius:9999px;font-weight:700;letter-spacing:-.2px;text-align:center;line-height:1.2;text-decoration:none}
.aff-cta a + a{margin-top:0}

/* Colors */
/* ⬇⬇⬇ 버튼 중앙 정렬·세로 배치·간격/사이즈 고정 (테마 오버라이드 대비) */
.aff-cta {
  display:flex;
  flex-direction:column;
  align-items:center;
  justify-content:center;
  gap:20px;
  width:100%;
  margin:20px 0;
}

.aff-cta a {
  display:inline-flex !important;
  justify-content:center;
  align-items:center;
  text-align:center;
  padding:14px 24px;
  min-height:56px;
  font-size:16px;
  font-weight:700;
  border-radius:999px;
  width:clamp(280px, 80%, 420px) !important; /* 본문 폭 기준 중앙 고정 */
  margin:0 auto;
  text-decoration:none;
  box-shadow:0 4px 12px rgba(0,0,0,0.08);
}

/* 데스크톱에서 버튼을 더 크게 */
@media (min-width: 720px) {
  .aff-cta a {
    min-height:64px;
    font-size:18px;
  }
}

/* --- Tables (unchanged) --- */
.aff-table{width:100%;border-collapse:collapse;margin:8px 0 14px}
.aff-table th,.aff-table td{border:1px solid #e5e7eb;padding:8px 10px;text-align:left}
.aff-table thead th{background:#f1f5f9}

/* --- Headings (unchanged) --- */
.aff-wrap h2{margin:18px 0 8px;padding-top:8px;border-top:1px solid #e5e7eb}
.aff-wrap h3{margin:14px 0 6px}

@media(max-width:480px){
  .aff-cta a{width:90%;padding:14px 18px;font-size:15px}
}
</style>
"""
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
    link = res.get("link")
    print(json.dumps({
        "post_id":res.get("id") or res.get("post") or 0,
        "link": link,
        "status":res.get("status"),
        "date_gmt":res.get("date_gmt"),
        "title": title,
        "keyword": kw
    }, ensure_ascii=False))
    _mark_used(kw)
    rotate_sources(kw)

def main():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    run_once()

if __name__=="__main__":
    main()
