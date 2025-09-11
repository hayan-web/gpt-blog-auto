# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상형 포스트 자동 발행 (SEO 구조/표/광고 반영)
- 키워드 기반: keywords_general.csv 에서 선택
- 본문: H2 부제목 + 요약(<=300자) + H3 섹션 6~8개 + 표 1개 이상 + 내부광고(상단/중간)
- 태그는 키워드 1~2개에 맞춰 자동 생성
- 워크플로에서 --mode=two-posts 등으로 호출 가능(기존 인터페이스 유지)
"""

import os, csv, json, html, random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import requests
from dotenv import load_dotenv

load_dotenv()

WP_URL=(os.getenv("WP_URL") or "").strip().rstrip("/")
WP_USER=os.getenv("WP_USER") or ""
WP_APP_PASSWORD=os.getenv("WP_APP_PASSWORD") or ""
WP_TLS_VERIFY=(os.getenv("WP_TLS_VERIFY") or "true").lower()!="false"
POST_STATUS=(os.getenv("POST_STATUS") or "future").strip()

DEFAULT_CATEGORY=(os.getenv("DEFAULT_CATEGORY") or "정보").strip() or "정보"
DEFAULT_TAGS=(os.getenv("DEFAULT_TAGS") or "").strip()

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-daily/3.0"
REQ_HEADERS={"User-Agent":USER_AGENT,"Accept":"application/json","Content-Type":"application/json; charset=utf-8"}

def _adsense_block()->str:
    sc=(os.getenv("AD_SHORTCODE") or "").strip()
    return f'<div class="ads-wrap" style="margin:16px 0">{sc}</div>' if sc else ""

def _css()->str:
    return """
<style>
.daily-wrap{line-height:1.7}
.daily-sub{margin:10px 0 6px;font-size:1.2rem;color:#334155}
.daily-hr{border:0;border-top:1px solid #e5e7eb;margin:16px 0}
.daily-table{width:100%;border-collapse:collapse;margin:8px 0 14px}
.daily-table th,.daily-table td{border:1px solid #e5e7eb;padding:8px 10px;text-align:left}
.daily-table thead th{background:#f8fafc}
.daily-wrap h2{margin:18px 0 6px}
.daily-wrap h3{margin:16px 0 6px}
</style>
"""

def _ensure_term(kind:str, name:str)->int:
    r=requests.get(f"{WP_URL}/wp-json/wp/v2/{kind}",params={"search":name,"per_page":50,"context":"edit"},
                   auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status()
    for it in r.json():
        if (it.get("name") or "").strip()==name: return int(it["id"])
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/{kind}", json={"name":name},
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=15, headers=REQ_HEADERS)
    r.raise_for_status(); return int(r.json()["id"])

def post_wp(title:str, html_body:str, when_gmt:str, category:str, tags:list[str])->dict:
    cat_id=_ensure_term("categories", category or DEFAULT_CATEGORY)
    tag_ids=[]
    for t in tags[:5]:
        try: tag_ids.append(_ensure_term("tags", t))
        except Exception: pass
    payload={"title":title,"content":html_body,"status":POST_STATUS,"categories":[cat_id],"tags":tag_ids,
             "comment_status":"closed","ping_status":"closed","date_gmt":when_gmt}
    r=requests.post(f"{WP_URL}/wp-json/wp/v2/posts", json=payload,
                    auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=20, headers=REQ_HEADERS)
    r.raise_for_status(); return r.json()

def _read_col(path:str)->list[str]:
    if not os.path.exists(path): return []
    rows=list(csv.reader(open(path,"r",encoding="utf-8",newline="")))
    out=[]
    for i,row in enumerate(rows):
        if not row: continue
        if i==0 and row[0].strip().lower() in ("keyword","title"): continue
        if row[0].strip(): out.append(row[0].strip())
    return out

def _pick_general_keyword()->str:
    ks=_read_col("keywords_general.csv")
    return ks[0] if ks else "오늘의 기록"

def _slot_at(hour:int, minute:int)->str:
    now=datetime.now(ZoneInfo("Asia/Seoul"))
    tgt=now.replace(hour=hour,minute=minute,second=0,microsecond=0)
    if tgt<=now: tgt+=timedelta(days=1)
    return tgt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

def _title_from_kw(kw:str)->str:
    s=kw.strip()
    s=s.replace('"',"").replace("'","")
    if len(s)<10: s=f"{s}에 대해 차분히 정리해 봤어요"
    if len(s)>38: s=s[:38]+"…"
    return s

def _category_url(name:str)->str:
    try:
        r=requests.get(f"{WP_URL}/wp-json/wp/v2/categories",
                       params={"search":name,"per_page":50,"context":"view"},
                       headers=REQ_HEADERS, auth=(WP_USER,WP_APP_PASSWORD), verify=WP_TLS_VERIFY, timeout=12)
        r.raise_for_status()
        for it in r.json():
            if (it.get("name") or "").strip()==name:
                link=(it.get("link") or "").strip()
                if link: return link
    except Exception:
        pass
    return f"{WP_URL}/category/{name}/"

def _html_daily(keyword:str)->str:
    k=html.escape(keyword)
    subtitle=f"{k} 핵심만 담아보기"
    summary=(f"{k}와 관련된 내용을 일상 맥락에서 자연스럽게 정리했습니다. "
             f"누가·언제·어디서 쓰는지에 따라 포인트가 달라지므로, 사용 장면을 먼저 떠올리고 핵심만 빠르게 읽을 수 있도록 구성했어요.")

    table=f"""
<table class="daily-table">
  <thead><tr><th>구간</th><th>핵심</th><th>참고</th></tr></thead>
  <tbody>
    <tr><td>준비</td><td>용도/장소 정리</td><td>과투자 방지</td></tr>
    <tr><td>사용</td><td>자주 쓰는 기능 위주</td><td>습관 붙이기</td></tr>
    <tr><td>관리</td><td>세척/보관 루틴</td><td>유지비 체크</td></tr>
    <tr><td>업그레이드</td><td>부족한 한 가지</td><td>단계적 전환</td></tr>
  </tbody>
</table>
""".strip()

    cat_url=_category_url(DEFAULT_CATEGORY)

    body=f"""
{_css()}
<div class="daily-wrap">
  {_adsense_block()}
  <h2 class="daily-sub">{subtitle}</h2>
  <p>{summary}</p>
  <hr class="daily-hr">

  <h3>장면을 먼저 떠올리기</h3>
  <p>“어디서, 언제, 누구와”가 정해지면 선택과 실행이 놀랄 만큼 빨라집니다. 필요한 요소만 남기고 나머지는 가볍게 두세요.</p>
  <hr class="daily-hr">

  <h3>핵심 두세 가지에 집중</h3>
  <p>모든 걸 다 하려 하지 않기. 자주 쓰는 기능 두세 가지가 루틴을 만듭니다. 익숙해지면 자동으로 손이 가요.</p>
  <hr class="daily-hr">

  <h3>가성비를 좌우하는 것</h3>
  <p>구매가보다 유지비가 체감을 좌우합니다. 주기/소모품/시간을 한 번에 계산해 보세요.</p>
  {table}
  <hr class="daily-hr">

  <h3>관리 루틴 짧게 만들기</h3>
  <p>세척과 보관을 한 동선 안에 묶으면 피곤함이 확 줄어듭니다. ‘바로 닿는 자리’를 확보하세요.</p>
  <hr class="daily-hr">

  {_adsense_block()}

  <h3>부담 없이 시작하는 방법</h3>
  <p>처음부터 완벽함을 노리기보다, 지금 있는 것부터 가볍게. 쓰면서 필요한 한 가지를 발견하는 편이 현실적입니다.</p>
  <hr class="daily-hr">

  <h3>한 줄 결론</h3>
  <p>목적이 선명하면 선택은 빨라지고, 관리가 쉬우면 꾸준함이 만들어집니다. 그래서 작은 개선이 가장 큽니다.</p>

  <p style="margin:18px 0 0"><a href="{html.escape(cat_url)}">더 많은 글 보기</a></p>
</div>
""".strip()
    return body

def create_one(hour:int, minute:int)->dict:
    kw=_pick_general_keyword()
    title=_title_from_kw(kw)
    when=_slot_at(hour,minute)
    body=_html_daily(kw)
    tags=[kw] if kw and kw!="오늘의 기록" else []
    res=post_wp(title, body, when, category=DEFAULT_CATEGORY, tags=tags)
    return {"id":res.get("id"),"title":title,"date_gmt":res.get("date_gmt"),"link":res.get("link")}

def main(mode:str="two-posts"):
    if not (WP_URL and WP_USER and WP_APP_PASSWORD):
        raise RuntimeError("WP_URL/WP_USER/WP_APP_PASSWORD 필요")
    out=[]
    if mode=="two-posts":
        out.append(create_one(10,0))
        out.append(create_one(13,0))
    else:
        out.append(create_one(10,0))
    print(json.dumps(out, ensure_ascii=False))

if __name__=="__main__":
    import sys
    mode="two-posts"
    for i,a in enumerate(sys.argv):
        if a.startswith("--mode="): mode=a.split("=",1)[1]
    main(mode)
