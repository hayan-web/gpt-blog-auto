# utils_cache.py
# - 디스크 캐시( JSON ) 간단 유틸
# - 호환: cached_call(fn, ttl_sec=..., **kwargs) 시그니처 유지
# - 개선:
#   * 환경변수로 TTL/비활성/최대 파일 수 제어
#   * 네임스페이스/소금(salt)로 키 충돌 최소화
#   * 원자적 쓰기(tempfile + os.replace), 손상 파일 자동 건너뜀
#   * 실패 시 스테일 캐시 반환 옵션(cache_on_error)
#   * LRU 기반 오래된 캐시 자동 정리

import os, json, hashlib, time, tempfile
from pathlib import Path
from typing import Any, Dict, Optional

# ====== Env ======
CACHE_DIR = Path(os.getenv("CACHE_DIR", ".cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_DISABLE = os.getenv("CACHE_DISABLE", "false").lower() == "true"
CACHE_TTL_DEFAULT = int(os.getenv("CACHE_TTL_DEFAULT", str(60 * 60 * 24)))  # 1d
CACHE_MAX_FILES = int(os.getenv("CACHE_MAX_FILES", "500"))

# ====== Helpers ======
def _stable_json(data: Any) -> str:
    """해시용 안정 JSON 직렬화 (정렬/비ASCII 허용/미직렬 객체는 repr)."""
    try:
        return json.dumps(data, sort_keys=True, ensure_ascii=False, default=repr, separators=(",", ":"))
    except Exception:
        return repr(data)

def _hash_key(fn_name: str, kwargs: Dict[str, Any], namespace: Optional[str], salt: Optional[str]) -> str:
    base = {
        "fn": fn_name or "anon",
        "namespace": namespace or "default",
        "salt": salt or "",
        "kwargs": kwargs or {},
    }
    raw = _stable_json(base).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()

def _cache_path(fn_name: str, key_hex: str, namespace: Optional[str]) -> Path:
    ns = (namespace or "default").replace(os.sep, "_")
    fname = f"cache_{ns}_{fn_name or 'anon'}_{key_hex}.json"
    return CACHE_DIR / fname

def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

def _atomic_write_json(path: Path, obj: Dict[str, Any]) -> None:
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(CACHE_DIR), delete=False) as tmp:
            json.dump(obj, tmp, ensure_ascii=False, separators=(",", ":"))
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, path)
    except Exception:
        # 실패해도 캐싱이 필수는 아님
        try:
            if "tmp_path" in locals() and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)  # py3.8 호환 시 try/except
        except Exception:
            pass

def _prune_cache(max_files: int) -> None:
    try:
        files = sorted((p for p in CACHE_DIR.glob("cache_*.json") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[max_files:]:
            try: p.unlink()
            except Exception: pass
    except Exception:
        pass

# ====== Public API ======
def cached_call(
    fn,
    ttl_sec: Optional[int] = None,
    *,
    namespace: Optional[str] = None,
    key_salt: Optional[str] = None,
    cache_on_error: bool = True,
    **kwargs
):
    """
    디스크 캐시 래퍼: 동일 파라미터 호출 결과를 JSON 파일로 저장/재사용.
    - fn: 호출할 함수 (반환값은 JSON 직렬화 가능해야 함)
    - ttl_sec: 캐시 유효기간(초). 미지정 시 CACHE_TTL_DEFAULT 적용
    - namespace: 캐시 이름공간(파일명/키 분리)
    - key_salt: 키 소금값(모델 버전 등)
    - cache_on_error: 만료 후 호출 실패 시, 스테일 캐시가 있으면 그것을 반환

    사용 예:
        def _call(model, prompt): ...
        res = cached_call(_call, ttl_sec=3600, namespace="openai", model="gpt-5-nano", prompt="hello")
    """
    if CACHE_DISABLE:
        return fn(**kwargs)

    ttl = int(ttl_sec if ttl_sec is not None else CACHE_TTL_DEFAULT)

    # 키 생성
    fn_name = getattr(fn, "__name__", "anon")
    key_hex = _hash_key(fn_name, kwargs, namespace=namespace, salt=key_salt)
    fpath = _cache_path(fn_name, key_hex, namespace)

    now = time.time()

    # 1) 히트 확인
    if fpath.exists():
        obj = _read_json(fpath)
        if obj and "_ts" in obj and "data" in obj:
            age = now - float(obj.get("_ts", 0))
            if age <= ttl:
                return obj["data"]
            # 만료되었지만 스테일 백업 가능성을 위해 보관
            stale = obj
        else:
            stale = None
    else:
        stale = None

    # 2) 미스 → 실제 호출
    try:
        result = fn(**kwargs)
    except Exception:
        if cache_on_error and stale is not None:
            # 호출 실패 시 스테일 데이터라도 반환
            return stale["data"]
        raise

    # 3) 쓰기
    try:
        _atomic_write_json(fpath, {"_ts": now, "data": result})
        _prune_cache(CACHE_MAX_FILES)
    except Exception:
        pass

    return result

# (선택) 모듈 단독 실행 시 간단한 상태/정리 기능
if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="utils_cache maintenance")
    ap.add_argument("--prune", type=int, default=CACHE_MAX_FILES, help="최대 파일 수 유지 (LRU 제거)")
    ap.add_argument("--clear", action="store_true", help="모든 cache_*.json 삭제")
    args = ap.parse_args()

    if args.clear:
        n = 0
        for p in CACHE_DIR.glob("cache_*.json"):
            try: p.unlink(); n += 1
            except Exception: pass
        print(f"[OK] cleared {n} cache files")
    else:
        _prune_cache(args.prune)
        print(f"[OK] pruned to <= {args.prune} files in {CACHE_DIR}")
