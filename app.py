import os
import re
import time
import tempfile
from datetime import datetime
from typing import List
import requests
import streamlit as st
from google import genai
from google.genai import types
from pydantic import BaseModel
from notion_client import Client
from dotenv import load_dotenv, set_key

env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path)

DEPARTMENTS = ["NW망", "DC운영", "DC구축", "재무/투자심의", "전략기획", "법무", "구매", "영업", "경영진"]
MEETING_STATUS_OPTIONS = [
    {"name": "논의 완료", "color": "green"},
    {"name": "후속 협의 필요", "color": "yellow"},
    {"name": "진행 중", "color": "blue"},
    {"name": "검토 대기", "color": "gray"},
]


def extract_date_from_filename(filename: str) -> str:
    if not filename:
        return datetime.now().strftime("%y%m%d")
    # 1. 스마트폰 통화 녹음 형식: _YYMMDD_ (예: 통화 박진만 매니저_261006_161815)
    call_match = re.search(r"_(\d{6})_", filename)
    if call_match:
        return call_match.group(1)
    # 2. 8자리 날짜: 20261006, 2024-10-06 등
    match8 = re.search(r"20(\d{2})[-._]?(0[1-9]|1[0-2])[-._]?(0[1-9]|[12]\d|3[01])", filename)
    if match8:
        return f"{match8.group(1)}{match8.group(2)}{match8.group(3)}"
    # 3. 6자리 날짜: YYMMDD
    match6 = re.search(r"(?:^|[^\d])(\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))(?:[^\d]|$)", filename)
    if match6:
        return match6.group(1)
    # 4. 없으면 오늘 날짜 (YYMMDD)
    return datetime.now().strftime("%y%m%d")


def clean_md(text: str) -> str:
    """마크다운 기호(#, **, ---)가 그대로 노출되지 않도록 순수 텍스트로 정제합니다."""
    if not text:
        return ""
    t = str(text)
    t = re.sub(r"\*\*(.*?)\*\*", r"\1", t)
    t = re.sub(r"__(.*?)__", r"\1", t)
    t = re.sub(r"^\s*#{1,6}\s*", "", t)
    t = re.sub(r"^\s*[-*•]\s+", "", t)
    t = re.sub(r"^\s*---\s*$", "", t)
    return t.strip()


class KeyValueBullet(BaseModel):
    keyword: str
    description: str


class DiscussionTopic(BaseModel):
    topic_title: str
    bullets: List[KeyValueBullet]


class ActionDecisionItem(BaseModel):
    assignee: str
    task: str
    deadline: str
    status: str


class TodoItem(BaseModel):
    task_name: str
    category: str
    related_departments: List[str]
    assignee: str
    deadline: str


class MeetingResult(BaseModel):
    meeting_title: str
    meeting_datetime: str
    participants: str
    purpose: str
    meeting_status: str
    summary_opportunity: str
    summary_schedule: str
    summary_next_action: str
    customer_status_summary: List[KeyValueBullet]
    timeline_summary: List[KeyValueBullet]
    discussion_topics: List[DiscussionTopic]
    action_items_table: List[ActionDecisionItem]
    other_internal_trends: List[KeyValueBullet]
    todos: List[TodoItem]


def _notion_headers(notion_token: str) -> dict:
    return {
        "Authorization": f"Bearer {notion_token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def fetch_notion_pages(notion_token):
    try:
        notion = Client(auth=notion_token)
        response = notion.search(filter={"value": "page", "property": "object"})
        pages = {}
        for res in response.get("results", []):
            if res.get("archived") or res.get("in_trash"):
                continue
            parent_type = res.get("parent", {}).get("type")
            if parent_type == "database_id":
                continue
            props = res.get("properties", {})
            title = "제목 없는 페이지"
            for k, v in props.items():
                if v.get("id") == "title":
                    t_arr = v.get("title", [])
                    title = "".join([t.get("plain_text", "") for t in t_arr]) or "제목 없는 페이지"
            pages[f"{title} ({res['id']})"] = res["id"]
        return pages
    except Exception:
        return {}


def create_minutes_db_in_page(notion_token: str, min_page_id: str) -> str:
    url = "https://api.notion.com/v1/databases"
    headers = _notion_headers(notion_token)
    min_data = {
        "parent": {"type": "page_id", "page_id": min_page_id},
        "is_inline": True,
        "title": [{"type": "text", "text": {"content": "📝 회의록"}}],
        "properties": {
            "회의 제목": {"title": {}},
            "일시": {"rich_text": {}},
            "대화자": {"rich_text": {}},
            "목적": {"rich_text": {}},
            "상태": {"select": {"options": MEETING_STATUS_OPTIONS}},
            "등록일": {"created_time": {}},
        },
    }
    res_min = requests.post(url, headers=headers, json=min_data)
    min_db = res_min.json()
    return min_db["id"]


def ensure_minutes_db(notion_token: str, db_id: str) -> str:
    """회의록 DB가 존재하는지 확인하고, (일시/대화자/목적/상태) 속성을 자동 보장합니다.
    절대로 To-do 페이지 안에 회의록 DB를 중복 생성하지 않습니다."""
    headers = _notion_headers(notion_token)
    todo_db_id = os.getenv("NOTION_TODOS_DB_ID", "")
    todo_parent_page_id = None
    if todo_db_id:
        r_td = requests.get(f"https://api.notion.com/v1/databases/{todo_db_id}", headers=headers)
        if r_td.status_code == 200:
            todo_parent_page_id = r_td.json().get("parent", {}).get("page_id")

    target_db_id = db_id

    # 1. 기존 db_id가 유효한지 확인 (단, To-do 페이지 하위에 잘못 생성된 DB는 제외)
    is_valid = False
    if target_db_id:
        r = requests.get(f"https://api.notion.com/v1/databases/{target_db_id}", headers=headers)
        if r.status_code == 200:
            db_info = r.json()
            parent_pid = db_info.get("parent", {}).get("page_id")
            if not db_info.get("archived") and not db_info.get("in_trash") and parent_pid != todo_parent_page_id:
                is_valid = True

    # 2. 유효하지 않다면 노션에서 '회의록' 페이지 안의 회의록 DB 탐색
    if not is_valid:
        s_res = requests.post("https://api.notion.com/v1/search", headers=headers, json={}).json()
        results = s_res.get("results", [])

        # (a) To-do 페이지가 아닌 곳에 있는 살아있는 '회의록' DB 찾기
        for item in results:
            if item.get("object") == "database" and not item.get("archived") and not item.get("in_trash"):
                parent_pid = item.get("parent", {}).get("page_id")
                if todo_parent_page_id and parent_pid == todo_parent_page_id:
                    continue
                t_str = "".join([t.get("plain_text", "") for t in item.get("title", [])])
                if "회의록" in t_str:
                    target_db_id = item["id"]
                    is_valid = True
                    break

        # (b) 없으면 '회의록' 이름의 일반 페이지 안에만 생성 (절대 To-do 페이지에는 생성하지 않음)
        if not is_valid:
            candidate_page_id = None
            for item in results:
                if item.get("object") == "page" and not item.get("archived") and not item.get("in_trash"):
                    if item.get("parent", {}).get("type") == "database_id":
                        continue
                    if todo_parent_page_id and item["id"] == todo_parent_page_id:
                        continue
                    p_title = ""
                    for _, v in item.get("properties", {}).items():
                        if v.get("id") == "title":
                            p_title = "".join([t.get("plain_text", "") for t in v.get("title", [])])
                    if "회의록" in p_title:
                        candidate_page_id = item["id"]
                        break

            if candidate_page_id:
                target_db_id = create_minutes_db_in_page(notion_token, candidate_page_id)
                is_valid = True
            else:
                raise RuntimeError(
                    "노션의 '회의록' 페이지(또는 회의록 DB)에 봇(VoiceAgent) 연결이 되어 있지 않아 접근할 수 없습니다. "
                    "노션 왼쪽 메뉴의 [회의록] 페이지로 들어가셔서 우측 상단 점 3개(...) ➔ [연결(Connections)] ➔ [VoiceAgent]를 추가해 주세요!"
                )

    # 3. 속성(일시 / 대화자 / 목적 / 상태 / 등록일) 스키마 패치 보장
    if is_valid and target_db_id:
        patch_payload = {
            "is_inline": True,
            "properties": {
                "일시": {"rich_text": {}},
                "대화자": {"rich_text": {}},
                "목적": {"rich_text": {}},
                "상태": {"select": {"options": MEETING_STATUS_OPTIONS}},
                "등록일": {"created_time": {}},
            },
        }
        requests.patch(f"https://api.notion.com/v1/databases/{target_db_id}", headers=headers, json=patch_payload)

    return target_db_id


def create_dbs_in_page(notion_token, min_page_id, todo_page_id):
    url = "https://api.notion.com/v1/databases"
    headers = _notion_headers(notion_token)

    min_db_id = create_minutes_db_in_page(notion_token, min_page_id)

    # 이미 살아있는 To-Do DB가 있으면 새로 만들지 않고 재사용하여 누적 유지
    existing_todo_id = os.getenv("NOTION_TODOS_DB_ID", "")
    if existing_todo_id:
        r_todo = requests.get(f"https://api.notion.com/v1/databases/{existing_todo_id}", headers=headers)
        if r_todo.status_code == 200 and not r_todo.json().get("archived") and not r_todo.json().get("in_trash"):
            return min_db_id, existing_todo_id

    todo_data = {
        "parent": {"type": "page_id", "page_id": todo_page_id},
        "is_inline": True,
        "title": [{"type": "text", "text": {"content": "✅ To-Do 통합 현황"}}],
        "properties": {
            "작업명": {"title": {}},
            "분류": {
                "select": {
                    "options": [
                        {"name": "CSP / 고객사", "color": "blue"},
                        {"name": "내부 유관부서", "color": "green"},
                        {"name": "투자 / 경영진 보고", "color": "red"},
                        {"name": "팀 내 실무", "color": "default"},
                    ]
                }
            },
            "유관부서": {"multi_select": {"options": [{"name": d} for d in DEPARTMENTS]}},
            "담당자": {"rich_text": {}},
            "기한": {"rich_text": {}},
            "완료": {"checkbox": {}},
            "진행률": {
                "select": {
                    "options": [
                        {"name": "0%", "color": "gray"},
                        {"name": "25%", "color": "red"},
                        {"name": "50%", "color": "orange"},
                        {"name": "75%", "color": "yellow"},
                        {"name": "100%", "color": "green"},
                    ]
                }
            },
            "비고": {"rich_text": {}},
            "관련 회의록": {"url": {}},
            "등록일": {"created_time": {}},
        },
    }
    res_todo = requests.post(url, headers=headers, json=todo_data)
    todo_db = res_todo.json()

    return min_db_id, todo_db["id"]


def process_audio(api_key, audio_file_path, mime_type, original_filename=""):
    client = genai.Client(api_key=api_key)
    file_date = extract_date_from_filename(original_filename)
    formatted_date = f"20{file_date[:2]}-{file_date[2:4]}-{file_date[4:6]}"

    uploaded_file = client.files.upload(file=audio_file_path, config={"mime_type": mime_type})

    prompt = f"""
    당신은 LG유플러스의 '글로벌 AI/Data Center 인프라 및 엔터프라이즈(AICC/콜센터 인프라 등) Business Development 수석 전문가'이자 전담 비서입니다.
    업로드된 회의/통화 녹음 파일을 분석하여 **읽는 사람이 30초 안에 핵심을 파악하고, 필요할 때만 상세 내용을 펼쳐볼 수 있는 노션 회의록 데이터**와 **To-Do 리스트**를 JSON 스키마에 맞춰 생성하세요.

    [업로드 원본 파일명] {original_filename}

    ================================================================
    [회의록 페이지 구조 및 작성 규칙 - 절대 준수]
    ================================================================
    * 절대 주의: 모든 문자열에 마크다운 기호(#, **, ---, * 등)를 직접 넣지 마세요! (코드에서 노션 네이티브 블록 및 볼드 속성으로 자동 변환합니다.)

    1. 제목 및 상단 속성 (Properties)
    - meeting_title: 반드시 "[YYMMDD] 회의 주제" 형식 (예: [{file_date}] 미쓰비시 콜센터 인프라 개편 및 AICC 제안 협의)
      * 날짜 6자리(YYMMDD)는 파일명('{original_filename}')에서 추출한 '{file_date}'를 최우선 반영하세요. 절대 '[날짜미상]'을 쓰지 마세요.
    - meeting_datetime: 회의 일시 (파일명 기준 기본값: "{formatted_date}", 통화 시간이나 언급된 시간이 있으면 함께 표기)
    - participants: 대화자 (파일명 및 음성에서 파악된 참석자 이름/직책/소속, 불확실하면 "(확인 필요)" 병기)
    - purpose: 회의 목적을 1줄로 간결하게 요약
    - meeting_status: "논의 완료", "후속 협의 필요", "진행 중", "검토 대기" 중 가장 적절한 1개 선택

    2. 상단 요약 콜아웃 (3줄 이내 "한 줄 결론")
    - summary_opportunity: 핵심 기회 (1줄 요약)
    - summary_schedule: 가장 중요한 일정 (1줄 요약)
    - summary_next_action: 가장 시급한 다음 액션 1개 (1줄 요약)

    3. 핵심 숫자·일정 (2열 컬럼 레이아웃용, 각 최대 4개 항목)
    - customer_status_summary (왼쪽 컬럼 - 고객/사업 현황 요약):
      * keyword: 항목 라벨 (예: "좌석 규모", "현행 인프라", "예산 규모")
      * description: 수치 및 핵심 현황을 1줄로 요약 (불확실한 수치는 "(확인 필요)" 표시)
    - timeline_summary (오른쪽 컬럼 - 타임라인):
      * keyword: 날짜 또는 시기 (예: "3월 결산", "4월 초", "10월 말" - 노션에서 자동으로 굵게 표시됨)
      * description: 해당 시점의 일정/이벤트 설명 (예: "예산 집행 및 구축 착수" - 일반 글씨로 표시됨)

    4. 주요 논의 내용 (discussion_topics)
    - 주제별로 2~4개의 토글 헤딩(H2) 주제로 구성하세요 (topic_title 예: "1. 콜센터 인프라 현황 및 개편 방향").
    - 각 주제(bullets) 안에서는 불릿을 **최대 4개**로 엄격히 제한하고, 한 불릿은 **한두 줄 이내**로 압축하세요.
    - 각 불릿은 keyword(앞에 굵게 표시될 핵심 키워드)와 description(콜론 뒤에 일반 글씨로 이어질 짧은 설명)으로 나누어 작성하세요.
    - 긴 서술형 문장은 짧게 끊어 명사형/간결체로 작성하세요.

    5. 의사결정 / 액션 아이템 표 (action_items_table)
    - 체크박스 표에 들어갈 항목들입니다 (담당자 / 할 일 / 기한 / 상태).
    - assignee: 담당자 이름 (불명확하면 "(확인 필요)")
    - task: 구체적인 할 일 내용 (기한 날짜는 deadline 필드에 적으면 코드에서 할 일 맨 앞에 자동 배치합니다)
    - deadline: 기한 (예: "10/20", "10월 말", 없으면 "미정")
    - status: 상태 (예: "진행 예정", "검토 중", "확인 필요")

    6. 기타 사내 동향 (other_internal_trends)
    - 회의 본론과 관계없는 사내 동향, 인사/조직 소식, 타 프로젝트 언급, 가벼운 근황 등은 본문에 절대 섞지 말고 반드시 여기로 이동하세요.
    - keyword(주제 키워드)와 description(간결한 설명)으로 작성하세요. 해당 내용이 전혀 없으면 빈 배열([])로 두세요.

    ================================================================
    [스타일 및 용어 규칙 - 절대 준수]
    ================================================================
    - 고유명사·회사명·솔루션명은 keyword가 아닌 일반 문장 안에서 볼드(**) 처리하지 말고, 첫 등장 시에만 괄호로 간단한 설명을 추가하세요.
    - 약어(EICN, AICC, CTI, IVR, PBX, DCI, ROADM, OLS, Dark Fiber, PNI, DBO 등)는 첫 등장 시 반드시 풀네임을 병기하세요. (예: AICC(AI Contact Center), PBX(사설교환기))
    - 불확실하거나 전사본(음성)에서 정확히 확인이 안 되는 수치·일정·내용은 추측하지 말고 반드시 "(확인 필요)"를 표시하세요.
    - 전체 분량이 A4 1~1.5장 이내로 들어오도록 군더더기 없이 핵심만 압축하세요.

    ================================================================
    [To-Do 통합 현황 DB 누적용 (todos)]
    ================================================================
    - category는 반드시 다음 4가지 중 하나로만 분류:
      1. "CSP / 고객사"
      2. "내부 유관부서"
      3. "투자 / 경영진 보고"
      4. "팀 내 실무"
    - related_departments: 협업/보고가 필요한 사내 부서 배열 (가급적 NW망, DC운영, DC구축, 재무/투자심의, 전략기획, 법무, 구매, 영업, 경영진 중 선택, 없으면 빈 배열)
    """

    max_retries = 10
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-3.8-flash",
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=MeetingResult,
                    temperature=0.2,
                ),
            )
            break
        except Exception as e:
            if "503" in str(e) and attempt < max_retries - 1:
                st.warning(
                    f"구글 서버가 현재 꽉 찼습니다. 포기하지 않고 {retry_delay}초 후 다시 서버 문을 두드립니다... (불굴의 시도 {attempt+1}/{max_retries})"
                )
                time.sleep(retry_delay)
                retry_delay += 5
            else:
                client.files.delete(name=uploaded_file.name)
                raise e

    client.files.delete(name=uploaded_file.name)
    parsed = response.parsed

    # 제목 날짜 검증 및 보정 (2중 안전장치)
    if parsed and parsed.meeting_title:
        clean_title = clean_md(parsed.meeting_title)
        if "[날짜미상]" in clean_title:
            parsed.meeting_title = clean_title.replace("[날짜미상]", f"[{file_date}]")
        elif not re.match(r"^\[\d{6}\]", clean_title):
            parsed.meeting_title = f"[{file_date}] {clean_title}"
        else:
            parsed.meeting_title = clean_title
    return parsed


def _kv_bullet_block(keyword: str, description: str, sep: str = ": ") -> dict:
    kw = clean_md(keyword)[:200]
    desc = clean_md(description)[:1500]
    rich_text = []
    if kw:
        rich_text.append({"type": "text", "text": {"content": kw}, "annotations": {"bold": True}})
        if desc:
            rich_text.append({"type": "text", "text": {"content": f"{sep}{desc}"}})
    else:
        rich_text.append({"type": "text", "text": {"content": desc or "(내용 없음)"}})
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": rich_text},
    }


def build_meeting_minutes_blocks(result: MeetingResult) -> list:
    """마크다운 기호 없이 노션 네이티브 블록(콜아웃, 2열 컬럼, 토글 H2, 체크박스 표, 하단 접힌 토글)으로 페이지를 구성합니다."""
    blocks = []

    # 1. 상단 요약 콜아웃 (💡, 회색 배경, 3줄 이내 한 줄 결론: 핵심 기회 / 핵심 일정 / 다음 액션)
    blocks.append(
        {
            "object": "block",
            "type": "callout",
            "callout": {
                "icon": {"type": "emoji", "emoji": "💡"},
                "color": "gray_background",
                "rich_text": [
                    {"type": "text", "text": {"content": "핵심 기회: "}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": f"{clean_md(result.summary_opportunity)}\n"}},
                    {"type": "text", "text": {"content": "핵심 일정: "}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": f"{clean_md(result.summary_schedule)}\n"}},
                    {"type": "text", "text": {"content": "다음 액션: "}, "annotations": {"bold": True}},
                    {"type": "text", "text": {"content": clean_md(result.summary_next_action)}},
                ],
            },
        }
    )

    # 2. 핵심 숫자·일정 (2열 컬럼 레이아웃: 왼쪽 고객 현황 요약 / 오른쪽 타임라인)
    left_children = [
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": "고객 현황 요약"}}]},
        }
    ]
    for item in (result.customer_status_summary or [])[:4]:
        left_children.append(_kv_bullet_block(item.keyword, item.description, sep=": "))
    if len(left_children) == 1:
        left_children.append(_kv_bullet_block("현황", "(확인 필요)", sep=": "))

    right_children = [
        {
            "object": "block",
            "type": "heading_3",
            "heading_3": {"rich_text": [{"type": "text", "text": {"content": "타임라인"}}]},
        }
    ]
    for item in (result.timeline_summary or [])[:4]:
        right_children.append(_kv_bullet_block(item.keyword, item.description, sep=" → "))
    if len(right_children) == 1:
        right_children.append(_kv_bullet_block("일정", "(확인 필요)", sep=" → "))

    blocks.append(
        {
            "object": "block",
            "type": "column_list",
            "column_list": {
                "children": [
                    {"object": "block", "type": "column", "column": {"children": left_children}},
                    {"object": "block", "type": "column", "column": {"children": right_children}},
                ]
            },
        }
    )

    # 구분선 #1 (한 페이지 최대 2개 규칙 준수)
    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # 3. 주요 논의 내용 (주제별 토글 헤딩 H2, 각 토글 내 불릿 최대 4개, "**키워드**: 설명" 형식)
    for topic in result.discussion_topics or []:
        topic_bullets = []
        for b in (topic.bullets or [])[:4]:
            topic_bullets.append(_kv_bullet_block(b.keyword, b.description, sep=": "))
        if not topic_bullets:
            topic_bullets.append(_kv_bullet_block("논의 내용", "(확인 필요)", sep=": "))

        blocks.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": clean_md(topic.topic_title)[:200]}}],
                    "is_toggleable": True,
                    "children": topic_bullets,
                },
            }
        )

    # 구분선 #2 (한 페이지 최대 2개 규칙 준수)
    blocks.append({"object": "block", "type": "divider", "divider": {}})

    # 4. 의사결정 / 액션 아이템 (체크박스 표: 담당자 / 할 일 / 기한 / 상태, 기한이 있는 항목은 날짜를 맨 앞에 표시)
    blocks.append(
        {
            "object": "block",
            "type": "heading_2",
            "heading_2": {"rich_text": [{"type": "text", "text": {"content": "의사결정 / 액션 아이템"}}]},
        }
    )

    table_rows = [
        {
            "object": "block",
            "type": "table_row",
            "table_row": {
                "cells": [
                    [{"type": "text", "text": {"content": "담당자"}, "annotations": {"bold": True}}],
                    [{"type": "text", "text": {"content": "할 일"}, "annotations": {"bold": True}}],
                    [{"type": "text", "text": {"content": "기한"}, "annotations": {"bold": True}}],
                    [{"type": "text", "text": {"content": "상태"}, "annotations": {"bold": True}}],
                ]
            },
        }
    ]

    no_deadline_tokens = {"", "미정", "없음", "-", "추후 협의", "확인 필요", "(확인 필요)"}
    for item in result.action_items_table or []:
        assignee_txt = clean_md(item.assignee) or "(확인 필요)"
        deadline_txt = clean_md(item.deadline) or "미정"
        status_txt = clean_md(item.status) or "진행 예정"
        raw_task = clean_md(item.task)
        # 혹시 할 일 앞에 이미 [기한]이 중복되어 있으면 제거 후 깔끔하게 재결합
        raw_task = re.sub(r"^\[.*?\]\s*", "", raw_task).strip()

        if deadline_txt not in no_deadline_tokens:
            task_cell = [
                {"type": "text", "text": {"content": "☐ "}},
                {"type": "text", "text": {"content": f"[{deadline_txt}] "}, "annotations": {"bold": True}},
                {"type": "text", "text": {"content": raw_task}},
            ]
        else:
            task_cell = [{"type": "text", "text": {"content": f"☐ {raw_task}"}}]

        table_rows.append(
            {
                "object": "block",
                "type": "table_row",
                "table_row": {
                    "cells": [
                        [{"type": "text", "text": {"content": assignee_txt}}],
                        task_cell,
                        [{"type": "text", "text": {"content": deadline_txt}}],
                        [{"type": "text", "text": {"content": status_txt}}],
                    ]
                },
            }
        )

    if len(table_rows) == 1:
        table_rows.append(
            {
                "object": "block",
                "type": "table_row",
                "table_row": {
                    "cells": [
                        [{"type": "text", "text": {"content": "-"}}],
                        [{"type": "text", "text": {"content": "☐ 도출된 액션 아이템 없음"}}],
                        [{"type": "text", "text": {"content": "-"}}],
                        [{"type": "text", "text": {"content": "-"}}],
                    ]
                },
            }
        )

    blocks.append(
        {
            "object": "block",
            "type": "table",
            "table": {
                "table_width": 4,
                "has_column_header": True,
                "has_row_header": False,
                "children": table_rows,
            },
        }
    )

    # 5. 기타 사내 동향 (본문과 구분되도록 하단에 접힌 토글로 분리)
    trend_children = []
    for item in result.other_internal_trends or []:
        trend_children.append(_kv_bullet_block(item.keyword, item.description, sep=": "))
    if not trend_children:
        trend_children.append(_kv_bullet_block("특이사항 없음", "회의 본론 외 별도 언급된 사내 동향 없음", sep=": "))

    blocks.append(
        {
            "object": "block",
            "type": "toggle",
            "toggle": {
                "rich_text": [
                    {"type": "text", "text": {"content": "기타 사내 동향"}, "annotations": {"bold": True}}
                ],
                "children": trend_children,
            },
        }
    )

    return blocks


def add_notion_meeting_minutes(notion_token: str, db_id: str, result: MeetingResult) -> str:
    active_db_id = ensure_minutes_db(notion_token, db_id)
    headers = _notion_headers(notion_token)
    blocks = build_meeting_minutes_blocks(result)

    valid_statuses = {opt["name"] for opt in MEETING_STATUS_OPTIONS}
    status_val = clean_md(result.meeting_status)
    if status_val not in valid_statuses:
        status_val = "논의 완료"

    payload = {
        "parent": {"database_id": active_db_id},
        "properties": {
            "title": {"title": [{"text": {"content": clean_md(result.meeting_title)}}]},
            "일시": {"rich_text": [{"text": {"content": clean_md(result.meeting_datetime)}}]},
            "대화자": {"rich_text": [{"text": {"content": clean_md(result.participants)}}]},
            "목적": {"rich_text": [{"text": {"content": clean_md(result.purpose)}}]},
            "상태": {"select": {"name": status_val}},
        },
        "children": blocks,
    }

    res = requests.post("https://api.notion.com/v1/pages", headers=headers, json=payload)
    if res.status_code != 200:
        # 만약 DB 속성 중 일부가 수동 변경되어 실패한 경우 기본 title 속성만으로 재시도
        fallback_payload = {
            "parent": {"database_id": active_db_id},
            "properties": {
                "title": {"title": [{"text": {"content": clean_md(result.meeting_title)}}]},
            },
            "children": blocks,
        }
        res = requests.post("https://api.notion.com/v1/pages", headers=headers, json=fallback_payload)
        res.raise_for_status()

    return res.json()["url"]


def add_notion_todo(notion_token, db_id, todo: TodoItem, meeting_url: str = ""):
    notion = Client(auth=notion_token)
    notion.pages.create(
        parent={"database_id": db_id},
        properties={
            "title": {"title": [{"text": {"content": clean_md(todo.task_name)}}]},
            "분류": {"select": {"name": todo.category}},
            "유관부서": {
                "multi_select": [
                    {"name": d.replace(",", "/")[:100]} for d in todo.related_departments if d.strip()
                ]
            },
            "담당자": {"rich_text": [{"text": {"content": clean_md(todo.assignee)}}]},
            "기한": {"rich_text": [{"text": {"content": clean_md(todo.deadline)}}]},
            "완료": {"checkbox": False},
            "진행률": {"select": {"name": "0%"}},
            "관련 회의록": {"url": meeting_url if meeting_url else None},
        },
    )


st.set_page_config(page_title="음성 회의록 & To-Do 자동화", layout="centered", initial_sidebar_state="auto")

# 스포티파이 스타일 커스텀 CSS 강제 주입 (초강력 버전)
st.markdown(
    """
<style>
    /* 전체 배경: 스포티파이 플레이리스트 상단 그라데이션 느낌 */
    .stApp {
        background: linear-gradient(to bottom, #15522b 0%, #121212 25%, #121212 100%) !important;
        background-attachment: fixed !important;
    }
    /* 사이드바 배경색 */
    [data-testid="stSidebar"] {
        background-color: #000000 !important;
        border-right: 1px solid #282828 !important;
    }
    /* 모든 텍스트 색상 */
    .stApp p, .stApp h1, .stApp h2, .stApp h3, .stApp label {
        color: #FFFFFF !important;
    }
    
    /* 타이틀 폰트 과감하게 */
    h1 {
        font-size: 3.2rem !important;
        font-weight: 900 !important;
        letter-spacing: -1.5px !important;
        margin-bottom: 0.2rem !important;
    }
    
    /* 둥근 버튼 (일반) */
    .stButton>button {
        border-radius: 500px !important;
        font-weight: bold !important;
        transition: all 0.2s ease-in-out !important;
        border: none !important;
    }
    
    /* 메인 강조 버튼 (스포티파이 재생 버튼 느낌으로 거대하게) */
    button[kind="primary"] {
        background-color: #1DB954 !important;
        color: black !important;
        font-size: 1.1rem !important;
        padding: 1rem 3rem !important;
        box-shadow: 0 8px 24px rgba(29, 185, 84, 0.4) !important;
        text-transform: uppercase !important;
        letter-spacing: 1px !important;
        margin-top: 15px !important;
    }
    button[kind="primary"]:hover {
        transform: scale(1.05) !important;
        background-color: #1ed760 !important; /* 호버 시 더 밝은 초록 */
    }
    
    /* 성공/경고 알림창 커스텀 (투박한 기본색 대신 다크 모드+포인트 컬러) */
    [data-testid="stAlert"] {
        background-color: #181818 !important;
        border: none !important;
        border-left: 5px solid #1DB954 !important;
        color: white !important;
        border-radius: 4px !important;
    }
    
    /* 파일 업로더 큼직하고 세련되게 */
    [data-testid="stFileUploadDropzone"] {
        border-radius: 12px !important;
        background-color: #181818 !important;
        border: 2px dashed #404040 !important;
        padding: 3rem !important;
    }
    [data-testid="stFileUploadDropzone"]:hover {
        border-color: #1DB954 !important;
        background-color: #282828 !important;
    }
</style>
""",
    unsafe_allow_html=True,
)

st.title("🎧 음성 회의록 & To-Do 생성기")
st.markdown(
    "<p style='color: #B3B3B3; font-size: 0.9rem;'>회의 음성 파일을 업로드하면 30초 핵심 파악용 노션 네이티브 회의록과 To-Do로 자동 정리해 드립니다.</p>",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("⚙️ 설정 (Settings)")

    saved_gemini_key = os.getenv("GEMINI_API_KEY", "")
    saved_notion_token = os.getenv("NOTION_API_TOKEN", "")
    saved_minutes_db_id = os.getenv("NOTION_MINUTES_DB_ID", "")
    saved_todos_db_id = os.getenv("NOTION_TODOS_DB_ID", "")

    with st.expander("🔑 API 키 설정", expanded=not (saved_gemini_key and saved_notion_token)):
        gemini_api_key = st.text_input("Gemini API Key", value=saved_gemini_key, type="password")
        notion_token = st.text_input("Notion API Token", value=saved_notion_token, type="password")

        if st.button("💾 모바일 자동 로그인을 위해 키 저장하기", type="secondary"):
            if not os.path.exists(env_path):
                open(env_path, "a").close()
            set_key(env_path, "GEMINI_API_KEY", gemini_api_key)
            set_key(env_path, "NOTION_API_TOKEN", notion_token)
            st.success("✅ 키가 저장되었습니다! 모바일로 접속해도 유지됩니다.")

    st.divider()

    if saved_minutes_db_id and saved_todos_db_id:
        st.success("✅ 회의록 및 To-Do DB가 설정되어 있습니다.")
        notion_minutes_db_id = saved_minutes_db_id
        notion_todos_db_id = saved_todos_db_id

        if st.button("🔄 연결된 DB 초기화 (새로 만들기)", type="secondary"):
            if "NOTION_MINUTES_DB_ID" in os.environ:
                del os.environ["NOTION_MINUTES_DB_ID"]
            if "NOTION_TODOS_DB_ID" in os.environ:
                del os.environ["NOTION_TODOS_DB_ID"]

            if os.path.exists(env_path):
                with open(env_path, "r") as f:
                    lines = f.readlines()
                with open(env_path, "w") as f:
                    for line in lines:
                        if "NOTION_MINUTES_DB_ID" not in line and "NOTION_TODOS_DB_ID" not in line:
                            f.write(line)
            st.rerun()
    else:
        st.warning("⚠️ 아직 연결된 데이터베이스가 없습니다.")
        st.info("데이터베이스를 자동으로 생성해 드릴까요? 봇이 초대된 '일반 페이지'를 선택하고 버튼을 누르세요.")

        notion_minutes_db_id = ""
        notion_todos_db_id = ""

        if notion_token:
            pages_dict = fetch_notion_pages(notion_token)
            if pages_dict:
                page_names = list(pages_dict.keys())
                min_page = st.selectbox("회의록 DB를 생성할 페이지 선택", options=page_names)
                todo_page = st.selectbox("To-Do DB를 생성할 페이지 선택", options=page_names)
                if st.button("✨ 선택한 페이지들에 DB 자동 생성하기", type="primary"):
                    try:
                        with st.spinner("DB를 생성 중입니다..."):
                            m_id, t_id = create_dbs_in_page(
                                notion_token, pages_dict[min_page], pages_dict[todo_page]
                            )
                            if not os.path.exists(env_path):
                                open(env_path, "a").close()
                            set_key(env_path, "NOTION_MINUTES_DB_ID", m_id)
                            set_key(env_path, "NOTION_TODOS_DB_ID", t_id)
                            st.success("🎉 DB 생성이 완료되었습니다! 화면을 새로고침(F5) 해주세요.")
                    except Exception as e:
                        st.error(f"DB 생성 중 오류가 발생했습니다: {e}")
            else:
                st.error(
                    "🚫 봇이 초대된 빈 페이지를 찾을 수 없습니다. 노션에서 새 페이지를 만들고 연결 추가(Connect to)를 해주세요!"
                )

uploaded_audio = st.file_uploader("회의 음성 파일을 업로드하세요", type=["mp3", "wav", "m4a", "flac", "ogg"])

if st.button("분석 및 노션 업데이트 시작", type="primary") and uploaded_audio:
    if not all([gemini_api_key, notion_token, notion_minutes_db_id, notion_todos_db_id]):
        st.error("좌측 설정 탭에서 API Key와 DB가 완벽히 설정되지 않았습니다.")
    else:
        with st.spinner("🎵 오디오 파일을 분석하고 있습니다... (시간이 소요될 수 있습니다)"):
            ext = uploaded_audio.name.split(".")[-1].lower()
            mime_types_map = {
                "mp3": "audio/mp3",
                "wav": "audio/wav",
                "m4a": "audio/mp4",
                "flac": "audio/flac",
                "ogg": "audio/ogg",
            }
            mime_type = mime_types_map.get(ext, "audio/mpeg")

            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp_file:
                tmp_file.write(uploaded_audio.read())
                tmp_path = tmp_file.name

            try:
                result = process_audio(gemini_api_key, tmp_path, mime_type, uploaded_audio.name)
                st.success("✅ 분석 완료! 노션에 업로드를 시작합니다.")

                st.subheader(f"📝 {result.meeting_title}")
                st.caption(
                    f"**일시**: {result.meeting_datetime} | **대화자**: {result.participants} | **목적**: {result.purpose} | **상태**: {result.meeting_status}"
                )
                st.info(
                    f"💡 **한 줄 결론 요약**\n"
                    f"- **핵심 기회**: {result.summary_opportunity}\n"
                    f"- **핵심 일정**: {result.summary_schedule}\n"
                    f"- **다음 액션**: {result.summary_next_action}"
                )

                col1, col2 = st.columns(2)
                with col1:
                    st.markdown("#### 고객 현황 요약")
                    for item in (result.customer_status_summary or [])[:4]:
                        st.markdown(f"- **{item.keyword}**: {item.description}")
                with col2:
                    st.markdown("#### 타임라인")
                    for item in (result.timeline_summary or [])[:4]:
                        st.markdown(f"- **{item.keyword}** → {item.description}")

                for topic in result.discussion_topics or []:
                    with st.expander(topic.topic_title, expanded=True):
                        for b in (topic.bullets or [])[:4]:
                            st.markdown(f"- **{b.keyword}**: {b.description}")

                with st.spinner("Notion에 네이티브 블록 회의록을 생성하는 중..."):
                    meeting_url = add_notion_meeting_minutes(notion_token, notion_minutes_db_id, result)

                with st.spinner("Notion에 To-Do 리스트를 누적 등록하는 중..."):
                    for todo in result.todos:
                        add_notion_todo(notion_token, notion_todos_db_id, todo, meeting_url)

                st.success(f"🎉 모든 작업이 완료되었습니다! [회의록 보러가기]({meeting_url})")

            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")
            finally:
                os.remove(tmp_path)
