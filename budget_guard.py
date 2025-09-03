# budget_guard.py
import os, json, time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

USAGE_DIR = Path(os.getenv("USAGE_DIR", ".usage"))
USAGE_DIR.mkdir(parents=True, exist_ok=True)

MONTHLY_BUDGET_USD = float(os.getenv("MONTHLY_BUDGET_USD", "10"))
BUDGET_MARGIN = float(os.getenv("BUDGET_MARGIN", "0.85"))

COST_PER_MTOKEN_NANO = float(os.getenv("COST_PER_MTOKEN_NANO", "0.02"))
COST_PER_MTOKEN_MINI = float(os.getenv("COST_PER_MTOKEN_MINI", "0.15"))
COST_PER_IMAGE_1024 = float(os.getenv("COST_PER_IMAGE_1024", "0.04"))
COST_PER_IMAGE_768  = float(os.getenv("COST_PER_IMAGE_768", "0.03"))

def _month_key(ts=None):
    ts = ts or time.time()
    return time.strftime("%Y%m", time.localtime(ts))

def _logfile():
    return USAGE_DIR / f"usage_{_month_key()}.jsonl"

def approx_tokens(text: str) -> int:
    return max(1, len(text) // 4)

def log_llm(model: str, prompt: str, output: str):
    rec = {
        "ts": time.time(),
        "kind": "llm",
        "model": model,
        "in_tokens": approx_tokens(prompt),
        "out_tokens": approx_tokens(output),
    }
    with open(_logfile(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def log_image(size_px: int = 768):
    rec = {"ts": time.time(), "kind": "image", "size": size_px}
    with open(_logfile(), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def _price_rec(r):
    if r["kind"] == "llm":
        mtok = (r["in_tokens"] + r["out_tokens"]) / 1_000_000
        unit = COST_PER_MTOKEN_MINI if "mini" in r["model"] else COST_PER_MTOKEN_NANO
        return mtok * unit
    if r["kind"] == "image":
        return COST_PER_IMAGE_1024 if r.get("size", 768) >= 1024 else COST_PER_IMAGE_768
    return 0.0

def current_cost_usd():
    fp = _logfile()
    if not fp.exists():
        return 0.0
    total = 0.0
    with open(fp, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line.strip())
                total += _price_rec(rec)
            except Exception:
                continue
    return round(total, 4)

def should_emergency_save():
    return current_cost_usd() >= MONTHLY_BUDGET_USD * BUDGET_MARGIN

def recommend_models():
    """ 예산 상황에 따른 모델 추천(자동 다운그레이드).
        환경변수 값이 비어있으면 안전한 기본값으로 대체한다.
    """
    def _coalesce_env(name: str, default: str) -> str:
        v = os.getenv(name, "")
        v = (v or "").strip()
        return v if v else default

    if should_emergency_save():
        # 강제 저비용: 본문도 nano
        return dict(short="gpt-5-nano", long="gpt-5-nano", max_tokens_body=700)

    short = _coalesce_env("OPENAI_MODEL", "gpt-5-nano")
    long  = _coalesce_env("OPENAI_MODEL_LONG", "gpt-4o-mini")
    max_body = os.getenv("MAX_TOKENS_BODY", "900")
    try:
        max_body = int(max_body)
    except Exception:
        max_body = 900
    return dict(short=short, long=long, max_tokens_body=max_body)

def allowed_images(default_num: int = 1):
    if should_emergency_save():
        return 0
    return min(default_num, 1)
