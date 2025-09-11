# auto_wp_gpt.py
# -*- coding: utf-8 -*-
"""
auto_wp_gpt.py — 일상형 포스트 자동 발행 (SEO 구조/표/광고 반영, 1500자 내외)
- 키워드 기반: keywords_general.csv 에서 선택 (없으면 안전 폴백 제목)
- 본문: H2 부제목 + 개요(<=300자, 존댓말) + H3 섹션 6~8개 + 표 1개 이상 + 내부광고(상단/중간)
- 태그는 키워드 1~2개에 맞춰 자동 생성
- 워크플로에서 --mode=two-posts 등으로 호출 가능(기존 인터페이스 유지)
- 워드프레스에 그대로 HTML로 들어가며 "요약글/본문1/본문2" 같은 안내 문구는 출력하지 않음
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

USER_AGENT=os.getenv("USER_AGENT") or "gpt-blog-daily/3.1"
REQ_HEADERS={"User-Agent":USER_AGENT,"Accept":"application/json","Content-Type":"application/json; charset=utf-8"}

def _adsense_block()->str:
    sc=(os.getenv("AD_SHORTCODE") or "").strip()
    return f'<div class="ads-wrap" style="margin:16px 0">{sc}</div>' if sc else ""

def _css()->str:
    return """
<style>
.daily-wrap{line-height:1.78;font-size:16px;color:#0f172a}
.daily-sub{margin:10px 0 6px;font-size:1.2rem;color:#334155}
.daily-hr{border:0;border-top:1px solid #e5e7eb;margin:18px 0}
.daily-table{width:100%;border-collapse:collapse;margin:8px 0 14px}
.daily-table th,.daily-table td{border:1px solid #e5e7eb;padding:8px 10px;text-align:left}
.daily-table thead th{background:#f8fafc}
.daily-wrap h2{margin:18px 0 6px}
.daily-wrap h3{margin:18px 0 8px;font-size:1.05rem}
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
    if len(s)<6: s=f"{s}에 대해 차분히 정리해 봤어요"
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

def _mk_paragraph(txt:str)->str:
    return f"<p>{txt}</p>"

def _html_daily(keyword:str)->str:
    k=html.escape(keyword)
    subtitle=f"{k} 핵심만 담아보기"
    summary=(f"{k}와 관련된 내용을 일상 맥락에서 자연스럽게 정리했습니다. "
             f"누가·언제·어디서 쓰는지에 따라 포인트가 달라지므로, 사용 장면을 먼저 떠올리고 핵심만 빠르게 읽을 수 있도록 구성했어요.")

    # 4x5 표 한 개
    table=f"""
<table class="daily-table">
  <thead><tr><th>구간</th><th>핵심</th><th>실수</th><th>대안</th></tr></thead>
  <tbody>
    <tr><td>준비</td><td>용도·장소 정의</td><td>과투자</td><td>필수/옵션 분리</td></tr>
    <tr><td>사용</td><td>두세 기능 집중</td><td>설정 과도화</td><td>프리셋 고정</td></tr>
    <tr><td>관리</td><td>세척·보관 동선</td><td>방치</td><td>루틴 묶기</td></tr>
    <tr><td>업그레이드</td><td>한 가지씩 보강</td><td>일괄 교체</td><td>단계 전환</td></tr>
  </tbody>
</table>
""".strip()

    # 섹션 본문(1500자 근사치)
    sec = []
    sec.append((
        "장면을 먼저 떠올리기",
        "어디서 언제 누구와 사용할지부터 정리하면 선택이 쉬워집니다. 공간의 제약, 소음 허용치, 보관 위치 같은 현실 조건을 미리 적어두면 필요 이상으로 욕심내지 않게 되고, 사소해 보이는 불편을 줄일 수 있어요. 오늘 당장 쓰일 장면 한 가지만 확실히 잡아도 방향이 선명해집니다."
    ))
    sec.append((
        "핵심 두세 가지에 집중",
        "모든 기능을 잘 쓰려는 순간 복잡해집니다. 자주 쓰게 될 두세 기능만 정하고 그 외는 숨기는 편이 유지에 유리합니다. 처음 일주일은 일부러 단순한 설정으로 반복해 보세요. 몸에 익는 순간 루틴이 생기고, 자연스레 사용 빈도가 올라갑니다."
    ))
    sec.append((
        "가성비를 좌우하는 요소",
        "구매가보다 유지비가 체감을 좌우합니다. 소모품 가격, 교체 주기, 세척 시간, 전력 사용량을 같이 계산해 보면 값이 싸도 비싼 선택이 있고, 반대로 처음 값이 높아도 총비용이 낮은 경우가 뚜렷합니다. 장바구니에 담기 전 ‘유지비 합계’를 한 번만 적어보세요."
    ))
    sec.append((
        "관리 루틴을 짧게",
        "세척과 보관은 한 동선 안에 묶는 게 핵심입니다. ‘바로 닿는 자리’가 있으면 귀찮음이 크게 줄어들어요. 사용 직후 가볍게 털어내고 제자리로 돌려놓는 1분 루틴을 만들면, 제품 수명과 위생이 함께 좋아집니다."
    ))
    sec.append((
        "작은 개선이 큰 차이",
        "처음부터 완벽한 세팅을 만들려 하면 지칩니다. 지금 쓰는 방식에서 한 가지 불편만 줄여보세요. 예를 들어 전원 케이블을 정리하거나, 자주 쓰는 모드를 첫 화면에 두는 식의 작은 개선이 실제 만족도를 크게 바꿉니다."
    ))
    sec.append((
        "상황별 체크포인트",
        "가정용과 사무용, 개인과 공용은 기준이 달라야 합니다. 공용이라면 누구나 이해할 수 있는 간단한 안내와 표준 설정을 마련하고, 개인이라면 손에 익는 조작과 보관성을 더 우선하세요. 상황이 바뀌면 기준도 바뀌어야 합니다."
    ))
    sec.append((
        "한 줄 결론",
        "목적이 선명하면 선택은 빨라지고, 관리가 쉬우면 꾸준함이 만들어집니다. 그래서 작은 개선이 결국 가장 큰 결과를 만듭니다."
    ))

    # 광고 위치: 상단/중간
    mid_index = len(sec)//2

    cat_url=_category_url(DEFAULT_CATEGORY)

    parts = [ _css(), '<div class="daily-wrap">', _adsense_block(),
              f'<h2 class="daily-sub">{subtitle}</h2>',
              _mk_paragraph(summary), '<hr class="daily-hr">']

    # 섹션 렌더링
    for idx, (h, p) in enumerate(sec):
        parts.append(f"<h3>{html.escape(h)}</h3>")
        # 긴 문단을 두세 문장으로 나눠 가독성 확보
        for chunk in re_split_sentences(p):
            parts.append(_mk_paragraph(html.escape(chunk)))
        # 표는 가성비 섹션 뒤에 삽입
        if h.startswith("가성비"):
            parts.append(table)
        parts.append('<hr class="daily-hr">')
        if idx == mid_index:
            parts.append(_adsense_block())

    parts.append(f'<p style="margin:18px 0 0"><a href="{html.escape(cat_url)}">더 많은 글 보기</a></p>')
    parts.append("</div>")
    return "\n".join(parts).strip()

def re_split_sentences(text:str)->list[str]:
    # 아주 단순한 문장 분리: 마침표/물결/느낌표/물음표 기준
    chunks=[]
    buf=""
    for ch in text:
        buf+=ch
        if ch in "…?!.":
            chunks.append(buf.strip())
            buf=""
    if buf.strip():
        chunks.append(buf.strip())
    # 너무 짧으면 붙이기
    out=[]
    acc=""
    for c in chunks:
        if len(acc)+len(c) < 60:
            acc = (acc+" "+c).strip()
        else:
            if acc: out.append(acc); acc=""
            out.append(c)
    if acc: out.append(acc)
    return out

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
