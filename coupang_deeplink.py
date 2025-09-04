# -*- coding: utf-8 -*-
"""Coupang Partners Deeplink helper (HMAC, robust/batch)"""
import json, time, hmac, hashlib, random
from datetime import datetime, timezone
from typing import List, Dict, Optional
from urllib.parse import urlparse, urlunparse

import requests

DOMAIN = "https://api-gateway.coupang.com"
DEEPLINK_PATH = "/v2/providers/affiliate_open_api/apis/openapi/v1/deeplink"
MAX_BATCH = 50  # Coupang API는 여러 URL을 한 번에 받음(안전 상한 50으로 설정)

def _signed_datetime() -> str:
    # Coupang CEA 서명은 UTC YYMMDD'T'HHMMSS'Z' 포맷
    return datetime.now(timezone.utc).strftime("%y%m%dT%H%M%SZ")

def build_auth_header(method: str, uri: str, access_key: str, secret_key: str) -> str:
    dt = _signed_datetime()
    msg = f"{dt}{method}{uri}"
    sig = hmac.new(secret_key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"CEA algorithm=HmacSHA256, access-key={access_key}, signed-date={dt}, signature={sig}"

def _normalize_for_match(u: str) -> str:
    """매칭을 위한 느슨한 정규화: scheme/host 소문자, path의 trailing '/' 제거"""
    try:
        pu = urlparse((u or "").strip())
        scheme = (pu.scheme or "https").lower()
        netloc = pu.netloc.lower()
        path = pu.path.rstrip("/")
        return urlunparse((scheme, netloc, path, "", "", ""))
    except Exception:
        return (u or "").strip().rstrip("/")

def _chunk(lst: List[str], size: int):
    for i in range(0, len(lst), size):
        yield lst[i:i+size]

def _post_deeplink_batch(
    urls_batch: List[str],
    access_key: str,
    secret_key: str,
    sub_id: Optional[str],
    channel_id: Optional[str],
    timeout: int,
    session: requests.Session,
) -> Dict[str, str]:
    payload = {"coupangUrls": urls_batch}
    if sub_id:
        payload["subId"] = sub_id
    if channel_id:
        payload["channelId"] = channel_id

    headers = {
        "Authorization": build_auth_header("POST", DEEPLINK_PATH, access_key, secret_key),
        "Content-Type": "application/json",
    }
    r = session.post(DOMAIN + DEEPLINK_PATH, json=payload, headers=headers, timeout=timeout)
    text = getattr(r, "text", "")
    if not r.ok:
        # 가능한 한 많은 디버그 정보를 남긴다
        raise RuntimeError(f"Deeplink HTTP {r.status_code}: {text[:400]}")

    data = r.json()
    arr = data.get("data")
    mapping: Dict[str, str] = {}
    if isinstance(arr, list):
        # 원본 문자열과 응답의 originUrl 모두에 대해 매핑
        # (정규화 매칭으로 http/https, trailing slash 차이를 흡수)
        originals_norm = { _normalize_for_match(u): u for u in urls_batch }
        for item in arr:
            o = item.get("originUrl") or item.get("coupangUrl")
            s = item.get("shortenUrl")
            if not (o and s):
                continue
            o_norm = _normalize_for_match(o)
            mapping[o] = s  # 응답의 키 그대로
            if o_norm in originals_norm:
                mapping[originals_norm[o_norm]] = s  # 호출자가 준 원본 문자열에도 매핑
    return mapping

def create_deeplinks(
    urls: List[str],
    access_key: str,
    secret_key: str,
    sub_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    retries: int = 3,
    timeout: int = 15,
) -> Dict[str, str]:
    """
    여러 URL을 쿠팡 파트너스 딥링크로 변환.
    - 중복 제거(순서 보존) + 50개 배치 API 호출
    - 오류 시 지수형 백오프 재시도
    - 반환: {원본(or originUrl): shortenUrl}
      (원본 문자열과 응답 originUrl 모두에 대해 매핑 시도)
    """
    if not urls:
        return {}
    if not (access_key and secret_key):
        raise ValueError("access_key/secret_key가 필요합니다.")

    # 중복 제거(원본 순서 보존)
    uniq: List[str] = []
    seen = set()
    for u in urls:
        if u and (u not in seen):
            seen.add(u)
            uniq.append(u)

    out: Dict[str, str] = {}
    with requests.Session() as session:
        for batch in _chunk(uniq, MAX_BATCH):
            last_err = None
            for attempt in range(1, max(1, retries) + 1):
                try:
                    part = _post_deeplink_batch(
                        urls_batch=batch,
                        access_key=access_key,
                        secret_key=secret_key,
                        sub_id=sub_id,
                        channel_id=channel_id,
                        timeout=timeout,
                        session=session,
                    )
                    out.update(part)
                    break  # 배치 성공
                except Exception as e:
                    last_err = e
                    # 429/5xx 등은 백오프 후 재시도
                    sleep_s = min(20, (2 ** (attempt - 1))) + random.uniform(0, 0.5)
                    time.sleep(sleep_s)
            else:
                # 모든 재시도 실패
                raise RuntimeError(f"Deeplink batch failed after {retries} tries: {last_err}")

    return out

def create_deeplink(
    url: str,
    access_key: str,
    secret_key: str,
    sub_id: Optional[str] = None,
    channel_id: Optional[str] = None,
    retries: int = 3,
    timeout: int = 15,
) -> str:
    """단일 URL용 헬퍼: 실패 시 원본 URL 그대로 반환."""
    if not url:
        return url
    try:
        m = create_deeplinks([url], access_key, secret_key, sub_id=sub_id, channel_id=channel_id, retries=retries, timeout=timeout)
        return m.get(url) or m.get(_normalize_for_match(url)) or url
    except Exception:
        return url
