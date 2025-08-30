# post_test.py : 워드프레스 연결 테스트 (드래프트 글 1건 올리기)
import os
import requests
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

load_dotenv()

WP_URL = os.getenv("WP_URL", "").rstrip("/")
WP_USER = os.getenv("WP_USER")
WP_APP_PASSWORD = os.getenv("WP_APP_PASSWORD")

assert WP_URL and WP_USER and WP_APP_PASSWORD, ".env에 WP_URL/WP_USER/WP_APP_PASSWORD를 채워주세요."

def main():
    endpoint = f"{WP_URL}/wp-json/wp/v2/posts"
    payload = {
        "title": "API 연결 테스트",
        "content": "<p>이 글이 보이면 워드프레스 REST API 연결 성공입니다.</p>",
        "status": "draft"
    }
    auth = HTTPBasicAuth(WP_USER, WP_APP_PASSWORD)
    r = requests.post(endpoint, auth=auth, json=payload, timeout=30)

    if r.status_code in (200, 201):
        data = r.json()
        print("[성공] 드래프트가 생성되었습니다:", data.get("link"))
    else:
        print("[실패]", r.status_code, r.text)

if __name__ == "__main__":
    main()
