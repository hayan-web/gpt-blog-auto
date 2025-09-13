# coupang_api.py
# Coupang Partners OpenAPI v1 딥링크 생성 유틸 (HMAC-SHA256, CEA 헤더)
from __future__ import annotations
import os, hmac, hashlib, base64, time
from datetime import datetime, timezone
from urllib.parse import urlencode, quote
import requests

COUPANG_ACCESS_KEY = (os.getenv("COUPANG_ACCESS_KEY") or "").strip()
COUPANG_SECRET_KEY = (os.getenv("COUPANG_SECRET_KEY") or "").strip()
COUPANG_CHANNEL_ID = (os.getenv("COUPANG_CHANNEL_ID") or "").strip()
COUPANG_SUBID_PREFIX = (os.getenv("COUPANG_SUBID_PREFIX") or "auto").strip()

VERIFY_TLS = (os.getenv("WP_TLS_VERIFY") or "true").lower() != "false"
USER_AGENT = os.getenv("USER_AGENT") or "gpt-blog-auto/aff-2.0"

# 엔드포인트(운영)
HOST = "https://api-gateway.coupang.com"
# 예: /v2/providers/affiliate_open_api/apis/openapi/v1/deeplink
DEEPLINK_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"

def _signed_date_utc() -> str:
    # 20250107T120102Z 형식
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

def _canonical_query(params: dict) -> str:
    # 알파벳 오름차순 정렬 + RFC3986 인코딩
    # 값은 str로 맞춤
    items = []
    for k in sorted(params.keys()):
        v = "" if params[k] is None else str(params[k])
        items.append(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}")
    return "&".join(items)

def _string_to_sign(method: str, path: str, query: str, signed_date: str, access_key: str) -> str:
    # 문서 규칙: method + \n + path + \n + query + \n + signed-date + \n + access-key
    return "\n".join([method.upper(), path, query, signed_date, access_key])

def _make_signature(string_to_sign: str, secret_key: str) -> str:
    mac = hmac.new(secret_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(mac).decode("utf-8")

def _auth_header(access_key: str, signed_date: str, signature_b64: str) -> str:
    # CEA 인증 헤더 포맷
    return f"CEA algorithm=HmacSHA256, access-key={access_key}, signed-date={signed_date}, signature={signature_b64}"

def deeplink_for_query(keyword: str, sub_id: str | None = None) -> str:
    """
    키워드(상품명/검색어)를 받아 쿠팡 파트너스 딥링크 생성.
    실패 시 raise로 던짐.
    """
    if not (COUPANG_ACCESS_KEY and COUPANG_SECRET_KEY and COUPANG_CHANNEL_ID):
        raise RuntimeError("Coupang credentials missing")

    # API 스펙상 여러 URL을 배열로 보낼 수 있으나, 여기선 검색 URL 1개를 딥링크 변환
    # (일반적으로는 상품 상세 URL을 넣는 걸 권장)
    base_url = f"https://search.shopping.coupang.com/search?component=&q={quote(keyword)}&channel=rel"

    params = {
        "subId": sub_id or f"{COUPANG_SUBID_PREFIX}-{int(time.time())}",
        "coupangUrls": base_url,              # 콤마구분, 혹은 배열: 여기선 단건
        "channelId": COUPANG_CHANNEL_ID,
    }
    method = "GET"
    query = _canonical_query(params)
    signed_date = _signed_date_utc()
    sts = _string_to_sign(method, DEEPLINK_PATH, query, signed_date, COUPANG_ACCESS_KEY)
    signature = _make_signature(sts, COUPANG_SECRET_KEY)
    auth = _auth_header(COUPANG_ACCESS_KEY, signed_date, signature)

    headers = {
        "Authorization": auth,
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": USER_AGENT,
        "X-Authorization-Date": signed_date,  # 일부 환경에서 요구됨
    }

    # 디버그(리포 비공개 전제): 401 원인 추적에 매우 유용
    if (os.getenv("COUPANG_DEBUG") or "0").strip() in ("1", "true", "yes", "on"):
        print("[COUPANG DEBUG] method=GET")
        print(f"[COUPANG DEBUG] path={DEEPLINK_PATH}")
        print(f"[COUPANG DEBUG] query={query}")
        print(f"[COUPANG DEBUG] signed_date={signed_date}")
        print(f"[COUPANG DEBUG] string_to_sign={sts}")
        print(f"[COUPANG DEBUG] signature_b64={signature}")

    resp = requests.get(f"{HOST}{DEEPLINK_PATH}", params=params, headers=headers, timeout=20, verify=VERIFY_TLS)
    if resp.status_code != 200:
        raise RuntimeError(f"Coupang API {resp.status_code}: {resp.text}")
    data = resp.json() if resp.headers.get("content-type","").startswith("application/json") else {}
    # 응답 구조 예시 가정: { "rCode":"0", "data":[{"originUrl":"...","shortenUrl":"..."}] }
    dlist = (data or {}).get("data") or []
    if not dlist:
        raise RuntimeError(f"Coupang API empty: {resp.text}")
    # 단건 기준
    return dlist[0].get("shortenUrl") or dlist[0].get("landingUrl") or dlist[0].get("deeplinkUrl") or base_url
