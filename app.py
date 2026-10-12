import os
import re
import tempfile
from datetime import datetime
import streamlit as st
from google import genai
from google.genai import types
from pydantic import BaseModel
from typing import List
from notion_client import Client
from dotenv import load_dotenv, set_key

env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path)

DEPARTMENTS = ["NW망", "DC운영", "DC구축", "재무/투자심의", "전략기획", "법무", "구매", "영업", "경영진"]

def extract_date_from_filename(filename: str) -> str:
    if not filename:
        return datetime.now().strftime("%y%m%d")
    # 1. 스마트폰 통화 녹음 형식: _YYMMDD_ (예: 통화 박진만 매니저_261006_161815)
    call_match = re.search(r'_(\d{6})_', filename)
    if call_match:
        return call_match.group(1)
    # 2. 8자리 날짜: 20261006, 2024-10-06 등
    match8 = re.search(r'20(\d{2})[-._]?(0[1-9]|1[0-2])[-._]?(0[1-9]|[12]\d|3[01])', filename)
    if match8:
        return f"{match8.group(1)}{match8.group(2)}{match8.group(3)}"
    # 3. 6자리 날짜: YYMMDD
    match6 = re.search(r'(?:^|[^\d])(\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01]))(?:[^\d]|$)', filename)
    if match6:
        return match6.group(1)
    # 4. 없으면 오늘 날짜 (YYMMDD)
    return datetime.now().strftime("%y%m%d")

class TodoItem(BaseModel):
    task_name: str
    category: str
    related_departments: List[str]
    assignee: str
    deadline: str

class MeetingResult(BaseModel):
    meeting_title: str
    meeting_minutes_markdown: str
    todos: List[TodoItem]

def fetch_notion_pages(notion_token):
    try:
        notion = Client(auth=notion_token)
        response = notion.search(filter={"value": "page", "property": "object"})
        pages = {}
        for res in response.get("results", []):
            if res.get("parent", {}).get("type") == "workspace":
                pass
            props = res.get("properties", {})
            title = "제목 없는 페이지"
            for k, v in props.items():
                if v.get("id") == "title":
                    t_arr = v.get("title", [])
                    title = "".join([t.get("plain_text", "") for t in t_arr])
            pages[f"{title} ({res['id']})"] = res['id']
        return pages
    except Exception:
        return {}

def create_dbs_in_page(notion_token, min_page_id, todo_page_id):
    import requests
    
    url = "https://api.notion.com/v1/databases"
    headers = {
        "Authorization": f"Bearer {notion_token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json"
    }
    
    min_data = {
        "parent": {"type": "page_id", "page_id": min_page_id},
        "is_inline": True,
        "title": [{"type": "text", "text": {"content": "📝 회의록"}}],
        "properties": {
            "회의 제목": {"title": {}},
            "등록일": {"created_time": {}}
        }
    }
    res_min = requests.post(url, headers=headers, json=min_data)
    min_db = res_min.json()
    
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
                        {"name": "팀 내 실무", "color": "default"}
                    ]
                }
            },
            "유관부서": {"multi_select": {"options": [{"name": d} for d in DEPARTMENTS]}},
            "담당자": {"rich_text": {}},
            "기한": {"rich_text": {}},
            "완료": {"checkbox": {}},
            "진행률": {"select": {"options": [
                {"name": "0%", "color": "gray"},
                {"name": "25%", "color": "red"},
                {"name": "50%", "color": "orange"},
                {"name": "75%", "color": "yellow"},
                {"name": "100%", "color": "green"}
            ]}},
            "비고": {"rich_text": {}},
            "관련 회의록": {"url": {}},
            "등록일": {"created_time": {}}
        }
    }
    res_todo = requests.post(url, headers=headers, json=todo_data)
    todo_db = res_todo.json()
    
    return min_db["id"], todo_db["id"]

def process_audio(api_key, audio_file_path, mime_type, original_filename=""):
    client = genai.Client(api_key=api_key)
    file_date = extract_date_from_filename(original_filename)
    
    uploaded_file = client.files.upload(file=audio_file_path, config={'mime_type': mime_type})
    
    prompt = f"""
    당신은 LG유플러스의 '글로벌 AI/Data Center 인프라 Business Development 수석 전문가'이자 전담 비서입니다.
    주요 업무 맥락은 Microsoft, AWS, Google, Meta 등 글로벌 CSP 대상 사업개발 및 이를 지원하는 사내 유관 부서(NW망, DC운영/구축, 재무/투자심의 등)와의 전략적 협의입니다.

    제공된 음성 파일을 바탕으로 회의록과 To-Do 리스트를 도출하세요.
    [업로드 원본 파일명] {original_filename}

    [회의록 제목 (meeting_title) 작성 규칙]
    - 반드시 "[YYMMDD] 핵심 주제" 형식으로 작성하세요. (예: [{file_date}] Google 망 연동 RFS 일정 협의)
    - [날짜 6자리(YYMMDD) 결정 기준 - 매우 중요]
      1순위: 파일명('{original_filename}')에서 추출된 날짜 '{file_date}'를 최우선으로 반드시 반영하세요.
      2순위: 음성 대화 내용에 다른 날짜가 구체적으로 언급된 경우 그 날짜를 YYMMDD 6자리로 변환
      3순위: 위 두 곳 모두 날짜 정보가 전혀 없다면 오늘 날짜 '{datetime.now().strftime("%y%m%d")}' 사용
      * 주의: 절대로 '[날짜미상]'이라고 적지 마시고 반드시 6자리 숫자(YYMMDD)로 시작하세요!

    [To-Do 리스트 분류 카테고리]
    도출된 각 To-Do는 반드시 다음 4가지 카테고리 중 하나로만 분류하세요:
    1. "CSP / 고객사" (글로벌 기업 요구사항 대응, 대외 협상, 제안서 발송 등)
    2. "내부 유관부서" (NW망, DC운영/구축 등 기술 부서와의 물리적/기술적 협업)
    3. "투자 / 경영진 보고" (CAPEX 검토, 투자심의, 임원 보고, 재무적 의사결정)
    4. "팀 내 실무" (일반 팔로업, 내부 리서치, 단순 자료 취합)

    [유관부서 (related_departments) 작성 규칙]
    - 해당 To-Do 수행 시 협업/보고가 필요한 사내 부서를 배열로 적습니다.
    - 가급적 다음 중에서 선택하세요: NW망, DC운영, DC구축, 재무/투자심의, 전략기획, 법무, 구매, 영업, 경영진
    - 목록에 없는 부서가 명확히 언급되면 그 이름을 짧게 그대로 적고, 관련 부서가 없으면 빈 배열로 둡니다.

    [핵심 작성 원칙]
    - To-Do 가시성 극대화: 누가 무엇을 언제까지 해야 하는지 선명하게 도출합니다.
    - 용어 자동 보정: DCI, ROADM, OLS, Dark Fiber, Subsea Cable, PNI, 상면/수전, 액침냉각, DBO 등 통신/DC 용어를 문맥에 맞게 보정합니다.

    출력 포맷은 반드시 요청된 JSON 스키마를 따르며, meeting_minutes_markdown에는 마크다운 양식에 맞게 상세한 회의록을 작성해주세요.
    """
    import time
    
    max_retries = 10
    retry_delay = 5
    
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=[uploaded_file, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=MeetingResult,
                    temperature=0.2
                )
            )
            break
        except Exception as e:
            if "503" in str(e) and attempt < max_retries - 1:
                st.warning(f"구글 서버가 현재 꽉 찼습니다. 포기하지 않고 {retry_delay}초 후 다시 서버 문을 두드립니다... (불굴의 시도 {attempt+1}/{max_retries})")
                time.sleep(retry_delay)
                retry_delay += 5  # 5초씩 대기 시간을 늘려가며 재시도
            else:
                client.files.delete(name=uploaded_file.name)
                raise e
                
    client.files.delete(name=uploaded_file.name)
    parsed = response.parsed
    # 제목 날짜 검증 및 보정 (2중 안전장치: 혹시라도 [날짜미상]이거나 [YYMMDD] 누락 시 자동 교정)
    if parsed and parsed.meeting_title:
        clean_title = parsed.meeting_title.strip()
        if "[날짜미상]" in clean_title:
            parsed.meeting_title = clean_title.replace("[날짜미상]", f"[{file_date}]")
        elif not re.match(r"^\[\d{6}\]", clean_title):
            parsed.meeting_title = f"[{file_date}] {clean_title}"
    return parsed

def add_notion_meeting_minutes(notion_token, db_id, title, markdown_content):
    notion = Client(auth=notion_token)
    blocks = []
    chunks = [markdown_content[i:i+1900] for i in range(0, len(markdown_content), 1900)]
    for chunk in chunks:
        blocks.append({
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": chunk}}]
            }
        })
    res = notion.pages.create(
        parent={"database_id": db_id},
        properties={
            "title": {"title": [{"text": {"content": title}}]}
        },
        children=blocks
    )
    return res["url"]

def add_notion_todo(notion_token, db_id, todo: TodoItem, meeting_url: str = ""):
    notion = Client(auth=notion_token)
    notion.pages.create(
        parent={"database_id": db_id},
        properties={
            "title": {"title": [{"text": {"content": todo.task_name}}]},
            "분류": {"select": {"name": todo.category}},
            "유관부서": {"multi_select": [{"name": d.replace(",", "/")[:100]} for d in todo.related_departments if d.strip()]},
            "담당자": {"rich_text": [{"text": {"content": todo.assignee}}]},
            "기한": {"rich_text": [{"text": {"content": todo.deadline}}]},
            "완료": {"checkbox": False},
            "진행률": {"select": {"name": "0%"}},
            "관련 회의록": {"url": meeting_url if meeting_url else None}
        }
    )

st.set_page_config(page_title="음성 회의록 & To-Do 자동화", layout="centered", initial_sidebar_state="auto")

# 스포티파이 스타일 커스텀 CSS 강제 주입 (초강력 버전)
st.markdown("""
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
""", unsafe_allow_html=True)

st.title("🎧 음성 회의록 & To-Do 생성기")
st.markdown("<p style='color: #B3B3B3; font-size: 0.9rem;'>회의 음성 파일을 업로드하면 스포티파이처럼 매끄럽게 분석해 드립니다.</p>", unsafe_allow_html=True)

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
                open(env_path, 'a').close()
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
                            m_id, t_id = create_dbs_in_page(notion_token, pages_dict[min_page], pages_dict[todo_page])
                            if not os.path.exists(env_path):
                                open(env_path, 'a').close()
                            set_key(env_path, "NOTION_MINUTES_DB_ID", m_id)
                            set_key(env_path, "NOTION_TODOS_DB_ID", t_id)
                            st.success("🎉 DB 생성이 완료되었습니다! 화면을 새로고침(F5) 해주세요.")
                    except Exception as e:
                        st.error(f"DB 생성 중 오류가 발생했습니다: {e}")
            else:
                st.error("🚫 봇이 초대된 빈 페이지를 찾을 수 없습니다. 노션에서 새 페이지를 만들고 연결 추가(Connect to)를 해주세요!")
    
uploaded_audio = st.file_uploader("회의 음성 파일을 업로드하세요", type=["mp3", "wav", "m4a", "flac", "ogg"])

if st.button("분석 및 노션 업데이트 시작", type="primary") and uploaded_audio:
    if not all([gemini_api_key, notion_token, notion_minutes_db_id, notion_todos_db_id]):
        st.error("좌측 설정 탭에서 API Key와 DB가 완벽히 설정되지 않았습니다.")
    else:
        with st.spinner("🎵 오디오 파일을 분석하고 있습니다... (시간이 소요될 수 있습니다)"):
            ext = uploaded_audio.name.split('.')[-1].lower()
            mime_types_map = {
                'mp3': 'audio/mp3',
                'wav': 'audio/wav',
                'm4a': 'audio/mp4',
                'flac': 'audio/flac',
                'ogg': 'audio/ogg'
            }
            mime_type = mime_types_map.get(ext, 'audio/mpeg')
            
            with tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}") as tmp_file:
                tmp_file.write(uploaded_audio.read())
                tmp_path = tmp_file.name

            try:
                result = process_audio(gemini_api_key, tmp_path, mime_type, uploaded_audio.name)
                st.success("✅ 분석 완료! 노션에 업로드를 시작합니다.")
                
                st.subheader("📝 생성된 회의록 요약 (미리보기)")
                st.markdown(result.meeting_minutes_markdown)
                
                st.subheader("✅ 추출된 To-Do 리스트")
                st.json([todo.model_dump() for todo in result.todos])
                
                with st.spinner("Notion에 회의록을 생성하는 중..."):
                    meeting_url = add_notion_meeting_minutes(notion_token, notion_minutes_db_id, result.meeting_title, result.meeting_minutes_markdown)
                
                with st.spinner("Notion에 To-Do 리스트를 등록하는 중..."):
                    for todo in result.todos:
                        add_notion_todo(notion_token, notion_todos_db_id, todo, meeting_url)
                        
                st.success(f"🎉 모든 작업이 완료되었습니다! [회의록 보러가기]({meeting_url})")

            except Exception as e:
                st.error(f"오류가 발생했습니다: {e}")
            finally:
                os.remove(tmp_path)
