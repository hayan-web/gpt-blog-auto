@echo off
REM run_local.bat — 일상글 2개 + 쿠팡글 1개 + 품질체크 + 성공 시 키워드 회전

REM ===== 콘솔/경로 설정 =====
chcp 65001 >nul
setlocal enableextensions enabledelayedexpansion
set "ROOT=C:\Users\ansdj\OneDrive\Desktop\gpt-blog"
cd /d "%ROOT%" || (echo [ERROR] 작업 폴더로 이동 실패: %ROOT% & pause & exit /b 1)

REM ===== 파이썬/의존성 =====
python -V
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

REM ===== 선택: .env 존재 안내 =====
if not exist ".env" (
  echo [WARN] .env 파일이 없습니다. 스크립트가 기본값으로 동작할 수 있습니다.
)

REM ===== 시드 품질체크 (클린 파일 생성) =====
if exist "seed_quality_check.py" (
  echo [STEP] 시드 품질 체크 중...
  python seed_quality_check.py --strict --write-clean --max-per-keyword 5
  if exist "products_seed.cleaned.csv" (
    set "PRODUCTS_SEED_CSV=products_seed.cleaned.csv"
    echo [OK] 클린 시드를 사용합니다: products_seed.cleaned.csv
  )
) else (
  echo [INFO] seed_quality_check.py 없음 → 건너뜀
)

REM ===== 일상글 2건 예약 (10:00 / 17:00 KST) =====
if exist "auto_wp_gpt.py" (
  echo [STEP] 일상글 2건 예약 실행...
  set "PYTHONIOENCODING=utf-8"
  set "WP_CATEGORY_DEFAULT=정보"
  python auto_wp_gpt.py --mode=two-posts
) else (
  echo [INFO] auto_wp_gpt.py 없음 → 일상글 단계 건너뜀
)

REM ===== 쿠팡 파트너스 글 1건 생성 (기본 13:00 KST 예약) =====
if exist "affiliate_post.py" (
  echo [STEP] 쿠팡 글 생성/예약 실행...
  set "PYTHONIOENCODING=utf-8"
  REM 필요 시 예약 시각 변경: set "AFFILIATE_TIME_KST=13:00"
  python affiliate_post.py 1> "%TEMP%\affiliate_out.txt" 2>&1
  type "%TEMP%\affiliate_out.txt"

  REM 성공 판단: 출력에 "post_id"가 포함되어 있으면 성공 처리
  findstr /c:"\"post_id\"" "%TEMP%\affiliate_out.txt" >nul
  if %errorlevel%==0 (
    echo [OK] 쿠팡 글 생성/예약 성공 → 키워드 회전 실행
    if exist "rotate_keywords.py" (
      python rotate_keywords.py
    ) else (
      echo [WARN] rotate_keywords.py 없음 → 회전 스킵
    )
  ) else (
    echo [INFO] 쿠팡 글이 생성되지 않음(시드 없음/권한/네트워크 등) → 회전 스킵
  )
  del "%TEMP%\affiliate_out.txt" 2>nul
) else (
  echo [INFO] affiliate_post.py 없음 → 쿠팡 단계 건너뜀
)

echo.
echo [DONE] 모든 단계 완료.
pause
