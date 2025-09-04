# budget_guard.py — no-image edition
# - 이미지 비용/로깅 전부 삭제
# - 단순 LLM 호출 로그 + 모델 추천만 유지
# - 추가: KST ISO 타임스탬프, 로깅 예외 안전성, 커스텀 로그파일명 지원

import os, json, time, pathlib
from datetime import datetime
from zoneinfo import ZoneInfo

# === Paths / Dirs ===
USAGE_DIR = os.getenv("USAGE_DIR", ".usage")
pathlib.Path(USAGE_DIR).mkdir(parents=True, exist_ok=True)

LLM_LOG_FILENAME = os.getenv("LLM_LOG_FILENAME", "llm.log")
LLM_LOG = os.path.join(USAGE_DIR, LLM_LOG_FILENAME)

def _now_kst_iso() -> str:
    try:
        return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%dT%H:%M:%S%z")
    except Exception:
        # Fallback: UTC ISO
        return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")

def log_llm(model: str, prompt: str, text: str) -> None:
    """간단 사용 로그(문자수 기준). 토큰 집계는 비용 절감을 위해 생략."""
    rec = {
        "ts": time.time(),                 # epoch seconds
        "ts_kst": _now_kst_iso(),          # 사람이 읽기 쉬운 KST
        "model": (model or "").strip(),
        "prompt_chars": len(prompt or ""),
        "output_chars": len(text or ""),
    }
    try:
        with open(LLM_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        # 로그 실패해도 파이프라인이 멈추지 않도록 방어
        try:
            print("[budget_guard] log_llm fallback:", json.dumps(rec, ensure_ascii=False))
        except Exception:
            pass

def recommend_models():
    """환경변수 → 모델/토큰 설정 반환"""
    return {
        "short": (os.getenv("OPENAI_MODEL") or "gpt-5-nano").strip(),
        "long": (os.getenv("OPENAI_MODEL_LONG") or "gpt-4o-mini").strip(),
        "max_tokens_body": int(os.getenv("MAX_TOKENS_BODY", "900")),
    }
