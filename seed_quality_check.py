# -*- coding: utf-8 -*-
"""
seed_quality_check.py (enhanced)
- Validate products_seed.csv for affiliate_post.py
- Checks: header, required fields, domain, duplicates, HTTP status (HEAD with optional GET fallback),
          per-keyword counts, product_name duplicates-in-keyword
- Features:
    * CSV auto dialect (comma/semicolon/pipe) + UTF-8 BOM tolerant
    * Concurrent network checks (ThreadPoolExecutor)
    * --http-fallback : HEAD 실패/비정상 시 GET 재시도
    * --report PATH   : Markdown 품질 리포트 작성 (기본 .cache/seed_report.md)
    * --write-clean   : products_seed.cleaned.csv 생성 (중복/결측/도메인 제외)
    * --max-per-keyword N : 클린 저장 시 키워드별 최대 행수 제한
    * --strict        : 에러가 있으면 종료코드 1 반환

Env (optional):
    PRODUCTS_SEED_CSV (default: products_seed.csv)
    KEYWORDS_CSV      (default: keywords.csv)
    MIN_PER_KEYWORD   (default: 2)
    VERIFY_SSL        (default: true)
    TIMEOUT_SEC       (default: 8)
    MAX_WORKERS       (default: 8)
    ALLOW_DOMAINS     (default: "coupang.com,link.coupang.com")
"""

import os, sys, csv, argparse, itertools
from urllib.parse import urlparse, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter, defaultdict
import requests
from pathlib import Path

# ========= Env & Defaults =========
PRODUCTS_SEED_CSV = os.getenv("PRODUCTS_SEED_CSV", "products_seed.csv")
KEYWORDS_CSV      = os.getenv("KEYWORDS_CSV", "keywords.csv")
MIN_PER_KEYWORD   = int(os.getenv("MIN_PER_KEYWORD", "2"))
VERIFY_SSL        = os.getenv("VERIFY_SSL", "true").lower() != "false"
TIMEOUT_SEC       = int(os.getenv("TIMEOUT_SEC", "8"))
MAX_WORKERS       = int(os.getenv("MAX_WORKERS", "8"))
ALLOW_DOMAINS     = [d.strip().lower() for d in (os.getenv("ALLOW_DOMAINS", "coupang.com,link.coupang.com").split(","))
                     if d.strip()]

REQUIRED_HEADERS = ["keyword","product_name","raw_url","pros","cons"]

# ========= Utils =========
def normalize_url(u: str) -> str:
    """느슨한 정규화 (scheme/host 소문자, trailing slash 제거)"""
    try:
        pu = urlparse((u or "").strip())
        scheme = (pu.scheme or "https").lower()
        netloc = pu.netloc.lower()
        path = pu.path.rstrip("/")
        return urlunparse((scheme, netloc, path, "", "", ""))
    except Exception:
        return (u or "").strip().rstrip("/")

def is_allowed_domain(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return any(host.endswith(d) for d in ALLOW_DOMAINS)
    except Exception:
        return False

def read_keywords(path: str):
    if not os.path.exists(path): return []
    words = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            parts = [x.strip() for x in line.strip().split(",") if x.strip()]
            words.extend(parts)
    seen=set(); uniq=[]
    for w in words:
        if w not in seen:
            seen.add(w); uniq.append(w)
    return uniq

def sniff_dialect(fp) -> csv.Dialect:
    data = fp.read(2048)
    fp.seek(0)
    try:
        return csv.Sniffer().sniff(data, delimiters=";,|\t,")
    except Exception:
        class _D(csv.Dialect):
            delimiter = ","
            quotechar = '"'
            escapechar = None
            doublequote = True
            skipinitialspace = True
            lineterminator = "\n"
            quoting = csv.QUOTE_MINIMAL
        return _D

def load_rows(path: str):
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        dialect = sniff_dialect(f)
        reader = csv.DictReader(f, dialect=dialect)
        headers = [h.strip() for h in (reader.fieldnames or [])]
        rows = list(reader)
    return headers, rows

def head_ok(session: requests.Session, url: str, timeout: int, verify: bool, http_fallback: bool):
    try:
        r = session.head(url, allow_redirects=True, timeout=timeout, verify=verify)
        if 200 <= r.status_code < 400:
            return True, r.status_code
        if http_fallback:
            # 일부 호스트가 HEAD 403/405를 낼 수 있음 → GET 최소 확인
            rg = session.get(url, allow_redirects=True, timeout=timeout, verify=verify, stream=True)
            try:
                if 200 <= rg.status_code < 400:
                    return True, rg.status_code
            finally:
                rg.close()
        return False, r.status_code
    except Exception:
        return False, None

def ensure_parent(path: str):
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

# ========= Main =========
def main():
    ap = argparse.ArgumentParser(description="Validate products_seed.csv for affiliate_post.py")
    ap.add_argument("--strict", action="store_true", help="에러 발생 시 종료코드 1")
    ap.add_argument("--write-clean", action="store_true", help="클린 CSV(products_seed.cleaned.csv) 생성")
    ap.add_argument("--no-network", action="store_true", help="HEAD 요청 생략")
    ap.add_argument("--http-fallback", action="store_true", help="HEAD 실패 시 GET으로 재시도")
    ap.add_argument("--max-per-keyword", type=int, default=0, help="키워드별 최대 행수 (클린 저장 시만 적용)")
    ap.add_argument("--report", default=".cache/seed_report.md", help="마크다운 리포트 경로")
    ap.add_argument("--pros-min", type=int, default=4, help="pros 최소 글자 수 (경고 기준)")
    ap.add_argument("--cons-min", type=int, default=4, help="cons 최소 글자 수 (경고 기준)")
    args = ap.parse_args()

    if not os.path.exists(PRODUCTS_SEED_CSV):
        print(f"[ERROR] '{PRODUCTS_SEED_CSV}' 파일이 없습니다.")
        return sys.exit(1 if args.strict else 0)

    # --- read csv ---
    headers, rows = load_rows(PRODUCTS_SEED_CSV)
    missing = [h for h in REQUIRED_HEADERS if h not in headers]
    if missing:
        print(f"[ERROR] 헤더 누락: {missing} / 현재 헤더: {headers}")
        return sys.exit(1 if args.strict else 0)

    errors = []
    warns  = []

    # --- required fields & domain & duplicates ---
    seen_norm_urls = set()
    cleaned_rows = []

    # 키워드 내 product_name 중복 체크를 위한 버킷
    name_buckets = defaultdict(list)

    for i, r in enumerate(rows, start=2):  # 2 = header next line
        kw   = (r.get("keyword") or "").strip()
        name = (r.get("product_name") or "").strip()
        url  = (r.get("raw_url") or "").strip()
        pros = (r.get("pros") or "").strip()
        cons = (r.get("cons") or "").strip()
        line_id = f"line {i}"

        if not kw:   errors.append(f"{line_id}: keyword 비어있음")
        if not name: errors.append(f"{line_id}: product_name 비어있음")
        if not url:  errors.append(f"{line_id}: raw_url 비어있음")

        if url:
            if not (url.lower().startswith("http://") or url.lower().startswith("https://")):
                errors.append(f"{line_id}: URL 스킴 누락/비정상 -> {url}")
            elif not is_allowed_domain(url):
                errors.append(f"{line_id}: 허용 도메인 아님 -> {url} (허용: {', '.join(ALLOW_DOMAINS)})")

            norm = normalize_url(url)
            if norm in seen_norm_urls:
                warns.append(f"{line_id}: 중복 URL(정규화 기준) -> {url}")
            else:
                seen_norm_urls.add(norm)

        if pros and len(pros) < args.pros_min:
            warns.append(f"{line_id}: pros 내용이 짧음({len(pros)}<{args.pros_min})")
        if cons and len(cons) < args.cons_min:
            warns.append(f"{line_id}: cons 내용이 짧음({len(cons)}<{args.cons_min})")

        if kw and name:
            key = (kw, name)
            name_buckets[key].append(i)

        cleaned_rows.append({"keyword": kw, "product_name": name, "raw_url": url, "pros": pros, "cons": cons})

    # 키워드 내 상품명 중복 경고
    for (kw, name), lines in name_buckets.items():
        if len(lines) > 1:
            warns.append(f"[중복상품] '{kw}' 내 '{name}'가 {len(lines)}회 등장 (lines {', '.join(map(str, lines))})")

    # --- optional network check ---
    if not args.no_network:
        to_check = [(idx, r["raw_url"]) for idx, r in enumerate(cleaned_rows, start=2) if r["raw_url"]]
        if to_check:
            session = requests.Session()
            results = {}
            with ThreadPoolExecutor(max_workers=max(1, MAX_WORKERS)) as ex:
                fut_to_idx = {
                    ex.submit(head_ok, session, url, TIMEOUT_SEC, VERIFY_SSL, args.http_fallback): (idx, url)
                    for idx, url in to_check
                }
                for fut in as_completed(fut_to_idx):
                    idx, url = fut_to_idx[fut]
                    ok, status = False, None
                    try:
                        ok, status = fut.result()
                    except Exception:
                        ok, status = False, None
                    results[(idx, url)] = (ok, status)

            for (idx, url), (ok, status) in results.items():
                if not ok:
                    if status is None:
                        errors.append(f"line {idx}: URL 응답 실패(timeout/예외) -> {url}")
                    else:
                        errors.append(f"line {idx}: URL 응답 비정상(status={status}) -> {url}")

    # --- per-keyword counts ---
    want_keywords = read_keywords(KEYWORDS_CSV)
    if want_keywords:
        cnt = Counter([r["keyword"] for r in cleaned_rows if r["keyword"]])
        lacking = [k for k in want_keywords if cnt.get(k, 0) < MIN_PER_KEYWORD]
        for k in lacking:
            warns.append(f"[부족] '{k}' 키워드 행수 {cnt.get(k,0)}/{MIN_PER_KEYWORD}")

    # --- summary ---
    total = len(rows)
    print("\n===== products_seed.csv 품질 리포트 =====")
    print(f"총 행수: {total}")
    print(f"에러: {len(errors)}건 / 경고: {len(warns)}건")

    if warns:
        print("\n[경고]")
        for w in warns[:80]:
            print(" -", w)
        if len(warns) > 80:
            print(f" ... (이하 {len(warns)-80}건 생략)")

    if errors:
        print("\n[에러]")
        for e in errors[:120]:
            print(" -", e)
        if len(errors) > 120:
            print(f" ... (이하 {len(errors)-120}건 생략)")

    # --- write clean ---
    if args.write_clean:
        filtered = []
        seen_norm = set()
        for r in cleaned_rows:
            if not (r["keyword"] and r["product_name"] and r["raw_url"]):
                continue
            if not is_allowed_domain(r["raw_url"]):
                continue
            norm = normalize_url(r["raw_url"])
            if norm in seen_norm:
                continue
            seen_norm.add(norm)
            filtered.append(r)

        # cap per keyword if needed
        if args.max_per_keyword and args.max_per_keyword > 0:
            buckets = defaultdict(list)
            for r in filtered:
                buckets[r["keyword"]].append(r)
            trimmed = list(itertools.chain.from_iterable(
                arr[:args.max_per_keyword] for _, arr in buckets.items()
            ))
            filtered = trimmed

        out_path = "products_seed.cleaned.csv"
        with open(out_path, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=REQUIRED_HEADERS)
            w.writeheader()
            w.writerows(filtered)
        print(f"\n[클린 파일] {out_path} 작성 (행수: {len(filtered)})")

    # --- write markdown report ---
    report_path = args.report
    if report_path:
        p = ensure_parent(report_path)
        try:
            cnt_kw = Counter([r["keyword"] for r in cleaned_rows if r["keyword"]])
            lines = []
            lines.append("# products_seed 품질 리포트\n")
            lines.append(f"- 총 행수: **{total}**")
            lines.append(f"- 에러: **{len(errors)}** 건 / 경고: **{len(warns)}** 건\n")
            if cnt_kw:
                lines.append("## 키워드별 건수\n")
                lines.append("| 키워드 | 건수 |")
                lines.append("|---|---:|")
                for k, c in sorted(cnt_kw.items(), key=lambda x: (-x[1], x[0])):
                    lines.append(f"| {k} | {c} |")
                lines.append("")
            if warns:
                lines.append("## 경고")
                for w in warns:
                    lines.append(f"- {w}")
                lines.append("")
            if errors:
                lines.append("## 에러")
                for e in errors:
                    lines.append(f"- {e}")
                lines.append("")
            with open(p, "w", encoding="utf-8") as rf:
                rf.write("\n".join(lines))
            print(f"[리포트] {p} 저장")
        except Exception as e:
            print("[리포트 오류]", e)

    # --- exit code ---
    if args.strict and errors:
        sys.exit(1)
    print("\n[OK] 품질체크 완료")
    return 0

if __name__ == "__main__":
    sys.exit(main())
