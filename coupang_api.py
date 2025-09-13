# -*- coding: utf-8 -*-
"""
coupang_api.py — Coupang Partners 딥링크
- 공식 경로로 수정: /v2/providers/affiliate_open_api/apis/openapi/v1/deeplink
- 검색 키워드 기반: 검색 URL을 deeplink로 변환 시도 → 실패 시 검색 URL 그대로 반환
"""

from __future__ import annotations
import os, time, hmac, hashlib, base64, json
from typing import Optional, Sequence
from urllib.parse import quote_plus
import requests
from dotenv import load_dotenv

load_dotenv()

ACCESS_KEY = (os.getenv("COUPANG_ACCESS_KEY") or "").strip()
SECRET_KEY = (os.getenv("COUPANG_SECRET_KEY") or "").strip()
CHANNEL_ID = (os.getenv("COUPANG_CHANNEL_ID") or "").strip()
SUBID_PREFIX = (os.getenv("COUPANG_SUBID_PREFIX") or "auto").strip()

BASE_URL = "https://api-gateway.coupang.com"
DEEPLINK_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"

UA = os.getenv("USER_AGENT") or "gpt-blog-auto/coupang-1.3"

def _sign(method: str, path: str, query: str = "", body: str = "") -> dict:
    """
    Coupang CEA 서명 헤더 생성.
    (기존 구현이 있다면 그걸 사용해도 됨. 여기 구현은 일반적인 CEA 서명 규칙.)
    """
    signed_date = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    message = f"{method}\n{path}\n{query}\n{signed_date}\n{body}"
    signature = hmac.new(
        SECRET_KEY.encode("utf-8"),
        message.encode("utf-8"),
        hashlib.sha256
    ).digest()
    sig_b64 = base64.b64encode(signature).decode("utf-8")
    return {
        "Authorization": f"CEA algorithm=HmacSHA256, access-key={ACCESS_KEY}, signed-date={signed_date}, signature={sig_b64}",
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": UA
    }

def _deeplink(coupang_urls: Sequence[str], sub_id: Optional[str]=None) -> Optional[str]:
    if not ACCESS_KEY or not SECRET_KEY:
        return None
    payload = {"coupangUrls": list(coupang_urls)}
    if sub_id:
        payload["subId"] = sub_id
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    headers = _sign("POST", DEEPLINK_PATH, "", body)
    url = BASE_URL + DEEPLINK_PATH
    r = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=12)
    if r.status_code != 200:
        raise RuntimeError(f"[deeplink] HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    # 응답 형식: {"rCode":"0","message":"SUCCESS","data":[{"originUrl":..., "shortenUrl":...}]}
    items = (data or {}).get("data") or []
    if not items:
        return None
    return items[0].get("shortenUrl") or None

def coupang_search_url(query: str) -> str:
    return f"https://www.coupang.com/np/search?q={quote_plus(query)}"

def deeplink_for_query(query: str) -> str:
    """
    키워드 검색 URL을 딥링크로 변환. 실패하면 검색 URL 그대로 반환.
    """
    raw = coupang_search_url(query)
    try:
        sub = f"{SUBID_PREFIX}-{int(time.time())%86400}"
        link = _deeplink([raw], sub_id=sub)
        if isinstance(link, str) and link:
            return link
    except Exception as e:
        # 워크플로 로그에서 쉽게 확인 가능
        print(f"[deeplink_for_search] fallback to search URL: {e}")
    return raw
