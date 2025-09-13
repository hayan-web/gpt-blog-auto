# -*- coding: utf-8 -*-
"""
coupang_api.py — Coupang Partners OpenAPI v1 딥링크 유틸
- CEA(HMAC-SHA256) 서명
- GET 시도 후 실패하면 POST로 재시도(듀얼 경로)
- 단건 키워드→검색URL 딥링크(deeplink_for_query)
- 다건 URL 딥링크(deeplink_for_urls)

환경변수(.env / GitHub Secrets)
- COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, COUPANG_CHANNEL_ID (필수)
- COUPANG_SUBID_PREFIX=auto (선택)
- COUPANG_DEBUG=1 (선택, 서명/쿼리 디버그 출력)
- WP_TLS_VERIFY=true (선택, 기본 true)
"""

from __future__ import annotations
import os, hmac, hashlib, base64, time, json
from datetime import datetime, timezone
from typing import Iterable, List, Tuple
from urllib.parse import quote, quote_plus
import requests

COUPANG_ACCESS_KEY = (os.getenv("COUPANG_ACCESS_KEY") or "").strip()
COUPANG_SECRET_KEY = (os.getenv("COUPANG_SECRET_KEY") or "").strip()
COUPANG_CHANNEL_ID = (os.getenv("COUPANG_CHANNEL_ID") or "").strip()
COUPANG_SUBID_PREFIX = (os.getenv("COUPANG_SUBID_PREFIX") or "auto").strip()

VERIFY_TLS = (os.getenv("WP_TLS_VERIFY") or "true").lower() != "false"
USER_AGENT = os.getenv("USER_AGENT") or "gpt-blog-auto/aff-2.0"
COUPANG_DEBUG = (os.getenv("COUPANG_DEBUG") or "0").strip().lower() in ("1","true","yes","on")

HOST = "https://api-gateway.coupang.com"
DEEPLINK_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"


# -----------------------------
# 내부 유틸
# -----------------------------
def _utc_signed_date() -> str:
    # 예: 20250107T120102Z
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

def _rfc3986(s: str) -> str:
    # RFC3986 안전 문자만 허용(-_.~)하고 나머지는 퍼센트 인코딩
    return quote(s, safe="-_.~")

def _canonical_query(params: dict) -> str:
    if not params:
        return ""
    items = []
    for k in sorted(params.keys()):
        v = "" if params[k] is None else str(params[k])
        items.append(f"{_rfc3986(k)}={_rfc3986(v)}")
    return "&".join(items)

def _string_to_sign(method: str, path: str, query: str, signed_date: str, access_key: str) -> str:
    # Coupang 문서 규칙:
    # method + \n + path + \n + query + \n + signed-date + \n + access-key
    return "\n".join([method.upper(), path, query, signed_date, access_key])

def _signature_b64(sts: str, secret_key: str) -> str:
    mac = hmac.new(secret_key.encode("utf-8"), sts.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(mac).decode("utf-8")

def _cea_header(access_key: str, signed_date: str, signature_b64: str) -> str:
    return f"CEA algorithm=HmacSHA256, access-key={access_key}, signed-date={signed_date}, signature={signature_b64}"

def _check_credentials():
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_CHANNEL_ID):
        raise RuntimeError("Coupang credentials missing (COUPANG_ACCESS_KEY/SECRET_KEY/CHANNEL_ID)")


# -----------------------------
# HTTP 호출 (GET 우선 → 실패 시 POST)
# -----------------------------
def _deeplink_request_get(urls: List[str], sub_id: str) -> List[str]:
    """
    GET 방식:
      params = {subId, channelId, coupangUrls(콤마구분 단건/다건)}
    """
    method = "GET"
    signed_date = _utc_signed_date()
    params = {
        "subId": sub_id,
        "channelId": COUPANG_CHANNEL_ID,
        "coupangUrls": ",".join(urls),
    }
    query = _canonical_query(params)
    sts = _string_to_sign(method, DEEPLINK_PATH, query, signed_date, COUPANG_ACCESS_KEY)
    sig = _signature_b64(sts, COUPANG_SECRET_KEY)
    auth = _cea_header(COUPANG_ACCESS_KEY, signed_date, sig)

    headers = {
        "Authorization": auth,
        "X-Authorization-Date": signed_date,  # 일부 환경에서 필요
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json; charset=utf-8",
    }

    if COUPANG_DEBUG:
        print("[COUPANG DEBUG][GET] path:", DEEPLINK_PATH)
        print("[COUPANG DEBUG][GET] query:", query)
        print("[COUPANG DEBUG][GET] signed_date:", signed_date)
        print("[COUPANG DEBUG][GET] sts:", sts)
        print("[COUPANG DEBUG][GET] sig:", sig)

    resp = requests.get(f"{HOST}{DEEPLINK_PATH}", params=params, headers=headers, timeout=20, verify=VERIFY_TLS)
    return _parse_response(resp, urls)

def _deeplink_request_post(urls: List[str], sub_id: str) -> List[str]:
    """
    POST 방식:
      body = {"subId": "...", "channelId": "...", "coupangUrls": ["...", "..."]}
      (서명은 query string 기준이므로 보통 query=빈문자열)
    """
    method = "POST"
    signed_date = _utc_signed_date()
    query = ""  # POST는 보통 쿼리 없음
    sts = _string_to_sign(method, DEEPLINK_PATH, query, signed_date, COUPANG_ACCESS_KEY)
    sig = _signature_b64(sts, COUPANG_SECRET_KEY)
    auth = _cea_header(COUPANG_ACCESS_KEY, signed_date, sig)

    headers = {
        "Authorization": auth,
        "X-Authorization-Date": signed_date,
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json; charset=utf-8",
    }
    body = {
        "subId": sub_id,
        "channelId": COUPANG_CHANNEL_ID,
        "coupangUrls": urls,  # 배열
    }

    if COUPANG_DEBUG:
        print("[COUPANG DEBUG][POST] path:", DEEPLINK_PATH)
        print("[COUPANG DEBUG][POST] signed_date:", signed_date)
        print("[COUPANG DEBUG][POST] sts:", sts)
        print("[COUPANG DEBUG][POST] sig:", sig)
        print("[COUPANG DEBUG][POST] body:", json.dumps(body, ensure_ascii=False))

    resp = requests.post(f"{HOST}{DEEPLINK_PATH}", json=body, headers=headers, timeout=20, verify=VERIFY_TLS)
    return _parse_response(resp, urls)

def _parse_response(resp: requests.Response, fallback_urls: List[str]) -> List[str]:
    if COUPANG_DEBUG:
        print("[COUPANG DEBUG] status:", resp.status_code)
        ct = resp.headers.get("content-type", "")
        print("[COUPANG DEBUG] content-type:", ct)
        try:
            print("[COUPANG DEBUG] raw text:", resp.text[:800])
        except Exception:
            pass

    if resp.status_code != 200:
        raise RuntimeError(f"Coupang API {resp.status_code}: {resp.text}")

    data = {}
    try:
        if resp.headers.get("content-type", "").lower().startswith("application/json"):
            data = resp.json()
    except Exception as e:
        raise RuntimeError(f"Coupang API invalid JSON: {e}")

    # 응답 예시 가정
    # { "rCode": "0", "data": [ {"originUrl":"...","shortenUrl":"..."} ] }
    arr = (data or {}).get("data") or []
    if not isinstance(arr, list) or not arr:
        # 비어 있으면 폴백
        return fallback_urls

    out: List[str] = []
    for i, item in enumerate(arr):
        if not isinstance(item, dict):
            out.append(fallback_urls[i] if i < len(fallback_urls) else fallback_urls[-1])
            continue
        u = item.get("shortenUrl") or item.get("landingUrl") or item.get("deeplinkUrl")
        if not u:
            u = fallback_urls[i] if i < len(fallback_urls) else fallback_urls[-1]
        out.append(u)
    return out


# -----------------------------
# 퍼블릭 API
# -----------------------------
def deeplink_for_urls(urls: Iterable[str], sub_id: str | None = None) -> List[str]:
    """
    주어진 원본 URL 목록(상품/검색)을 쿠팡 파트너스 딥링크로 변환.
    - 1) GET 시도 → 2) 실패하면 POST 시도
    - 둘 다 실패하면 예외 발생
    """
    _check_credentials()
    urls = [str(u) for u in urls if str(u).strip()]
    if not urls:
        raise ValueError("urls is empty")

    sub = sub_id or f"{COUPANG_SUBID_PREFIX}-{int(time.time())}"

    # 1st: GET
    try:
        return _deeplink_request_get(urls, sub)
    except Exception as e_get:
        if COUPANG_DEBUG:
            print(f"[COUPANG DEBUG] GET failed → fallback to POST: {e_get}")

    # 2nd: POST
    return _deeplink_request_post(urls, sub)

def deeplink_for_query(keyword: str, sub_id: str | None = None) -> str:
    """
    키워드를 쿠팡 검색 URL로 만든 뒤, 그 URL을 딥링크로 변환하여 반환.
    실패 시 예외 발생(호출측에서 폴백 권장).
    """
    _check_credentials()
    base_url = f"https://search.shopping.coupang.com/search?component=&q={quote_plus(keyword)}&channel=rel"
    dl = deeplink_for_urls([base_url], sub_id=sub_id)
    return dl[0] if dl else base_url


# -----------------------------
# 모듈 단독 테스트
# -----------------------------
if __name__ == "__main__":
    import sys
    what = " ".join(sys.argv[1:]).strip() or "무선 청소기"
    try:
        print("TRY deeplink_for_query:", what)
        print(deeplink_for_query(what))
    except Exception as e:
        print("ERROR:", e)
