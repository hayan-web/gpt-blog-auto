# budget_guard.py — no-image edition
# - 이미지 비용/로깅 전부 삭제
# - 단순 LLM 호출 로그 + 모델 추천만 유지

import os, json, time, pathlib

USAGE_DIR = os.getenv("USAGE_DIR", ".usage")
pathlib.Path(USAGE_DIR).mkdir(parents=True, exist_ok=True)

LLM_LOG = os.path.join(USAGE_DIR, "llm.log")

def log_llm(model: str, prompt: str, text: str) -> None:
    """간단 사용 로그(문자수 기준). 토큰 집계는 비용 절감을 위해 생략."""
    rec = {
        "ts": time.time(),
        "model": (model or "").strip(),
        "prompt_chars": len(prompt or ""),
        "output_chars": len(text or ""),
    }
    with open(LLM_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def recommend_models():
    """환경변수 → 모델/토큰 설정 반환"""
    return {
        "short": (os.getenv("OPENAI_MODEL") or "gpt-5-nano").strip(),
        "long": (os.getenv("OPENAI_MODEL_LONG") or "gpt-4o-mini").strip(),
        "max_tokens_body": int(os.getenv("MAX_TOKENS_BODY", "900")),
    }
