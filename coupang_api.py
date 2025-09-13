# -*- coding: utf-8 -*-
"""
coupang_api.py — Coupang Partners 딥링크 유틸 (검색 페이지 전용)
- 환경변수:
  COUPANG_ACCESS_KEY, COUPANG_SECRET_KEY, COUPANG_CHANNEL_ID(옵션),
  COUPANG_SUBID_PREFIX(기본 auto), REQUIRE_COUPANG_API(1/true면 API 시도)
- 기능:
  1) coupang_search_url(keyword): 쿠팡 검색 결과 URL(일반)
  2) api_create_deeplink(urls, sub_id): API로 딥링크(단축링크) 생성
  3) deeplink_for_search(keyword, sub_id=None): 검색 URL → 딥링크(실패시 일반 URL)
  4) deeplink_for_query(keyword): 기존 호환용(= deeplink_for_search)
"""

from __future__ import annotations
import os, json, hmac, hashlib, base64
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import quote_plus
import requests
from dotenv import load_dotenv

load_dotenv()

API_HOST = "https://api-gateway.coupang.com"
DEEPLINK_PATH = "/v2/providers/affiliate_open_api/apis/open/api/v1/deeplink"

ACCESS_KEY = (os.getenv("COUPANG_ACCESS_KEY") or "").strip()
SECRET_KEY = (os.getenv("COUPANG_SECRET_KEY") or "").strip()
CHANNEL_ID = (os.getenv("COUPANG_CHANNEL_ID") or "").strip()  # 옵션
SUBID_PREFIX = (os.getenv("COUPANG_SUBID_PREFIX") or "auto").strip() or "auto"
REQUIRE_COUPANG_API = (os.getenv("REQUIRE_COUPANG_API") or "0").strip().lower() in ("1", "true", "yes", "on")

UA = os.getenv("USER_AGENT") or "gpt-blog-auto/coupang-api-1.0"

def _now_utc() -> str:
    # e.g., 2025-09-14T00:00:00Z
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _auth_headers(method: str, path_with_query: str) -> dict:
    """
    Coupang CEA 서명 헤더 생성.
    message = x-coupang-date + \n + method + \n + path + \n + query
    """
    if not (ACCESS_KEY and SECRET_KEY):
        raise RuntimeError("COUPANG_ACCESS_KEY/SECRET_KEY 누락")

    dt = _now_utc()
    # path_with_query는 '/v2/.../deeplink' 또는 '/v2/.../deeplink?param=...' 형태
    if "?" in path_with_query:
        path, query = path_with_query.split("?", 1)
        query = "?" + query
    else:
        path, query = path_with_query, ""

    msg = f"{dt}\n{method.upper()}\n{path}\n{query}"
    sig = base64.b64encode(
        hmac.new(SECRET_KEY.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).digest()
    ).decode("utf-8")

    return {
        "Authorization": f"CEA algorithm=HmacSHA256, access-key={ACCESS_KEY}, signed-date={dt}, signature={sig}",
        "x-coupang-date": dt,
        "Content-Type": "application/json; charset=utf-8",
        "User-Agent": UA,
    }

def coupang_search_url(keyword: str) -> str:
    """쿠팡 'www' 도메인의 안정적인 검색 페이지 URL(트래킹 파라미터 없음)."""
    return f"https://www.coupang.com/np/search?component=&q={quote_plus(keyword)}&channel=rel"

def _gen_subid(base: Optional[str] = None) -> str:
    ts = datetime.now(timezone.utc).strftime("%y%m%d%H%M%S")
    prefix = (base or SUBID_PREFIX or "auto").strip()
    return f"{prefix}-{ts}"

def api_create_deeplink(urls: List[str], sub_id: Optional[str] = None) -> List[str]:
    """
    Coupang Partners 'deeplink' API로 단축/트래킹 링크 생성.
    - urls: 원본 쿠팡 URL 리스트(검색 페이지 포함 가능)
    - sub_id: 퍼포먼스 추적용 서브아이디(옵션)
    반환: shortenUrl 또는 landingUrl 리스트
    """
    if not REQUIRE_COUPANG_API:
        raise RuntimeError("REQUIRE_COUPANG_API 비활성")

    if not urls:
        return []

    body = {
        "coupangUrls": urls,
        # subId는 옵션. 채널 구분 원하면 사용.
        "subId": (sub_id or _gen_subid())[:50],  # 길이 안전
    }
    # 일부 환경에서 channelId를 요구할 수 있어 옵션으로 전달
    if CHANNEL_ID:
        body["channelId"] = CHANNEL_ID

    headers = _auth_headers("POST", DEEPLINK_PATH)
    resp = requests.post(API_HOST + DEEPLINK_PATH, headers=headers, data=json.dumps(body), timeout=20)
    if resp.status_code >= 400:
        raise RuntimeError(f"[deeplink] HTTP {resp.status_code}: {resp.text[:200]}")

    data = resp.json()
    # 예상 구조: {"rCode":"OK","data":[{"originUrl":"...","shortenUrl":"https://link.coupang.com/a/...","landingUrl":"..."}]}
    out: List[str] = []
    for item in (data.get("data") or []):
        u = item.get("shortenUrl") or item.get("landingUrl")
        if isinstance(u, str) and u:
            out.append(u)
    return out

def deeplink_for_search(keyword: str, *, sub_id: Optional[str] = None) -> str:
    """
    '검색 결과 페이지' 딥링크를 우선 생성.
    - API 사용 가능 → 검색 URL을 딥링크로 변환(트래킹 유지, 404 리스크 최소)
    - 실패/비활성 → 일반 검색 URL 반환
    """
    search = coupang_search_url(keyword)
    if REQUIRE_COUPANG_API and (ACCESS_KEY and SECRET_KEY):
        try:
            dl = api_create_deeplink([search], sub_id=sub_id or _gen_subid())
            if dl and isinstance(dl[0], str):
                return dl[0]
        except Exception as e:
            # 문제 시 즉시 안전 폴백
            print(f"[deeplink_for_search] fallback to search URL: {e}")
    return search

def deeplink_for_query(keyword: str) -> str:
    """기존 호환용 별칭(검색 페이지 딥링크 우선)."""
    return deeplink_for_search(keyword)

__all__ = [
    "coupang_search_url",
    "api_create_deeplink",
    "deeplink_for_search",
    "deeplink_for_query",
]
