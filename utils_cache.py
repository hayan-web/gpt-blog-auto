# utils_cache.py
import os, json, hashlib, time
from pathlib import Path

CACHE_DIR = Path(os.getenv("CACHE_DIR", ".cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

def _stable_json(data):
    try:
        return json.dumps(data, sort_keys=True, ensure_ascii=False, default=repr)
    except Exception:
        return repr(data)

def _hash_key(fn_name: str, kwargs: dict):
    base = {"fn": fn_name, "kwargs": kwargs}
    raw = _stable_json(base).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()

def cached_call(fn, ttl_sec: int = 60*60*24, **kwargs):
    """
    간단 디스크 캐시: 동일 파라미터 호출 결과를 json으로 저장.
    기본 TTL=하루. 실패하면 원 호출 진행.
    """
    key = _hash_key(getattr(fn, "__name__", "anon"), kwargs)
    fpath = CACHE_DIR / f"cache_{key}.json"
    now = time.time()

    if fpath.exists():
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if now - obj.get("_ts", 0) <= ttl_sec:
                return obj["data"]
        except Exception:
            pass

    # 캐시 미스 → 실제 호출
    result = fn(**kwargs)
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump({"_ts": now, "data": result}, f, ensure_ascii=False)
    except Exception:
        pass
    return result
