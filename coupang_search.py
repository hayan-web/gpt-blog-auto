# -*- coding: utf-8 -*-
"""
coupang_search.py — Coupang Partners "products/search" helper
- Uses same CEA(HmacSHA256) header style as deeplink helper
- Returns normalized list: [{"productName","productUrl","imageUrl","price","category"}]
"""
from typing import List, Dict, Optional
from urllib.parse import urlencode
import requests
from coupang_deeplink import build_auth_header, DOMAIN

SEARCH_PATH = "/v2/providers/affiliate_open_api/apis/openapi/products/search"

def search_products(
    keyword: str,
    access_key: str,
    secret_key: str,
    limit: int = 10,
    sort: Optional[str] = None,  # accuracy | salesVolume | keywordRank | priceAsc | priceDesc | latest
    **params
) -> List[Dict]:
    if not (keyword and access_key and secret_key):
        return []
    q = {"keyword": keyword, "limit": max(1, min(50, int(limit)))}
    if sort:
        q["sort"] = sort
    # optional passthrough
    for k in ("minPrice","maxPrice","rocketOnly"):
        if k in params and params[k] is not None:
            q[k] = params[k]
    # Build header (include query string in signature to be safe)
    qs = "?" + urlencode(q, doseq=True)
    headers = {
        "Authorization": build_auth_header("GET", SEARCH_PATH + qs, access_key, secret_key),
        "Accept": "application/json",
    }
    url = DOMAIN + SEARCH_PATH
    r = requests.get(url, headers=headers, params=q, timeout=15)
    if not r.ok:
        raise RuntimeError(f"[coupang_search] HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    # Typical shape: {"rCode":"0","rMessage":"OK","data":[{...}]}
    items = data.get("data") or data.get("productData") or []
    out = []
    for it in items:
        out.append({
            "productName": it.get("productName") or it.get("title") or "",
            "productUrl": it.get("productUrl") or it.get("link") or "",
            "imageUrl": it.get("imageUrl") or it.get("image") or "",
            "price": it.get("price") or it.get("lPrice") or None,
            "category": it.get("categoryName") or "",
        })
    return [x for x in out if x["productName"] and x["productUrl"]]
