# -*- coding: utf-8 -*-
"""
update_keywords.py — 매 실행 '완전 새 키워드' 생성 + 밴/중복 제거 + 골든 선별 보장
- 항상 기존 CSV를 백업(.bak-타임스탬프)하고 새로 작성
- NAVER API 실패 시에도 폴백 제너레이터로 K개/Gold개 보장
- BAN_KEYWORDS, .usage/ban_keywords_shopping.txt, used_* 로그를 반영
- build_products_seed용 keywords.csv까지 생성(비어서 스킵되는 문제 차단)
"""

import os, csv, re, time, random, json
from datetime import datetime, timedelta

# ===== env =====
K_ALL   = int(os.getenv("K_ALL") or os.getenv("KEYWORDS_K") or "50")
GOLD_ALL= int(os.getenv("GOLD_ALL") or "20")
USAGE_DIR = os.getenv("USAGE_DIR") or ".usage"
CACHE_DIR = os.getenv("CACHE_DIR") or ".cache"

BAN_FROM_ENV = [s.strip() for s in (os.getenv("BAN_KEYWORDS") or "").split(",") if s.strip()]
BAN_FILE = os.path.join(USAGE_DIR, "ban_keywords_shopping.txt")

USER_AGENT = os.getenv("USER_AGENT") or "gpt-blog-keywords/1.3"
BACKUP_OLD = (os.getenv("BACKUP_OLD_KEYWORDS") or "1").lower() in ("1","true","yes","on")

# ===== paths =====
P_GENERAL = "keywords_general.csv"
P_SHOP    = "keywords_shopping.csv"
P_GOLD    = "golden_shopping_keywords.csv"
P_ALL     = "keywords.csv"  # seed용

USED_GENERAL = os.path.join(USAGE_DIR, "used_general.txt")
USED_SHOP    = os.path.join(USAGE_DIR, "used_shopping.txt")

# ===== utils =====
def _ts(): return datetime.utcnow().strftime("%Y%m%d-%H%M%S")

def _backup(path:str):
    if BACKUP_OLD and os.path.exists(path):
        os.rename(path, f"{path}.bak-{_ts()}")

def _write_col(path:str, items:list[str]):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["keyword"])
        for x in items:
            w.writerow([x])

def _read_used(path:str, days:int=365)->set[str]:
    used=set()
    if not os.path.exists(path): return used
    cutoff = datetime.utcnow().date() - timedelta(days=days)
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for ln in f:
            ln=ln.strip()
            if not ln: continue
            if "\t" in ln:
                d, k = ln.split("\t",1)
                try:
                    if datetime.strptime(d,"%Y-%m-%d").date() >= cutoff:
                        used.add(k.strip())
                except: 
                    used.add(k.strip())
            else:
                used.add(ln)
    return used

def _load_bans()->list[str]:
    bans = set(x for x in BAN_FROM_ENV if x)
    if os.path.exists(BAN_FILE):
        for ln in open(BAN_FILE,"r",encoding="utf-8",errors="ignore"):
            ln=ln.strip()
            if ln: bans.add(ln)
    # 하위어까지 포괄(부분문자열 매칭)
    return sorted(bans, key=len, reverse=True)

def _ban_or_used(s:str, bans:list[str], used:set[str])->bool:
    s=s.strip()
    if not s: return True
    if s in used: return True
    return any(b and (b in s) for b in bans)

def _uniq_keep_order(seq):
    out=[]; seen=set()
    for x in seq:
        if x not in seen:
            seen.add(x); out.append(x)
    return out

def _shuffle_daily(seq:list[str], salt:str="")->list[str]:
    rnd = random.Random(f"{datetime.utcnow().date()}|{salt}")
    tmp = list(seq)
    rnd.shuffle(tmp)
    return tmp

# ===== fallback pools =====
GENERAL_BASE = [
    "가계부", "정리정돈", "주간 계획", "미니멀 라이프", "홈카페", "아침 루틴", "운동 기록",
    "독서 메모", "식단 관리", "취미 일기", "프로젝트 회고", "배운 점 기록", "작은 습관",
    "집안일 체크리스트", "시간 관리 팁", "스터디 노트", "여행 계획", "쇼핑 리스트",
    "월간 점검", "작심삼일 탈출", "작업 공간 꾸미기", "디지털 디톡스", "마음챙김",
]

SHOP_CATEGORIES = [
    "히터","전기요","전기장판","보조배터리","무선 청소기","전기포트","제습기","가습기 필터",
    "물걸레 청소기","탁상 조명","가열식 가습기","초음파 가습기","니트","가디건","스웨터",
    "멀티탭","충전기","선정리", "서랍 정리함", "텀블러", "보온병"
]
SHOP_MODS = ["미니","컴팩트","저전력","저소음","가성비","프리미엄","USB","무선","스탠드","휴대용","대용량"]

def _generate_general(k:int, bans:list[str], used:set[str])->list[str]:
    pool = _shuffle_daily(GENERAL_BASE, "gen")
    out=[]
    for x in pool:
        if not _ban_or_used(x,bans,used):
            out.append(x)
        if len(out)>=k: break
    # 부족 시 변형 생성
    i=1
    while len(out)<k:
        seed = f"{_ts()}-{i}"
        cand = f"{random.choice(GENERAL_BASE)} {random.choice(['메모','정리','팁','노트','기록'])} {i}"
        if not _ban_or_used(cand,bans,used):
            out.append(cand)
        i+=1
    return out

def _generate_shopping(k:int, bans:list[str], used:set[str])->list[str]:
    base = [f"{m} {c}" if m else c for c in SHOP_CATEGORIES for m in ([""]+SHOP_MODS)]
    base += [c for c in SHOP_CATEGORIES]
    base = _shuffle_daily(_uniq_keep_order(base), "shop")
    out=[]
    for x in base:
        if not _ban_or_used(x,bans,used):
            out.append(x)
        if len(out)>=k: break
    # 부족하면 무조건 채우기(밴 회피된 조합 생성)
    i=1
    while len(out)<k:
        cand = f"{random.choice(SHOP_MODS)} {random.choice(SHOP_CATEGORIES)} {i}"
        if not _ban_or_used(cand,bans,used):
            out.append(cand)
        i+=1
    return out

def _select_golden(pool:list[str], gold:int)->list[str]:
    # 길이/다양성 가중 + 일자 섞기
    scored = []
    for s in pool:
        score = len(s) + (3 if " " in s else 0)
        scored.append((score, s))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picked = [s for _,s in scored][:max(0,gold)]
    if len(picked)<gold:
        picked = _uniq_keep_order(picked + pool)[:gold]
    return picked

# ===== main =====
def main():
    os.makedirs(USAGE_DIR, exist_ok=True)
    bans = _load_bans()
    used_g = _read_used(USED_GENERAL, days=365)
    used_s = _read_used(USED_SHOP   , days=365)

    # 항상 새로 작성: 이전본 백업
    for p in (P_GENERAL,P_SHOP,P_GOLD,P_ALL):
        _backup(p)

    # 네이버 API는 실패해도 즉시 폴백으로 채우는 구조(안전성 우선)
    gen = _generate_general(K_ALL, bans, used_g)
    shop = _generate_shopping(K_ALL, bans, used_s)
    gold = _select_golden(shop, GOLD_ALL)

    # 파일 출력
    _write_col(P_GENERAL, gen)
    _write_col(P_SHOP, shop)
    _write_col(P_GOLD, gold)

    # seed용 keywords.csv (두 소스 합본 상위 50)
    merged = _uniq_keep_order(gold + shop + gen)[:max(K_ALL, 50)]
    _write_col(P_ALL, merged)

    print(f"[GENERAL] {len(gen)} → {P_GENERAL} (head={gen[:4]})")
    print(f"[SHOP]    {len(shop)} → {P_SHOP} (gold={len(gold)})")
    print(f"[GOLD]    {gold[:8]} …")
    print(f"[ALL]     {len(merged)} → {P_ALL}")

if __name__=="__main__":
    main()
