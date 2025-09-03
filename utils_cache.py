# utils_cache.py
import hashlib, json, os
from dotenv import load_dotenv
load_dotenv()

CACHE_DIR = os.getenv("CACHE_DIR", ".cache")
os.makedirs(CACHE_DIR, exist_ok=True)

def _key(model, prompt, kwargs):
    import json as _json
    payload = _json.dumps({"m": model, "p": prompt, "k": kwargs}, ensure_ascii=False, sort_keys=True)
    import hashlib as _hashlib
    return _hashlib.sha256(payload.encode("utf-8")).hexdigest()

def cached_call(fn, *, model, prompt, **kwargs):
    key = _key(model, prompt, kwargs)
    fp = os.path.join(CACHE_DIR, key + ".json")
    if os.path.exists(fp):
        with open(fp, "r", encoding="utf-8") as f:
            return json.load(f)
    result = fn(model=model, prompt=prompt, **kwargs)
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    return result
