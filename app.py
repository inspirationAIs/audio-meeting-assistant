import os
import re
import json
import glob
import hmac
import subprocess
import time
import tempfile
from datetime import datetime
from typing import List
import requests
import streamlit as st
import openai
from openai import OpenAI
import imageio_ffmpeg
from pydantic import BaseModel
from notion_client import Client
from dotenv import load_dotenv, set_key

env_path = os.path.join(os.path.dirname(__file__), ".env")
load_dotenv(env_path)

DEPARTMENTS = ["NW망", "DC운영", "DC구축", "재무/투자심의", "전략기획", "법무", "구매", "영업", "경영진"]

# AI 설정: 기본은 Groq 무료 플랜(OpenAI 호환 API). 환경변수로 교체 가능.
AI_BASE_URL = os.getenv("AI_BASE_URL", "https://api.groq.com/openai/v1")
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
LLM_MODEL = os.getenv("LLM_MODEL", "openai/gpt-oss-120b")
TPM_BUDGET = int(os.getenv("TPM_BUDGET", "6500"))  # 무료 8,000 TPM 보다 여유 있게
DIRECT_MAX_CHARS = 3000      # 이보다 짧으면 요약 단계 없이 바로 구조화
PIECE_MAX_CHARS = 4500       # 구간 요약 입력 최대 글자 수
NOTES_TOTAL_CHARS = 2400     # 구간 요약 노트 전체 합계(최종 호출 토큰 제한 대응)
TRANSCRIBE_HINT = (
    "통신/데이터센터 사업개발 대화입니다. 용어 예시: DCI, ROADM, OLS, Dark Fiber, Subsea Cable, PNI, "
    "상면, 수전, 액침냉각, DBO, AICC, EICN, PBX, CTI, IVR, CSP, CAPEX, RFS, 파주, 용인"
)

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


class TokenLimiter:
    """무료 플랜의 분당 토큰(TPM) 제한에 걸리지 않도록 호출 간격을 조절합니다."""

    def __init__(self, budget: int, window: float = 60.0):
        self.budget = budget
        self.window = window
        self.events = []

    def wait_for(self, est_tokens: int, progress=None):
        while True:
            now = time.time()
            self.events = [(t, n) for t, n in self.events if now - t < self.window]
            used = sum(n for _, n in self.events)
            if used + est_tokens <= self.budget or not self.events:
                return
            wait = max(1.0, self.window - (now - self.events[0][0]) + 0.5)
            if progress:
                progress(f"⏳ 무료 사용량(분당 토큰) 보호를 위해 {int(wait)}초 대기 중입니다...")
            time.sleep(min(wait, 15))

    def record(self, tokens: int):
        self.events.append((time.time(), tokens))


def est_tokens(text: str) -> int:
    # 한글은 토큰이 많이 소모되므로 보수적으로 추정
    return int(len(text) / 1.1) + 50


def split_text(text: str, max_chars: int) -> List[str]:
    pieces, buf = [], ""
    for sentence in re.split(r"(?<=[.!?。\n])\s*", text):
        if not sentence:
            continue
        while len(sentence) > max_chars:  # 마침표 없이 매우 긴 구간 방어
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if len(buf) + len(sentence) + 1 > max_chars and buf:
            pieces.append(buf)
            buf = ""
        buf = f"{buf} {sentence}".strip()
    if buf:
        pieces.append(buf)
    return pieces


def split_audio_to_chunks(src_path: str, workdir: str, chunk_seconds: int = 600) -> List[str]:
    """업로드된 음성을 16kHz 모노 저용량 mp3 조각(기본 10분)으로 변환/분할합니다.
    전사 API의 파일 크기 제한을 피하고, 긴 통화도 안정적으로 처리하기 위함입니다."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    out_pattern = os.path.join(workdir, "chunk_%03d.mp3")
    cmd = [
        ffmpeg_exe, "-y", "-i", src_path,
        "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
        "-f", "segment", "-segment_time", str(chunk_seconds), "-reset_timestamps", "1",
        out_pattern,
    ]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", errors="ignore")[-500:]
        raise RuntimeError(f"오디오 변환(ffmpeg)에 실패했습니다: {tail}")
    chunks = sorted(glob.glob(os.path.join(workdir, "chunk_*.mp3")))
    if not chunks:
        raise RuntimeError("오디오에서 음성 구간을 찾지 못했습니다. 파일이 손상되지 않았는지 확인해 주세요.")
    return chunks


def transcribe_audio(client: OpenAI, audio_file_path: str, progress=None) -> str:
    texts = []
    with tempfile.TemporaryDirectory() as workdir:
        chunks = split_audio_to_chunks(audio_file_path, workdir)
        for i, chunk_path in enumerate(chunks, 1):
            if progress:
                progress(f"🎙️ 음성을 텍스트로 변환 중... ({i}/{len(chunks)})")
            with open(chunk_path, "rb") as f:
                res = client.audio.transcriptions.create(
                    model=TRANSCRIBE_MODEL,
                    file=f,
                    language="ko",
                    prompt=TRANSCRIBE_HINT,
                )
            texts.append((res.text or "").strip())
    return "\n".join(t for t in texts if t)


def _llm_kwargs() -> dict:
    # gpt-oss 계열은 추론 토큰을 많이 쓰므로 최소로 낮춰 무료 한도를 아낍니다.
    return {"extra_body": {"reasoning_effort": "low"}} if "gpt-oss" in LLM_MODEL else {}


def summarize_piece(client, limiter, piece: str, idx: int, total: int, max_chars: int, progress=None) -> str:
    system = (
        f"회의/통화 녹취 일부({idx}/{total})입니다. 핵심만 한국어 불릿 노트로 정리하세요. "
        "포함: 사실·수치·일정·결정사항·액션(담당자/기한)·고객사/사업 현황·사내 동향(본론과 무관한 내용은 [사내동향] 표시). "
        "음성인식 오인식은 문맥에 맞게 보정하고, 불확실하면 (확인 필요)를 붙이세요. "
        f"마크다운 기호 없이 {max_chars}자 이내로 쓰세요."
    )
    limiter.wait_for(est_tokens(system + piece) + 1000, progress)
    res = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": piece}],
        max_completion_tokens=1000,
        **_llm_kwargs(),
    )
    limiter.record(res.usage.total_tokens if getattr(res, "usage", None) else est_tokens(system + piece) + 600)
    note = (res.choices[0].message.content or "").strip()
    return note[: int(max_chars * 1.4)]


def build_system_prompt(original_filename: str, file_date: str, formatted_date: str) -> str:
    return f"""
당신은 LG유플러스 글로벌 AI/DC 인프라·엔터프라이즈(AICC/콜센터 인프라) 사업개발 수석 전문가이자 비서입니다.
아래 회의/통화 자료로 노션 회의록 데이터와 To-Do를 JSON 스키마대로 작성하세요. 읽는 사람이 30초 안에 핵심을 파악하고 필요할 때만 상세를 펼쳐볼 수 있어야 합니다.
자료는 음성인식 결과라 오인식이 있을 수 있으니 통신/DC 용어(DCI, ROADM, OLS, Dark Fiber, PNI, 상면/수전, 액침냉각, DBO 등)는 문맥에 맞게 보정하세요.
[원본 파일명] {original_filename}
[공통] 모든 문자열에 마크다운 기호(#, **, ---, *)를 넣지 마세요.
[제목/속성] meeting_title은 "[YYMMDD] 회의 주제" 형식이며 날짜는 파일명 기준 {file_date}를 반영하고 '[날짜미상]'은 금지. meeting_datetime 기본값 "{formatted_date}". participants=대화자(불확실하면 "(확인 필요)"). purpose=1줄. meeting_status는 "논의 완료","후속 협의 필요","진행 중","검토 대기" 중 1개.
[요약 콜아웃] summary_opportunity(핵심 기회), summary_schedule(가장 중요한 일정), summary_next_action(다음 액션 1개)는 각 1줄.
[2열 요약] customer_status_summary: keyword=라벨, description=수치·현황 1줄. timeline_summary: keyword=시기(예 "3월 결산"), description=이벤트. 각 최대 4개.
[주요 논의] discussion_topics 2~4개. topic_title 예 "1. 현행 인프라 현황". bullets는 최대 4개이며 각 keyword(굵게 표시될 핵심어)+description(한두 줄, 명사형 간결체).
[액션 표] action_items_table: assignee, task, deadline(예 "10/20", 없으면 "미정"), status(예 "진행 예정").
[기타 사내 동향] other_internal_trends: 회의 본론과 무관한 사내 소식·인사·타 프로젝트 언급은 모두 여기로. 없으면 빈 배열.
[스타일] 고유명사·회사명·솔루션명은 첫 등장 시에만 괄호로 짧게 설명. 약어(EICN, AICC, CTI, IVR, PBX 등)는 첫 등장 시 풀네임 병기. 자료에서 확인되지 않는 내용은 추측하지 말고 "(확인 필요)". 전체 분량은 A4 1~1.5장 이내로 압축.
[todos] category는 "CSP / 고객사","내부 유관부서","투자 / 경영진 보고","팀 내 실무" 중 1개. related_departments는 NW망, DC운영, DC구축, 재무/투자심의, 전략기획, 법무, 구매, 영업, 경영진 중 선택(없으면 빈 배열).
"""


def structure_meeting(client, limiter, material: str, system_prompt: str, progress=None) -> MeetingResult:
    user_prompt = f"[회의/통화 자료]\n{material}"
    limiter.wait_for(est_tokens(system_prompt + user_prompt) + 800 + 3000, progress)
    parse_fn = getattr(client.chat.completions, "parse", None) or client.beta.chat.completions.parse
    try:
        completion = parse_fn(
            model=LLM_MODEL,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            response_format=MeetingResult,
            max_completion_tokens=3000,
            **_llm_kwargs(),
        )
        limiter.record(completion.usage.total_tokens if getattr(completion, "usage", None) else 7000)
        parsed = completion.choices[0].message.parsed
        if parsed is not None:
            return parsed
    except openai.BadRequestError:
        pass  # strict json_schema 미지원/생성 실패 → 아래 json_object 방식으로 재시도

    schema_txt = json.dumps(MeetingResult.model_json_schema(), ensure_ascii=False)
    completion = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": system_prompt + "\n반드시 아래 JSON 스키마를 만족하는 JSON만 출력하세요.\n" + schema_txt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        max_completion_tokens=3000,
        **_llm_kwargs(),
    )
    limiter.record(completion.usage.total_tokens if getattr(completion, "usage", None) else 7000)
    return MeetingResult.model_validate_json(completion.choices[0].message.content or "{}")


def process_audio(api_key, audio_file_path, original_filename="", progress=None):
    client = OpenAI(api_key=api_key, base_url=AI_BASE_URL, max_retries=5, timeout=600)
    limiter = TokenLimiter(TPM_BUDGET)
    file_date = extract_date_from_filename(original_filename)
    formatted_date = f"20{file_date[:2]}-{file_date[2:4]}-{file_date[4:6]}"

    try:
        transcript = transcribe_audio(client, audio_file_path, progress)
        if len(transcript.strip()) < 20:
            raise RuntimeError("음성에서 전사된 내용이 거의 없습니다. 녹음 파일을 확인해 주세요.")

        # 짧은 통화는 바로 구조화, 긴 통화는 구간별 요약 후 합쳐서 구조화 (무료 플랜 토큰 제한 대응)
        if len(transcript) <= DIRECT_MAX_CHARS:
            material = transcript
        else:
            pieces = split_text(transcript, PIECE_MAX_CHARS)
            max_chars = max(300, min(900, NOTES_TOTAL_CHARS // len(pieces)))
            notes = []
            for i, piece in enumerate(pieces, 1):
                if progress:
                    progress(f"🧠 내용을 구간별로 정리 중... ({i}/{len(pieces)})")
                notes.append(f"[구간 {i}/{len(pieces)}]\n" + summarize_piece(client, limiter, piece, i, len(pieces), max_chars, progress))
            material = "\n\n".join(notes)

        if progress:
            progress("📝 회의록과 To-Do를 작성 중...")
        system_prompt = build_system_prompt(original_filename, file_date, formatted_date)
        parsed = structure_meeting(client, limiter, material, system_prompt, progress)
    except openai.RateLimitError as e:
        raise RuntimeError(
            "AI 무료 사용 한도(분당/일일)를 초과했습니다. 잠시 뒤에 다시 시도하거나, 일일 한도라면 내일 다시 시도해 주세요. "
            f"(상세: {str(e)[:300]})"
        )
    except openai.APIStatusError as e:
        if getattr(e, "status_code", None) == 413:
            raise RuntimeError("녹음이 너무 길어 무료 한도의 요청 크기를 넘었습니다. 파일을 나눠서 올려 주세요.")
        raise

    # 제목 날짜 검증 및 보정 (2중 안전장치)
    if parsed.meeting_title:
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

APP_PASSWORD = os.getenv("APP_PASSWORD", "")
if APP_PASSWORD and not st.session_state.get("authed"):
    st.title("🔒 접속 비밀번호")
    _pw = st.text_input("비밀번호를 입력하세요", type="password")
    if _pw:
        if hmac.compare_digest(_pw, APP_PASSWORD):
            st.session_state["authed"] = True
            st.rerun()
        else:
            st.error("비밀번호가 틀렸습니다.")
    st.stop()

st.title("🎧 음성 회의록 & To-Do 생성기")
st.markdown(
    "<p style='color: #B3B3B3; font-size: 0.9rem;'>회의 음성 파일을 업로드하면 30초 핵심 파악용 노션 네이티브 회의록과 To-Do로 자동 정리해 드립니다.</p>",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("⚙️ 설정 (Settings)")

    saved_ai_key = os.getenv("GROQ_API_KEY", "")
    saved_notion_token = os.getenv("NOTION_API_TOKEN", "")
    saved_minutes_db_id = os.getenv("NOTION_MINUTES_DB_ID", "")
    saved_todos_db_id = os.getenv("NOTION_TODOS_DB_ID", "")

    with st.expander("🔑 API 키 설정", expanded=not (saved_ai_key and saved_notion_token)):
        ai_api_key = st.text_input("Groq API Key", value=saved_ai_key, type="password")
        notion_token = st.text_input("Notion API Token", value=saved_notion_token, type="password")

        if st.button("💾 모바일 자동 로그인을 위해 키 저장하기", type="secondary"):
            if not os.path.exists(env_path):
                open(env_path, "a").close()
            set_key(env_path, "GROQ_API_KEY", ai_api_key)
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
    if not all([ai_api_key, notion_token, notion_minutes_db_id, notion_todos_db_id]):
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
                progress_box = st.empty()
                result = process_audio(ai_api_key, tmp_path, uploaded_audio.name, lambda m: progress_box.info(m))
                progress_box.empty()
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
