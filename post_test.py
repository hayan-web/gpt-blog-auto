# post_test.py : 워드프레스 연결/권한/발행(예약) 테스트
import os, argparse
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

WP_URL = (os.getenv("WP_URL", "") or "").rstrip("/")
WP_USER = os.getenv("WP_USER")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")
TLS_VERIFY = os.getenv("WP_TLS_VERIFY", "true").lower() != "false"

def die(msg):
    print("[오류]", msg)
    raise SystemExit(1)

def kst_now():
    return datetime.now(ZoneInfo("Asia/Seoul"))

def to_utc_iso(dt_kst):
    return dt_kst.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def hint_by_status(code, body=""):
    body = (body or "")[:400]
    if code == 401:
        return ("[힌트] 401 Unauthorized → 사용자명/앱 비밀번호 확인, "
                "보안 플러그인(Basic Auth 차단) 또는 서버 리버스프록시에서 Authorization 헤더 제거 여부 확인.\n"
                f"응답 일부: {body}")
    if code == 403:
        return ("[힌트] 403 Forbidden → REST API 차단, 애플리케이션 비밀번호 비활성, "
                "IP 차단/웹방화벽, 캡차 플러그인 등 확인.\n"
                f"응답 일부: {body}")
    if code == 404:
        return ("[힌트] 404 Not Found → 사이트 주소/퍼머링크/REST 엔드포인트 확인. "
                "리버스프록시에서 /wp-json 경로가 막혀있을 수 있습니다.\n"
                f"응답 일부: {body}")
    if 500 <= code < 600:
        return ("[힌트] 5xx 서버 오류 → 서버/PHP 에러 로그 확인, 보안 모듈(ModSecurity) 규칙 충돌 가능.\n"
                f"응답 일부: {body}")
    return f"[응답 일부] {body}"

def preflight():
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        die(".env에 WP_URL / WP_USER / WP_APP_PASSWORD 를 채워주세요.")
    # 1) REST 루트 확인
    try:
        r = requests.get(f"{WP_URL}/wp-json", timeout=15, verify=TLS_VERIFY)
        if not r.ok:
            print("[점검] /wp-json 접근 실패:", r.status_code)
            print(hint_by_status(r.status_code, r.text))
        else:
            print("[OK] /wp-json 접근 가능")
    except requests.exceptions.SSLError as e:
        die(f"SSL 오류: {e}. 자체서명 인증서면 WP_TLS_VERIFY=false 로 시도해보세요.")
    except Exception as e:
        die(f"/wp-json 접근 중 예외: {e}")

    # 2) 인증 확인 (내 사용자)
    try:
        r = requests.get(f"{WP_URL}/wp-json/wp/v2/users/me",
                         auth=HTTPBasicAuth(WP_USER, WP_APP_PASSWORD),
                         timeout=15, verify=TLS_VERIFY)
        if not r.ok:
            print("[점검] /users/me 인증 실패:", r.status_code)
            print(hint_by_status(r.status_code, r.text))
        else:
            j = r.json()
            print(f"[OK] 인증 성공: id={j.get('id')} name={j.get('name')}")
    except Exception as e:
        die(f"/users/me 요청 예외: {e}")

def create_post(status="draft", minutes_ahead=5):
    endpoint = f"{WP_URL}/wp-json/wp/v2/posts"
    title = f"API 연결 테스트 - {kst_now().strftime('%Y-%m-%d %H:%M')}"
    payload = {
        "title": title,
        "content": "<p>이 글이 보이면 워드프레스 REST API 연결/권한 설정이 정상입니다.</p>",
        "status": status
    }
    if status == "future":
        when_kst = kst_now() + timedelta(minutes=minutes_ahead)
        payload["date_gmt"] = to_utc_iso(when_kst)
        print(f"[예약] {when_kst.strftime('%Y-%m-%d %H:%M KST')} (UTC: {payload['date_gmt']}) 로 예약")

    try:
        r = requests.post(
            endpoint,
            auth=HTTPBasicAuth(WP_USER, WP_APP_PASSWORD),
            json=payload,
            timeout=30,
            verify=TLS_VERIFY
        )
    except requests.exceptions.SSLError as e:
        die(f"SSL 오류: {e} (WP_TLS_VERIFY=false 로 재시도 가능)")
    except Exception as e:
        die(f"POST 예외: {e}")

    if r.status_code in (200, 201):
        data = r.json()
        print(f"[성공] 생성됨 → status={data.get('status')} link={data.get('link')}")
        if status == "future":
            print(f"[예약확인] date_gmt={data.get('date_gmt')}")
    else:
        print("[실패]", r.status_code)
        print(hint_by_status(r.status_code, r.text))
        # 추가 디버깅 포인터
        print("- 앱 비밀번호가 사용자 프로필에서 생성된 것인지 확인")
        print("- 사용자 역할에 '글 작성/발행' 권한이 있는지 확인")
        print("- 보안 플러그인(예: Authorization 헤더 차단/REST 차단) 설정 점검")
        print("- 서버 리버스프록시(Nginx/Cloudflare)에서 헤더 제거/캐시 규칙 확인")

def main():
    parser = argparse.ArgumentParser(description="WordPress REST API 연결/발행 테스트")
    parser.add_argument("--status", choices=["draft","publish","future"], default="draft",
                        help="생성 상태 (draft/publish/future)")
    parser.add_argument("--minutes", type=int, default=5,
                        help="예약(future) 시각: 현재로부터 +N분 (KST)")
    args = parser.parse_args()

    preflight()
    create_post(status=args.status, minutes_ahead=args.minutes)

if __name__ == "__main__":
    main()
