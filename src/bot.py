import os
import sys
import json
import asyncio
import random
import re
import math
import discord
import aiohttp
from discord import app_commands
import unicodedata
import contextvars
from pathlib import Path

from collections import defaultdict, deque
from rp_policy import FEATURES, policy_prompt, dialogue_reason, relationship_reason, completed_transfer
from life_engine import LifeEngine
from world_actions import WorldActions, validate_extensions
from feature_dependencies import validate_features
from world_calendar import WorldClock, validate_calendar, calendar_display, seasonal_weather, season_profile, place_open, weekly_entry, world_context, visible_events, scheduled_destination
import functools
import inspect
from dotenv import load_dotenv
from model_client import ModelClient, GENERATION_PRIORITY
from operations import diagnose

import sqlite3
from datetime import datetime, timezone, timedelta, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ------------------------------------------------------------
# 실행 위치
# ------------------------------------------------------------
# PyInstaller로 만든 EXE에서도 config/, prompts/, .env, DB, logs가
# EXE가 있는 폴더를 기준으로 동작하도록 한다.
if getattr(sys, "frozen", False):
    APP_ROOT = Path(sys.executable).resolve().parent
else:
    APP_ROOT = Path(__file__).resolve().parent.parent

os.chdir(APP_ROOT)

# Hold an OS lock so the desktop updater cannot replace a running bot.
from updater import BotLock
_application_lock = BotLock(APP_ROOT)
if not _application_lock.acquire():
    raise SystemExit("RPBot이 이미 실행 중이거나 업데이트 중입니다.")
if getattr(sys, "frozen", False) and not os.getenv('RPBOT_MANAGED_DESKTOP'):
    import subprocess
    _manager = APP_ROOT / 'RPBot_Setup.exe'
    if _manager.exists():
        subprocess.Popen([str(_manager), '--monitor'], cwd=APP_ROOT)

if getattr(sys, "frozen", False):
    import threading
    from updater import startup_notice
    _desktop_preferences = APP_ROOT / "desktop_settings.json"
    try:
        _check_updates = json.loads(_desktop_preferences.read_text(encoding="utf-8")).get("check_updates_on_start", True) if _desktop_preferences.exists() else True
    except Exception:
        _check_updates = True
    if _check_updates:
        threading.Thread(target=startup_notice, daemon=True).start()


# Railway Volume 등에서 SQLite와 로그를 배포 파일과 분리하여 보관한다.
# 환경변수가 없으면 Windows/로컬에서 기존처럼 APP_ROOT에 저장한다.
_data_path = os.getenv("BOT_DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
DATA_DIR = Path(_data_path).resolve() if _data_path else APP_ROOT
DATA_DIR.mkdir(parents=True, exist_ok=True)
from desktop_monitor import Monitor, redact
from dotenv import dotenv_values
_monitor = Monitor(DATA_DIR, [v for k, v in {**dotenv_values(APP_ROOT / '.env'), **os.environ}.items() if 'TOKEN' in k or 'KEY' in k])

# CMD 로그 머리말
# 캐릭터별 로그는 [캐릭터명], 공통 시스템 로그는 [General]로 표시
# 콘솔 출력과 동시에 logs/bot_YYYY-MM-DD.log 파일에도 저장한다.
LOG_DIR = str(DATA_DIR / "logs")

# 현재 비동기 작업이 어느 대화 영역에서 발생했는지 기록한다.
# ContextVar를 사용하므로 여러 서버/DM 메시지가 동시에 처리되어도 서로 섞이지 않는다.
CURRENT_LOG_SCOPE = contextvars.ContextVar("CURRENT_LOG_SCOPE", default="world")
CURRENT_LOG_GUILD_ID = contextvars.ContextVar("CURRENT_LOG_GUILD_ID", default=None)
CURRENT_LOG_GUILD_NAME = contextvars.ContextVar("CURRENT_LOG_GUILD_NAME", default=None)

def set_log_context(scope="world", guild_id=None, guild_name=None):
    CURRENT_LOG_SCOPE.set(scope or "world")
    CURRENT_LOG_GUILD_ID.set(str(guild_id) if guild_id is not None else None)
    CURRENT_LOG_GUILD_NAME.set(str(guild_name) if guild_name else None)

def log_message(category, *parts):
    scope = CURRENT_LOG_SCOPE.get() or "world"
    guild_id = CURRENT_LOG_GUILD_ID.get()
    guild_name = CURRENT_LOG_GUILD_NAME.get()

    if scope == "rp":
        scope_label = f"RP:{guild_name or guild_id or 'unknown'}"
    elif scope == "dm":
        scope_label = "DM"
    else:
        scope_label = "WORLD"

    prefix = f"[{category}][{scope_label}]"
    parts = tuple(redact(part, _monitor.secrets) for part in parts)
    print(prefix, *parts)
    _monitor.event('오류' if any('오류' in part or '실패' in part for part in parts) else '기록',
                   str(category), ' '.join(parts), guild_id or scope)

    try:
        now = datetime.now(
            globals().get("BOT_TIMEZONE", timezone(timedelta(hours=9)))
        )
        date_text = now.strftime("%Y-%m-%d")

        if scope == "rp":
            rp_dir = os.path.join(LOG_DIR, "rp")
            os.makedirs(rp_dir, exist_ok=True)
            safe_guild = re.sub(r"[^0-9A-Za-z가-힣._-]+", "_", str(guild_id or "unknown"))
            log_path = os.path.join(
                rp_dir,
                f"{safe_guild}_{date_text}.log"
            )
        elif scope == "dm":
            dm_dir = os.path.join(LOG_DIR, "dm")
            os.makedirs(dm_dir, exist_ok=True)
            log_path = os.path.join(
                dm_dir,
                f"dm_{date_text}.log"
            )
        else:
            world_dir = os.path.join(LOG_DIR, "world")
            os.makedirs(world_dir, exist_ok=True)
            log_path = os.path.join(
                world_dir,
                f"bot_{date_text}.log"
            )

        message = " ".join(
            str(part)
            for part in parts
        )

        with open(
            log_path,
            "a",
            encoding="utf-8"
        ) as log_file:
            log_file.write(
                f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] "
                f"{prefix} {message}\n"
            )

    except OSError:
        # 로그 파일 쓰기 실패가 봇 동작 자체를 멈추게 하지는 않는다.
        pass

# Public release source: v0.1.0
# Windows EXE 배포 대응 - EXE 기준 config/prompts/.env/DB/logs 경로 처리
# bot_v64: settings.json timezone으로 RP 생활 시간대 설정 지원
# bot_v63: 예시 prompt/config 한국어화 + 설명용 _주석 필드 지원
# bot_v62: settings.json 기능 ON/OFF + /서버초기화 채널 자동 생성
# bot_v61: 초기 설정 마법사 지원 번들
# bot_v60: 외부 JSON 템플릿으로 캐릭터/장소/세계/초기 관계를 정의하는 범용 RP 엔진 모드 추가
# bot_v59: Gateway 시작 재시도에서 client.close()/clear() 제거하여 'Session is closed' 방지 + 다중 봇 로그인 분산
# bot_v58: 봇 반응 후보 제한 로그를 메시지별 한 줄 요약으로 통합하여 CMD 로그 도배 완화
# bot_v57: 실제 반복 근거가 없을 때 '또 같은 말'류 허위 반복 언급 차단 + 금지 예시 프롬프트 제거
# bot_v56: 기존 last_seen과 무관하게 최초 1회 최근 48시간 강제 백필하여 이전 버전에서 놓친 사람 멘션/답글도 복구
# bot_v55: 최초 실행도 최근 48시간 백필 + 사람의 직접 멘션/답글만 복구 + 봇→봇/자율발언 제외
# bot_v54: 종료 중 받은 서버 멘션/답글을 다음 실행 때 최대 48시간 범위에서 복구 처리
# bot_v53: Pocket 자율발언 책 소재 편향 완화 + 자율발언 주제 다양성 추적
# bot_v52: 개인 RP가 본서버 위치/생활/지식/기억/후처리에 영향을 주지 않도록 완전 분리
# bot_v51: 개인 RP 서버 자율발언 + 캐릭터/최근 사용자에게 먼저 말걸기 + RP 채널 관리
# bot_v50: 개인 RP 서버/DM/본서버 로그 파일 완전 분리
# bot_v23: v22 기능 유지 + 장소 계층/출입 + 이동 유예 + 운영 명령 + 도시사건 확장
# bot_v48: v47 기능 유지 + 캐릭터별 DM 1:1 대화 + DM/서버 대화 기록·장기기억 분리
# 중복실행 방지용 PID 프린트
log_message(
    "General",
    '현재 bot.py PID:',
    os.getpid(),
)

# .env 로딩
load_dotenv(APP_ROOT / ".env")

# 데이터베이스 경로
DB_PATH = str(DATA_DIR / "discord_memory.db")

# 봇 토큰과 API 키
OPENAI_API_KEY = os.getenv(
    "OPENAI_API_KEY"
)

openai_client = ModelClient(
    api_key=OPENAI_API_KEY
)

# 상태 대시보드 전용 Discord 봇
STATUS_BOT_TOKEN = os.getenv("DISCORD_STATUS_TOKEN")
STATUS_CHANNEL_NAME = os.getenv("DISCORD_STATUS_CHANNEL", "캐릭터-상태")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

# 개인 RP 서버 설정
# .env 예시: DISCORD_PERSONAL_RP_GUILD_IDS=123456789012345678,234567890123456789
PERSONAL_RP_GUILD_IDS = {
    value.strip()
    for value in os.getenv("DISCORD_PERSONAL_RP_GUILD_IDS", "").split(",")
    if value.strip()
}

def is_main_world_guild_id(guild_id):
    return bool(DISCORD_GUILD_ID) and str(guild_id) == str(DISCORD_GUILD_ID)

def is_personal_rp_guild_id(guild_id):
    return str(guild_id) in PERSONAL_RP_GUILD_IDS


# 상태봇 관리자 슬래시 명령 전용 채널.
# 0/미설정이면 채널 제한 없이 관리자 권한만 검사한다.
try:
    COMMAND_CHANNEL_ID = int(os.getenv("DISCORD_COMMAND_CHANNEL_ID", "0"))
except (TypeError, ValueError):
    COMMAND_CHANNEL_ID = 0

# 상태 대시보드 표시 순서.
# CHARACTERS 정의가 끝난 뒤 표시 이름 기준 알파벳순으로 자동 생성한다.
STATUS_CHARACTER_ORDER = []

# 생활 시간대 기본값.
# 범용 모드에서는 config/settings.json의 "timezone"으로 덮어쓸 수 있다.
BOT_TIMEZONE_NAME = "Asia/Seoul"
try:
    BOT_TIMEZONE = ZoneInfo(BOT_TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    # Windows 환경에 tzdata 패키지가 없어도 기본 서울 시간은 동작하도록 보장한다.
    BOT_TIMEZONE = timezone(timedelta(hours=9))

# 기존 함수/코드 호환을 위해 KST 이름은 유지하지만 실제로는 선택된 생활 시간대를 가리킨다.
KST = BOT_TIMEZONE

PROGRAM_STARTED_AT = datetime.now(timezone.utc)

# 시작 시 밀린 멘션을 on_message 경로로 재처리할 때만 True.
# 오래된 메시지 때문에 현재 월드 위치/수면/약속 등이 바뀌지 않도록 사용한다.
OFFLINE_RECOVERY_ACTIVE = contextvars.ContextVar(
    "OFFLINE_RECOVERY_ACTIVE",
    default=False,
)

OFFLINE_MENTION_RECOVERY_HOURS = 48
FIRST_RUN_BACKFILL_HOURS = 48

OFFLINE_MENTION_SCAN_LIMIT_PER_CHANNEL = 500


def now_kst():
    """현재 설정된 RP 생활 시간대의 현재 시각을 반환한다.

    함수명은 이전 버전 호환 때문에 now_kst()를 유지한다.
    """
    if globals().get("_world_clock") and globals().get("GENERIC_CONFIG_ACTIVE"):
        return _world_clock.now(CUSTOM_SETTINGS)
    return datetime.now(KST)


conversation_history = defaultdict(
    lambda: deque(maxlen=10)
)

# ===================================
# 구동용 일반 전역 변수
# -----------------------------------
bot_user_characters = {}
bot_character_ids = {}
bot_chain_count = defaultdict(int)
bot_chain_updated_at = {}

MAX_BOT_CHAIN = 8
DYNAMIC_EVENT_MIN_TURNS = 5
DYNAMIC_EVENT_MAX_TURNS = 12
dynamic_event_channels = {}

# 캐릭터 수가 늘어도 한 메시지에 너무 많은 봇이 GPT 판정을 호출하지 않도록 제한한다.
MAX_BOT_REACTORS_PER_MESSAGE = 2
bot_reaction_candidates = {}
bot_reaction_summary_logged = set()

# 새 자율발언 시작에만 적용되는 쿨다운.
# 사용자 멘션 답변이나 이미 이어지고 있는 캐릭터끼리의 대화에는 적용하지 않는다.
AUTONOMOUS_CHARACTER_COOLDOWN_MIN = 90
AUTONOMOUS_CHARACTER_COOLDOWN_MAX = 150
AUTONOMOUS_PLACE_COOLDOWN_MIN = 8
AUTONOMOUS_PLACE_COOLDOWN_MAX = 15

autonomous_character_cooldown_until = {}

autonomous_place_cooldown_until = {}

# 개인 RP 서버 자율발언 런타임 상태
# 설정 자체는 DB에 저장하고, 다음 발언 가능 시각만 메모리에서 관리한다.
rp_autonomous_cooldown_until = {}

# 캐릭터가 자율발언에서 같은 소재만 반복하지 않도록 최근 발언을 따로 기억한다.
# world와 각 개인 RP 서버는 서로 다른 기록을 사용한다.
recent_autonomous_messages = defaultdict(lambda: deque(maxlen=6))

def _autonomous_history_key(character, scope="world", guild_id=None):
    if scope == "rp":
        return ("rp", str(guild_id), character)
    return ("world", character)

def remember_autonomous_message(character, message, scope="world", guild_id=None):
    if not message:
        return
    key = _autonomous_history_key(character, scope=scope, guild_id=guild_id)
    recent_autonomous_messages[key].append(str(message).strip())

def get_recent_autonomous_context(character, scope="world", guild_id=None):
    key = _autonomous_history_key(character, scope=scope, guild_id=guild_id)
    items = list(recent_autonomous_messages.get(key, []))
    if not items:
        return "(최근 자율발언 없음)"
    return "\n".join(f"- {item}" for item in items[-6:])

def get_character_autonomous_variety_rule(character):
    # 내장 Deadlock 모드에서만 Pocket 전용 보정을 적용한다.
    # 범용 모드에서는 특정 캐릭터 이름에 의존하지 않는다.
    if (not GENERIC_CONFIG_ACTIVE) and character == "pocket":
        return """
[Pocket 자율발언 다양성 규칙]
- 책, 독서, 페이지, 책장, 읽던 내용은 Pocket의 여러 일상 소재 중 하나일 뿐이다.
- 최근 자율발언에서 책/독서 소재가 한 번이라도 나왔다면 이번에는 가능하면 다른 소재를 고른다.
- 주변 사람에 대한 가벼운 관찰, 음식이나 음료, 피곤함과 휴식, 날씨와 시간대,
  옷차림이나 소지품, 이동과 외출, 사생활을 지키려는 태도, 사소한 불편이나 농담,
  현재 주변 분위기와 대화거리처럼 평범한 일상 소재도 적극적으로 사용한다.
- 책을 들고 있거나 방에 책이 있다는 이유만으로 자동으로 책 이야기를 꺼내지 않는다.
- 캐릭터의 비밀이나 큰 목표를 매번 꺼내지 말고, 평범한 일상 화제를 충분히 섞는다.
"""
    return """
[자율발언 다양성 규칙]
- 최근 자율발언과 같은 핵심 소재를 연속해서 반복하지 않는다.
- 현재 활동이나 소지품 하나를 캐릭터의 유일한 화제로 고정하지 않는다.
- 일상적인 관찰, 기분, 사람, 음식, 휴식, 날씨, 주변 분위기 등 다른 화제도 자연스럽게 섞는다.
"""

RP_AUTONOMOUS_CHECK_MIN_MINUTES = 30
RP_AUTONOMOUS_CHECK_MAX_MINUTES = 90
RP_AUTONOMOUS_USER_MENTION_CHANCE = 0.18
RP_AUTONOMOUS_CHARACTER_START_CHANCE = 0.55


ROLEPLAY_OUTPUT_RULES = """
출력 형식 규칙:
- 필요하다면 대사와 함께 짧은 행동지문을 넣는다.
- 행동지문은 반드시 (행동) 형식으로 작성한다.
- 행동지문은 보통 1문장으로 작성한다.
- 필요한 경우에만 최대 2문장까지 사용할 수 있다.
- 2문장 행동지문은 드물게 사용하고, 대부분은 1문장으로 유지한다.
- 행동지문 다음 줄에 실제 대사를 쓴다.
- 매번 반드시 행동지문을 넣을 필요는 없다.
- 단순한 답변이나 짧은 반응이라면 대사만 써도 된다.
- 행동은 현재 장소, 현재 활동, 바로 앞 상황과 자연스럽게 이어져야 한다.
- 상대방의 행동이나 생각을 대신 확정하지 않는다.
- 과도한 소설체 묘사나 긴 상황 설명은 피한다.
- 별표(*)를 이용한 행동지문은 사용하지 않는다.
- 대사의 앞뒤에 큰따옴표(")나 작은따옴표(')를 붙이지 않는다.
"""

# ------------------------------------------------------------
# 모든 캐릭터가 공유하는 공통 역할극 규칙
# 캐릭터별 prompt 파일에서는 앞으로 이 내용들을 중복해서 적지 않아도 된다.
# ------------------------------------------------------------
COMMON_ROLEPLAY_RULES = """
[공통 세계/역할극 규칙]
- 배경은 초자연 현상이 일상에 섞여 있는 1940년대 대체역사 뉴욕이다.
- 너는 Discord의 봇이나 프로그램이 아니라 이 세계에 실제로 존재하는 인물로 행동한다.
- Discord, 채팅창, 메시지 알림, 스마트폰, 화면, 키보드, 서버 같은 현대 인터페이스를 세계 내부 행동으로 묘사하지 않는다.
- 현재 장소, 활동, 수면/외출 상태, 피로, 배고픔, 기분, 도시 사건, 장소 사건, 관계 DB, 장기 기억, 직접 들은 지식 등 런타임 정보가 캐릭터 프롬프트의 일반 설정보다 우선한다.
- 런타임과 오래된 프롬프트 설명이 충돌하면 현재 런타임 상태를 따른다.
- 다른 장소에서 벌어진 일이나 자신이 직접 듣지 못한 대화를 자동으로 알고 있다고 가정하지 않는다.
- 다른 캐릭터만 알고 있는 비밀이나 사적인 정보를 전지적으로 알지 않는다.
- 사용자가 "누가 이렇게 말했다"고 전달한 내용은 검증되지 않은 주장으로 취급한다.
- 최근 대화 기록이나 직접 들은 지식에 실제 발언이 있을 때만 그 캐릭터가 그렇게 말했다고 확정한다.
- 존재하지 않는 공식 설정, 과거 사건, 가족관계, 비밀, 약속, 전투, 사고를 임의로 만들어내지 않는다.
- 공식 설정에 없는 것을 추측할 때는 사실처럼 단정하지 않는다.
- 사용자의 부탁이나 제안은 명령이 아니다. 캐릭터 성격에 따라 수락, 거절, 질문, 무시가 가능하다.
- 상대방의 행동, 생각, 감정, 결정을 대신 확정하거나 조종하지 않는다.
- 같은 장소에 있을 때만 자연스러운 물리적 상호작용을 한다. 다른 장소에 있는 상대에게 손을 잡거나 물건을 건네는 식의 행동을 하지 않는다.
- 개인실은 사적인 공간이다. 직접 초대나 허가가 없으면 들어가지 않는다.
- 현재 장소에 없는 구체적 시설이나 사물을 필요 없이 만들어내지 않는다.
- 개인 목표는 삶의 한 부분일 뿐이다. 매 대화와 행동을 장기 목표, 복수, 수사, 연구, 사냥, 비밀 추적으로 연결하지 않는다.
- 평범한 식사, 휴식, 산책, 잡담, 취미, 사소한 불평과 관찰도 자연스럽게 한다.

[공통 관계 규칙]
- 캐릭터별 관계 설명은 출발점이지 영구 고정된 결론이 아니다.
- 실제 대화와 사건을 통해 신뢰, 친밀감, 경계, 불편함이 변할 수 있다.
- 관계 설명이 있다고 해서 매 대화에서 그 관계를 직접 언급하거나 과장하지 않는다.
- 호감이 있다고 자동으로 연애 관계로 확정하지 않는다.
- 적대나 경계가 있다고 모든 만남을 싸움이나 살해 위협으로 만들지 않는다.
- 실제 확인된 행동과 사건을 관계 변화의 근거로 사용한다.

[공통 말투 안정 규칙]
- 캐릭터 프롬프트에 지정된 기본 말투와 종결형을 우선하고 한 대화 안에서 임의로 바꾸지 않는다.
- 상대가 존댓말이나 반말을 사용하더라도 단순히 따라 말투 단계를 바꾸지 않는다.
- 존댓말/반말 변화가 필요한 특별한 관계 설정이나 사건이 없다면 기본 말투를 유지한다.
- 감정 변화는 존댓말↔반말 전환보다 어휘, 문장 길이, 속도, 직접성으로 표현한다.
- 다른 캐릭터의 말투, 비유, 말버릇을 바로 흉내 내지 않는다.
- 최근 서버 전체에서 반복된 숫자 비유나 문장 구조를 캐치프레이즈처럼 재사용하지 않는다.
- 실제 대화에 근거가 없다면 "열 번", "백 번", "몇 번이고" 같은 구체적인 반복 횟수를 만들어내지 않는다.

[공통 복장/외형 규칙]
- 캐릭터 프롬프트에 명시된 기본 복장과 외형을 우선한다.
- 런타임에 명시적인 복장 변경이 없다면 임의로 다른 옷으로 갈아입었다고 가정하지 않는다.
- 확인되지 않은 의상, 색상, 장신구, 신발, 재질, 무기를 임의로 추가하지 않는다.
- 설정에 없는 셔츠 소매, 코트 깃, 장갑, 넥타이 등을 전제로 한 행동을 만들지 않는다.
- 복장이 확실하지 않을 때는 시선, 자세, 손동작, 주변 사물과의 상호작용 등 복장과 무관한 행동지문을 사용한다.

[공통 출력 규칙]
- 대사는 현재 상황에 직접 반응하고 짧고 자연스럽게 유지한다.
- 같은 내용을 불필요하게 반복하지 않는다.
- 행동지문은 필요할 때만 사용하며 너무 길게 쓰지 않는다.
- 설명문, 메타 해설, 시스템 설명을 출력하지 않는다.
"""



# 새 캐릭터를 추가할 때는 이 딕셔너리에 항목을 하나 추가하면 된다.
# token_env / prompt_file / private_room / sleep / inventory / habits 정도만 설정하면
# 공통 루프(상태, 이동, 자율발언, 수면, 이벤트)가 자동으로 따라간다.
CHARACTERS = {
    "apollo": {
        "name": "Apollo",
        "token_env": "DISCORD_APOLLO_TOKEN",
        "prompt_file": "prompts/apollo_prompt.txt",
        "private_room": "아폴로 방",
        "sleep_start_range": ((0, 0), (2, 0)),
        "wake_range": ((7, 0), (9, 0)),
        "inventory": ["검 관리 도구", "훈련 수첩"],
        "habits": ["펜싱 훈련을 꾸준히 함", "자세와 체면을 흐트러뜨리지 않으려 함"],
    },
    "doorman": {
        "name": "The Doorman",
        "token_env": "DISCORD_DOORMAN_TOKEN",
        "prompt_file": "prompts/doorman_prompt.txt",
        "private_room": "도어맨 방",
        "sleep_start_range": ((2, 0), (4, 0)),
        "wake_range": ((9, 0), (11, 0)),
        "inventory": ["호텔 열쇠", "작은 호출 벨"],
        "habits": ["사람들의 요구와 행동을 관찰함", "정중한 서비스 태도를 유지함"],
    },
    "graves": {
        "name": "Graves",
        "token_env": "DISCORD_GRAVES_TOKEN",
        "prompt_file": "prompts/graves_prompt.txt",
        "private_room": "그레이브즈 방",
        "sleep_start_range": ((0, 30), (2, 30)),
        "wake_range": ((7, 30), (9, 30)),
        "inventory": ["주술 노트", "작은 유리병"],
        "habits": ["묘지와 죽은 자에 관한 메모를 정리함", "논쟁할 때 쉽게 물러서지 않음"],
    },
    "mina": {
        "name": "Mina",
        "token_env": "DISCORD_MINA_TOKEN",
        "prompt_file": "prompts/mina_prompt.txt",
        "private_room": "미나 방",
        "sleep_start_range": ((0, 30), (2, 0)),
        "wake_range": ((7, 30), (9, 30)),
        "inventory": ["손거울", "손수건", "작은 장신구"],
        "habits": ["옷과 장신구를 정돈함", "주변의 분위기와 외관을 자주 살핌"],
    },
    "paige": {
        "name": "Paige",
        "token_env": "DISCORD_PAIGE_TOKEN",
        "prompt_file": "prompts/paige_prompt.txt",
        "private_room": "페이지 방",
        "sleep_start_range": ((0, 30), (2, 30)),
        "wake_range": ((8, 0), (10, 0)),
        "inventory": [],
        "habits": [],
    },
    "pocket": {
        "name": "Pocket",
        "token_env": "DISCORD_POCKET_TOKEN",
        "prompt_file": "prompts/pocket_prompt.txt",
        "private_room": "포켓 방",
        "sleep_start_range": ((1, 0), (3, 30)),
        "wake_range": ((8, 0), (10, 30)),
        "inventory": ["책", "작은 가방"],
        "habits": ["조용한 곳에서 혼자 생각함", "옥상이나 창가에서 시간을 보내는 편"],
    },
    "rem": {
        "name": "Rem",
        "token_env": "DISCORD_REM_TOKEN",
        "prompt_file": "prompts/rem_prompt.txt",
        "private_room": "렘 방",
        "sleep_start_range": ((21, 30), (23, 30)),
        "wake_range": ((7, 0), (9, 0)),
        "inventory": ["좋아하는 베개"],
        "habits": ["쉽게 졸고 낮잠을 잠", "작은 도우미들과 주변을 호기심 있게 살핌"],
    },
    "victor": {
        "name": "Victor",
        "token_env": "DISCORD_VICTOR_TOKEN",
        "prompt_file": "prompts/victor_prompt.txt",
        "private_room": "빅터 방",
        "sleep_start_range": ((1, 0), (4, 0)),
        "wake_range": ((8, 0), (11, 0)),
        "inventory": ["낡은 수첩", "몸의 단서를 적은 메모"],
        "habits": ["자신의 몸과 기원에 관한 단서를 기록함", "고통이나 위험을 건조한 농담으로 넘기기도 함"],
    },
    "drifter": {
        "name": "Drifter",
        "token_env": "DISCORD_DRIFTER_TOKEN",
        "prompt_file": "prompts/drifter_prompt.txt",
        "private_room": "드리프터 방",
        "sleep_start_range": ((1, 0), (3, 30)),
        "wake_range": ((8, 0), (10, 30)),
        "inventory": [],
        "habits": [],
    },
    "dynamo": {
        "name": "Dynamo",
        "token_env": "DISCORD_DYNAMO_TOKEN",
        "prompt_file": "prompts/dynamo_prompt.txt",
        "private_room": "다이나모 방",
        "sleep_start_range": ((0, 30), (3, 0)),
        "wake_range": ((7, 30), (10, 0)),
        "inventory": [],
        "habits": [
            "컬럼비아 칼리지에서 수업 준비나 학생 과제를 살핌",
            "연구 메모와 논문을 정리함",
        ],
    },
    "venator": {
        "name": "Venator",
        "token_env": "DISCORD_VENATOR_TOKEN",
        "prompt_file": "prompts/venator_prompt.txt",
        "private_room": "베네터 방",
        "sleep_start_range": ((23, 30), (1, 30)),
        "wake_range": ((6, 0), (8, 0)),
        "inventory": ["성 베네딕트의 표식", "사냥 기록 수첩", "무기 손질 도구"],
        "habits": [
            "뉴욕의 초자연적 위협에 관한 기록을 검토함",
            "무기와 장비를 점검하고 다음 사냥을 준비함",
            "성급히 판결하기보다 먼저 관찰하고 확인하려 함",
        ],
    },
    "abrams": {
        "name": "Abrams",
        "token_env": "DISCORD_ABRAMS_TOKEN",
        "prompt_file": "prompts/abrams_prompt.txt",
        "private_room": "에이브람스 방",
        "sleep_start_range": ((0, 30), (3, 0)),
        "wake_range": ((7, 30), (10, 0)),
        "inventory": ['사건 수첩'],
        "habits": ['사람들과 어울리거나 카드게임을 하기도 함', '새로운 사건이 있을 때만 단서를 집중적으로 검토함'],
    },
    "bebop": {
        "name": "Bebop",
        "token_env": "DISCORD_BEBOP_TOKEN",
        "prompt_file": "prompts/bebop_prompt.txt",
        "private_room": "비밥 방",
        "sleep_start_range": ((1, 0), (4, 0)),
        "wake_range": ((8, 0), (11, 0)),
        "inventory": ['정비 도구'],
        "habits": ['몸의 부품과 장비를 정비함', '재즈를 듣거나 조용한 시간을 보냄'],
    },
    "billy": {
        "name": "Billy",
        "token_env": "DISCORD_BILLY_TOKEN",
        "prompt_file": "prompts/billy_prompt.txt",
        "private_room": "빌리 방",
        "sleep_start_range": ((2, 0), (5, 0)),
        "wake_range": ((9, 0), (12, 0)),
        "inventory": ['병'],
        "habits": ['거리를 돌아다니며 시비나 소동을 피우기도 함', '흥분이 가라앉으면 먹고 마시며 빈둥거림'],
    },
    "calico": {
        "name": "Calico",
        "token_env": "DISCORD_CALICO_TOKEN",
        "prompt_file": "prompts/calico_prompt.txt",
        "private_room": "칼리코 방",
        "sleep_start_range": ((1, 0), (3, 30)),
        "wake_range": ((8, 0), (10, 30)),
        "inventory": [],
        "habits": ['혼자 조용히 시간을 보내며 주변을 관찰함', '일이 없을 때는 굳이 살인이나 임무를 찾지 않음'],
    },
    "celeste": {
        "name": "Celeste",
        "token_env": "DISCORD_CELESTE_TOKEN",
        "prompt_file": "prompts/celeste_prompt.txt",
        "private_room": "셀레스트 방",
        "sleep_start_range": ((0, 0), (2, 30)),
        "wake_range": ((8, 0), (10, 0)),
        "inventory": [],
        "habits": ['사람들과 어울리며 공연과 일상 이야기를 즐김', '가족처럼 여기는 사람들을 챙김'],
    },
    "gray_talon": {
        "name": "Gray Talon",
        "token_env": "DISCORD_GRAY_TALON_TOKEN",
        "prompt_file": "prompts/gray_talon_prompt.txt",
        "private_room": "그레이 탈론 방",
        "sleep_start_range": ((22, 30), (0, 30)),
        "wake_range": ((6, 0), (8, 30)),
        "inventory": ['활 관리 도구'],
        "habits": ['조용히 장비를 관리하거나 식사를 준비함', '가족을 떠올리되 하루 종일 복수만 생각하지 않음'],
    },
    "haze": {
        "name": "Haze",
        "token_env": "DISCORD_HAZE_TOKEN",
        "prompt_file": "prompts/haze_prompt.txt",
        "private_room": "헤이즈 방",
        "sleep_start_range": ((0, 30), (3, 0)),
        "wake_range": ((7, 0), (9, 30)),
        "inventory": ['업무 수첩'],
        "habits": ['훈련과 스트레칭을 함', '첼로를 연주하거나 조용히 쉬기도 함'],
    },
    "holliday": {
        "name": "Holliday",
        "token_env": "DISCORD_HOLLIDAY_TOKEN",
        "prompt_file": "prompts/holliday_prompt.txt",
        "private_room": "홀리데이 방",
        "sleep_start_range": ((23, 30), (2, 0)),
        "wake_range": ((7, 0), (9, 30)),
        "inventory": ['리볼버 관리 도구', '사건 수첩'],
        "habits": ['사격 장비를 손질함', '평범한 보안관처럼 주변 사람들과 이야기를 나누기도 함'],
    },
    "infernus": {
        "name": "Infernus",
        "token_env": "DISCORD_INFERNUS_TOKEN",
        "prompt_file": "prompts/infernus_prompt.txt",
        "private_room": "인페르누스 방",
        "sleep_start_range": ((2, 0), (5, 0)),
        "wake_range": ((9, 0), (12, 0)),
        "inventory": [],
        "habits": ['음료와 바 일을 생각하거나 사람들과 수다를 즐김', '라이브 음악과 느긋한 시간을 좋아함'],
    },
    "ivy": {
        "name": "Ivy",
        "token_env": "DISCORD_IVY_TOKEN",
        "prompt_file": "prompts/ivy_prompt.txt",
        "private_room": "아이비 방",
        "sleep_start_range": ((0, 0), (2, 30)),
        "wake_range": ((7, 30), (10, 0)),
        "inventory": [],
        "habits": ['도시의 새로운 장소를 구경함', '사람들과 어울리며 평범한 경험을 쌓으려 함'],
    },
    "kelvin": {
        "name": "Kelvin",
        "token_env": "DISCORD_KELVIN_TOKEN",
        "prompt_file": "prompts/kelvin_prompt.txt",
        "private_room": "켈빈 방",
        "sleep_start_range": ((23, 30), (2, 0)),
        "wake_range": ((6, 30), (9, 0)),
        "inventory": ['탐험 수첩'],
        "habits": ['책과 지도를 읽거나 산책함', '새 단서가 없을 때는 북극 원정 기록을 반복해서 뒤지지 않음'],
    },
    "lady_geist": {
        "name": "Lady Geist",
        "token_env": "DISCORD_LADY_GEIST_TOKEN",
        "prompt_file": "prompts/lady_geist_prompt.txt",
        "private_room": "가이스트 방",
        "sleep_start_range": ((1, 0), (3, 30)),
        "wake_range": ((9, 0), (11, 0)),
        "inventory": [],
        "habits": ['사교와 개인적인 휴식을 즐김', '조용히 책을 읽거나 주변 사람을 관찰함'],
    },
    "lash": {
        "name": "Lash",
        "token_env": "DISCORD_LASH_TOKEN",
        "prompt_file": "prompts/lash_prompt.txt",
        "private_room": "래쉬 방",
        "sleep_start_range": ((2, 0), (4, 30)),
        "wake_range": ((9, 0), (11, 30)),
        "inventory": [],
        "habits": ['몸을 풀거나 싸움 기술을 연습함', '다른 사람을 놀리거나 허세 섞인 잡담을 즐김'],
    },
    "mcginnis": {
        "name": "McGinnis",
        "token_env": "DISCORD_MCGINNIS_TOKEN",
        "prompt_file": "prompts/mcginnis_prompt.txt",
        "private_room": "맥기니스 방",
        "sleep_start_range": ((0, 0), (3, 0)),
        "wake_range": ((7, 0), (10, 0)),
        "inventory": ['공구 세트'],
        "habits": ['기계를 손보거나 작은 물건을 만듦', '일이 없을 때는 식사하고 쉬며 사람들과 평범하게 이야기함'],
    },
    "mirage": {
        "name": "Mirage",
        "token_env": "DISCORD_MIRAGE_TOKEN",
        "prompt_file": "prompts/mirage_prompt.txt",
        "private_room": "미라지 방",
        "sleep_start_range": ((0, 0), (2, 30)),
        "wake_range": ((7, 0), (9, 30)),
        "inventory": [],
        "habits": ['경계와 예절을 유지하며 주변을 살핌', '임무가 없을 때는 식사와 휴식, 가벼운 대화를 즐김'],
    },
    "paradox": {
        "name": "Paradox",
        "token_env": "DISCORD_PARADOX_TOKEN",
        "prompt_file": "prompts/paradox_prompt.txt",
        "private_room": "패러독스 방",
        "sleep_start_range": ((1, 0), (4, 0)),
        "wake_range": ((8, 0), (11, 0)),
        "inventory": [],
        "habits": ['자료와 장비를 정리함', '사람을 관찰하거나 가벼운 장난과 대화를 즐김'],
    },
    "seven": {
        "name": "Seven",
        "token_env": "DISCORD_SEVEN_TOKEN",
        "prompt_file": "prompts/seven_prompt.txt",
        "private_room": "세븐 방",
        "sleep_start_range": ((2, 0), (5, 0)),
        "wake_range": ((10, 0), (12, 0)),
        "inventory": [],
        "habits": ['오컬트 서적과 지식을 탐구함', '매 순간 힘과 승천만 이야기하지 않고 조용히 시간을 보내기도 함'],
    },
    "shiv": {
        "name": "Shiv",
        "token_env": "DISCORD_SHIV_TOKEN",
        "prompt_file": "prompts/shiv_prompt.txt",
        "private_room": "쉬브 방",
        "sleep_start_range": ((1, 0), (4, 0)),
        "wake_range": ((8, 0), (11, 0)),
        "inventory": ['칼 관리 도구'],
        "habits": ['무기와 장비를 정비함', '사냥이 없을 때는 먹고 쉬며 사람들과 거칠지만 평범하게 어울림'],
    },
    "silver": {
        "name": "Silver",
        "token_env": "DISCORD_SILVER_TOKEN",
        "prompt_file": "prompts/silver_prompt.txt",
        "private_room": "실버 방",
        "sleep_start_range": ((2, 0), (5, 0)),
        "wake_range": ((9, 0), (12, 0)),
        "inventory": ['수갑'],
        "habits": ['현상금 일을 확인하거나 장비를 손봄', '술을 마시거나 빈둥거리며 쉬기도 함'],
    },
    "vindicta": {
        "name": "Vindicta",
        "token_env": "DISCORD_VINDICTA_TOKEN",
        "prompt_file": "prompts/vindicta_prompt.txt",
        "private_room": "빈딕타 방",
        "sleep_start_range": ((1, 0), (4, 0)),
        "wake_range": ((8, 0), (11, 0)),
        "inventory": [],
        "habits": ['도시를 돌아보며 초자연적 존재들의 안전을 살핌', '조용한 곳에서 혼자 시간을 보내기도 함'],
    },
    "vyper": {
        "name": "Vyper",
        "token_env": "DISCORD_VYPER_TOKEN",
        "prompt_file": "prompts/vyper_prompt.txt",
        "private_room": "바이퍼 방",
        "sleep_start_range": ((2, 0), (5, 0)),
        "wake_range": ((9, 0), (12, 0)),
        "inventory": [],
        "habits": ['돈과 기회를 찾아 돌아다님', '일이 없을 때는 놀거나 사람들과 장난스러운 대화를 나눔'],
    },
    "wraith": {
        "name": "Wraith",
        "token_env": "DISCORD_WRAITH_TOKEN",
        "prompt_file": "prompts/wraith_prompt.txt",
        "private_room": "레이스 방",
        "sleep_start_range": ((2, 0), (5, 0)),
        "wake_range": ((9, 0), (12, 0)),
        "inventory": ['카드 덱'],
        "habits": ['카드게임과 사업 이야기를 즐김', '일이 없을 때는 식사와 휴식, 사교에 시간을 씀'],
    },

}


# ============================================================
# v60 범용 RP 설정 로더
# ------------------------------------------------------------
# config/characters.json 이 존재하면 코드에 하드코딩된 예시 캐릭터 대신
# 사용자가 작성한 캐릭터 구성을 사용한다.
# 파일이 없으면 기존 내장 구성을 그대로 사용하므로 이전 버전과도 호환된다.
# ============================================================

CONFIG_DIR = APP_ROOT / "config"
CONFIG_CHARACTERS_PATH = CONFIG_DIR / "characters.json"
CONFIG_PLACES_PATH = CONFIG_DIR / "places.json"
CONFIG_RELATIONS_PATH = CONFIG_DIR / "relations.json"
CONFIG_SETTINGS_PATH = CONFIG_DIR / "settings.json"

GENERIC_CONFIG_ACTIVE = False
CUSTOM_SETTINGS = {}
CONFIG_RELATIONSHIPS = {}


def _load_json_file(path, default=None):
    if default is None:
        default = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            value = json.load(f)
        return value
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"설정 JSON 문법 오류: {path} | line {e.lineno}, column {e.colno}: {e.msg}"
        ) from e


def _normalize_time_range(value, fallback):
    """
    JSON의 [[1, 0], [3, 0]] 형태를 코드가 쓰는 ((1, 0), (3, 0)) 형태로 바꾼다.
    """
    try:
        if (
            isinstance(value, list)
            and len(value) == 2
            and all(isinstance(item, list) and len(item) == 2 for item in value)
        ):
            return (
                (int(value[0][0]), int(value[0][1])),
                (int(value[1][0]), int(value[1][1])),
            )
    except (TypeError, ValueError):
        pass
    return fallback


def _normalize_character_config(raw_characters):
    normalized = {}

    if not isinstance(raw_characters, dict):
        raise RuntimeError("config/characters.json의 최상위 값은 객체여야 합니다.")

    for key, raw in raw_characters.items():
        if str(key).startswith("_"):
            # 예시 설정의 한국어 설명/주석용 항목
            continue

        if not isinstance(raw, dict):
            continue

        character_key = str(key).strip()
        if not character_key:
            continue

        name = str(raw.get("name") or character_key).strip()
        token_env = str(
            raw.get("token_env")
            or f"DISCORD_{character_key.upper()}_TOKEN"
        ).strip()

        prompt_file = str(
            raw.get("prompt_file")
            or f"prompts/{character_key}.txt"
        ).strip()

        private_room = raw.get("private_room")
        if private_room is not None:
            private_room = str(private_room).strip() or None

        normalized[character_key] = {
            "name": name,
            "token_env": token_env,
            "prompt_file": prompt_file,
            "private_room": private_room,
            "default_outfit": str(raw.get("default_outfit") or "").strip(),
            "current_outfit": str(raw.get("current_outfit") or "").strip(),
            **{k: raw.get(k, False if k == "outfit_preferences_enabled" else "") for k in ("outfit_preferences_enabled", "preferred_style", "preferred_colors", "disliked_outfits", "hated_outfits", "outfit_notes")},
            "sleep_start_range": _normalize_time_range(
                raw.get("sleep_start_range"),
                ((1, 0), (3, 0)),
            ),
            "wake_range": _normalize_time_range(
                raw.get("wake_range"),
                ((8, 0), (10, 0)),
            ),
            "weekly_schedule": raw.get("weekly_schedule", []),
            **{k: raw.get(k, d) for k,d in [('required_sleep_hours',8),('sleep_recovery_per_hour',7),('sleep_outfit',''),('day_outfits',[]),('weekend_extra_sleep_hours',1),('wake_acceptance',.65),('grooming_place','')]},
            "inventory": [
                str(item)
                for item in raw.get("inventory", [])
                if str(item).strip()
            ],
            "habits": [
                str(item)
                for item in raw.get("habits", [])
                if str(item).strip()
            ],
            # 아래 필드는 v60 범용 설정용 확장 필드다.
            "goals": [
                str(item)
                for item in raw.get("goals", [])
                if str(item).strip()
            ],
            "place_weights": {
                str(place): float(weight)
                for place, weight in (raw.get("place_weights") or {}).items()
            },
            "restricted_places": {
                str(place)
                for place in raw.get("restricted_places", [])
                if str(place).strip()
            },
        }

    if not normalized:
        raise RuntimeError(
            "config/characters.json에 사용할 캐릭터가 하나도 없습니다."
        )

    return normalized


if CONFIG_CHARACTERS_PATH.exists():
    _external_characters = _load_json_file(CONFIG_CHARACTERS_PATH, {})
    CHARACTERS = _normalize_character_config(_external_characters)
    GENERIC_CONFIG_ACTIVE = True

# 캐릭터가 늘어나도 상태봇은 표시 이름 기준 알파벳순으로 자동 정렬한다.
STATUS_CHARACTER_ORDER = sorted(
    CHARACTERS,
    key=lambda key: CHARACTERS[key]["name"].casefold(),
)

for _character, _config in CHARACTERS.items():
    _config["token"] = os.getenv(_config["token_env"])
    if _config["token"]: _monitor.secrets = [*_monitor.secrets, _config["token"]]

# 상태메시지 / 장소 / 생활 상태
current_activity = {character: None for character in CHARACTERS}
current_place = {character: None for character in CHARACTERS}

# 캐릭터 런타임 생활 상태
character_state = {
    character: {
        "sleeping": False,
        "sleep_interruptions": 0,
        "last_sleep_interaction": None,
        "away": False,
        "hunger": random.randint(15, 45),
        "fatigue": random.randint(10, 35),
        "mood": "평온",
        "mood_reason": "특별한 이유 없음",
        "mood_until": None,
        "dream": None,
        "dream_expires_at": None,
        "last_wake_at": None,
        "sleep_started_at": None,
        "last_need_update": datetime.now(),
        "last_place_effect": datetime.now(),
    }
    for character in CHARACTERS
}

# 하루마다 정해지는 수면 시간
sleep_schedule = {character: {} for character in CHARACTERS}

# 채널별 최근 사용자 언어와 최근 사용자
conversation_language = defaultdict(lambda: "ko")
recent_human_by_channel = {}

# 봇끼리 같은 이야기를 너무 오래 끌지 않도록 피로도
conversation_fatigue = defaultdict(int)
conversation_fatigue_updated_at = {}

# 장소에 잠시 남는 환경 흔적 {place: {text, expires_at}}
place_state = {}

# 약속/예정 목록
pending_appointments = []
appointment_counter = 0

# 개인실 초대 만료 정보 {(target, room): expires_at}
room_invitation_expiry = {}

# 실제 소지품/장소에 놓인 물건
character_inventory = {
    character: set(config.get("inventory", []))
    for character, config in CHARACTERS.items()
}
place_items = defaultdict(set)

# 이동 유예/운영 제어 상태
movement_defer_until = {character: None for character in CHARACTERS}
movement_defer_reason = {character: None for character in CHARACTERS}
autonomous_global_paused = False
autonomous_paused_characters = set()



def reset_bot_chain(channel_id, reason=None):
    """새 대화 흐름이 시작될 때 채널의 봇 연쇄 횟수를 초기화한다."""
    bot_chain_count[channel_id] = 0
    bot_chain_updated_at[channel_id] = datetime.now()
    if reason:
        log_message(
            "General",
            "봇 대화 횟수 초기화:",
            reason,
            "| 채널:",
            channel_id,
        )


def get_bot_chain_count(channel_id):
    """
    오래된 봇 대화 횟수가 새 대화를 막지 않도록 시간에 따라 감쇠한다.
    5분 이상 비면 절반, 15분 이상 비면 완전히 초기화한다.
    """
    last = bot_chain_updated_at.get(channel_id)
    if last is None:
        return bot_chain_count[channel_id]

    elapsed = (datetime.now() - last).total_seconds()

    if elapsed >= 15 * 60:
        bot_chain_count[channel_id] = 0
        bot_chain_updated_at.pop(channel_id, None)
    elif elapsed >= 5 * 60:
        current = bot_chain_count[channel_id]
        reduced = max(0, current // 2)
        if reduced != current:
            bot_chain_count[channel_id] = reduced
            # 같은 elapsed 구간에서 반복해서 계속 반감되지 않도록 기준 시각 갱신
            bot_chain_updated_at[channel_id] = datetime.now()

    return bot_chain_count[channel_id]


def increment_bot_chain(channel_id):
    bot_chain_count[channel_id] += 1
    bot_chain_updated_at[channel_id] = datetime.now()
    return bot_chain_count[channel_id]


def touch_conversation_fatigue(channel_id):
    """채널 대화 피로도를 시간에 따라 감쇠시키고 1 증가시킨다."""
    now = datetime.now()
    last = conversation_fatigue_updated_at.get(channel_id)

    if last is not None:
        elapsed = (now - last).total_seconds()

        # 15분 넘게 대화가 없었다면 피로도를 완전히 초기화.
        if elapsed >= 15 * 60:
            conversation_fatigue[channel_id] = 0
        # 5분 넘게 비었다면 절반 정도로 감쇠.
        elif elapsed >= 5 * 60:
            conversation_fatigue[channel_id] = max(
                0,
                conversation_fatigue[channel_id] // 2
            )

    conversation_fatigue[channel_id] += 1
    conversation_fatigue_updated_at[channel_id] = now
    return conversation_fatigue[channel_id]


def reset_conversation_fatigue(channel_id):
    conversation_fatigue[channel_id] = 0
    conversation_fatigue_updated_at[channel_id] = datetime.now()


def defer_character_movement(character, min_minutes=5, max_minutes=15, reason="대화/이벤트 후 여유"):
    if character not in CHARACTERS:
        return
    minutes = random.randint(min_minutes, max_minutes)
    until = datetime.now() + timedelta(minutes=minutes)
    old_until = movement_defer_until.get(character)
    if old_until is None or until > old_until:
        movement_defer_until[character] = until
        movement_defer_reason[character] = reason


def get_movement_busy_reason(character, client=None):
    """이동 시간이 와도 지금 떠나면 부자연스러운 상황인지 확인한다."""
    state = character_state[character]
    if "_life" in globals():
        reason = _life.busy(character, CUSTOM_SETTINGS)
        if reason: return reason
    if state.get("sleeping"):
        return "수면 중"
    if state.get("away"):
        return "외출 중"

    place = current_place.get(character)
    if client is None:
        client = clients.get(character)
    if client is not None and place:
        channel = find_place_channel(client, place)
        if channel is not None and dynamic_event_channels.get(channel.id, False):
            return "다이나믹 이벤트 진행 중"

    until = movement_defer_until.get(character)
    if until is not None:
        if datetime.now() < until:
            return movement_defer_reason.get(character) or "대화 중"
        movement_defer_until[character] = None
        movement_defer_reason[character] = None

    return None


# 날씨는 하루 단위로 바뀜
daily_world = {
    "date": None,
    "weather": "맑음",
    "temperature": 20,
    "city_event": None,
    "city_event_place": None,
    "city_event_source": None,
    "city_event_started_at": None,
    "city_event_expires_at": None,
}

# 캐릭터별 장기 목표. 강제 퀘스트가 아니라 행동/대화의 약한 방향성으로만 사용한다.
PERSONAL_GOALS = {
    "apollo": [
        "Blackmore Academy에서 실력을 갈고닦고 자신의 기준에 맞는 성취를 추구한다",
        "언젠가 Ixia로 돌아가 가문의 기대와 자신의 자격을 증명하려 한다",
    ],
    "doorman": [
        "Baroness Hotel의 도어맨으로서 손님과 인간의 행동을 관찰한다",
        "단순한 파괴보다 사람을 당황하게 하거나 흥미로운 반응을 끌어내는 것을 즐긴다",
    ],
    "graves": [
        "리치 스승에게서 네크로맨시를 배우고 실력을 늘린다",
        "죽은 자들이 남긴 요구와 미해결 문제를 이해하고 처리하려 한다",
    ],
    "mina": [
        "패션 사업과 사회적 입지를 넓힐 기회를 살핀다",
        "체면과 생활을 흐트러뜨리지 않으려 한다",
    ],
    "paige": [
        "흥미로운 책과 이야기를 찾아 기록한다",
        "Bryce에 관한 단서가 있다면 놓치지 않으려 한다",
    ],
    "pocket": [
        "과거의 암살 시도와 관련된 단서를 조심스럽게 찾는다",
        "자신의 정체와 사생활을 함부로 노출하지 않는다",
    ],
    "rem": [
        "꿈의 세계로 돌아갈 방법을 찾는다",
        "가족에게 돌아가기 위해 Patrons와 의식에 관한 단서를 놓치지 않으려 한다",
    ],
    "victor": [
        "자신을 만든 존재와 자신의 기원을 찾는다",
        "몸을 이루는 각 부분이 누구에게서 왔는지 단서를 찾는다",
    ],
    "venator": [
        "뉴욕에서 활동하는 초자연적 포식자와 위협을 관찰하고 식별한다",
        "바티칸과 성 베네딕트의 베네터로서 맡은 임무를 완수하되 성급한 판결은 피하려 한다",
        "필요하다고 판단한 대가를 자신이 감당하려 하며 다른 사람을 가능한 한 위험에서 멀리 두려 한다",
    ],
    "abrams": [
        "자신에게 얽힌 초자연적 사건과 수상한 단서가 생기면 해결하려 한다",
        "평범한 인간관계와 카드게임 같은 일상도 유지하려 한다",
    ],
    "bebop": [
        "자신을 만든 Miss Shelly를 지키고 필요한 치료비를 마련하려 한다",
        "일이 없을 때는 정비와 재즈 같은 익숙한 일상을 지킨다",
    ],
    "billy": [
        "자신을 억누르거나 통제하려는 것에 반발하며 자기 방식대로 살려 한다",
        "분노와 소동 사이에도 먹고 마시고 빈둥거리는 평범한 시간을 보낸다",
    ],
    "calico": [
        "돈과 안전, 자신의 선택권을 확보하려 한다",
        "계약이 없을 때는 굳이 새로운 살인이나 위험을 만들지 않는다",
    ],
    "celeste": [
        "자신이 가족처럼 여기는 사람들을 지키려 한다",
        "공연과 친구, 식사와 휴식 같은 일상을 소중히 여긴다",
    ],
    "gray_talon": [
        "가족에게 일어난 일의 책임자를 찾고 대가를 치르게 하려 한다",
        "복수와 별개로 가족을 기억하며 조용한 생활 습관을 유지한다",
    ],
    "haze": [
        "OSIC의 임무를 완수하고 위험한 초자연적 위협을 평가한다",
        "임무 밖에서는 훈련, 음악, 휴식 같은 개인적인 시간을 갖는다",
    ],
    "holliday": [
        "Troubadour를 추적해 그가 저지른 살인에 책임을 묻게 하려 한다",
        "수사와 별개로 보안관다운 생활과 사소한 인간관계를 이어간다",
    ],
    "infernus": [
        "Jezebel's Billiards Hall과 그곳의 사람들을 지키려 한다",
        "과거의 폭력보다 바텐더 일과 음악, 사람들과의 대화를 더 평범한 삶으로 여긴다",
    ],
    "ivy": [
        "Spanish Harlem을 지킨 뒤 더 넓은 세상과 자신의 삶을 경험하려 한다",
        "항상 자경단 임무만 찾지 않고 새로운 장소와 사람을 즐긴다",
    ],
    "kelvin": [
        "북극 원정에서 무슨 일이 있었는지 밝혀내려 한다",
        "새 단서가 없을 때는 탐험가이자 학자로서 평범한 일상을 보낸다",
    ],
    "lady_geist": [
        "자신의 이해관계와 생존, 영향력을 지키려 한다",
        "사교와 휴식 같은 개인적인 일상도 중요하게 여긴다",
    ],
    "lash": [
        "자신의 강함과 존재감을 증명하고 원하는 방식대로 살려 한다",
        "훈련 외에도 농담, 허세, 먹고 쉬는 평범한 시간을 보낸다",
    ],
    "mcginnis": [
        "자신의 기술과 발명 능력으로 문제를 해결하고 더 나은 것을 만들려 한다",
        "매 순간 발명만 하지 않고 식사, 휴식, 잡담도 한다",
    ],
    "mirage": [
        "Nashala와 Djinn의 외교 임무를 보호하고 지원한다",
        "임무가 없을 때는 절제된 휴식과 평범한 대화를 한다",
    ],
    "paradox": [
        "Paradox 조직의 목적과 현재 임무를 수행한다",
        "임무 밖에서는 자료 정리, 관찰, 사소한 장난과 대화도 즐긴다",
    ],
    "seven": [
        "금지된 지식과 힘을 추구하며 궁극적으로 더 높은 존재가 되려 한다",
        "매 순간 승천과 지배만 말하지 않고 자신의 방식으로 일상을 보낸다",
    ],
    "shiv": [
        "Baxter Society의 사냥꾼으로서 사람을 해치는 괴물들을 추적한다",
        "사냥이 없을 때는 장비 정비, 식사, 휴식과 인간관계를 이어간다",
    ],
    "silver": [
        "현상금 사냥으로 돈을 벌고 엉망인 삶을 어떻게든 유지하려 한다",
        "일이 없을 때는 술, 휴식, 사소한 잡담과 옛 취미 같은 일상을 보낸다",
    ],
    "vindicta": [
        "Friends of Humanity의 위협으로부터 초자연적 존재들을 지키려 한다",
        "항상 복수와 전투만 생각하지 않고 조용한 시간도 보낸다",
    ],
    "vyper": [
        "돈과 기회, 자유를 좇으며 자기 방식대로 살아남으려 한다",
        "범죄나 추격이 없을 때는 놀고 쉬며 인간관계를 만든다",
    ],
    "wraith": [
        "자신의 사업과 영향력, 재산을 넓히려 한다",
        "일이 없을 때는 카드게임과 사교, 식사와 휴식을 즐긴다",
    ],

}

# 최근 표현/행동 반복 억제용. 재시작 시 비워져도 되는 런타임 상태.
recent_expressions = defaultdict(lambda: deque(maxlen=12))

# 캐릭터별 반복 방지만으로는 다른 캐릭터가 같은 비유/문장 구조를 베끼는 현상을 막기 어렵다.
# 최근 서버 전체에서 등장한 "반복 횟수 비유" 같은 표현 패턴을 따로 공유한다.
recent_global_expression_patterns = deque(maxlen=40)


# 장소 목록
PLACE_CHANNELS = [
    "카페",
    "거실",
    "거리",
    "옥상",
    "광장",
    "식당",
    "블랙모어 아카데미",
    "실험실",
    "도서관",
    "바로네스 호텔",
    "컬럼비아 칼리지",
    "제제벨",
    "도박장",
    "미나 방",
    "포켓 방",
    "페이지 방",
]

# 내부 장소명과 Discord 실제 채널명을 분리한다.
# Discord에서 공백이 하이픈으로 바뀌는 개인실 채널을 여기서 매핑한다.
PLACE_CHANNEL_NAMES = {
    "블랙모어 아카데미": "블랙모어-아카데미",
    "실험실": "실험실",
    "도서관": "도서관",
    "바로네스 호텔": "바로네스-호텔",
    "컬럼비아 칼리지": "컬럼비아-칼리지",
    "제제벨": "제제벨",
    "도박장": "도박장",
    "미나 방": "미나-방",
    "포켓 방": "포켓-방",
    "페이지 방": "페이지-방",
}

# 상태봇에 표시할 장소 그룹.
# 이 분류는 Discord 카테고리/세계 표시용이며 이동 경로를 강제하지 않는다.
PLACE_GROUPS = {
    "거리": "실외",
    "광장": "실외",

    "카페": "실내",
    "식당": "실내",
    "도서관": "실내",
    "바로네스 호텔": "실내",
    "컬럼비아 칼리지": "실내",
    "제제벨": "실내",
    "도박장": "실내",

    "옥상": "집",
    "거실": "집",

    "미나 방": "집",
    "포켓 방": "집",
    "페이지 방": "집",
    "아폴로 방": "집",
    "도어맨 방": "집",
    "그레이브즈 방": "집",
    "렘 방": "집",
    "빅터 방": "집",
    "드리프터 방": "집",
    "다이나모 방": "집",
    "베네터 방": "집",

    # 실험실은 실제로 블랙모어 아카데미 내부에 있는 장소다.
    "실험실": "블랙모어 아카데미",
}

# 실제 이동/출입 규칙에 사용하는 물리적 상위 장소.
# 표시용 PLACE_GROUPS와 분리해서 실내/실외/집이 이동 단계로 취급되지 않게 한다.
PHYSICAL_PARENT_PLACES = {
    "실험실": "블랙모어 아카데미",
}

# 기본 출입 제한이 있는 공용 내부 장소.
# 관리자 강제 이동은 이 제한을 우회할 수 있다.
PLACE_ALLOWED_CHARACTERS = {
    "실험실": {"apollo", "graves", "mina"},
}

# 런타임 임시 출입 허가. 향후 초대/사건으로 외부인을 들일 때 사용 가능.
place_access_permissions = defaultdict(set)

# 캐릭터별 고정 출입 제한.
# 미성년자인 Apollo, Graves, Rem은 제제벨과 도박장에 자연 이동/약속/대화 이동으로 들어가지 않는다.
# 관리자 강제 이동 명령은 기존 구조대로 이 제한을 우회할 수 있다.
RESTRICTED_PLACES_BY_CHARACTER = {
    "apollo": {"제제벨", "도박장"},
    "graves": {"제제벨", "도박장"},
    "rem": {"제제벨", "도박장"},
}


def get_parent_place(place):
    """상태봇에 표시할 상위 장소/장소 그룹을 반환한다."""
    return PLACE_GROUPS.get(place)


def get_physical_parent_place(place):
    """실제 이동/출입 경로에 사용되는 물리적 상위 장소를 반환한다."""
    return PHYSICAL_PARENT_PLACES.get(place)


def get_location_path_text(place):
    if not place:
        return "외출 중 / 알 수 없음"

    parent = get_parent_place(place)
    if parent and parent != place:
        return f"{parent} → {place}"

    return place


def get_presence_location_text(character, place=None):
    """
    Discord 멤버 목록의 짧은 상태메시지.
    상위 장소는 제외하고 현재 위치 + 현재 활동만 표시한다.
    """
    if character_state.get(character, {}).get("away"):
        return "외출 중"

    place = place if place is not None else current_place.get(character)
    place_text = place or "위치 알 수 없음"

    activity = (current_activity.get(character) or "").strip()
    if not activity:
        return place_text

    # 위치와 활동이 같은 말로 중복되는 경우에는 위치만 표시한다.
    if activity == place_text:
        return place_text

    status_text = f"{place_text} · {activity}"

    # Discord CustomActivity 표시가 지나치게 길어지지 않도록 짧게 유지한다.
    if len(status_text) > 45:
        status_text = status_text[:44].rstrip() + "…"

    return status_text

# 새 캐릭터의 private_room이 목록에 빠져 있어도 자동 등록한다.
for _config in CHARACTERS.values():
    _room = _config.get("private_room")
    if _room and _room not in PLACE_CHANNELS:
        PLACE_CHANNELS.append(_room)
    if _room and _room not in PLACE_CHANNEL_NAMES:
        PLACE_CHANNEL_NAMES[_room] = _room.replace(" ", "-")
    if _room:
        PLACE_GROUPS.setdefault(_room, "집")

# 캐릭터별 장소 가중치
PLACE_WEIGHTS = {
    "apollo": {
        "카페": 5,
        "거실": 6,
        "거리": 7,
        "옥상": 8,
        "광장": 10,
        "식당": 6,
        "블랙모어 아카데미": 14,
        "실험실": 11,
        "도서관": 6,
        "바로네스 호텔": 2,
        "아폴로 방": 14,
    },

    "doorman": {
        "카페": 4,
        "거실": 5,
        "거리": 3,
        "옥상": 2,
        "광장": 3,
        "식당": 4,
        "블랙모어 아카데미": 1,
        "실험실": 1,
        "도서관": 2,
        "바로네스 호텔": 28,
        "도어맨 방": 8,
    },

    "graves": {
        "카페": 6,
        "거실": 7,
        "거리": 8,
        "옥상": 8,
        "광장": 5,
        "식당": 5,
        "블랙모어 아카데미": 13,
        "실험실": 15,
        "도서관": 9,
        "바로네스 호텔": 2,
        "그레이브즈 방": 14,
    },

    "mina": {
        "카페": 8,
        "거실": 10,
        "거리": 5,
        "옥상": 4,
        "광장": 6,
        "식당": 7,
        "블랙모어 아카데미": 3,
        "실험실": 1,
        "도서관": 4,
        "바로네스 호텔": 8,
        "미나 방": 14,
        "포켓 방": 1,
        "페이지 방": 1,
    },

    "paige": {
        "카페": 7,
        "거실": 8,
        "거리": 7,
        "옥상": 6,
        "광장": 7,
        "식당": 7,
        "블랙모어 아카데미": 3,
        "실험실": 2,
        "도서관": 14,
        "바로네스 호텔": 5,
        "미나 방": 1,
        "포켓 방": 1,
        "페이지 방": 14,
    },

    "pocket": {
        "카페": 8,
        "거실": 7,
        "거리": 8,
        "옥상": 9,
        "광장": 6,
        "식당": 6,
        "블랙모어 아카데미": 2,
        "실험실": 2,
        "도서관": 5,
        "바로네스 호텔": 5,
        "미나 방": 1,
        "포켓 방": 14,
        "페이지 방": 1,
    },

    "rem": {
        "카페": 7,
        "거실": 11,
        "거리": 6,
        "옥상": 3,
        "광장": 8,
        "식당": 7,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 4,
        "바로네스 호텔": 5,
        "렘 방": 15,
    },

    "victor": {
        "카페": 5,
        "거실": 5,
        "거리": 10,
        "옥상": 8,
        "광장": 7,
        "식당": 6,
        "블랙모어 아카데미": 2,
        "실험실": 5,
        "도서관": 3,
        "바로네스 호텔": 4,
        "빅터 방": 12,
    },

    # Drifter / Dynamo는 세부 성향 프롬프트가 정해지기 전까지
    # 특정 장소에 과도하게 치우치지 않는 중립 가중치로 시작한다.
    "drifter": {
        "카페": 6,
        "거실": 7,
        "거리": 8,
        "옥상": 7,
        "광장": 7,
        "식당": 6,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 5,
        "바로네스 호텔": 5,
        "드리프터 방": 14,
    },

    "dynamo": {
        "카페": 6,
        "거실": 7,
        "거리": 7,
        "옥상": 6,
        "광장": 7,
        "식당": 6,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 5,
        "바로네스 호텔": 5,
        "컬럼비아 칼리지": 26,
        "다이나모 방": 14,
    },
    "venator": {
        "카페": 4,
        "거실": 5,
        "거리": 13,
        "옥상": 8,
        "광장": 10,
        "식당": 5,
        "블랙모어 아카데미": 4,
        "실험실": 1,
        "도서관": 7,
        "바로네스 호텔": 7,
        "컬럼비아 칼리지": 2,
        "베네터 방": 14,
    },
    "abrams": {
        "카페": 5,
        "거실": 6,
        "거리": 11,
        "옥상": 7,
        "광장": 8,
        "식당": 6,
        "블랙모어 아카데미": 3,
        "실험실": 1,
        "도서관": 9,
        "바로네스 호텔": 6,
        "컬럼비아 칼리지": 2,
        "에이브람스 방": 14,
    },
    "bebop": {
        "카페": 4,
        "거실": 7,
        "거리": 8,
        "옥상": 6,
        "광장": 7,
        "식당": 6,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 3,
        "바로네스 호텔": 3,
        "컬럼비아 칼리지": 1,
        "비밥 방": 14,
    },
    "billy": {
        "카페": 5,
        "거실": 5,
        "거리": 12,
        "옥상": 8,
        "광장": 10,
        "식당": 8,
        "블랙모어 아카데미": 1,
        "실험실": 1,
        "도서관": 2,
        "바로네스 호텔": 4,
        "컬럼비아 칼리지": 1,
        "빌리 방": 14,
    },
    "calico": {
        "카페": 6,
        "거실": 4,
        "거리": 10,
        "옥상": 8,
        "광장": 7,
        "식당": 5,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 5,
        "바로네스 호텔": 8,
        "컬럼비아 칼리지": 2,
        "칼리코 방": 14,
    },
    "celeste": {
        "카페": 8,
        "거실": 8,
        "거리": 7,
        "옥상": 6,
        "광장": 10,
        "식당": 7,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 5,
        "바로네스 호텔": 5,
        "컬럼비아 칼리지": 2,
        "셀레스트 방": 14,
    },
    "gray_talon": {
        "카페": 5,
        "거실": 9,
        "거리": 8,
        "옥상": 8,
        "광장": 6,
        "식당": 7,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 5,
        "바로네스 호텔": 3,
        "컬럼비아 칼리지": 1,
        "그레이 탈론 방": 14,
    },
    "haze": {
        "카페": 5,
        "거실": 5,
        "거리": 10,
        "옥상": 7,
        "광장": 8,
        "식당": 5,
        "블랙모어 아카데미": 3,
        "실험실": 1,
        "도서관": 8,
        "바로네스 호텔": 5,
        "컬럼비아 칼리지": 2,
        "헤이즈 방": 14,
    },
    "holliday": {
        "카페": 5,
        "거실": 5,
        "거리": 12,
        "옥상": 7,
        "광장": 9,
        "식당": 7,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 6,
        "바로네스 호텔": 6,
        "컬럼비아 칼리지": 2,
        "홀리데이 방": 14,
    },
    "infernus": {
        "카페": 9,
        "거실": 7,
        "거리": 7,
        "옥상": 5,
        "광장": 8,
        "식당": 9,
        "블랙모어 아카데미": 1,
        "실험실": 1,
        "도서관": 3,
        "바로네스 호텔": 7,
        "컬럼비아 칼리지": 1,
        "인페르누스 방": 14,
    },
    "ivy": {
        "카페": 7,
        "거실": 8,
        "거리": 10,
        "옥상": 7,
        "광장": 9,
        "식당": 7,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 4,
        "바로네스 호텔": 5,
        "컬럼비아 칼리지": 1,
        "아이비 방": 14,
    },
    "kelvin": {
        "카페": 6,
        "거실": 7,
        "거리": 8,
        "옥상": 7,
        "광장": 7,
        "식당": 6,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 10,
        "바로네스 호텔": 4,
        "컬럼비아 칼리지": 2,
        "켈빈 방": 14,
    },
    "lady_geist": {
        "카페": 8,
        "거실": 7,
        "거리": 6,
        "옥상": 6,
        "광장": 7,
        "식당": 7,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 6,
        "바로네스 호텔": 8,
        "컬럼비아 칼리지": 2,
        "가이스트 방": 14,
    },
    "lash": {
        "카페": 6,
        "거실": 5,
        "거리": 10,
        "옥상": 10,
        "광장": 9,
        "식당": 7,
        "블랙모어 아카데미": 1,
        "실험실": 1,
        "도서관": 2,
        "바로네스 호텔": 5,
        "컬럼비아 칼리지": 1,
        "래쉬 방": 14,
    },
    "mcginnis": {
        "카페": 6,
        "거실": 7,
        "거리": 7,
        "옥상": 6,
        "광장": 7,
        "식당": 6,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 7,
        "바로네스 호텔": 4,
        "컬럼비아 칼리지": 3,
        "맥기니스 방": 14,
    },
    "mirage": {
        "카페": 6,
        "거실": 6,
        "거리": 8,
        "옥상": 6,
        "광장": 7,
        "식당": 6,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 5,
        "바로네스 호텔": 10,
        "컬럼비아 칼리지": 2,
        "미라지 방": 14,
    },
    "paradox": {
        "카페": 6,
        "거실": 5,
        "거리": 9,
        "옥상": 8,
        "광장": 7,
        "식당": 5,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 8,
        "바로네스 호텔": 6,
        "컬럼비아 칼리지": 2,
        "패러독스 방": 14,
    },
    "seven": {
        "카페": 3,
        "거실": 3,
        "거리": 6,
        "옥상": 7,
        "광장": 5,
        "식당": 4,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 10,
        "바로네스 호텔": 2,
        "컬럼비아 칼리지": 2,
        "세븐 방": 14,
    },
    "shiv": {
        "카페": 5,
        "거실": 5,
        "거리": 12,
        "옥상": 8,
        "광장": 8,
        "식당": 7,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 4,
        "바로네스 호텔": 4,
        "컬럼비아 칼리지": 1,
        "쉬브 방": 14,
    },
    "silver": {
        "카페": 7,
        "거실": 5,
        "거리": 11,
        "옥상": 7,
        "광장": 8,
        "식당": 7,
        "블랙모어 아카데미": 1,
        "실험실": 1,
        "도서관": 3,
        "바로네스 호텔": 6,
        "컬럼비아 칼리지": 1,
        "실버 방": 14,
    },
    "vindicta": {
        "카페": 4,
        "거실": 5,
        "거리": 10,
        "옥상": 9,
        "광장": 7,
        "식당": 5,
        "블랙모어 아카데미": 2,
        "실험실": 1,
        "도서관": 6,
        "바로네스 호텔": 3,
        "컬럼비아 칼리지": 1,
        "빈딕타 방": 14,
    },
    "vyper": {
        "카페": 7,
        "거실": 5,
        "거리": 12,
        "옥상": 7,
        "광장": 9,
        "식당": 7,
        "블랙모어 아카데미": 1,
        "실험실": 1,
        "도서관": 3,
        "바로네스 호텔": 6,
        "컬럼비아 칼리지": 1,
        "바이퍼 방": 14,
    },
    "wraith": {
        "카페": 8,
        "거실": 7,
        "거리": 8,
        "옥상": 6,
        "광장": 8,
        "식당": 7,
        "블랙모어 아카데미": 1,
        "실험실": 1,
        "도서관": 4,
        "바로네스 호텔": 9,
        "컬럼비아 칼리지": 2,
        "레이스 방": 14,
    },

}

# 제제벨 / 도박장 기본 가중치.
# 모든 캐릭터가 이동 후보로 사용할 수 있게 하되,
# 실제 연관이 강한 캐릭터는 아래에서 개별 가중치를 높인다.
for _character in CHARACTERS:
    PLACE_WEIGHTS.setdefault(_character, {})
    PLACE_WEIGHTS[_character].setdefault("제제벨", 2)
    PLACE_WEIGHTS[_character].setdefault("도박장", 2)

_SPECIAL_NIGHTLIFE_WEIGHTS = {
    "apollo": {"제제벨": 0, "도박장": 0},
    "doorman": {"제제벨": 3, "도박장": 3},
    "graves": {"제제벨": 0, "도박장": 0},
    "mina": {"제제벨": 3, "도박장": 6},
    "paige": {"제제벨": 4, "도박장": 3},
    "pocket": {"제제벨": 4, "도박장": 4},
    "rem": {"제제벨": 0, "도박장": 0},
    "victor": {"제제벨": 4, "도박장": 3},
    "drifter": {"제제벨": 6, "도박장": 4},
    "dynamo": {"제제벨": 3, "도박장": 2},
    "venator": {"제제벨": 3, "도박장": 2},

    "abrams": {"제제벨": 10, "도박장": 9},
    "bebop": {"제제벨": 4, "도박장": 3},
    "billy": {"제제벨": 7, "도박장": 8},
    "calico": {"제제벨": 4, "도박장": 9},
    "celeste": {"제제벨": 8, "도박장": 3},
    "gray_talon": {"제제벨": 3, "도박장": 2},
    "haze": {"제제벨": 3, "도박장": 4},
    "holliday": {"제제벨": 5, "도박장": 3},
    "infernus": {"제제벨": 30, "도박장": 3},
    "ivy": {"제제벨": 5, "도박장": 3},
    "kelvin": {"제제벨": 6, "도박장": 3},
    "lady_geist": {"제제벨": 6, "도박장": 10},
    "lash": {"제제벨": 12, "도박장": 5},
    "mcginnis": {"제제벨": 3, "도박장": 3},
    "mirage": {"제제벨": 4, "도박장": 3},
    "paradox": {"제제벨": 4, "도박장": 12},
    "seven": {"제제벨": 2, "도박장": 2},
    "shiv": {"제제벨": 11, "도박장": 4},
    "silver": {"제제벨": 12, "도박장": 5},
    "vindicta": {"제제벨": 2, "도박장": 2},
    "vyper": {"제제벨": 7, "도박장": 10},
    "wraith": {"제제벨": 5, "도박장": 30},
}

for _character, _weights in _SPECIAL_NIGHTLIFE_WEIGHTS.items():
    if _character in PLACE_WEIGHTS:
        PLACE_WEIGHTS[_character].update(_weights)

# 시간대별 장소 가중치
TIME_PLACE_MULTIPLIERS = {
    "morning": {
        "카페": 1.2,
        "거실": 1.0,
        "거리": 0.8,
        "옥상": 0.7,
        "광장": 0.8,
        "식당": 1.5,
        "블랙모어 아카데미": 1.3,
        "실험실": 1.0,
        "도서관": 1.1,
        "바로네스 호텔": 0.9,
        "컬럼비아 칼리지": 1.4,
        "제제벨": 0.4,
        "도박장": 0.3,
        "미나 방": 0.9,
        "포켓 방": 0.9,
        "페이지 방": 0.9,
    },

    "day": {
        "카페": 1.3,
        "거실": 0.8,
        "거리": 1.2,
        "옥상": 0.8,
        "광장": 1.3,
        "식당": 1.1,
        "블랙모어 아카데미": 1.5,
        "실험실": 1.3,
        "도서관": 1.4,
        "바로네스 호텔": 1.0,
        "컬럼비아 칼리지": 1.6,
        "제제벨": 0.8,
        "도박장": 0.7,
        "미나 방": 0.7,
        "포켓 방": 0.7,
        "페이지 방": 0.7,
    },

    "evening": {
        "카페": 1.1,
        "거실": 1.3,
        "거리": 0.9,
        "옥상": 1.2,
        "광장": 0.9,
        "식당": 1.5,
        "블랙모어 아카데미": 0.8,
        "실험실": 1.0,
        "도서관": 1.1,
        "바로네스 호텔": 1.4,
        "컬럼비아 칼리지": 0.8,
        "제제벨": 1.7,
        "도박장": 1.6,
        "미나 방": 1.2,
        "포켓 방": 1.2,
        "페이지 방": 1.2,
    },

    "night": {
        "카페": 0.5,
        "거실": 1.3,
        "거리": 0.4,
        "옥상": 1.0,
        "광장": 0.3,
        "식당": 0.5,
        "블랙모어 아카데미": 0.25,
        "실험실": 0.7,
        "도서관": 0.5,
        "바로네스 호텔": 1.5,
        "컬럼비아 칼리지": 0.25,
        "제제벨": 2.0,
        "도박장": 2.2,
        "미나 방": 1.8,
        "포켓 방": 1.8,
        "페이지 방": 1.8,
    },
    "late_night": {
        "카페": 0.15,
        "거실": 0.8,
        "거리": 0.05,
        "옥상": 0.35,
        "광장": 0.05,
        "식당": 0.10,
        "블랙모어 아카데미": 0.05,
        "실험실": 0.25,
        "도서관": 0.10,
        "바로네스 호텔": 1.2,
        "컬럼비아 칼리지": 0.05,
        "제제벨": 1.4,
        "도박장": 1.8,
        "미나 방": 4.0,
        "포켓 방": 4.0,
        "페이지 방": 4.0,
    },
}


# 새 개인실도 기존 개인실과 같은 시간대 기본 가중치를 적용한다.
_PRIVATE_ROOM_TIME_MULTIPLIERS = {
    "morning": 0.9,
    "day": 0.7,
    "evening": 1.2,
    "night": 1.8,
    "late_night": 4.0,
}
for _period, _room_multiplier in _PRIVATE_ROOM_TIME_MULTIPLIERS.items():
    for _config in CHARACTERS.values():
        _room = _config.get("private_room")
        if _room:
            TIME_PLACE_MULTIPLIERS[_period].setdefault(_room, _room_multiplier)


# 장소 정보
PLACE_INFO = {
    "카페": {
        "description": (
            "조용한 카페. 창가 좌석과 작은 원형 테이블들이 있고, "
            "음료와 간단한 디저트를 주문할 수 있다."
        ),
        "objects": [
            "찻잔",
            "커피잔",
            "설탕 통",
            "냅킨",
            "메뉴판",
            "의자",
            "창문",
            "우산꽂이",        ],
    },

    "거실": {
        "description": (
            "여러 사람이 편하게 머물 수 있는 공용 거실. "
            "소파와 낮은 탁자, 창문이 있다."
        ),
        "objects": [
            "소파",
            "탁자",
            "신문",
            "책",
            "커튼",
            "창문",
            "램프",
        ],
    },

    "거리": {
        "description": (
            "사람들이 오가는 거리. 상점과 가로등, "
            "길가의 벤치가 보인다."
        ),
        "objects": [
            "가로등",
            "벤치",
            "상점 간판",
            "쇼윈도",
            "신문 가판대",
        ],
    },

    "옥상": {
        "description": (
            "건물 위의 탁 트인 옥상. 난간 너머로 "
            "주변 거리와 하늘을 볼 수 있다."
        ),
        "objects": [
            "난간",
            "의자",
            "환기구",
            "문",
            "바닥에 떨어진 종이",
        ],
    },

    "광장": {
        "description": (
            "사람들이 오가거나 잠시 머무는 넓은 광장. "
            "벤치와 가게들이 주변에 있다."
        ),
        "objects": [
            "벤치",
            "분수",
            "가로등",
            "안내판",
            "신문",
            "노점",
        ],
    },

    "식당": {
        "description": (
            "식사를 할 수 있는 식당. 여러 테이블과 "
            "식기, 메뉴판이 놓여 있다."
        ),
        "objects": [
            "접시",
            "컵",
            "식기",
            "냅킨",
            "메뉴판",
            "물병",
            "소금통",
        ],
    },

    "블랙모어 아카데미": {
        "description": (
            "뉴욕의 명문 교육기관 Blackmore Academy. 학생과 교직원이 오가는 "
            "복도와 교실, 휴게 공간이 있는 학교 건물이다."
        ),
        "objects": [
            "책상",
            "의자",
            "칠판",
            "게시판",
            "교과서",
            "학교 신문",
            "사물함",
        ],
    },

    "실험실": {
        "description": (
            "실험과 연구가 이루어지는 작업 공간. 여러 실험대와 기록물, "
            "각종 장비와 용기가 정리되어 있다."
        ),
        "objects": [
            "실험대",
            "유리병",
            "시험관",
            "기록 노트",
            "측정 기구",
            "보관함",
            "보호 장갑",
        ],
    },

    "도서관": {
        "description": (
            "조용히 책과 기록을 찾아볼 수 있는 공공 도서관. "
            "높은 서가와 열람용 책상들이 줄지어 있다."
        ),
        "objects": [
            "책장",
            "책",
            "신문",
            "열람대",
            "카드 목록함",
            "책갈피",
            "스탠드 램프",
        ],
    },

    "컬럼비아 칼리지": {
        "description": (
            "뉴욕의 고등교육기관 Columbia College. 학생과 교수들이 오가며 "
            "강의와 연구, 학술 토론이 이루어지는 대학 건물이다."
        ),
        "objects": [
            "강의용 책상",
            "의자",
            "칠판",
            "게시판",
            "교재",
            "논문",
            "교수실 안내판",
        ],
    },

    "바로네스 호텔": {
        "description": (
            "The Doorman이 일하는 Baroness Hotel의 로비와 공용 공간. "
            "손님들이 드나들며 프런트와 좌석, 엘리베이터가 보인다."
        ),
        "objects": [
            "프런트 데스크",
            "호출 벨",
            "열쇠걸이",
            "소파",
            "여행 가방",
            "엘리베이터",
            "호텔 안내판",
        ],
    },

    "제제벨": {
        "description": (
            "Infernus가 바텐더로 일하는 Jezebel's Billiards Hall. "
            "당구대와 바 카운터가 있고, 저녁이 되면 술과 음악을 즐기러 온 손님들로 활기를 띤다."
        ),
        "objects": [
            "바 카운터",
            "바 스툴",
            "당구대",
            "당구공",
            "큐대",
            "술병",
            "칵테일 잔",
            "테이블",
            "의자",
            "라디오",
            "공연 공간",
        ],
    },

    "도박장": {
        "description": (
            "Wraith의 영향력이 강하게 미치는 화려한 도박장. "
            "카드 게임과 룰렛, 각종 내기가 오가며 손님과 직원들이 끊임없이 드나든다."
        ),
        "objects": [
            "포커 테이블",
            "카드 덱",
            "도박 칩",
            "룰렛 테이블",
            "바 카운터",
            "의자",
            "현금 보관함",
            "장부",
            "조명",
        ],
    },

    "미나 방": {
        "description": (
            "Mina의 개인 방. 옷과 장신구가 정돈되어 있고 "
            "개인적인 물건들이 놓여 있다."
        ),
        "objects": [
            "화장대",
            "거울",
            "옷장",
            "책상",
            "의자",
            "장신구",
            "침대",
        ],
    },

    "포켓 방": {
        "description": (
            "Pocket의 개인 방. 비교적 실용적이고 "
            "단정하게 정리된 공간이다."
        ),
        "objects": [
            "책상",
            "의자",
            "서랍",
            "가방",
            "침대",
            "책",
            "램프",
        ],
    },

    "페이지 방": {
        "description": (
            "Paige의 개인 방. 침대와 책상, 수납공간이 있는 "
            "개인적인 생활 공간이다."
        ),
        "objects": [
            "침대",
            "책상",
            "의자",
            "옷장",
            "서랍",
            "램프",
        ],
    },

    "아폴로 방": {
        "description": "Apollo의 개인 방. 펜싱 장비와 훈련 기록이 정돈되어 있는 단정한 공간이다.",
        "objects": ["침대", "책상", "의자", "검 거치대", "훈련 수첩", "램프"],
    },
    "도어맨 방": {
        "description": "The Doorman이 사용하는 Baroness Hotel의 사적인 서비스 공간. 열쇠와 호텔 용품이 정돈되어 있다.",
        "objects": ["열쇠걸이", "책상", "의자", "호출 벨", "옷걸이", "램프"],
    },
    "그레이브즈 방": {
        "description": "Graves의 개인 방. 학교 물건과 주술 공부 자료가 함께 놓여 있는 공간이다.",
        "objects": ["침대", "책상", "교과서", "주술 노트", "유리병", "램프"],
    },
    "렘 방": {
        "description": "Rem이 쉬는 개인 공간. 편하게 잠들 수 있도록 베개와 담요가 놓여 있다.",
        "objects": ["침대", "베개", "담요", "작은 탁자", "램프"],
    },
    "빅터 방": {
        "description": "Victor의 개인 공간. 자신의 기원과 몸의 단서를 정리한 메모들이 놓여 있다.",
        "objects": ["침대", "책상", "의자", "수첩", "메모", "램프"],
    },
    "드리프터 방": {
        "description": "Drifter의 개인 공간. 다른 사람이 허락 없이 들어갈 수 없는 사적인 방이다.",
        "objects": ["침대", "책상", "의자", "옷장", "램프"],
    },
    "다이나모 방": {
        "description": "Dynamo의 개인 공간. 다른 사람이 허락 없이 들어갈 수 없는 사적인 방이다.",
        "objects": ["침대", "책상", "의자", "옷장", "램프"],
    },

    "베네터 방": {
        "description": (
            "Venator가 사냥 사이에 쉬고 장비를 정비하는 개인 공간. "
            "실용적으로 정돈되어 있으며 임무 기록과 무기 손질 도구가 놓여 있다."
        ),
        "objects": [
            "침대",
            "책상",
            "의자",
            "사냥 기록 수첩",
            "무기 손질 도구",
            "램프",
        ],
    },
}

# 개인실 소유 정보

# 캐릭터별 개인실이 PLACE_INFO에 없으면 기본 생활 공간 설명을 자동 생성한다.
for _character, _config in CHARACTERS.items():
    _room = _config.get("private_room")
    if _room:
        PLACE_INFO.setdefault(
            _room,
            {
                "description": f"{_config['name']}의 개인 생활 공간. 잠을 자고 쉬며 개인 물건을 보관하는 방이다.",
                "objects": ["침대", "책상", "의자", "수납공간", "램프"],
            },
        )


# ============================================================
# v60 범용 장소/세계 설정 적용
# ============================================================

def _load_generic_world_config():
    global CUSTOM_SETTINGS
    global CONFIG_RELATIONSHIPS
    global PERSONAL_GOALS
    global PLACE_CHANNELS
    global PLACE_CHANNEL_NAMES
    global PLACE_GROUPS
    global PHYSICAL_PARENT_PLACES
    global PLACE_ALLOWED_CHARACTERS
    global RESTRICTED_PLACES_BY_CHARACTER
    global PLACE_WEIGHTS
    global TIME_PLACE_MULTIPLIERS
    global PLACE_INFO
    global COMMON_ROLEPLAY_RULES

    if not GENERIC_CONFIG_ACTIVE:
        return

    CUSTOM_SETTINGS = _load_json_file(CONFIG_SETTINGS_PATH, {})
    raw_places = _load_json_file(CONFIG_PLACES_PATH, {})
    CONFIG_RELATIONSHIPS = _load_json_file(CONFIG_RELATIONS_PATH, {})
    validate_features(CUSTOM_SETTINGS)
    validate_calendar(CUSTOM_SETTINGS, CHARACTERS, raw_places)

    if not isinstance(raw_places, dict) or not raw_places:
        raise RuntimeError(
            "범용 설정을 사용할 때는 config/places.json에 최소 1개의 장소가 필요합니다."
        )

    # 캐릭터 파일 안의 목표/장소 선호를 전역 구조로 변환한다.
    PERSONAL_GOALS = {
        character: list(config.get("goals", []))
        for character, config in CHARACTERS.items()
    }

    PLACE_WEIGHTS = {
        character: dict(config.get("place_weights", {}))
        for character, config in CHARACTERS.items()
    }

    RESTRICTED_PLACES_BY_CHARACTER = {
        character: set(config.get("restricted_places", set()))
        for character, config in CHARACTERS.items()
        if config.get("restricted_places")
    }

    PLACE_CHANNELS = []
    PLACE_CHANNEL_NAMES = {}
    PLACE_GROUPS = {}
    PHYSICAL_PARENT_PLACES = {}
    PLACE_ALLOWED_CHARACTERS = {}
    PLACE_INFO = {}

    periods = ("morning", "day", "evening", "night", "late_night")
    TIME_PLACE_MULTIPLIERS = {
        period: {}
        for period in periods
    }

    for place_key, raw in raw_places.items():
        if str(place_key).startswith("_"):
            # 예시 설정의 한국어 설명/주석용 항목
            continue

        if not isinstance(raw, dict):
            continue

        place = str(place_key).strip()
        if not place:
            continue

        PLACE_CHANNELS.append(place)

        channel_name = str(
            raw.get("channel_name")
            or place.replace(" ", "-")
        ).strip()
        PLACE_CHANNEL_NAMES[place] = channel_name

        group = raw.get("group")
        if group:
            PLACE_GROUPS[place] = str(group)

        parent = raw.get("parent")
        if parent:
            PHYSICAL_PARENT_PLACES[place] = str(parent)

        allowed = raw.get("allowed_characters")
        if isinstance(allowed, list) and allowed:
            PLACE_ALLOWED_CHARACTERS[place] = {
                str(character)
                for character in allowed
                if str(character) in CHARACTERS
            }

        PLACE_INFO[place] = {
            "description": str(
                raw.get("description")
                or f"{place}."
            ),
            **{k: raw.get(k, {} if k in ("seats", "menus") else False if k in ("nap_allowed", "outdoor") else []) for k in ("seats", "menus", "nap_allowed", "outdoor", "activities")},
            "routine_capacity": raw.get("routine_capacity",0),
            "opening_hours": raw.get("opening_hours", {}),
            **{k: raw.get(k,{}) for k in ('food_definitions','object_states','leisure')},
            "objects": [
                str(item)
                for item in raw.get("objects", [])
                if str(item).strip()
            ],
        }

        time_multipliers = raw.get("time_multipliers") or {}
        for period in periods:
            try:
                multiplier = float(time_multipliers.get(period, 1.0))
            except (TypeError, ValueError):
                multiplier = 1.0
            TIME_PLACE_MULTIPLIERS[period][place] = multiplier

    # 캐릭터 개인실은 places.json에 직접 적지 않아도 자동으로 장소에 추가한다.
    private_room_multipliers = {
        "morning": 0.9,
        "day": 0.7,
        "evening": 1.2,
        "night": 1.8,
        "late_night": 4.0,
    }

    for character, config in CHARACTERS.items():
        room = config.get("private_room")
        if not room:
            continue

        if room not in PLACE_CHANNELS:
            PLACE_CHANNELS.append(room)

        PLACE_CHANNEL_NAMES.setdefault(
            room,
            room.replace(" ", "-")
        )
        PLACE_GROUPS.setdefault(room, "private")

        PLACE_INFO.setdefault(
            room,
            {
                "description": f"{config['name']}의 개인 공간.",
                "objects": ["침대", "의자", "수납공간"],
            },
        )

        for period in periods:
            TIME_PLACE_MULTIPLIERS[period].setdefault(
                room,
                private_room_multipliers[period],
            )

        # 사용자가 가중치를 생략하면 자기 방은 기본적으로 자주 방문한다.
        PLACE_WEIGHTS.setdefault(character, {})
        PLACE_WEIGHTS[character].setdefault(room, 14.0)

    # place_weights가 비어 있어도 모든 캐릭터가 모든 공용 장소를 후보로 삼을 수 있게 한다.
    for character in CHARACTERS:
        weights = PLACE_WEIGHTS.setdefault(character, {})
        own_room = CHARACTERS[character].get("private_room")
        for place in PLACE_CHANNELS:
            weights.setdefault(
                place,
                14.0 if place == own_room else 5.0,
            )

    # 범용 세계관 설명/규칙을 기존 엔진 공통 규칙 뒤에 추가한다.
    world_name = str(CUSTOM_SETTINGS.get("world_name") or "사용자 정의 RP 세계").strip()
    world_description = str(CUSTOM_SETTINGS.get("world_description") or "").strip()
    common_rules = CUSTOM_SETTINGS.get("common_rules") or []

    custom_lines = [
        "",
        "[사용자 정의 세계 설정]",
        f"세계 이름: {world_name}",
    ]

    if world_description:
        custom_lines.append(f"세계 설명: {world_description}")

    if common_rules:
        custom_lines.append("세계 규칙:")
        custom_lines.extend(
            f"- {str(rule)}"
            for rule in common_rules
            if str(rule).strip()
        )

    # World rules are independent from the optional common policy and interface mode.
    COMMON_ROLEPLAY_RULES = "\n".join(custom_lines) + "\n"


_load_generic_world_config()
_world_clock = WorldClock(DATA_DIR)

# ============================================================
# v62 기능 ON/OFF 설정
# config/settings.json -> "features"
# 키가 없으면 기본적으로 켜짐(True).
# ============================================================

FEATURE_DEFAULTS = {
    "world_simulation": True,
    "place_movement": True,
    "autonomous_messages": True,
    "bot_to_bot_chat": True,
    "long_term_memory": True,
    "relationships": True,
    "personal_rp": True,
    "dm": True,
    "offline_mention_recovery": True,
    "sleep_system": True,
    "away_system": True,
    "appointments": True,
    "dynamic_events": True,
    "city_events": True,
    "status_dashboard": True,
    "presence": True,
}

FEATURE_DEFAULTS.update({key: default for key, _, default in FEATURES})

def feature_enabled(name):
    features = (
        CUSTOM_SETTINGS.get("features", {})
        if isinstance(CUSTOM_SETTINGS, dict)
        else {}
    )
    if not isinstance(features, dict):
        features = {}

    default = FEATURE_DEFAULTS.get(name, True)
    value = features.get(name, default)

    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "off", "no", "n")

    return bool(value)


def configure_runtime_timezone():
    """settings.json의 IANA timezone 이름을 생활 시간대에 적용한다."""
    global BOT_TIMEZONE_NAME
    global BOT_TIMEZONE
    global KST

    if not GENERIC_CONFIG_ACTIVE:
        return

    requested = str(
        CUSTOM_SETTINGS.get("timezone")
        or "Asia/Seoul"
    ).strip()

    try:
        resolved = ZoneInfo(requested)
    except ZoneInfoNotFoundError:
        # tzdata가 없는 Windows에서도 기본값과 UTC는 안전하게 지원한다.
        normalized = requested.lower()
        if normalized in ("asia/seoul", "kst"):
            resolved = timezone(timedelta(hours=9))
            requested = "Asia/Seoul"
        elif normalized in ("utc", "etc/utc", "gmt"):
            resolved = timezone.utc
            requested = "UTC"
        else:
            raise RuntimeError(
                f"알 수 없는 timezone '{requested}'. "
                "IANA 형식(예: Asia/Seoul, America/New_York)을 사용하세요. "
                "Windows에서 다른 시간대를 쓰려면 `pip install tzdata`가 필요할 수 있습니다."
            )

    BOT_TIMEZONE_NAME = requested
    BOT_TIMEZONE = resolved
    KST = resolved


configure_runtime_timezone()

if GENERIC_CONFIG_ACTIVE:
    from runtime_options import validate_options
    globals().update(validate_options(CUSTOM_SETTINGS.get("runtime", {})))

if GENERIC_CONFIG_ACTIVE:
    log_message(
        "General",
        "RP 생활 시간대:",
        BOT_TIMEZONE_NAME,
    )


# 범용 설정에서는 장소 설정이 뒤늦게 적용되므로 캐릭터별 파생 상태가
# 외부 캐릭터 목록과 정확히 맞도록 한 번 더 동기화한다.
for _character in CHARACTERS:
    current_activity.setdefault(_character, None)
    current_place.setdefault(_character, None)
    sleep_schedule.setdefault(_character, {})
    character_inventory.setdefault(
        _character,
        set(CHARACTERS[_character].get("inventory", [])),
    )
    movement_defer_until.setdefault(_character, None)
    movement_defer_reason.setdefault(_character, None)

# 범용 설정 적용 후 파생 목록을 다시 계산한다.
STATUS_CHARACTER_ORDER = sorted(
    CHARACTERS,
    key=lambda key: CHARACTERS[key]["name"].casefold(),
)

for _character, _config in CHARACTERS.items():
    _config["token"] = os.getenv(_config["token_env"])
    if _config["token"]: _monitor.secrets = [*_monitor.secrets, _config["token"]]

PRIVATE_ROOMS = {
    config["private_room"]: character
    for character, config in CHARACTERS.items()
    if config.get("private_room")
}

room_access_permissions = {character: set() for character in CHARACTERS}
room_invitations = {character: set() for character in CHARACTERS}

# 클라이언트도 캐릭터 키로 관리한다.
clients = {}


async def safe_change_presence(client, *, character=None, status=None, activity=None, reason=""):
    if GENERIC_CONFIG_ACTIVE and not feature_enabled("presence"):
        return False

    """
    Discord Gateway가 재연결/종료 중일 때 presence 전송이 전체 봇을 종료시키지 않도록 한다.
    성공하면 True, 연결 문제로 건너뛰면 False를 반환한다.
    """
    if client is None or client.is_closed():
        return False

    try:
        await client.change_presence(status=status, activity=activity)
        return True
    except (
        discord.ConnectionClosed,
        aiohttp.ClientError,
        ConnectionResetError,
        OSError,
    ) as e:
        if character in CHARACTERS:
            label = CHARACTERS[character]["name"]
        else:
            label = "General"
        detail = f" | {reason}" if reason else ""
        log_message(
            label,
            "Presence 변경 건너뜀 - Discord 연결이 닫히거나 재연결 중:",
            type(e).__name__,
            "-",
            e,
            detail,
        )
        return False
    except Exception as e:
        # Presence 실패는 캐릭터의 생활/대화 루프 전체를 죽일 이유가 없다.
        if character in CHARACTERS:
            label = CHARACTERS[character]["name"]
        else:
            label = "General"
        detail = f" | {reason}" if reason else ""
        log_message(
            label,
            "Presence 변경 오류:",
            type(e).__name__,
            "-",
            e,
            detail,
        )
        return False


# ===================================
# 전역 함수
# -----------------------------------

# 캐릭터 프롬포트 로딩
def load_character_prompt(character):
    config = CHARACTERS[character]

    prompt_path = Path(config["prompt_file"])
    if not prompt_path.is_absolute():
        prompt_path = APP_ROOT / prompt_path

    with open(
        prompt_path,
        "r",
        encoding="utf-8"
    ) as f:
        character_prompt = f.read()

    # 공통 세계관/제약/출력 규칙은 COMMON_ROLEPLAY_RULES에서 중앙 관리한다.
    # 캐릭터 파일에는 앞으로 해당 캐릭터만의 정체, 성격, 말투, 관계,
    # 일상, 외형, 인게임 대사 참고 등을 중심으로 남기면 된다.
    outfit = config.get("current_outfit") or config.get("default_outfit")
    if globals().get('_life') and (_generation_scope.get() or CURRENT_LOG_SCOPE.get() or 'world') == 'world':
        outfit = _life.outfit(character,config,CUSTOM_SETTINGS,character_state[character].get('sleeping'),str(ensure_daily_weather()))
    scope = _generation_scope.get() or CURRENT_LOG_SCOPE.get() or "world"
    thought_rule='\n[관리자 전용 속마음]\n발언 뒤 별도 한 줄에 THOUGHT|현재 자신의 속마음을 짧게 기록한다. 다른 사람의 생각이나 미확인 사실을 확정하지 않는다. 이 줄은 공개 발언이 아니다.' if scope=='world' and globals().get('feature_enabled',lambda key:False)('inner_thoughts') else ''
    calendar_context = world_context(CUSTOM_SETTINGS, now_kst(), config, character) if scope == 'world' and globals().get('_world_clock') else ''
    return thought_rule + "\n" + calendar_context + "\n" + policy_prompt(CUSTOM_SETTINGS, config, scope) + "\n\n" + _generation_feedback.get() + "\n" + character_prompt + (
        "\n\n[복장]\n현재 참고할 복장: " + (outfit or "미지정")
        + "\n현재 복장이 지정되면 기본 복장보다 우선한다. 복장은 장면에 필요할 때만 묘사한다. "
        "지정되지 않은 옷·신발·장신구를 임의로 추가하지 않는다."
    )


# -----------------------------
# 공통 생활/언어/세계 상태 헬퍼
# -----------------------------
def normalize_place_name_from_channel(channel_name):
    for place, actual in PLACE_CHANNEL_NAMES.items():
        if actual == channel_name:
            return place
    return channel_name


def detect_message_language(text):
    text = text or ""
    korean = sum(1 for ch in text if "가" <= ch <= "힣")
    english = sum(1 for ch in text if ch.isascii() and ch.isalpha())
    if english >= 3 and english > korean:
        return "en"
    return "ko"


def language_instruction(language):
    if language == "en":
        return "이번 답변은 반드시 자연스러운 영어로 작성한다."
    return "이번 답변은 자연스러운 한국어로 작성한다."


def _minutes_from_pair(pair):
    return pair[0] * 60 + pair[1]


def _random_minutes(start_pair, end_pair):
    start = _minutes_from_pair(start_pair)
    end = _minutes_from_pair(end_pair)

    # 자정을 넘는 범위도 지원한다.
    # 예: 23:30 ~ 01:30
    if end < start:
        end += 24 * 60

    value = random.randint(start, end)
    return value % (24 * 60)


def roll_sleep_schedule(character, cycle_date=None):
    config = CHARACTERS[character]
    now = now_kst()
    if cycle_date is None:
        cycle_date = (now - timedelta(days=1)).date() if now.hour < 12 else now.date()
    sleep_min = _random_minutes(*config["sleep_start_range"])
    wake_min = _random_minutes(*config["wake_range"])
    sleep_schedule[character] = {
        "date": cycle_date,
        "sleep_minute": sleep_min,
        "wake_minute": wake_min,
    }
    log_message(
        config["name"],
        "오늘 수면 일정:",
        f"{sleep_min//60:02d}:{sleep_min%60:02d}",
        "~",
        f"{wake_min//60:02d}:{wake_min%60:02d}",
    )


def ensure_daily_sleep_schedule(character):
    now = now_kst()
    cycle_date = (now - timedelta(days=1)).date() if now.hour < 12 else now.date()
    sched = sleep_schedule.get(character) or {}
    if sched.get("date") != cycle_date:
        roll_sleep_schedule(character, cycle_date)
    return sleep_schedule[character]


def is_sleep_time(character, now=None):
    now = now or now_kst()
    sched = ensure_daily_sleep_schedule(character)

    minute = now.hour * 60 + now.minute
    sleep_min = sched["sleep_minute"]
    wake_min = sched["wake_minute"]

    # 같은 날 안에서 자고 일어나는 일정
    # 예: 01:42 ~ 07:30
    if sleep_min < wake_min:
        return sleep_min <= minute < wake_min

    # 자정을 넘어가는 일정
    # 예: 23:30 ~ 07:30
    return minute >= sleep_min or minute < wake_min


def update_character_needs(character):
    state = character_state[character]
    now = datetime.now()
    elapsed_h = max(0.0, (now - state["last_need_update"]).total_seconds() / 3600)
    state["last_need_update"] = now

    if state["sleeping"]:
        if not ("_life" in globals() and _life.record(character).get("nap")):
            state["fatigue"] = max(0, state["fatigue"] - (CHARACTERS[character].get("sleep_recovery_per_hour",7) if feature_enabled("actual_sleep") else 14) * elapsed_h)
        state["hunger"] = min(100, state["hunger"] + 4 * elapsed_h)
    else:
        state["fatigue"] = min(100, state["fatigue"] + 5 * elapsed_h)
        state["hunger"] = min(100, state["hunger"] + 7 * elapsed_h)

    f = state["fatigue"]
    h = state["hunger"]
    old_mood = state.get("mood", "평온")

    if f > 85:
        state["mood"] = "매우 피곤함"
        state["mood_reason"] = "피로가 많이 쌓임"
    elif h > 85:
        state["mood"] = "배고픔"
        state["mood_reason"] = "오랫동안 제대로 먹지 못함"
    elif f > 65:
        state["mood"] = "피곤함"
        state["mood_reason"] = "피로가 쌓임"
    elif h > 65:
        state["mood"] = "조금 예민함"
        state["mood_reason"] = "배가 고픔"
    elif mood_is_active(character):
        pass
    elif old_mood in ("매우 피곤함", "피곤함", "배고픔", "짜증남", "기대함", "졸림"):
        state["mood"] = "평온"
        state["mood_reason"] = "시간이 지나 감정이 가라앉음"
        state["mood_until"] = None
    elif random.random() < 0.03:
        state["mood"] = random.choice(["기분 좋음", "조금 예민함", "느긋함", "평온"])
        state["mood_reason"] = "특별한 이유 없는 일상적인 기분 변화"
        state["mood_until"] = datetime.now() + timedelta(hours=1)


def apply_place_need_effects(character):
    """현재 장소/활동이 배고픔과 피로에 실제로 영향을 주게 한다."""
    state = character_state[character]
    if state["sleeping"] or state["away"]:
        return

    now = datetime.now()
    last = state.get("last_place_effect", now)
    elapsed_min = max(0.0, min(10.0, (now - last).total_seconds() / 60))
    state["last_place_effect"] = now
    if elapsed_min <= 0:
        return

    place = current_place.get(character)
    activity = (current_activity.get(character) or "").lower()

    if not feature_enabled("meal_stages") and place in ("식당", "카페") and any(k in activity for k in ["먹", "식사", "마시", "차 ", "커피", "음료"]):
        state["hunger"] = max(0, state["hunger"] - 2.4 * elapsed_min)
        state["mood_reason"] = "먹거나 마시며 쉬는 중"

    own_room = CHARACTERS[character].get("private_room")
    if place in (own_room, "거실") and any(k in activity for k in ["쉬", "눕", "앉", "독서", "읽", "정리"]):
        state["fatigue"] = max(0, state["fatigue"] - 0.8 * elapsed_min)



def set_mood(character, mood, reason, hours=2):
    state = character_state[character]
    state["mood"] = mood
    state["mood_reason"] = reason
    state["mood_until"] = None if hours is None else datetime.now() + timedelta(hours=hours)


def mood_is_active(character):
    until = character_state[character].get("mood_until")
    return until is not None and datetime.now() < until


def get_goal_context(character):
    goals = PERSONAL_GOALS.get(character, [])
    return " / ".join(goals) if goals else "특별히 정해진 개인 목표 없음"


def get_routine_context(character):
    state = character_state[character]
    now = now_kst()
    own_room = CHARACTERS[character].get("private_room")
    wake = state.get("last_wake_at")
    if wake is not None:
        try:
            elapsed = (now - wake).total_seconds() / 60
            if 0 <= elapsed < 30:
                return f"기상 직후 루틴: {own_room or '방'}에서 씻고 소지품을 정리할 시간"
            if 30 <= elapsed < 90:
                return "아침 루틴: 가벼운 식사나 차를 마시고 하루를 시작할 시간"
        except TypeError:
            pass
    if 21 <= now.hour < 24:
        return f"저녁 루틴: 바깥일을 정리하고 {own_room or '자기 공간'} 쪽으로 돌아갈 준비를 할 시간"
    if 0 <= now.hour < 5 and not state.get("sleeping"):
        return f"늦은 밤 루틴: 무리한 외출보다 {own_room or '자기 공간'}에서 쉬는 편이 자연스러움"
    return "특별한 루틴 단계 아님"


def get_dream_context(character):
    state = character_state[character]
    dream = state.get("dream")
    expires = state.get("dream_expires_at")
    if not dream or not expires or datetime.now() >= expires:
        state["dream"] = None
        state["dream_expires_at"] = None
        return "최근 기억나는 꿈 없음"
    return f"최근 꿈: {dream} (꿈은 실제 사건이나 기억이 아님)"


def normalize_expression(text):
    return re.sub(r"[^0-9a-zA-Z가-힣]+", "", (text or "").lower())[:120]


REPEAT_COUNT_PATTERN = re.compile(
    r"(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|스무|백|\d+)\s*번(?:이나|이고|째|쯤)?"
)

_REPEAT_METAPHOR_WORDS = (
    "같은 말",
    "같은 문장",
    "같은 질문",
    "같은 길",
    "같은 얘기",
    "반복",
    "되풀이",
    "거듭",
    "인내심을 시험",
    "몇 번이고",
    "몇 번이나",
)


def extract_global_expression_patterns(text):
    """
    최근 서버 전체에서 전염되기 쉬운 비유/문장 습관만 추려낸다.
    실제 원문 전체를 공유하지 않아 다른 캐릭터가 문장을 그대로 모방하는 일을 줄인다.
    """
    value = (text or "").strip()
    if not value:
        return []

    patterns = []

    has_repeat_word = any(word in value for word in _REPEAT_METAPHOR_WORDS)
    has_count = bool(REPEAT_COUNT_PATTERN.search(value))

    if has_repeat_word and has_count:
        patterns.append("구체적인 횟수를 붙인 반복 비유")

    if any(word in value for word in ("같은 말", "같은 문장", "같은 질문", "같은 길")):
        patterns.append("'같은 X를 반복한다' 구조의 비유")

    if "인내심" in value and ("시험" in value or "테스트" in value):
        patterns.append("'인내심을 시험한다' 비유")

    if "몇 번이고" in value or "몇 번이나" in value:
        patterns.append("'몇 번이고/몇 번이나' 반복 표현")

    return patterns


def remember_expression(character, text):
    key = normalize_expression(text)
    if key:
        recent_expressions[character].append(key)

    for pattern in extract_global_expression_patterns(text):
        if not recent_global_expression_patterns or recent_global_expression_patterns[-1] != pattern:
            recent_global_expression_patterns.append(pattern)


def expression_was_recent(character, text):
    key = normalize_expression(text)
    return bool(key) and key in recent_expressions[character]


def get_recent_expression_context(character):
    """
    모델에게 금지 표현의 구체 예시를 다시 보여주지 않는다.
    최근 자기 표현의 존재 여부만 알려 문장 구조 재사용을 줄인다.
    """
    own_count = len(recent_expressions[character])
    global_count = len(recent_global_expression_patterns)

    return (
        f"최근 자기 표현 기록 {min(own_count, 6)}개, "
        f"최근 공용 표현 패턴 기록 {min(global_count, 6)}개. "
        "이 기록은 문장 구조와 말버릇의 재사용을 피하기 위한 참고 정보다. "
        "상대가 이전에 같은 말을 했다는 사실을 뜻하지 않는다."
    )

def _normalize_dialogue_for_repeat_check(value):
    value = (value or "").strip().lower()

    # "A → B:" / "A:" 같은 화자 표시는 비교 대상에서 제거한다.
    if ":" in value:
        value = value.split(":", 1)[1].strip()

    # Discord 멘션과 괄호 행동지문을 제거한다.
    value = re.sub(r"<@!?\d+>", " ", value)
    value = re.sub(r"\([^)]*\)", " ", value)

    # 비교에는 문자/숫자만 사용한다.
    return re.sub(r"[^0-9a-zA-Z가-힣]+", "", value)


def has_real_repetition_evidence(evidence_text):
    """
    최근 대화에 실제로 같은/매우 유사한 발언이 두 번 이상 있을 때만 True.

    단순히 프롬프트 안에 '반복', '몇 번' 같은 단어가 있다는 이유로
    반복 근거가 있다고 판단하지 않는다.
    """
    raw_lines = [
        line.strip()
        for line in (evidence_text or "").splitlines()
        if line.strip()
    ]

    values = []
    for line in raw_lines:
        normalized = _normalize_dialogue_for_repeat_check(line)

        # 너무 짧은 감탄/단답은 우연히 겹치기 쉬워 근거로 쓰지 않는다.
        if len(normalized) < 5:
            continue

        values.append(normalized)

    if len(values) < 2:
        return False

    # 완전히 같은 발언이 두 번 이상 있으면 확실한 반복 근거.
    seen = set()
    for value in values:
        if value in seen:
            return True
        seen.add(value)

    # 길이가 충분한 문장끼리 거의 같은 경우도 반복으로 인정.
    # 외부 라이브러리 없이 공통 접두/부분포함 정도만 보수적으로 확인한다.
    for i, left in enumerate(values):
        for right in values[i + 1:]:
            if min(len(left), len(right)) < 10:
                continue

            shorter, longer = (
                (left, right)
                if len(left) <= len(right)
                else (right, left)
            )

            # 한 문장이 다른 문장을 거의 그대로 포함하는 경우.
            if (
                shorter in longer
                and len(shorter) / max(len(longer), 1) >= 0.82
            ):
                return True

    return False


_UNGROUNDED_REPEAT_CLAIM_PATTERNS = (
    r"또\s*(?:똑같은|같은)\s*(?:말|얘기|이야기|문장|질문)",
    r"(?:계속|자꾸)\s*(?:똑같은|같은)\s*(?:말|얘기|이야기|문장|질문)",
    r"(?:몇\s*번|몇\s*번째|수차례|여러\s*번).{0,14}(?:말|얘기|이야기|묻|질문|설명)",
    r"(?:말|얘기|이야기|질문).{0,14}(?:몇\s*번|몇\s*번째|수차례|여러\s*번)",
    r"(?:또|계속|자꾸).{0,10}(?:말하|묻|물어|얘기하|설명하)",
    r"(?:반복하|되풀이하|되풀이|거듭\s*말)",
    r"(?:아까도|전에도|방금도).{0,18}(?:말했|물었|얘기했|설명했)",
)


def _contains_unsupported_repeat_claim(value):
    return any(
        re.search(pattern, value or "", flags=re.IGNORECASE)
        for pattern in _UNGROUNDED_REPEAT_CLAIM_PATTERNS
    )


def sanitize_ungrounded_repeat_count(text, evidence_text=""):
    """
    최근 대화에 실제 반복 근거가 없으면
    '또 같은 말을 하네', '몇 번을 말해야 해' 같은 허위 반복 주장을 제거한다.

    근거가 실제로 확인되는 경우에는 원문을 유지한다.
    """
    value = (text or "").strip()
    evidence = evidence_text or ""

    if not value:
        return value

    if has_real_repetition_evidence(evidence):
        return value

    # 우선 근거 없는 구체 횟수 표현을 중립화한다.
    def _replace_count(match):
        phrase = match.group(0)
        if phrase in evidence:
            return phrase
        return ""

    value = REPEAT_COUNT_PATTERN.sub(_replace_count, value)

    # 줄 단위로 근거 없는 반복 비난/주장을 제거한다.
    kept_lines = []
    removed_lines = []

    for line in value.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # 행동지문 안의 단순 반복 동작(예: "손가락으로 탁자를 반복해서 두드린다")까지
        # 과하게 지우지 않도록, 상대 발언에 대한 반복 주장 패턴만 제거한다.
        if _contains_unsupported_repeat_claim(stripped):
            removed_lines.append(stripped)
            continue

        kept_lines.append(line)

    cleaned = "\n".join(kept_lines).strip()

    if cleaned:
        return cleaned

    # 답변 전체가 반복 주장 한 줄뿐이었다면 빈 메시지를 보내지 않도록
    # 공격적인 반복 주장을 중립적인 짧은 반응으로 바꾼다.
    if removed_lines:
        return "그 말은 알겠어."

    return value


def get_needs_text(character):
    update_character_needs(character)
    state = character_state[character]
    return (
        f'기분={state["mood"]}, '
        f'배고픔={int(state["hunger"])}%, '
        f'피로={int(state["fatigue"])}%'
    )


def ensure_daily_weather():
    """하루 단위 범용 환경 상태를 만든다. 모든 캐릭터가 같은 날씨/기온을 공유한다."""
    today = now_kst().date()
    if daily_world["date"] != today:
        daily_world["date"] = today
        weather = random.choices(
            ["맑음", "흐림", "비", "쌀쌀함", "바람이 강함"],
            weights=[35, 25, 15, 15, 10],
            k=1,
        )[0]

        # 1940년대 대체역사 뉴욕의 계절감을 위한 느슨한 월별 기온 범위
        month_ranges = {
            1: (-6, 7), 2: (-5, 8), 3: (1, 13), 4: (7, 19),
            5: (13, 24), 6: (18, 29), 7: (21, 32), 8: (20, 31),
            9: (15, 27), 10: (9, 20), 11: (3, 14), 12: (-3, 9),
        }
        low, high = month_ranges[today.month]
        temperature = random.randint(low, high)
        if weather == "비":
            temperature -= random.randint(1, 3)
        elif weather == "쌀쌀함":
            temperature -= random.randint(2, 5)

        if feature_enabled("seasonal_weather"):
            weather, temperature = seasonal_weather(CUSTOM_SETTINGS, now_kst())
        daily_world["weather"] = weather
        daily_world["temperature"] = temperature
        log_message(
            "General",
            "오늘 환경:",
            weather,
            "|",
            f"{temperature}°C",
        )
    return daily_world["weather"]


def get_world_temperature():
    ensure_daily_weather()
    return daily_world["temperature"]


def get_active_place_state(place):
    item = place_state.get(place)
    if not item:
        return "특별히 남아 있는 상황 없음"
    if datetime.now() >= item["expires_at"]:
        place_state.pop(place, None)
        return "특별히 남아 있는 상황 없음"
    return item["text"]


def set_place_state(place, text, hours=4):
    if not text:
        return
    place_state[place] = {
        "text": text,
        "expires_at": datetime.now() + timedelta(hours=hours),
    }


def get_place_items_text(place):
    items = sorted(place_items.get(place, set()))
    return ", ".join(items) if items else "특별히 놓여 있는 개인 소지품 없음"


def maybe_update_inventory_from_text(character, text, place, target_character=None):
    """행동지문/대사에서 명확히 드러난 간단한 소지품 이동만 반영한다."""
    if not text or not place:
        return
    if feature_enabled("item_loans") and target_character is not None and not completed_transfer(text):
        return
    low = text.lower()
    owned = list(character_inventory.get(character, set()))

    # 다른 캐릭터에게 건네는 경우
    if target_character is not None and any(k in low for k in ["건네", "준다", "빌려", "내민", "pass", "give", "lend"]):
        for item in owned:
            if item in text:
                character_inventory[character].discard(item)
                character_inventory[target_character].add(item)
                if feature_enabled("item_loans"):
                    _life.transfer(item, character, target_character, borrowed=any(w in low for w in ("빌려", "lend")))
                log_message("General", "소지품 전달:", CHARACTERS[character]["name"], item, "→", CHARACTERS[target_character]["name"])
                return

    # 장소에 내려놓거나 두고 감
    if any(k in low for k in ["내려놓", "두고", "놓아두", "놓고", "테이블에 놓", "leave", "set down"]):
        for item in owned:
            if item in text:
                character_inventory[character].discard(item)
                place_items[place].add(item)
                log_message("General", "소지품 놓음:", CHARACTERS[character]["name"], item, "@", place)
                return

    # 장소에 있는 자기 물건을 다시 챙김
    if any(k in low for k in ["챙겨", "집어", "가져", "주워", "pick up", "take"]):
        for item in list(place_items.get(place, set())):
            if item in text:
                place_items[place].discard(item)
                character_inventory[character].add(item)
                log_message("General", "소지품 회수:", CHARACTERS[character]["name"], item, "@", place)
                return




CHARACTER_NAME_ALIASES = {
    "apollo": ["apollo", "아폴로"],
    "doorman": ["the doorman", "doorman", "도어맨", "더 도어맨"],
    "graves": ["graves", "그레이브즈", "그레이브스"],
    "mina": ["mina", "미나"],
    "paige": ["paige", "페이지"],
    "pocket": ["pocket", "포켓", "arin", "아린"],
    "rem": ["rem", "렘"],
    "victor": ["victor", "빅터"],
    "drifter": ["drifter", "드리프터"],
    "dynamo": ["dynamo", "다이나모"],
    "venator": ["venator", "베네터", "퀸", "quinn", "퀸 로크", "quinn rourke"],
    "abrams": ['abrams', '에이브람스'],
    "bebop": ['bebop', '비밥'],
    "billy": ['billy', '빌리'],
    "calico": ['calico', '칼리코'],
    "celeste": ['celeste', '셀레스트'],
    "gray_talon": ['gray talon', 'grey talon', '그레이 탈론', '그레이탈론'],
    "haze": ['haze', '헤이즈'],
    "holliday": ['holliday', '홀리데이'],
    "infernus": ['infernus', '인페르누스'],
    "ivy": ['ivy', '아이비'],
    "kelvin": ['kelvin', '켈빈'],
    "lady_geist": ['lady geist', 'geist', '레이디 가이스트', '가이스트'],
    "lash": ['lash', '래시'],
    "mcginnis": ['mcginnis', '맥기니스', '맥기니스'],
    "mirage": ['mirage', '미라지'],
    "paradox": ['paradox', '패러독스'],
    "seven": ['seven', '세븐'],
    "shiv": ['shiv', '쉬브'],
    "silver": ['silver', '실버'],
    "vindicta": ['vindicta', '빈딕타'],
    "vyper": ['vyper', '바이퍼'],
    "wraith": ['wraith', '레이스'],

}


def _text_mentions_character(text, character):
    low = (text or "").lower()
    aliases = CHARACTER_NAME_ALIASES.get(
        character,
        [CHARACTERS[character]["name"].lower()]
    )
    return any(alias.lower() in low for alias in aliases)


def _find_character_in_text(text, exclude=None):
    for character in CHARACTERS:
        if character == exclude:
            continue
        if _text_mentions_character(text, character):
            return character
    return None


def _extract_note_content(incoming_text, target_character, fallback_text):
    """사용자의 '누구에게 ~라고 메모 남겨줘' 같은 요청에서 메모 핵심 내용을 뽑는다."""
    text = (incoming_text or "").strip()
    if not text:
        return (fallback_text or "").strip()

    # 대상 이름/조사를 제거
    aliases = sorted(
        CHARACTER_NAME_ALIASES.get(
            target_character,
            [CHARACTERS[target_character]["name"]]
        ),
        key=len,
        reverse=True,
    )
    working = text
    for alias in aliases:
        working = re.sub(
            rf"(?i){re.escape(alias)}\s*(한테|에게|한테는|에게는)?",
            "",
            working,
            count=1,
        )

    # 메모/쪽지/편지 뒤의 부탁 표현은 제거
    parts = re.split(r"(?:메모|쪽지|편지|note|letter)", working, maxsplit=1, flags=re.IGNORECASE)
    candidate = parts[0].strip(" ,.!?…")
    candidate = re.sub(r"^(?:그냥\s*)?(?:좀\s*)?", "", candidate).strip()
    candidate = re.sub(r"(?:남겨\s*줄래|남겨줘|써\s*줄래|써줘|전해\s*줄래|전해줘).*$", "", candidate).strip()

    # 너무 짧거나 의미가 없으면 실제 캐릭터 답변을 보존한다.
    if len(candidate) < 2:
        return (fallback_text or "").strip()
    return candidate[:300]



def scoped_user_subject(user_id, *, guild_id=None, character=None, is_dm=False):
    if is_dm:
        return f"dm:{character}:user:{int(user_id)}"
    if guild_id is None:
        return user_subject_key(user_id)

    mode = "world" if is_main_world_guild_id(guild_id) else "rp"
    return f"{mode}:{guild_id}:{character}:user:{int(user_id)}"


def scoped_history_key(channel_id, *, guild_id=None, character=None, user_id=None, is_dm=False):
    if is_dm:
        return f"dm:{character}:{int(user_id)}"
    if guild_id is None or is_main_world_guild_id(guild_id):
        return channel_id

    return f"rp:{guild_id}:{character}:{channel_id}:{int(user_id) if user_id is not None else 'shared'}"


def user_subject_key(user_id):
    return f"user:{int(user_id)}"


def upsert_user_profile(user_id, display_name):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_profiles (user_id, display_name, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            display_name=excluded.display_name,
            updated_at=excluded.updated_at
    """, (str(user_id), display_name, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()


def migrate_legacy_user_memories(character, user_id, display_name):
    new_subject = user_subject_key(user_id)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT display_name FROM user_profiles WHERE user_id=?", (str(user_id),))
    row = cursor.fetchone()
    aliases = {display_name}
    if row and row[0]:
        aliases.add(row[0])
    changed = 0
    for alias in aliases:
        cursor.execute("""
            UPDATE memories SET subject=?
            WHERE character=? AND subject=?
        """, (new_subject, character, alias))
        changed += max(0, cursor.rowcount)
    conn.commit()
    conn.close()
    if changed:
        log_message(CHARACTERS[character]["name"], "기존 닉네임 기억을 user_id로 이전:", ", ".join(sorted(aliases)), "→", new_subject, f"({changed}개)")
    return new_subject


def save_knowledge(character, fact, source_type="observed", source_name=None, source_key=None, secret=False):
    fact = (fact or "").strip()[:300]
    if not fact:
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM character_knowledge WHERE character=? AND fact=? LIMIT 1", (character, fact))
    existing = cursor.fetchone()
    if existing is None:
        cursor.execute("""
            INSERT INTO character_knowledge
            (character, fact, source_type, source_name, source_key, secret, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (character, fact, source_type, source_name or "", source_key or "", 1 if secret else 0, datetime.now(timezone.utc).isoformat()))
        knowledge_id = cursor.lastrowid
        log_message(
            CHARACTERS[character]["name"],
            "비밀 지식 저장:" if secret else "지식 저장:",
            f"id={knowledge_id}",
            "| 출처:", source_name or source_type,
            "|", fact[:140],
        )
    conn.commit()
    conn.close()


def get_knowledge_context(character, limit=8):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT fact, source_type, source_name, secret
        FROM character_knowledge WHERE character=? ORDER BY id DESC LIMIT ?
    """, (character, limit))
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        return "별도로 알고 있는 정보 없음"
    out = []
    for fact, source_type, source_name, secret in reversed(rows):
        src = f" / 출처:{source_name}" if source_name else ""
        sec = " / 비밀" if secret else ""
        out.append(f"{fact} ({source_type}{src}{sec})")
    return "\n".join(out)


def maybe_store_user_knowledge(character, user_id, display_name, text):
    low = (text or "").lower()
    secret_words = ["비밀", "아무한테도 말", "말하지 마", "말하지마", "secret", "don't tell", "do not tell"]
    fact_words = ["기억해", "알아둬", "사실", "실은", "remember", "actually"]
    if any(k in low for k in secret_words + fact_words):
        save_knowledge(character, text, "직접 들음", display_name, user_subject_key(user_id), any(k in low for k in secret_words))


def maybe_store_overheard_knowledge(listener, speaker_character, text, place):
    if current_place.get(listener) != place or current_place.get(speaker_character) != place:
        return
    low = (text or "").lower()
    meaningful = ["비밀", "약속", "사실", "미안", "고마", "걱정", "찾고", "잃어", "secret", "promise", "sorry", "thank"]
    if any(k in low for k in meaningful):
        fact = f"{CHARACTERS[speaker_character]['name']}가 {place}에서 이렇게 말했다: {text[:180]}"
        save_knowledge(listener, fact, "목격/엿들음", CHARACTERS[speaker_character]["name"], speaker_character, "비밀" in low or "secret" in low)
        log_message(CHARACTERS[listener]["name"], "목격 지식 저장:", CHARACTERS[speaker_character]["name"], "@", place)


def create_note(from_character, to_character, content, place=None):
    if not to_character or to_character == from_character or not content:
        return None
    place = place or CHARACTERS[to_character].get("private_room")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO character_notes
        (from_character, to_character, place, content, created_at, read_at)
        VALUES (?, ?, ?, ?, ?, NULL)
    """, (
        from_character,
        to_character,
        place or "",
        content[:300],
        datetime.now(timezone.utc).isoformat()
    ))
    note_id = cursor.lastrowid
    conn.commit()
    conn.close()

    log_message(
        "General",
        "메모 저장:",
        f"id={note_id}",
        "|",
        CHARACTERS[from_character]["name"],
        "→",
        CHARACTERS[to_character]["name"],
        "| 위치:",
        place or "장소 미정",
        "| 내용:",
        content[:120],
    )
    return note_id


def maybe_create_note_from_exchange(character, incoming_text, reply_text, requester_name=None):
    """
    사용자가 '누구에게 메모/쪽지/편지를 남겨 달라'고 명시적으로 요청한 경우에만
    character_notes를 생성한다.

    예:
    - "Paige에게 메모 남겨줘"
    - "페이지한테 쪽지 써줘"
    - "Pocket에게 편지 전해줘"

    단순히 대화 중 '메모', '편지' 같은 단어가 등장하거나
    캐릭터의 답변에 다른 캐릭터 이름이 포함된 것만으로는 생성하지 않는다.
    """
    incoming = (incoming_text or "").strip()
    if not incoming:
        return None

    low = incoming.lower()

    # 메모/쪽지/편지라는 명사가 실제 사용자 요청문 안에 있어야 한다.
    note_words = r"(?:메모|쪽지|편지|note|letter)"
    if re.search(note_words, low, flags=re.IGNORECASE) is None:
        return None

    # 그리고 실제로 '남겨/써/전해/보내' 같은 전달 의도가 명시되어야 한다.
    request_patterns = [
        rf"{note_words}\s*(?:를|을)?\s*(?:남겨|남기|써|쓰|전해|전하|보내|붙여)",
        rf"(?:남겨|남기|써|쓰|전해|전하|보내|붙여).{{0,20}}{note_words}",
        r"(?:leave|write|send).{0,30}(?:note|letter)",
    ]
    if not any(re.search(pattern, incoming, flags=re.IGNORECASE) for pattern in request_patterns):
        return None

    # 대상 캐릭터 역시 사용자의 요청문 안에 직접 언급되어야 한다.
    # GPT 답변에 우연히 다른 캐릭터 이름이 나왔다고 메모를 만들지 않는다.
    target = _find_character_in_text(incoming, exclude=character)
    if target is None:
        log_message(
            CHARACTERS[character]["name"],
            "메모 요청 문구는 감지했지만 대상 캐릭터가 명시되지 않음:",
            incoming[:140],
        )
        return None

    place = CHARACTERS[target].get("private_room")
    content = _extract_note_content(incoming, target, reply_text)

    # '내가 ...'는 요청자 기준임을 메모에서 명확히 해 둔다.
    if requester_name and re.search(r"(^|\s)내가(\s|$)", content):
        content = re.sub(
            r"(^|\s)내가(\s|$)",
            rf"\1{requester_name}이(가)\2",
            content,
            count=1,
        )

    log_message(
        CHARACTERS[character]["name"],
        "명시적 메모 요청 감지:",
        "대상=", CHARACTERS[target]["name"],
        "| 위치=", place or "장소 미정",
    )
    return create_note(character, target, content, place)


def delete_character_note(note_id):
    """character_notes의 특정 실제 메모/쪽지를 ID로 삭제하고 삭제된 정보를 반환한다."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, from_character, to_character, place, content, read_at
        FROM character_notes
        WHERE id=?
    """, (int(note_id),))
    row = cursor.fetchone()

    if row is None:
        conn.close()
        return None

    cursor.execute(
        "DELETE FROM character_notes WHERE id=?",
        (int(note_id),)
    )
    conn.commit()
    conn.close()
    return row


def get_unread_notes_text(character):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    place = current_place.get(character) or ""
    cursor.execute("""
        SELECT id, from_character, place, content
        FROM character_notes
        WHERE to_character=? AND read_at IS NULL AND (place='' OR place=?)
        ORDER BY id ASC LIMIT 3
    """, (character, place))
    rows = cursor.fetchall()
    conn.close()
    if not rows:
        return "새로 남겨진 메모 없음"
    parts = []
    for note_id, sender, note_place, content in rows:
        sender_name = CHARACTERS.get(sender, {}).get("name", sender)
        parts.append(f"#{note_id} {sender_name}가 {note_place or '어딘가'}에 남김: {content}")
    return " / ".join(parts)


async def maybe_deliver_unread_note(character):
    state = character_state[character]
    if state.get("sleeping") or state.get("away"):
        return False

    place = current_place.get(character)
    if not place:
        return False

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, from_character, place, content
        FROM character_notes
        WHERE to_character=? AND read_at IS NULL AND (place='' OR place=?)
        ORDER BY id ASC LIMIT 1
    """, (character, place))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return False

    note_id, sender, note_place, content = row
    sender_name = CHARACTERS.get(sender, {}).get("name", sender)

    log_message(
        CHARACTERS[character]["name"],
        "읽지 않은 메모 발견:",
        f"id={note_id}",
        "| 보낸 사람:", sender_name,
        "| 위치:", note_place or place,
    )

    client = clients.get(character)
    channel = find_place_channel(client, place) if client else None
    if channel is None:
        log_message(
            CHARACTERS[character]["name"],
            "메모 전달 보류:",
            f"id={note_id}",
            "| 현재 장소 채널을 찾지 못함:",
            place,
        )
        return False

    prompt = load_character_prompt(character)
    input_text = f"""
{CHARACTERS[character]['name']}가 현재 {place}에서 {sender_name}가 남긴 메모/편지를 발견했다.
메모 내용: {content}

읽은 직후의 아주 짧은 자연스러운 행동이나 혼잣말을 만든다.
메모의 내용은 실제로 전달된 정보지만, 메모 작성자의 주장 자체가 사실인지까지 자동으로 확정하지 않는다.
설명 없이 실제 롤플레이 문장만 출력한다.
{ROLEPLAY_OUTPUT_RULES}
"""
    try:
        response = openai_client.responses.create(
            model="gpt-5.6-luna",
            instructions=prompt,
            input=input_text
        )
        reaction = clean_generated_text(response.output_text)
        reaction = sanitize_ungrounded_repeat_count(
            reaction,
            evidence_text=f"{sender_name}\n{content}",
        )
        if not reaction:
            log_message(
                CHARACTERS[character]["name"],
                "메모 반응 생성 실패:",
                f"id={note_id}",
                "| 빈 응답",
            )
            return False

        await channel.send(reaction)
        remember_expression(character, reaction)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE character_notes SET read_at=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), note_id)
        )
        conn.commit()
        conn.close()

        log_message(
            CHARACTERS[character]["name"],
            "메모 확인 완료:",
            f"id={note_id}",
            "|", sender_name,
            "→",
            CHARACTERS[character]["name"],
        )
        return True

    except Exception as e:
        # 실패 시 read_at을 찍지 않아 다음 루프에서 다시 시도한다.
        log_message(
            CHARACTERS[character]["name"],
            "메모 처리 오류:",
            f"id={note_id}",
            "|",
            type(e).__name__,
            "-",
            e,
        )
        return False


def maybe_update_place_state_from_text(place, text):
    if not place or not text:
        return
    low = text.lower()
    effects = []
    checks = [
        (["창문을 열", "창문 열"], "창문이 열려 있다."),
        (["창문을 닫", "창문 닫"], "창문이 닫혀 있다."),
        (["불을 켜", "등을 켜"], "불이 켜져 있다."),
        (["불을 끄", "등을 끄"], "불이 꺼져 있다."),
        (["의자를 옮", "의자 옮"], "의자 하나의 위치가 바뀌어 있다."),
    ]
    for keys, effect in checks:
        if any(k in low for k in keys):
            effects.append(effect)
    if effects:
        old = get_active_place_state(place)
        base = "" if old == "특별히 남아 있는 상황 없음" else old + " "
        set_place_state(place, base + " ".join(effects), hours=6)


def clear_city_event_state(log_reason=None):
    daily_world["city_event"] = None
    daily_world["city_event_place"] = None
    daily_world["city_event_source"] = None
    daily_world["city_event_started_at"] = None
    daily_world["city_event_expires_at"] = None

    if log_reason:
        log_message("General", "도시 사건 종료:", log_reason)


def set_city_event_state(event, duration_minutes, place=None, source="manual"):
    event = (event or "").strip()
    if not event:
        raise ValueError("도시 사건 내용이 비어 있습니다.")

    try:
        duration_minutes = int(duration_minutes)
    except (TypeError, ValueError):
        raise ValueError("도시 사건 지속시간이 올바르지 않습니다.")

    if not (10 <= duration_minutes <= 1440):
        raise ValueError("도시 사건 지속시간은 10~1440분 사이여야 합니다.")

    if place and place not in PLACE_CHANNELS:
        raise ValueError("알 수 없는 장소입니다.")

    now = datetime.now()
    daily_world["city_event"] = event[:1200]
    daily_world["city_event_place"] = place or None
    daily_world["city_event_source"] = source
    daily_world["city_event_started_at"] = now
    daily_world["city_event_expires_at"] = now + timedelta(minutes=duration_minutes)

    log_message(
        "General",
        "도시 사건 설정:",
        event[:180],
        "| 장소:",
        place or "도시 전체",
        "| 지속:",
        f"{duration_minutes}분",
        "| 출처:",
        source,
    )


def cleanup_city_event():
    expires = daily_world.get("city_event_expires_at")
    if expires and datetime.now() >= expires:
        clear_city_event_state("지속시간 만료")


def get_city_event_text():
    cleanup_city_event()
    event = daily_world.get("city_event")
    if not event:
        return "특별한 도시 소식 없음"

    place = daily_world.get("city_event_place")
    if place:
        return f"[{place}] {event}"
    return event


def get_city_event_status_text():
    cleanup_city_event()

    event = daily_world.get("city_event")
    if not event:
        return "현재 특별한 도시 사건 없음"

    lines = []
    place = daily_world.get("city_event_place")
    if place:
        lines.append(f"📍 **{place}**")

    lines.append(event)

    expires = daily_world.get("city_event_expires_at")
    if expires:
        remaining_seconds = max(0, int((expires - datetime.now()).total_seconds()))
        hours, remainder = divmod(remaining_seconds, 3600)
        minutes = remainder // 60

        if hours > 0:
            remaining_text = f"약 {hours}시간 {minutes}분"
        else:
            remaining_text = f"약 {minutes}분"

        lines.append(f"⏳ 남은 시간: {remaining_text}")

    source = daily_world.get("city_event_source")
    if source == "manual":
        lines.append("🛠️ 관리자 수동 설정")
    elif source == "auto":
        lines.append("🌆 자동 도시 사건")

    return "\n".join(lines)


def get_world_season(now=None):
    now = now or now_kst()
    if feature_enabled("world_calendar"):
        name = season_profile(CUSTOM_SETTINGS, now)[0]
        return {"봄":"spring", "여름":"summer", "가을":"autumn", "겨울":"winter"}.get(name, name)
    month = now.month
    if month in (3, 4, 5):
        return "spring"
    if month in (6, 7, 8):
        return "summer"
    if month in (9, 10, 11):
        return "autumn"
    return "winter"


def roll_small_city_event():
    """시간대/날씨/계절에 맞는 작은 도시 생활 사건을 선택한다."""
    period = get_time_period()
    weather = ensure_daily_weather()
    season = get_world_season()

    events = [
        ("광장 한쪽에서 작은 장터가 열렸다.", None, None, None),
        ("근처 가게 몇 곳이 평소보다 일찍 문을 닫는다는 소식이 돌고 있다.", ("evening", "night", "late_night"), None, None),
        ("거리의 가로등 하나가 고장 나 그 구간이 유난히 어둡다.", ("evening", "night", "late_night"), None, None),
        ("도서관에 새로 들어온 책 몇 권이 사람들의 화제가 되고 있다.", None, None, None),
        ("카페에서 오늘만 파는 작은 디저트가 있다는 이야기가 퍼졌다.", ("morning", "day", "evening"), None, None),
        ("신문 가판대에서 평소보다 사람들이 오래 머물며 한 기사를 두고 수군거린다.", ("morning", "day"), None, None),
        ("거리 악사가 익숙한 곡을 연주해 지나가던 사람들이 잠시 발을 멈춘다.", ("day", "evening"), None, None),
        ("광장 근처에서 잃어버린 물건을 찾는다는 전단이 붙기 시작했다.", None, None, None),
        ("한 골목의 수도관 공사 때문에 사람들이 조금 돌아가야 한다.", ("morning", "day"), None, None),
        ("식당가에서 오늘 들어온 재료가 좋다는 소문이 퍼졌다.", ("day", "evening"), None, None),
        ("늦은 시간인데도 바로네스 호텔 앞에 낯선 마차 한 대가 오래 서 있다.", ("night", "late_night"), None, None),
        ("새벽 거리에서 우유 배달 수레가 평소보다 일찍 돌아다닌다.", ("late_night", "morning"), None, None),
        ("오후부터 바람이 강해져 거리의 간판이 자주 흔들린다.", ("day", "evening"), ("바람이 강함", "흐림", "비"), None),
        ("비를 피하려는 사람들이 처마와 카페 안으로 몰리고 있다.", None, ("비",), None),
        ("젖은 도로 때문에 마차와 자동차가 평소보다 천천히 움직인다.", None, ("비",), None),
        ("맑은 날씨 덕분에 광장 벤치와 야외 자리가 평소보다 붐빈다.", ("day",), ("맑음",), None),
        ("쌀쌀한 바람 탓에 따뜻한 음료를 찾는 사람이 부쩍 늘었다.", None, None, ("autumn", "winter")),
        ("낙엽이 바람에 몰려 거리 모퉁이마다 작은 더미를 만들고 있다.", None, None, ("autumn",)),
        ("이른 추위 때문에 몇몇 상점이 난로를 평소보다 일찍 피웠다.", None, None, ("autumn", "winter")),
        ("눈이 녹지 않은 그늘진 길목이 미끄럽다는 이야기가 돈다.", None, None, ("winter",)),
        ("따뜻해진 날씨에 꽃을 파는 노점이 광장 근처에 자리를 잡았다.", ("day",), None, ("spring",)),
        ("봄비 뒤의 축축한 흙냄새가 거리 곳곳에 남아 있다.", None, ("비",), ("spring",)),
        ("더운 날씨 때문에 얼음과 차가운 음료를 찾는 손님이 늘었다.", ("day", "evening"), None, ("summer",)),
        ("해가 진 뒤에도 더위가 남아 옥상과 거리로 나온 사람이 많다.", ("evening", "night"), None, ("summer",)),
    ]

    candidates = []
    for event, periods, weathers, seasons in events:
        if periods and period not in periods:
            continue
        if weathers and weather not in weathers:
            continue
        if seasons and season not in seasons:
            continue
        candidates.append(event)

    if not candidates:
        candidates = [event for event, _, _, _ in events]

    event = random.choice(candidates)
    duration_minutes = random.randint(4 * 60, 8 * 60)
    set_city_event_state(
        event,
        duration_minutes,
        place=None,
        source="auto",
    )
    log_message("General", "작은 도시 사건:", event, "|", period, weather, season)


def maybe_update_shared_object_from_user(text, place):
    """사용자가 장소에 명확히 두고 가는 흔한 물건을 공용 장소 상태로 남긴다."""
    if not text or not place:
        return
    low = text.lower()
    leave_words = ["두고 갈", "두고가", "놓고 갈", "놓고가", "여기 둘", "내려놓", "leave", "set down"]
    if not any(k in low for k in leave_words):
        return
    common_items = ["책", "편지", "쪽지", "우산", "가방", "손수건", "꽃", "신문", "컵", "열쇠", "상자"]
    for item in common_items:
        if item in text:
            place_items[place].add(item)
            log_message("General", "사용자 물건 놓임:", item, "@", place)
            return


def get_character_context(character):
    state = character_state[character]
    weather = ensure_daily_weather()
    temperature = get_world_temperature()
    inventory = ", ".join(sorted(character_inventory.get(character, set()))) or "특별히 들고 있는 소지품 없음"
    habits = ", ".join(CHARACTERS[character].get("habits", [])) or "특별히 정해진 습관 없음"
    return (
        f"{_life.context(character, CUSTOM_SETTINGS) if '_life' in globals() else ''}\n"
        f"{_world_actions.context(character, current_place.get(character) or '', CUSTOM_SETTINGS, PLACE_INFO.get(current_place.get(character),{}), bool(_life.busy(character,CUSTOM_SETTINGS))) if '_world_actions' in globals() and (CURRENT_LOG_SCOPE.get() or 'world') == 'world' else ''}\n"
        f"현재 생활 상태: {get_needs_text(character)}\n"
        f"기분의 이유: {state.get('mood_reason', '특별한 이유 없음')}\n"
        f"수면 여부: {'자는 중' if state['sleeping'] else '깨어 있음'}\n"
        f"외출 여부: {'외출 중' if state['away'] else '서버 내에 있음'}\n"
        f"오늘 날씨: {weather}\n"
        f"현재 기온: {temperature}°C\n"
        f"현재 소지품: {inventory}\n"
        f"생활 습관: {habits}\n"
        f"개인 목표: {get_goal_context(character)}\n"
        f"현재 루틴: {get_routine_context(character)}\n"
        f"꿈 상태: {get_dream_context(character)}\n"
        f"현재 도시의 작은 소식: {get_city_event_text()}\n"
        f"가까운 약속/예정: {get_appointments_text(character)}\n"
        f"받은 메모: {get_unread_notes_text(character)}"
    )



# -----------------------------
# 관리자 지정 사용자-캐릭터 고정 관계
# -----------------------------
SIMPLE_RELATIONS = {
    "acquaintance": ("지인", "이 사용자를 알고 지내는 지인으로 대한다. 지나치게 친밀하거나 적대적으로 가정하지 않는다."),
    "friend": ("친구", "이 사용자를 친구로 대한다. 기본적인 친근함과 신뢰가 있지만 캐릭터 성격은 그대로 유지한다."),
    "close_friend": ("친한 친구", "이 사용자를 오래 알고 지낸 친한 친구로 대한다. 비교적 편하게 말하고 신뢰하는 편이다."),
    "trusted": ("신뢰하는 사람", "이 사용자를 신뢰할 만한 사람으로 여긴다. 그래도 모든 주장이나 부탁을 자동으로 받아들이지는 않는다."),
    "guarded": ("경계하는 사이", "이 사용자를 조심스럽게 대한다. 쉽게 속내를 보이거나 말을 곧이곧대로 믿지 않는다."),
    "uncomfortable": ("불편한 사이", "이 사용자와 있을 때 약간의 거리감이나 불편함이 있다. 노골적인 적대감으로 과장하지 않는다."),
    "benefactor": ("은인", "이 사용자에게 도움을 받은 중요한 사람이라는 인식과 감사가 있다."),
    "rival": ("라이벌", "이 사용자를 경쟁 상대로 의식한다. 경쟁심은 있지만 이유 없이 적대하거나 폭력적으로 굴지 않는다."),
    "family_like": ("가족 같은 사이", "이 사용자를 가족처럼 가까운 비연애적 관계로 여긴다."),
}





def ensure_offline_mention_tables():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_discord_mentions (
            character TEXT NOT NULL,
            message_id TEXT NOT NULL,
            processed_at TEXT NOT NULL,
            PRIMARY KEY (character, message_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS bot_runtime_state (            character TEXT PRIMARY KEY,
            last_seen_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS offline_recovery_flags (
            character TEXT NOT NULL,
            flag TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            PRIMARY KEY (character, flag)
        )
    """)

    conn.commit()
    conn.close()


def is_discord_mention_processed(character, message_id):
    ensure_offline_mention_tables()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 1
        FROM processed_discord_mentions
        WHERE character=? AND message_id=?
        LIMIT 1
    """, (character, str(message_id)))
    found = cursor.fetchone() is not None
    conn.close()
    return found


def mark_discord_mention_processed(character, message_id):
    ensure_offline_mention_tables()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO processed_discord_mentions
        (character, message_id, processed_at)
        VALUES (?, ?, ?)
    """, (
        character,
        str(message_id),
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()


def get_bot_last_seen_at(character):
    ensure_offline_mention_tables()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT last_seen_at
        FROM bot_runtime_state
        WHERE character=?
    """, (character,))
    row = cursor.fetchone()
    conn.close()

    if not row or not row[0]:
        return None

    try:
        value = datetime.fromisoformat(row[0])
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def set_bot_last_seen_at(character, when=None):
    ensure_offline_mention_tables()
    when = when or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO bot_runtime_state
        (character, last_seen_at)
        VALUES (?, ?)
        ON CONFLICT(character) DO UPDATE SET
            last_seen_at=excluded.last_seen_at
    """, (
        character,
        when.astimezone(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()



def has_offline_recovery_flag(character, flag):
    ensure_offline_mention_tables()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT 1
        FROM offline_recovery_flags
        WHERE character=? AND flag=?
        LIMIT 1
    """, (character, flag))
    found = cursor.fetchone() is not None
    conn.close()
    return found


def set_offline_recovery_flag(character, flag):
    ensure_offline_mention_tables()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR REPLACE INTO offline_recovery_flags
        (character, flag, completed_at)
        VALUES (?, ?, ?)
    """, (
        character,
        flag,
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()



async def bot_already_replied_to_message(message, client):
    """
    최초 백필 때 processed DB에 기록이 없어도,
    실제 Discord에서 이 봇이 해당 메시지에 이미 답글을 달았으면 중복 응답하지 않는다.
    """
    if client.user is None:
        return False

    try:
        async for candidate in message.channel.history(
            limit=300,
            after=message.created_at,
            oldest_first=True,
        ):
            if candidate.author.id != client.user.id:
                continue

            reference = candidate.reference
            if (
                reference is not None
                and reference.message_id == message.id
            ):
                return True

    except (discord.Forbidden, discord.HTTPException):
        return False

    return False


async def message_targets_client(message, client):
    """멘션 또는 해당 봇 메시지에 대한 답글인지 확인한다."""
    if message.guild is None:
        return True

    if client.user is None:
        return False

    if any(user.id == client.user.id for user in message.mentions):
        return True

    reference = message.reference
    if reference is None or reference.message_id is None:
        return False

    resolved = reference.resolved
    if isinstance(resolved, discord.Message):
        return resolved.author.id == client.user.id

    try:
        referenced_message = await message.channel.fetch_message(
            reference.message_id
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return False

    return referenced_message.author.id == client.user.id


async def bot_runtime_heartbeat_loop(character):
    """갑작스러운 종료 뒤 복구 범위를 알 수 있도록 마지막 온라인 시각을 주기적으로 저장한다."""
    client = clients[character]
    await client.wait_until_ready()

    while not client.is_closed():
        try:
            set_bot_last_seen_at(character)
        except Exception as e:
            log_message(
                CHARACTERS[character]["name"],
                "런타임 heartbeat 저장 오류:",
                type(e).__name__,
                "-",
                e,
            )
        await asyncio.sleep(60)


async def recover_offline_mentions(client, character):
    """
    이전 실행의 마지막 heartbeat 이후부터 이번 실행 시작 전까지의
    사람 멘션/답글을 찾아 기존 on_message 경로로 재처리한다.

    첫 v54 실행처럼 이전 heartbeat가 전혀 없으면 과거에 이미 답한 멘션을
    중복 처리할 수 있으므로 현재 시각을 기준점으로만 저장하고 스캔하지 않는다.
    """
    await client.wait_until_ready()
    await asyncio.sleep(random.randint(5, 15))

    config = CHARACTERS[character]
    last_seen = get_bot_last_seen_at(character)

    # v54/v55에서 이미 last_seen이 저장되어 있어도,
    # v56의 "한 번만 하는 48시간 강제 백필"은 별도 플래그로 판정한다.
    backfill_flag = "v56_initial_48h_backfill"
    first_backfill = not has_offline_recovery_flag(
        character,
        backfill_flag,
    )

    if first_backfill:
        scan_after = PROGRAM_STARTED_AT - timedelta(
            hours=FIRST_RUN_BACKFILL_HOURS
        )
        log_message(
            config["name"],
            "v56 최초 오프라인 멘션 백필:",
            f"최근 {FIRST_RUN_BACKFILL_HOURS}시간 강제 확인",
        )
    else:
        oldest_allowed = PROGRAM_STARTED_AT - timedelta(
            hours=OFFLINE_MENTION_RECOVERY_HOURS
        )

        if last_seen is None:
            scan_after = oldest_allowed
        else:
            # heartbeat 직전 2분을 겹쳐서 스캔하되,
            # 처리 완료 테이블로 중복을 막는다.
            scan_after = max(
                oldest_allowed,
                last_seen - timedelta(minutes=2),
            )
    scan_before = PROGRAM_STARTED_AT

    recovered = 0
    scanned_targets = 0

    log_message(
        config["name"],
        "오프라인 멘션 복구 시작:",
        scan_after.isoformat(),
        "→",
        scan_before.isoformat(),
        "| 사람의 직접 멘션/답글만 처리",
    )

    for guild in list(client.guilds):
        for channel in guild.text_channels:
            me = guild.me
            if me is None:
                continue

            permissions = channel.permissions_for(me)
            if not (
                permissions.view_channel
                and permissions.read_message_history
                and permissions.send_messages
            ):
                continue

            try:
                async for message in channel.history(
                    limit=OFFLINE_MENTION_SCAN_LIMIT_PER_CHANNEL,
                    after=scan_after,
                    before=scan_before,
                    oldest_first=True,
                ):
                    # 복구는 오직 "사람이 보낸 메시지"만 대상으로 한다.
                    # 봇→봇 멘션, 다른 캐릭터의 자율발언, 자율발언에 아무도 답하지 않은 경우는
                    # 전부 복구 대상이 아니다.
                    if message.author.bot:
                        continue

                    if is_discord_mention_processed(character, message.id):
                        continue

                    try:
                        targeted = await message_targets_client(
                            message,
                            client,
                        )
                    except Exception as e:
                        log_message(
                            config["name"],
                            "오프라인 멘션 대상 판정 오류:",
                            type(e).__name__,
                            "-",
                            e,
                        )
                        continue

                    if not targeted:
                        continue

                    # 최초 백필은 기존 processed DB가 비어 있을 수 있으므로
                    # Discord에 이미 이 봇의 답글이 달려 있으면 건너뛴다.
                    if first_backfill:
                        try:
                            already_replied = await bot_already_replied_to_message(
                                message,
                                client,
                            )
                        except Exception as e:
                            log_message(
                                config["name"],
                                "기존 답글 확인 오류:",
                                type(e).__name__,
                                "-",
                                e,
                            )
                            already_replied = False

                        if already_replied:
                            mark_discord_mention_processed(
                                character,
                                message.id,
                            )
                            continue

                    scanned_targets += 1

                    set_log_context(
                        "world" if is_main_world_guild_id(guild.id) else "rp",
                        guild_id=guild.id,
                        guild_name=getattr(guild, "name", None),
                    )

                    log_message(
                        config["name"],
                        "밀린 멘션 발견:",
                        getattr(message.author, "display_name", str(message.author)),
                        "|",
                        getattr(channel, "name", channel.id),
                        "|",
                        str(message.content)[:180],
                    )

                    token = OFFLINE_RECOVERY_ACTIVE.set(True)
                    try:
                        await client.on_message(message)
                    finally:
                        OFFLINE_RECOVERY_ACTIVE.reset(token)

                    if is_discord_mention_processed(character, message.id):
                        recovered += 1

                    # 여러 밀린 멘션을 한꺼번에 과속 처리하지 않는다.
                    await asyncio.sleep(random.uniform(1.0, 2.5))

            except (discord.Forbidden, discord.HTTPException) as e:
                set_log_context(
                    "world" if is_main_world_guild_id(guild.id) else "rp",
                    guild_id=guild.id,
                    guild_name=getattr(guild, "name", None),
                )
                log_message(
                    config["name"],
                    "오프라인 멘션 채널 스캔 건너뜀:",
                    getattr(channel, "name", channel.id),
                    "|",
                    type(e).__name__,
                )

    set_bot_last_seen_at(character)

    if first_backfill:
        set_offline_recovery_flag(
            character,
            backfill_flag,
        )

    log_message(
        config["name"],
        "오프라인 멘션 복구 완료:",
        f"대상 {scanned_targets}개",
        "|",
        f"답변 {recovered}개",
    )


def ensure_personal_rp_settings_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS personal_rp_settings (
            guild_id TEXT PRIMARY KEY,
            channel_id TEXT,
            autonomous_enabled INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def set_personal_rp_channel(guild_id, channel_id):
    ensure_personal_rp_settings_table()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO personal_rp_settings
        (guild_id, channel_id, autonomous_enabled, updated_at)
        VALUES (?, ?, 0, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            channel_id=excluded.channel_id,
            updated_at=excluded.updated_at
    """, (
        str(guild_id),
        str(channel_id),
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()


def set_personal_rp_autonomous_enabled(guild_id, enabled):
    ensure_personal_rp_settings_table()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO personal_rp_settings
        (guild_id, channel_id, autonomous_enabled, updated_at)
        VALUES (?, NULL, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            autonomous_enabled=excluded.autonomous_enabled,
            updated_at=excluded.updated_at
    """, (
        str(guild_id),
        1 if enabled else 0,
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()


def get_personal_rp_settings(guild_id):
    ensure_personal_rp_settings_table()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT channel_id, autonomous_enabled
        FROM personal_rp_settings
        WHERE guild_id=?
    """, (str(guild_id),))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None, False
    return row[0], bool(row[1])


def get_all_enabled_personal_rp_settings():
    ensure_personal_rp_settings_table()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT guild_id, channel_id
        FROM personal_rp_settings
        WHERE autonomous_enabled=1
          AND channel_id IS NOT NULL
          AND channel_id != ''
    """)
    rows = cursor.fetchall()
    conn.close()
    return [(str(guild_id), str(channel_id)) for guild_id, channel_id in rows]


def clear_personal_rp_settings(guild_id):
    ensure_personal_rp_settings_table()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM personal_rp_settings WHERE guild_id=?",
        (str(guild_id),),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return bool(changed)


def ensure_guild_relation_table():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS guild_user_character_relations (
            guild_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            character TEXT NOT NULL,
            relation_type TEXT NOT NULL DEFAULT '',
            custom_context TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, user_id, character)
        )
    """)
    conn.commit()
    conn.close()


def set_guild_user_character_relation(guild_id, user_id, character, relation_type=None, custom_context=None):
    if character not in CHARACTERS:
        raise ValueError("알 수 없는 캐릭터")
    ensure_guild_relation_table()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT relation_type, custom_context
        FROM guild_user_character_relations
        WHERE guild_id=? AND user_id=? AND character=?
    """, (str(guild_id), str(user_id), character))
    row = cursor.fetchone()

    old_type = row[0] if row else ""
    old_custom = row[1] if row else ""
    final_type = old_type if relation_type is None else (relation_type or "")
    final_custom = old_custom if custom_context is None else (custom_context or "")

    cursor.execute("""
        INSERT INTO guild_user_character_relations
        (guild_id, user_id, character, relation_type, custom_context, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(guild_id, user_id, character)
        DO UPDATE SET
            relation_type=excluded.relation_type,
            custom_context=excluded.custom_context,
            updated_at=excluded.updated_at
    """, (
        str(guild_id),
        str(user_id),
        character,
        final_type,
        final_custom[:1800],
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()


def get_guild_user_character_relation(guild_id, user_id, character):
    ensure_guild_relation_table()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT relation_type, custom_context
        FROM guild_user_character_relations
        WHERE guild_id=? AND user_id=? AND character=?
    """, (str(guild_id), str(user_id), character))
    row = cursor.fetchone()
    conn.close()
    return ((row[0] or ""), (row[1] or "")) if row else ("", "")


def clear_guild_user_character_relation(guild_id, user_id, character):
    ensure_guild_relation_table()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        DELETE FROM guild_user_character_relations
        WHERE guild_id=? AND user_id=? AND character=?
    """, (str(guild_id), str(user_id), character))
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return bool(changed)


def get_guild_user_relation_context(guild_id, user_id, character):
    relation_type, custom_context = get_guild_user_character_relation(
        guild_id, user_id, character
    )
    parts = []

    if relation_type:
        label, instruction = SIMPLE_RELATIONS.get(
            relation_type,
            (relation_type, f"관리자가 지정한 관계: {relation_type}")
        )
        parts.append(f"간단 관계: {label}\n적용 방식: {instruction}")

    if custom_context:
        parts.append(
            "이 서버의 고정 관계/설정:\n"
            + custom_context
            + "\n이 내용은 이 서버에서만 적용되는 고정 설정이다."
        )

    return "\n\n".join(parts) if parts else ""


def set_user_character_relation(user_id, character, relation_type=None, custom_context=None):
    if character not in CHARACTERS:
        raise ValueError("알 수 없는 캐릭터")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT relation_type, custom_context FROM user_character_relations WHERE user_id=? AND character=?",
        (str(user_id), character),
    )
    row = cursor.fetchone()
    old_type = row[0] if row else ""
    old_custom = row[1] if row else ""

    final_type = old_type if relation_type is None else (relation_type or "")
    final_custom = old_custom if custom_context is None else (custom_context or "")

    cursor.execute("""
        INSERT INTO user_character_relations
        (user_id, character, relation_type, custom_context, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(user_id, character)
        DO UPDATE SET
            relation_type=excluded.relation_type,
            custom_context=excluded.custom_context,
            updated_at=excluded.updated_at
    """, (
        str(user_id),
        character,
        final_type,
        final_custom[:1800],
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()


def get_user_character_relation(user_id, character):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT relation_type, custom_context
        FROM user_character_relations
        WHERE user_id=? AND character=?
    """, (str(user_id), character))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return "", ""
    return row[0] or "", row[1] or ""


def clear_user_character_relation(user_id, character):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM user_character_relations WHERE user_id=? AND character=?",
        (str(user_id), character),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return bool(changed)


def get_user_relation_context(character, user_subject):
    user_id = (user_subject or "").removeprefix("user:")
    if not user_id or user_id == user_subject:
        return "관리자가 지정한 고정 관계 없음"

    relation_type, custom_context = get_user_character_relation(user_id, character)
    parts = []

    if relation_type:
        label, instruction = SIMPLE_RELATIONS.get(
            relation_type,
            (relation_type, f"관리자가 지정한 관계: {relation_type}")
        )
        parts.append(f"간단 관계: {label}\\n적용 방식: {instruction}")

    if custom_context:
        parts.append(
            "복잡한 고정 관계/설정:\\n"
            + custom_context
            + "\\n이 내용은 관리자 지정 고정 설정으로 취급한다."
        )

    return "\\n\\n".join(parts) if parts else "관리자가 지정한 고정 관계 없음"


def set_user_roleplay_profile(user_id, profile):
    profile = (profile or "").strip()
    if not profile:
        raise ValueError("프로필 내용이 비어 있습니다.")
    if len(profile) > 3500:
        raise ValueError("프로필은 3500자 이하로 입력해주세요.")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_roleplay_profiles
        (user_id, profile, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            profile=excluded.profile,
            updated_at=excluded.updated_at
    """, (
        str(user_id),
        profile,
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()


def get_user_roleplay_profile(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT profile FROM user_roleplay_profiles WHERE user_id=?",
        (str(user_id),),
    )
    row = cursor.fetchone()
    conn.close()
    return (row[0] or "") if row else ""


def clear_user_roleplay_profile(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM user_roleplay_profiles WHERE user_id=?",
        (str(user_id),),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return bool(changed)


def get_user_roleplay_profile_context(user_subject):
    user_id = (user_subject or "").removeprefix("user:")
    if not user_id or user_id == user_subject:
        return "등록된 역할극용 사용자 캐릭터 프로필 없음"

    profile = get_user_roleplay_profile(user_id)
    if not profile:
        return "등록된 역할극용 사용자 캐릭터 프로필 없음"

    return (
        "[역할극용 사용자 캐릭터 고정 프로필]\n"
        + profile
        + "\n"
        "이 내용은 사용자 캐릭터의 고정 설정으로 취급한다. "
        "이름, 나이, 출신, 외형, 성격, 배경 등 명시된 내용은 임의로 바꾸지 않는다. "
        "다만 프로필에 비밀, 숨김, 아직 모름 등으로 명시된 정보는 "
        "현재 캐릭터가 자동으로 알고 있다고 가정하지 않는다."
    )


def reset_test_data():
    """공개 전 테스트 기록을 완전히 비울 때 수동으로 한 번 호출한다."""
    ensure_guild_relation_table()
    ensure_personal_rp_settings_table()
    ensure_offline_mention_tables()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM messages")
    cursor.execute("DELETE FROM memories")
    cursor.execute("DELETE FROM status_messages")
    cursor.execute("DELETE FROM character_knowledge")
    cursor.execute("DELETE FROM character_notes")
    cursor.execute("DELETE FROM user_roleplay_profiles")
    conn.commit()
    conn.close()

    conversation_history.clear()
    bot_chain_count.clear()
    bot_chain_updated_at.clear()
    bot_reaction_candidates.clear()
    bot_reaction_summary_logged.clear()
    conversation_language.clear()
    recent_human_by_channel.clear()
    conversation_fatigue.clear()
    conversation_fatigue_updated_at.clear()
    place_state.clear()
    pending_appointments.clear()
    room_invitation_expiry.clear()
    place_items.clear()
    dynamic_event_channels.clear()
    recent_expressions.clear()
    recent_global_expression_patterns.clear()
    recent_autonomous_messages.clear()
    autonomous_paused_characters.clear()
    for _character in CHARACTERS:
        movement_defer_until[_character] = None
        movement_defer_reason[_character] = None
        place_access_permissions[_character].clear()
    clear_city_event_state()

    for character in CHARACTERS:
        current_activity[character] = None
        current_place[character] = None
        room_access_permissions[character].clear()
        room_invitations[character].clear()
        character_state[character].update({
            "sleeping": False,
            "sleep_interruptions": 0,
            "last_sleep_interaction": None,
            "away": False,
            "hunger": random.randint(15, 45),
            "fatigue": random.randint(10, 35),
            "mood": "평온",
            "mood_reason": "특별한 이유 없음",
            "mood_until": None,
            "dream": None,
            "dream_expires_at": None,
            "last_wake_at": None,
            "sleep_started_at": None,
            "last_need_update": datetime.now(),
            "last_place_effect": datetime.now(),
        })
        sleep_schedule[character] = {}
        character_inventory[character] = set(CHARACTERS[character].get("inventory", []))

    log_message("General", "모든 테스트 데이터 초기화 완료")


def cleanup_room_invitations():
    now = datetime.now()
    for key, expires_at in list(room_invitation_expiry.items()):
        if now >= expires_at:
            target, room = key
            room_invitations.get(target, set()).discard(room)
            room_invitation_expiry.pop(key, None)


def maybe_register_room_invitation(speaker_character, target_character, text):
    """방 주인이 상대 캐릭터에게 직접 말한 경우에만 일회성 초대를 등록한다."""
    if not target_character or target_character == speaker_character or not text:
        return False
    room = CHARACTERS[speaker_character].get("private_room")
    if not room:
        return False
    low = text.lower()
    invite_words = ["와", "들어와", "내 방", "방으로", "come", "my room", "come in"]
    refusal_words = ["오지 마", "들어오지 마", "don't come", "do not come"]
    if any(word in low for word in refusal_words):
        return False
    if (room in text or "내 방" in text or "my room" in low) and any(word in low for word in invite_words):
        cleanup_room_invitations()
        room_invitations[target_character].add(room)
        room_invitation_expiry[(target_character, room)] = datetime.now() + timedelta(hours=3)
        log_message("General", "개인실 초대 등록:", CHARACTERS[speaker_character]["name"], "→", CHARACTERS[target_character]["name"], room, "(3시간)")
        return True
    return False


def consume_room_invitation(character, room):
    cleanup_room_invitations()
    if room in room_invitations.get(character, set()):
        room_invitations[character].discard(room)
        room_invitation_expiry.pop((character, room), None)
        return True
    return False



def _extract_appointment_time(text):
    """한국어/영어의 간단한 약속 표현을 실제 설정된 생활 시간대의 시각으로 바꾼다."""
    if not text:
        return None
    low = text.lower()
    now = now_kst()
    day_offset = 1 if ("내일" in text or "tomorrow" in low) else 0
    target_date = (now + timedelta(days=day_offset)).date()
    hour = minute = None

    m = re.search(r"(아침|오전|점심|오후|저녁|밤)?\s*(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분)?", text)
    if m:
        part, hh, mm = m.groups()
        hour = int(hh) % 24
        minute = int(mm or 0)
        if part in ("오후", "저녁", "밤") and hour < 12:
            hour += 12
        elif part in ("오전", "아침") and hour == 12:
            hour = 0
        elif part == "점심" and hour < 11:
            hour += 12
    else:
        em = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", low)
        if em:
            hour = int(em.group(1)) % 12
            minute = int(em.group(2) or 0)
            if em.group(3) == "pm":
                hour += 12
        elif "아침" in text or "morning" in low:
            hour, minute = 9, 0
        elif "점심" in text or "lunch" in low:
            hour, minute = 12, 30
        elif "저녁" in text or "tonight" in low or "evening" in low:
            hour, minute = 19, 0
        elif "밤" in text:
            hour, minute = 21, 0
        elif "이따" in text or "later" in low:
            return now + timedelta(hours=2)
        elif "나중에" in text:
            return now + timedelta(hours=3)
        elif "내일" in text or "tomorrow" in low:
            hour, minute = 10, 0
        else:
            return None

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None

    due = datetime.combine(target_date, time(hour, minute), tzinfo=KST)
    explicit_today = "오늘" in text or "today" in low
    explicit_daypart = any(k in text for k in ["아침", "오전", "점심", "오후", "저녁", "밤"])
    if due <= now and day_offset == 0 and not explicit_today and not explicit_daypart:
        due += timedelta(days=1)
    return due


def _appointment_place_from_text(text, fallback=None):
    for p in PLACE_CHANNELS:
        if p in (text or ""):
            return p
        actual = PLACE_CHANNEL_NAMES.get(p)
        if actual and actual in (text or ""):
            return p
    return fallback


def _has_appointment_intent(text):
    """
    실제 공동 행동을 제안하는 문장인지 엄격하게 확인한다.
    단순히 '저녁', '약속', '같이' 같은 단어 하나만 등장해서는 True가 되지 않는다.
    """
    raw = (text or "").strip()
    low = raw.lower()
    if not raw:
        return False

    korean_patterns = [
        r"(?:같이|함께)\s*.{0,12}(?:먹|가|보|만나|마시|걷|놀|하)",
        r"(?:먹|가|보|만나|마시|걷|놀|하).{0,6}(?:자|을래|ㄹ래|을까|ㄹ까)",
        r"약속.{0,8}(?:잡|정하|정할|하자|할래)",
    ]
    english_patterns = [
        r"\b(?:shall we|do you want to|want to|let's|lets)\b.{0,40}\b(?:meet|go|eat|drink|see|walk|do)\b",
        r"\b(?:meet|go|eat|drink|see|walk)\b.{0,30}\btogether\b",
    ]

    return (
        any(re.search(p, raw, flags=re.IGNORECASE) for p in korean_patterns)
        or any(re.search(p, low, flags=re.IGNORECASE) for p in english_patterns)
    )


def _has_appointment_schedule(text):
    """
    약속으로 등록할 만큼 일정 표현이 명확한지 확인한다.
    '저녁은 어때?'처럼 시간대 명사가 화제일 뿐인 문장은 제외한다.
    """
    raw = (text or "").strip()
    low = raw.lower()
    if not raw:
        return False

    # 시각이 직접 적힌 경우
    if re.search(r"(?:오전|오후|아침|저녁|밤)?\s*\d{1,2}\s*시(?:\s*\d{1,2}\s*분)?", raw):
        return True
    if re.search(r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b", low):
        return True

    # 날짜/상대시각이 명시된 경우
    if any(k in raw for k in ["오늘", "내일", "이따", "나중에"]):
        return True
    if any(k in low for k in ["today", "tomorrow", "later"]):
        return True

    # '저녁에/점심에/밤에'처럼 실제 일정 부사로 사용된 경우만 허용
    if re.search(r"(?:아침|오전|점심|오후|저녁|밤)\s*에", raw):
        return True

    return False

def _reply_accepts_plan(reply):
    """
    상대가 명시적으로 수락한 경우에만 약속으로 인정한다.
    '거절하지 않았다 = 수락'으로 처리하지 않는다.
    """
    low = (reply or "").lower()

    reject = [
        "안 가",
        "못 가",
        "싫",
        "안 돼",
        "모르겠",
        "확인해",
        "생각해볼",
        "나중에 보자",
        "not going",
        "can't",
        "cannot",
        "no thanks",
        "not sure",
        "maybe",
    ]

    if any(k in low for k in reject):
        return False

    accept = [
        "좋아",
        "그러자",
        "가자",
        "갈게",
        "만나자",
        "같이 먹자",
        "함께 먹자",
        "괜찮아",
        "좋겠군",
        "좋겠네",
        "좋지",
        "알겠어",
        "알겠습니다",
        "그래",
        "그렇게 하자",
        "yes",
        "sure",
        "sounds good",
        "i'll go",
        "i will go",
        "let's do it",
    ]

    return any(k in low for k in accept)


def create_appointment(character, text, place=None, with_character=None, source="conversation"):
    global appointment_counter
    due = _extract_appointment_time(text)
    if due is None:
        return None
    appointment_counter += 1
    item = {
        "id": appointment_counter,
        "character": character,
        "with_character": with_character,
        "text": text[:220],
        "place": _appointment_place_from_text(text, place),
        "due_at": due,
        "created_at": now_kst(),
        "expires_at": due + timedelta(hours=6),
        "completed": False,
        "source": source,
    }
    pending_appointments.append(item)
    log_message(CHARACTERS[character]["name"], "약속 등록:", item["place"] or "장소 미정", "@", due.strftime("%m-%d %H:%M"))
    return item


def register_appointment_from_exchange(character, incoming_text, reply_text, place=None, other_character=None):
    # 시간/장소 단어만 나왔다고 약속으로 등록하지 않는다.
    # 사용자의 발언에 실제 약속 제안 의도가 있어야 한다.
    if not _has_appointment_intent(incoming_text):
        return

    # 단순히 저녁/점심 같은 단어가 나온 것만으로는 일정을 만들지 않는다.
    if not _has_appointment_schedule(incoming_text):
        return

    # 상대도 명시적으로 수락해야 한다.
    if not _reply_accepts_plan(reply_text):
        return

    due = _extract_appointment_time(incoming_text)
    if due is None:
        return
    if any(x for x in pending_appointments if x["character"] == character and not x["completed"] and abs((x["due_at"] - due).total_seconds()) < 900 and x.get("place") == _appointment_place_from_text(incoming_text, place)):
        return
    created = create_appointment(character, incoming_text, place, other_character, "accepted")
    if created is not None and other_character is not None:
        # 제안한 캐릭터에게도 같은 약속을 등록해 둘이 같은 시각/장소를 향하게 한다.
        duplicate = any(
            x for x in pending_appointments
            if x["character"] == other_character
            and not x["completed"]
            and abs((x["due_at"] - created["due_at"]).total_seconds()) < 900
            and x.get("place") == created.get("place")
        )
        if not duplicate:
            create_appointment(other_character, incoming_text, place, character, "proposed")


def cleanup_appointments():
    now = now_kst()
    pending_appointments[:] = [x for x in pending_appointments if x["expires_at"] > now and not x.get("completed")]


def get_appointments_text(character):
    cleanup_appointments()
    items = [x for x in pending_appointments if x["character"] == character]
    if not items:
        return "특별히 잡힌 약속 없음"
    return " / ".join(
        f'{x.get("place") or "장소 미정"} {x["due_at"].strftime("%m/%d %H:%M")}: {x["text"]}'
        for x in items[-3:]
    )


def _overview_subject_label(subject):
    """장기기억 subject를 관리자에게 보기 좋은 이름으로 바꾼다."""
    subject = str(subject or "")
    if subject.startswith("user:"):
        user_id = subject.removeprefix("user:")
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT display_name FROM user_profiles WHERE user_id=?",
            (user_id,)
        )
        row = cursor.fetchone()
        conn.close()
        if row and row[0]:
            return f"{row[0]} ({user_id})"
        return f"사용자 {user_id}"
    return subject or "대상 없음"


def get_character_overview_counts(character):
    """한 캐릭터의 메모/약속/장기기억 개수를 집계한다."""
    cleanup_appointments()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        "SELECT COUNT(*) FROM memories WHERE character=?",
        (character,)
    )
    memory_count = int(cursor.fetchone()[0] or 0)

    cursor.execute(
        "SELECT COUNT(*) FROM character_notes WHERE to_character=?",
        (character,)
    )
    incoming_notes = int(cursor.fetchone()[0] or 0)

    cursor.execute(
        "SELECT COUNT(*) FROM character_notes WHERE from_character=?",
        (character,)
    )
    outgoing_notes = int(cursor.fetchone()[0] or 0)

    cursor.execute(
        "SELECT COUNT(*) FROM character_notes "
        "WHERE to_character=? AND read_at IS NULL",
        (character,)
    )
    unread_notes = int(cursor.fetchone()[0] or 0)

    conn.close()

    appointment_count = sum(
        1 for item in pending_appointments
        if item.get("character") == character
        and not item.get("completed")
    )

    return {
        "memories": memory_count,
        "appointments": appointment_count,
        "incoming_notes": incoming_notes,
        "outgoing_notes": outgoing_notes,
        "unread_notes": unread_notes,
    }


def get_character_overview_details(character, memory_limit=10, note_limit=6):
    """관리자용 상세 현황 텍스트를 만든다."""
    cleanup_appointments()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    cursor.execute("""
        SELECT id, subject, content, created_at
        FROM memories
        WHERE character=?
        ORDER BY id DESC
        LIMIT ?
    """, (character, memory_limit))
    memory_rows = cursor.fetchall()

    cursor.execute("""
        SELECT id, from_character, to_character, place, content, created_at, read_at
        FROM character_notes
        WHERE from_character=? OR to_character=?
        ORDER BY id DESC
        LIMIT ?
    """, (character, character, note_limit))
    note_rows = cursor.fetchall()

    conn.close()

    appointments = [
        item for item in pending_appointments
        if item.get("character") == character
        and not item.get("completed")
    ]
    appointments.sort(key=lambda item: item.get("due_at"))

    if memory_rows:
        memory_lines = []
        for memory_id, subject, content, created_at in memory_rows:
            label = _overview_subject_label(subject)
            memory_lines.append(
                f"#{memory_id} [{label}] {content}"
            )
        memories_text = "\n".join(memory_lines)
    else:
        memories_text = "장기기억 없음"

    if appointments:
        appointment_lines = []
        for item in appointments[:8]:
            due = item.get("due_at")
            due_text = due.strftime("%m/%d %H:%M") if due else "시간 미정"
            place = item.get("place") or "장소 미정"
            other = item.get("with_character")
            other_text = (
                CHARACTERS.get(other, {}).get("name", other)
                if other else "상대 미지정"
            )
            appointment_lines.append(
                f"#{item.get('id')} {due_text} · {place} · {other_text}\n"
                f"↳ {item.get('text') or '(내용 없음)'}"
            )
        appointments_text = "\n".join(appointment_lines)
    else:
        appointments_text = "진행 중인 약속 없음"

    if note_rows:
        note_lines = []
        for note_id, sender, target, place, content, created_at, read_at in note_rows:
            sender_name = CHARACTERS.get(sender, {}).get("name", sender)
            target_name = CHARACTERS.get(target, {}).get("name", target)
            state = "읽음" if read_at else "안 읽음"
            note_lines.append(
                f"#{note_id} {sender_name} → {target_name} · "
                f"{place or '장소 미정'} · {state}\n"
                f"↳ {content}"
            )
        notes_text = "\n".join(note_lines)
    else:
        notes_text = "캐릭터 메모/쪽지 없음"

    return memories_text, appointments_text, notes_text


def _truncate_embed_field(text, limit=1024):
    text = str(text or "")
    if len(text) <= limit:
        return text
    return text[:limit - 2].rstrip() + "…"


def load_subject_memories(character, subject, limit=8):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT content FROM memories
        WHERE character = ? AND subject = ?
        ORDER BY id DESC LIMIT ?
    """, (character, subject, limit))
    rows = [row[0] for row in cursor.fetchall()]
    conn.close()
    rows.reverse()
    return rows

def get_user_memory_context(character, user_subject):
    if GENERIC_CONFIG_ACTIVE and not feature_enabled("long_term_memory"):
        return "장기 기억 기능이 비활성화되어 있음"
    rows = load_subject_memories(character, user_subject, 8)
    return " / ".join(rows) if rows else "이 사용자에 대한 별도 장기 기억 없음"

def _canonical_character_pair(character_a, character_b):
    if character_a not in CHARACTERS or character_b not in CHARACTERS:
        raise ValueError("알 수 없는 캐릭터")
    if character_a == character_b:
        raise ValueError("같은 캐릭터끼리는 관계를 설정할 수 없습니다.")
    return tuple(sorted((character_a, character_b)))


def set_character_relationship_goal(character_a, character_b, context):
    a, b = _canonical_character_pair(character_a, character_b)
    context = (context or "").strip()
    if not context:
        raise ValueError("관계 방향 설정이 비어 있습니다.")

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO character_relationship_goals
        (character_a, character_b, context, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(character_a, character_b)
        DO UPDATE SET
            context=excluded.context,
            updated_at=excluded.updated_at
    """, (
        a,
        b,
        context[:1800],
        datetime.now(timezone.utc).isoformat(),
    ))
    conn.commit()
    conn.close()


def get_character_relationship_goal(character_a, character_b):
    try:
        a, b = _canonical_character_pair(character_a, character_b)
    except ValueError:
        return ""

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT context
        FROM character_relationship_goals
        WHERE character_a=? AND character_b=?
    """, (a, b))
    row = cursor.fetchone()
    conn.close()

    if row and row[0]:
        return row[0] or ""

    # config/relations.json의 정적 관계는 DB에 별도 저장하지 않고
    # 초기 관계 방향의 fallback으로 사용한다.
    if CONFIG_RELATIONSHIPS:
        pair_keys = [
            f"{character_a}|{character_b}",
            f"{character_b}|{character_a}",
        ]

        for pair_key in pair_keys:
            value = CONFIG_RELATIONSHIPS.get(pair_key)
            if isinstance(value, dict):
                context = value.get("context") or value.get("description")
            else:
                context = value

            if context:
                return str(context).strip()

    return ""


def clear_character_relationship_goal(character_a, character_b):
    a, b = _canonical_character_pair(character_a, character_b)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM character_relationship_goals WHERE character_a=? AND character_b=?",
        (a, b),
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return bool(changed)


def get_character_interaction_profile(character, other_character):
    """
    관리자 지정 관계와 실제 축적된 관계 메모를 이용해
    캐릭터끼리 자율 상호작용할 때 사용할 가중치/확률을 반환한다.

    반환값:
    (target_weight, same_place_conversation_chance, label)
    """
    if (
        not other_character
        or character not in CHARACTERS
        or other_character not in CHARACTERS
        or character == other_character
    ):
        return 1.0, 0.55, "일반 관계"

    has_goal = bool(
        get_character_relationship_goal(
            character,
            other_character
        )
    )

    my_name = CHARACTERS[character]["name"]
    other_name = CHARACTERS[other_character]["name"]

    has_dynamic = bool(
        load_subject_memories(
            character,
            f"관계:{other_name}",
            1
        )
        or load_subject_memories(
            other_character,
            f"관계:{my_name}",
            1
        )
    )

    if has_goal and has_dynamic:
        return 3.0, 0.75, "지정 관계 + 축적 관계"
    if has_dynamic:
        return 2.5, 0.70, "축적 관계"
    if has_goal:
        return 2.0, 0.70, "지정 관계"

    return 1.0, 0.55, "일반 관계"


def get_relationship_context(character, other_character):
    if GENERIC_CONFIG_ACTIVE and not feature_enabled("relationships"):
        return "관계 기능이 비활성화되어 있음"

    if not other_character or other_character not in CHARACTERS:
        return "특별히 축적된 관계 메모 없음"

    other_name = CHARACTERS[other_character]["name"]
    rows = load_subject_memories(character, f"관계:{other_name}", 6)
    dynamic_context = " / ".join(rows) if rows else "특별히 축적된 관계 메모 없음"

    goal_context = get_character_relationship_goal(character, other_character)
    if goal_context:
        return (
            "[관리자가 지정한 장기 관계 방향]\n"
            + goal_context
            + "\n\n[실제 대화와 사건으로 축적된 현재 관계 메모]\n"
            + dynamic_context
            + "\n\n"
            "장기 관계 방향은 관계가 발전할 수 있는 범위와 성향을 정한다. "
            "구체적인 감정과 사건은 실제 대화와 기억에 따라 자연스럽게 변화시킨다."
        )

    return dynamic_context


def maybe_analyze_relationship_update(character, other_character, incoming_text, reply_text):
    """의미 있는 대화일 때만 관계 메모를 매우 엄격하게 생성한다."""
    if GENERIC_CONFIG_ACTIVE and not feature_enabled("relationships"):
        return
    combined = f"{incoming_text} {reply_text}".lower()
    signals = ["고마", "미안", "약속", "믿", "걱정", "도와", "싫어", "좋아", "thank", "sorry", "promise", "trust", "worry", "help"]
    if not any(k in combined for k in signals):
        return
    existing = get_relationship_context(character, other_character)
    input_text = f"""
두 캐릭터의 방금 대화를 보고 관계에 장기적으로 남길 가치가 있는 변화만 판단한다.
애매하거나 일상적인 대화면 반드시 IGNORE한다.

현재 캐릭터: {CHARACTERS[character]['name']}
상대: {CHARACTERS[other_character]['name']}
기존 관계/관리자 지정 방향:
{existing}

상대의 말: {incoming_text}
현재 캐릭터의 답: {reply_text}

관리자가 지정한 장기 관계 방향이 있다면 그 방향을 임의로 뒤집는 관계 메모를 만들지 않는다.
단, 실제 사건으로 생긴 일시적인 갈등이나 친밀감 변화는 기록할 수 있다.

출력은 정확히 하나:
IGNORE
또는
NOTE|한 문장의 관계 메모
"""
    response = openai_client.responses.create(model="gpt-5.6-luna", input=input_text)
    result = clean_generated_text(response.output_text)
    if result.upper().startswith("NOTE|"):
        note = result.split("|", 1)[1].strip()
        if note:
            save_memory(character, f"관계:{CHARACTERS[other_character]['name']}", note)
            log_message(CHARACTERS[character]["name"], "관계 메모 추가:", note)

# 사용자가 말 걸었을 때 캐릭터 답글 생성
def generate_character_reply(
    character,
    username,
    user_subject,
    message_text,
    history,
    channel_id,
    place,
    movement_allowed=True,
    reply_language="ko",
    private_chat=False,
    relation_subject=None,
    profile_subject=None,
    fixed_relation_override=None,
    external_rp=False,
):
    memories = load_memories(
        character,
        limit=20
    )

    memory_text = "\n".join(
        memories
    )

    if not memory_text:
        memory_text = "(장기 기억 없음)"

    config = CHARACTERS[character]

    prompt = load_character_prompt(
        character
    )

    history_text = "\n".join(
        history
    )

    if not history_text:
        history_text = "(이전 대화 없음)"

    activity = current_activity.get(
        character
    )

    if not activity:
        activity = "특별히 하는 일 없음"
    
    place_description, place_objects = (
        get_place_info(
            place
        )
    )
    
    places_text = ", ".join(
        PLACE_CHANNELS
    )

    life_context = get_character_context(character)
    user_memory_context = get_user_memory_context(character, user_subject)
    fixed_relation_context = get_user_relation_context(
        character,
        relation_subject or user_subject,
    )
    if fixed_relation_override:
        fixed_relation_context = fixed_relation_override
    roleplay_profile_context = get_user_roleplay_profile_context(
        profile_subject or user_subject
    )
    knowledge_context = get_knowledge_context(character)
    repetition_context = get_recent_expression_context(character)
    persistent_place_state = get_active_place_state(place)
    place_items_text = get_place_items_text(place)
    lang_rule = language_instruction(reply_language)
    sleep_interruptions = (
        0
        if external_rp
        else character_state[character].get(
            "sleep_interruptions",
            0
        )
    )

    if external_rp:
        conversation_scene = (
            f'{config["name"]}가 본서버와 분리된 개인 RP 장면에서 사용자와 대화 중이다.'
        )
        place_rule = (
            '이 장면의 장소는 본서버 current_place와 무관하다. '
            '본서버의 위치를 변경하거나 본서버 장소에 있다고 가정하지 않는다. '
            '현재 채널에서 직접 합의된 장면 설정만 사용한다.'
        )
    elif private_chat:
        conversation_scene = (
            f'{config["name"]}가 사용자와 다른 사람에게 들리지 않는 '
            '1:1 사적 대화를 하고 있다. 이 대화의 내용은 공개 장소의 '
            '다른 캐릭터가 자동으로 알 수 없다.'
        )
        place_rule = (
            '현재 장소는 캐릭터가 실제로 머무는 장소다. '
            '이 1:1 대화만으로 캐릭터가 장소를 이동했다고 간주하지 않는다. '
            '장소는 관련 있을 때만 자연스럽게 참고한다.'
        )
    else:
        conversation_scene = f'{config["name"]}가 현재 설정된 대화 방식으로 대화 중이다.'
        place_rule = (
            '장소와 대화 방식은 설정된 장면 정보를 따른다. '
            '장소와 관련된 질문이나 상황이라면 자연스럽게 참고한다. '
            '관련 없는 대화에서는 억지로 장소를 언급하지 않는다.'
        )

    input_text = f"""
{conversation_scene}

{COMMON_ROLEPLAY_RULES}

{lang_rule}

{life_context}

현재 장소에 남아 있는 상황:
{persistent_place_state}

현재 장소에 놓여 있는 개인 소지품:
{place_items_text}

현재 대화 중인 사용자 캐릭터 프로필:
{roleplay_profile_context}

이 사용자와의 관리자 지정 고정 관계:
{fixed_relation_context}

이 사용자에 대한 별도 기억:
{user_memory_context}

사용자 캐릭터 프로필, 고정 관계 설정, 장기 기억은 서로 다른 정보다.
사용자 캐릭터 프로필은 사용자 캐릭터 자체의 고정 설정으로 사용한다.
고정 관계는 이 캐릭터와 사용자의 관계 틀로 사용하고, 장기 기억은 실제 대화에서 축적된 세부 정보로 사용한다.
관계 설정이 있어도 캐릭터의 성격·말투·세계관 규칙은 유지한다.
서로 충돌한다면 관리자 지정 고정 설정을 우선하되, 최근 대화에서 확인된 구체적 사실은 자연스럽게 함께 반영한다.

현재 이 캐릭터만 알고 있는 정보/목격:
{knowledge_context}

최근 표현 재사용 방지 참고:
{repetition_context}

장기 기억:
{memory_text}

장기 기억은 이전 대화에서 확인된 사실이다.
현재 대화와 관련이 있을 때만 자연스럽게 참고한다.
관련 없는 기억을 억지로 언급하지 않는다.

최근 대화:
{history_text}

최근 대화에서 "A → B:" 형식은
A가 B에게 직접 말한 메시지라는 뜻이다.
다른 캐릭터에게 향한 메시지와
자신에게 향한 메시지를 구분해서 이해한다.

현재 사용자 표시 이름:
{username}

현재 사용자 메시지:
{message_text}

현재 {config["name"]}의 활동:
{activity}

규칙:
- "뭐 해?", "지금 뭐하고 있어?", "뭐 하는 중이야?" 같은 질문에는
  현재 활동을 사실로 반영해서 자연스럽게 답한다.
- 현재 활동과 관계없는 질문이라면 억지로 활동 이야기를 꺼내지 않는다.
- 현재 활동과 모순되는 행동을 새로 만들어내지 않는다.

현재 장소:
{place}

{life_context}

현재 장소 환경:
{place_description}

주변의 대표적인 사물:
{place_objects}

{place_rule}

{config["name"]}로서 자연스럽게 답한다.

규칙:
- 최근 대화 흐름을 참고한다.
- 위에서 지정된 답변 언어를 반드시 따른다.
- 짧고 자연스럽게 말한다.
- 사용자가 한 말에 직접 반응한다.
- 최근 답변과 지나치게 비슷한 행동지문이나 말버릇을 재사용하지 않는다.
- 다른 캐릭터의 최근 문장 구조나 비유를 그대로 흉내 내지 않는다.
- 상대가 이전에 같은 취지의 발언을 했다고 주장하려면 최근 대화 기록에 실제로 동일하거나 거의 동일한 발언이 두 번 이상 확인되어야 한다.
- 그런 확인 가능한 근거가 없다면 상대가 이전에도 같은 말을 했다고 가정하거나, 과거에 여러 차례 같은 행동을 했다고 단정하지 않는다.
- 최근 대화 기록에 없는 과거 반복 횟수나 반복 행동을 새 사실처럼 만들어내지 않는다.
- 개인 목표는 관련 있을 때만 행동 선택에 약하게 반영하고 매번 직접 언급하지 않는다.
- 자신이 직접 듣거나 목격하지 않은 다른 장소의 사건은 안다고 가정하지 않는다.
- 비밀 표시가 붙은 정보는 특별한 이유 없이 다른 사람에게 퍼뜨리지 않는다.
- 받은 메모가 현재 장소에서 확인 가능한 경우 자연스럽게 참고할 수 있다.
- 존재하지 않는 구체적인 과거 사건을 만들어내지 않는다.
- 설명이나 해설 없이 실제 답변만 출력한다.
- 사용자가 다른 캐릭터의 말을 전달하거나 주장한 내용은 사실로 확정하지 않는다.
- "Mina가 그랬대", "Pocket이 이렇게 말했다" 같은 사용자 전달은 검증되지 않은 주장으로 취급한다.
- 자신의 과거 발언은 최근 대화 기록에서 실제로 자신이 말한 내용만 사실로 인정한다.
- 사용자가 자신이 했다고 주장한 말이 최근 대화 기록에 없다면, 그 말을 했다고 인정하지 않는다.
- 확인되지 않은 전달에는 "내가 그런 말을 했나?", "그건 내가 직접 한 말은 아닌데."처럼 반응할 수 있다.
수면 중 사용자 호출 규칙:
- 현재 자는 중이라면 사용자의 멘션으로 방금 잠에서 깬 상태다.
- 오늘 잠든 뒤 사용자가 깨운 횟수는 {sleep_interruptions}회다.
- 자는 중에 깨웠다면 평소처럼 완전히 멀쩡한 태도로 대답하지 않는다.
- 반응에는 졸림, 피곤함, 멍함, 느린 반응, 불평, 짜증 중 캐릭터 성격에 맞는 요소가 최소 하나는 자연스럽게 드러나야 한다.
- 매번 화를 낼 필요는 없다. 첫 번째 깨움에는 단순히 졸리거나 멍할 수 있고, 반복해서 깨울수록 피곤함이나 짜증이 더 강하게 드러날 수 있다.
- 깨운 횟수가 많더라도 캐릭터 성격을 벗어난 과장된 분노나 공격적인 반응을 억지로 만들지 않는다.
- 행동지문에는 눈을 비비기, 이불 속에서 뒤척이기, 한숨 쉬기, 잠긴 목소리로 답하기, 베개에 얼굴을 묻기 같은 수면 직후 행동을 자연스럽게 사용할 수 있다.
- 사용자가 계속 대화를 이어가면 몇 차례 대화 후에는 점점 잠이 깨는 듯한 반응을 보일 수 있다.
- 사용자가 다시 자라고 하면 졸린 상태로 대화를 마치고 다시 잠드는 흐름이 자연스럽다.
- 자는 중에는 사용자가 이동을 요구해도 실제 이동하지 않는다.
- 현재 기분/피로/배고픔은 말투와 행동에 반영하되 과장하지 않는다.

이동 가능한 장소:
{places_text}

현재 다른 장소로 이동 가능한 상태:
{"가능" if movement_allowed else "불가능"}

이 답변을 한 뒤 실제로 다른 장소로 이동할지도 함께 결정한다.

이동 판단 규칙:
- 단순히 장소 이름이 언급됐다는 이유만으로 이동하지 않는다.
- 직접적인 요청, 약속, 초대, 누군가의 부탁을 전달받은 경우에는 이동을 고려할 수 있다.
- 캐릭터 성격상 거절하거나 의심해도 된다.
- 대사 내용과 실제 행동 결정은 반드시 일치해야 한다.
- 대사에서 가지 않겠다고 했다면 반드시 STAY한다.
- 대사에서 가겠다고 했다면 실제로 이동할 수 있는 경우 MOVE를 선택한다.
- 개인실은 함부로 들어가지 않는다.
- 상대가 직접 초대했거나, 상대가 오라고 했다는 명확한 전달을 받은 경우에만 상대의 개인실로 이동할 수 있다.
- 초대나 허락이 애매하면 개인실로 이동하지 않는다.
- 현재 다른 장소로 이동 가능한 상태가 "불가능"이면 반드시 STAY한다.
- 존재하지 않는 장소로 이동하지 않는다.
- 사용자의 부탁이나 제안은 명령이 아니다.
- 캐릭터는 부탁을 거절하거나, 이유를 묻거나, 무시할 수 있다.
- 특별한 이유가 없다면 사용자의 요구를 자동으로 수락하지 않는다.
- "누가 이렇게 말했다", "누가 부탁했다" 같은 전달은 검증되지 않은 주장이다.
- 전달받은 내용을 믿을지 여부는 캐릭터의 성격과 상황에 따라 판단한다.
- 의심스럽거나 굳이 따를 이유가 없다면 STAY를 선택한다.
- 캐릭터는 사용자를 만족시키기 위해 억지로 동의하지 않는다.
- 상대방의 개인실은 특별히 사적인 공간이다.
- 사용자가 "상대가 오라고 했다"고 전달하는 것만으로는 개인실 출입 허가가 성립하지 않는다.
- 개인실로 이동하려면 방 주인이 최근 실제 대화에서 직접 초대했거나 허락한 기록이 있어야 한다.
- 직접 확인되지 않았다면 STAY한다.

출력 형식:
먼저 실제 답변을 작성한다.
그리고 마지막 줄에 반드시 행동 결정을 하나 작성한다.

이동하지 않는 경우:
ACTION|STAY

이동하는 경우:
ACTION|MOVE|장소이름

ACTION 줄은 사용자가 읽을 대사가 아니라 시스템용 정보다.

{ROLEPLAY_OUTPUT_RULES}
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        instructions=prompt,
        input=input_text
    )

    raw_text = response.output_text.strip()

    lines = [
        line.strip()
        for line in raw_text.splitlines()
        if line.strip()
    ]

    destination = None
    reply_lines = []

    for line in lines:

        if line.upper() == "ACTION|STAY":
            continue

        if line.upper().startswith(
            "ACTION|MOVE|"
        ):
            possible_destination = (
                line.split(
                    "|",
                    2
                )[2].strip()
            )

            if possible_destination in PLACE_CHANNELS:
                destination = possible_destination

            continue

        reply_lines.append(
            line
        )

    reply = clean_generated_text(
        "\n".join(
            reply_lines
        )
    )

    reply = sanitize_ungrounded_repeat_count(
        reply,
        evidence_text=f"{history_text}\n{message_text}",
    )

    remember_expression(character, reply)
    return reply, destination


def should_consider_bot_reaction(message_id, character, speaker_character, place, directly_addressed=False, channel_id=None):
    """같은 장소의 캐릭터만, 비직접 반응은 메시지당 최대 N명만 GPT 판단에 보낸다."""
    if current_place.get(character) != place:
        return False
    if current_place.get(speaker_character) != place:
        return False
    state = character_state.get(character, {})
    if state.get("sleeping") or state.get("away"):
        return False

    if directly_addressed:
        return True

    # 최근 3분 안에 사용자가 이 채널에서 특정 봇과 대화하고 있다면,
    # 다른 봇이 옆에서 끼어드는 빈도를 크게 낮춘다.
    human_focus_active = False
    if channel_id is not None:
        recent_human = recent_human_by_channel.get(channel_id)
        if recent_human is not None:
            last_seen = recent_human.get("last_seen")
            if last_seen is not None:
                human_focus_active = (
                    datetime.now() - last_seen
                ).total_seconds() <= 180

    entry = bot_reaction_candidates.get(message_id)
    if entry is None:
        eligible = [
            c for c, config in CHARACTERS.items()
            if c != speaker_character
            and config.get("token")
            and current_place.get(c) == place
            and not character_state[c].get("sleeping")
            and not character_state[c].get("away")
        ]

        # 비직접 반응 후보도 관계가 있는 캐릭터가 조금 더 자주 뽑히도록
        # 가중치 기반으로 중복 없이 선택한다.
        selected = set()
        pool = list(eligible)

        # 사용자가 한 봇과 대화 중이면 제3자 반응은 약 12% 확률로 최대 1명만 허용.
        # 평상시 봇끼리 대화에는 기존 제한을 그대로 사용한다.
        if human_focus_active:
            max_reactors = 1
            if random.random() >= 0.12:
                pool = []
        else:
            max_reactors = MAX_BOT_REACTORS_PER_MESSAGE

        while pool and len(selected) < max_reactors:
            weights = [
                get_character_interaction_profile(
                    candidate,
                    speaker_character
                )[0]
                for candidate in pool
            ]

            chosen = random.choices(
                pool,
                weights=weights,
                k=1
            )[0]

            selected.add(chosen)
            pool.remove(chosen)

        bot_reaction_candidates[message_id] = selected

        # 여러 봇이 같은 메시지를 각각 판정해 CMD를 도배하지 않도록
        # 메시지 하나당 후보 선정 결과를 한 줄로만 요약한다.
        if message_id not in bot_reaction_summary_logged:
            bot_reaction_summary_logged.add(message_id)

            selected_names = [
                CHARACTERS[c]["name"]
                for c in selected
            ]

            same_place_names = [
                CHARACTERS[c]["name"]
                for c in eligible
            ]

            if human_focus_active:
                reason = "최근 사용자 대화 집중 중이라 제3자 반응 후보 제한"
            elif len(eligible) > MAX_BOT_REACTORS_PER_MESSAGE:
                reason = (
                    f"같은 장소 후보 {len(eligible)}명 중 "
                    f"최대 {MAX_BOT_REACTORS_PER_MESSAGE}명만 선택"
                )
            elif not eligible:
                reason = "같은 장소에서 반응 가능한 캐릭터 없음"
            else:
                reason = "같은 장소 반응 후보 선정"

            log_message(
                "General",
                "봇 반응 후보:",
                reason,
                "| 장소:",
                place,
                "| 발언:",
                CHARACTERS[speaker_character]["name"],
                "| 같은 장소:",
                ", ".join(same_place_names) if same_place_names else "없음",
                "| 선택:",
                ", ".join(selected_names) if selected_names else "없음",
            )

        # 오래된 메시지 캐시는 적당히 버린다.
        while len(bot_reaction_candidates) > 300:
            old_message_id = next(iter(bot_reaction_candidates))
            bot_reaction_candidates.pop(old_message_id, None)
            bot_reaction_summary_logged.discard(old_message_id)

        entry = selected

    return character in entry


# 캐릭터 반응 결정
def decide_bot_reaction(
    character,
    other_character,
    message_text,
    history,
    reply_target=None
):
    config = CHARACTERS[character]
    prompt = load_character_prompt(character)

    history_text = "\n".join(
        history
    )

    if not history_text:
        history_text = "(이전 대화 없음)"

    target_text = (
        reply_target
        if reply_target is not None
        else "특정 대상 없음"
    )

    input_text = f"""
Discord 채널에서 대화 중이다.

최근 대화:
{history_text}

방금 {other_character}가 한 말:
{message_text}

이 메시지의 답글 대상:
{target_text}

너는 {config["name"]}다.

이 말에 직접 답할지 판단한다.

반드시 아래 둘 중 하나만 출력한다.

REPLY
IGNORE

판단 기준:
- 메시지가 누구에게 향한 말인지 반드시 구분한다.
- 다른 사람에게 한 질문을 자신에게 한 질문으로 받아들이지 않는다.- 다른 사람에게 답한 말에도 자연스럽게 끼어들 이유가 있다면 REPLY할 수 있다.
- 하지만 자신이 질문을 받은 것처럼 답해서는 안 된다.
- 실제로 할 말이 있을 때만 REPLY한다.
- 이미 자연스럽게 끝난 대화라면 IGNORE한다.
- 같은 내용을 반복해야 한다면 IGNORE한다.
- 대화가 아직 자연스럽게 이어질 여지가 있다면 REPLY를 선호한다.
- 상대가 질문하지 않았더라도 감상, 반박, 농담, 추가 질문, 개인적인 반응으로 자연스럽게 대화를 이어갈 수 있다.
- 단순히 정보 전달이 끝났다는 이유만으로 바로 대화를 종료하지 않는다.
- 관계가 있는 캐릭터끼리는 사소한 주제에서도 몇 차례 더 대화를 이어갈 수 있다.
- 대화가 아직 자연스럽게 이어질 여지가 있다면 REPLY를 선호한다.
- 질문이 없어도 반응, 농담, 반박, 추가 질문 등으로 이어갈 수 있다.
- 관계가 있는 캐릭터끼리는 사소한 주제에서도 몇 차례 더 대화를 이어갈 수 있다.
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        instructions=prompt,
        input=input_text
    )

    result = (
        response.output_text
        .strip()
        .upper()
    )

    if result not in (
        "REPLY",
        "IGNORE"
    ):
        return "IGNORE"

    return result

# 캐릭터가 다른 사람에게 답한 것에 끼어들기
def generate_bot_reply(
    character,
    other_character,
    message_text,
    history,
    reply_target=None,
    reply_language="ko",
    external_rp=False,
):
    config = CHARACTERS[character]
    prompt = load_character_prompt(character)

    history_text = "\n".join(
        history
    )

    if not history_text:
        history_text = "(이전 대화 없음)"

    target_text = (
        reply_target
        if reply_target is not None
        else "특정 대상 없음"
    )

    lang_rule = language_instruction(reply_language)
    other_key = next((k for k, v in CHARACTERS.items() if v["name"] == other_character), None)
    if external_rp:
        relationship_context = "본서버 관계 메모는 이 개인 RP 장면에 자동으로 적용하지 않음"
        knowledge_context = "본서버의 목격/장소 지식은 이 개인 RP 장면에 자동으로 적용하지 않음"
    else:
        relationship_context = get_relationship_context(character, other_key) if other_key else "특별히 축적된 관계 메모 없음"
        knowledge_context = get_knowledge_context(character)
    repetition_context = get_recent_expression_context(character)

    input_text = f"""
Discord 채널에서 캐릭터들이 대화 중이다.

{COMMON_ROLEPLAY_RULES}

{lang_rule}

최근 대화:
{history_text}

이 상대와의 관계 메모:
{relationship_context}

내가 직접 알고 있는 정보/목격:
{knowledge_context}

최근 표현 재사용 방지 참고:
{repetition_context}

방금 {other_character}가 한 말:
{message_text}

그 메시지가 향한 대상:
{target_text}

너는 {config["name"]}다.

자연스럽게 반응한다.

규칙:
- 누가 누구에게 말했는지 반드시 구분한다.
- 다른 사람에게 한 질문을 자신에게 한 질문처럼 받아들이지 않는다.
- 대화에 끼어든다면 제3자로서 자연스럽게 반응한다.
- {other_character}가 자신에게 직접 한 말이라면 직접 답할 수 있다.
- 최근 대화 흐름을 참고한다.
- 짧고 자연스럽게 말한다.
- 설명이나 해설 없이 실제 답변만 출력한다.
- 사용자가 다른 캐릭터의 발언을 대신 전달한 내용은 검증된 사실로 취급하지 않는다.
- 최근 대화 기록에서 실제 발언이 확인될 때만 "그 캐릭터가 그렇게 말했다"고 확정한다.
- 관계 메모의 신뢰/경계/친숙함/불편함은 말투와 부탁 수락 성향에 약하게 반영한다.
- 관계 메모 때문에 성격을 무시하거나 무조건 호의적/적대적으로 변하지 않는다.
- 자신이 직접 듣거나 목격하지 않은 다른 장소의 일을 아는 척하지 않는다.
- 최근 답변과 지나치게 비슷한 행동지문이나 말버릇을 재사용하지 않는다.
- 다른 캐릭터가 방금 쓴 문장 구조나 비유를 그대로 흉내 내지 않는다.
- 상대가 이전에도 같은 취지의 말을 했다고 주장하려면 최근 대화 기록에 실제로 동일하거나 거의 동일한 발언이 두 번 이상 확인되어야 한다.
- 확인 가능한 근거가 없다면 상대의 과거 반복 행동이나 반복 발언을 새로 만들어내지 않는다.

{ROLEPLAY_OUTPUT_RULES}
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        instructions=prompt,
        input=input_text
    )

    reply = clean_generated_text(
        response.output_text
    )
    reply = sanitize_ungrounded_repeat_count(
        reply,
        evidence_text=f"{history_text}\n{message_text}",
    )
    remember_expression(character, reply)
    return reply

# 데이터베이스
def init_db():
    conn = sqlite3.connect(
        DB_PATH
    )

    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            speaker TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id TEXT NOT NULL,
            subject TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        PRAGMA table_info(memories)
    """)

    memory_columns = [
        row[1]
        for row in cursor.fetchall()
    ]

    if "character" not in memory_columns:
        cursor.execute("""
            ALTER TABLE memories
            ADD COLUMN character TEXT
        """)

        log_message(
            "General",
            'memories.character 컬럼 추가 완료',
        )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS status_messages (
            status_key TEXT PRIMARY KEY,
            message_id TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_roleplay_profiles (
            user_id TEXT PRIMARY KEY,
            profile TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS character_knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            character TEXT NOT NULL,
            fact TEXT NOT NULL,
            source_type TEXT NOT NULL,
            source_name TEXT,
            source_key TEXT,
            secret INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS character_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_character TEXT NOT NULL,
            to_character TEXT NOT NULL,
            place TEXT,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            read_at TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS user_character_relations (
            user_id TEXT NOT NULL,
            character TEXT NOT NULL,
            relation_type TEXT NOT NULL DEFAULT '',
            custom_context TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (user_id, character)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS character_relationship_goals (
            character_a TEXT NOT NULL,
            character_b TEXT NOT NULL,
            context TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL,
            PRIMARY KEY (character_a, character_b)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS status_meta (
            meta_key TEXT PRIMARY KEY,
            meta_value TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

# 메세지 저장
def save_message(
    channel_id,
    speaker,
    content
):
    conn = sqlite3.connect(
        DB_PATH
    )

    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO messages (
            channel_id,
            speaker,
            content,
            created_at
        )
        VALUES (?, ?, ?, ?)
    """, (
        str(channel_id),
        speaker,
        content,
        datetime.now(
            timezone.utc
        ).isoformat()
    ))

    conn.commit()
    conn.close()

# 최근 기록 로드
def load_recent_history(
    channel_id,
    limit=10
):
    conn = sqlite3.connect(
        DB_PATH
    )

    cursor = conn.cursor()

    cursor.execute("""
        SELECT speaker, content
        FROM messages
        WHERE channel_id = ?
        ORDER BY id DESC
        LIMIT ?
    """, (
        str(channel_id),
        limit
    ))

    rows = cursor.fetchall()
    conn.close()

    rows.reverse()

    return [
        f"{speaker}: {content}"
        for speaker, content in rows
    ]

# 장기 기억 저장
def save_memory(
    character,
    subject,
    content
):
    if GENERIC_CONFIG_ACTIVE and not feature_enabled("long_term_memory"):
        return

    conn = sqlite3.connect(
        DB_PATH
    )

    cursor = conn.cursor()

    cursor.execute("""
        SELECT id
        FROM memories
        WHERE character = ?
          AND subject = ?
          AND content = ?
        LIMIT 1
    """, (
        character,
        subject,
        content
    ))

    existing = cursor.fetchone()

    if existing is None:
        cursor.execute("""
            INSERT INTO memories (
                channel_id,
                character,
                subject,
                content,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            "",
            character,
            subject,
            content,
            datetime.now(
                timezone.utc
            ).isoformat()
        ))

    conn.commit()
    conn.close()

# 장기 기억 로드
def load_memories(
    character,
    limit=20,
    include_dm=False,
):
    conn = sqlite3.connect(
        DB_PATH
    )

    cursor = conn.cursor()

    if include_dm:
        cursor.execute("""
            SELECT subject, content
            FROM memories
            WHERE character = ?
            ORDER BY id DESC
            LIMIT ?
        """, (
            character,
            limit
        ))
    else:
        # DM에서 얻은 사용자별 사적 기억은 서버/자율행동의 공용 기억으로
        # 섞이지 않게 한다. DM에서는 get_user_memory_context()로 해당 사용자
        # 전용 기억만 별도로 주입한다.
        cursor.execute("""
            SELECT subject, content
            FROM memories
            WHERE character = ?
              AND subject NOT LIKE 'DM:%'
              AND subject NOT LIKE 'rp:%'
            ORDER BY id DESC
            LIMIT ?
        """, (
            character,
            limit
        ))

    rows = cursor.fetchall()
    conn.close()

    rows.reverse()

    return [
        f"{subject}: {content}"
        for subject, content in rows
    ]

# 장기 기억 처리 판단
def analyze_memory(
    character,
    user_subject,
    display_name,
    message_text
):
    if GENERIC_CONFIG_ACTIVE and not feature_enabled("long_term_memory"):
        return "IGNORE"

    existing_memories = load_subject_memories(
        character,
        user_subject,
        limit=30
    )

    # 아래 기존 코드는 그대로

    existing_text = "\n".join(
        existing_memories
    )

    if not existing_text:
        existing_text = "(기존 장기 기억 없음)"

    input_text = f"""
사용자의 새 메시지를 보고
장기 기억을 어떻게 처리할지 판단한다.

사용자 이름:
{display_name}

기존 장기 기억:
{existing_text}

새 메시지:
{message_text}

아래 셋 중 하나만 선택한다.

NEW
UPDATE
IGNORE

의미:

NEW
- 새로운 장기적 사실이나 취향이다.
- 기존 기억과 충돌하지 않는다.

UPDATE
- 기존 기억 중 하나가 바뀌었거나 더 정확해졌다.
- 예전 취향, 상태, 목표 등이 변경된 경우다.

IGNORE
- 장기적으로 기억할 가치가 없다.
- 단순 질문, 인사, 농담, 일시적 기분이다.
- 이미 같은 내용이 충분히 저장되어 있다.

출력 형식은 반드시 아래와 같다.

NEW|기억할 문장

또는

UPDATE|기존 기억 문장|새 기억 문장

또는

IGNORE

예시:

NEW|프렌치토스트를 좋아한다.

UPDATE|딸기 케이크를 가장 좋아한다.|현재는 치즈케이크를 가장 좋아한다.

IGNORE
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        input=input_text
    )

    return clean_generated_text(
        response.output_text
    )

# 장기 기억 업데이트
def update_memory(
    character,
    subject,
    old_content,
    new_content
):
    conn = sqlite3.connect(
        DB_PATH
    )

    cursor = conn.cursor()

    cursor.execute("""
        UPDATE memories
        SET content = ?,
            created_at = ?
        WHERE character = ?
          AND subject = ?
          AND content = ?
    """, (
        new_content,
        datetime.now(
            timezone.utc
        ).isoformat(),
        character,
        subject,
        old_content
    ))

    conn.commit()
    conn.close()

# 시간대
def get_time_period():
    hour = now_kst().hour

    if 6 <= hour < 11:
        return "morning"
    if 11 <= hour < 18:
        return "day"
    if 18 <= hour < 24:
        return "evening"
    if 0 <= hour < 5:
        return "late_night"
    return "night"

# 상태메세지 활동 결정
def choose_activity(
    character,
    place=None
):
    config = CHARACTERS[character]
    if '_life' in globals():
        stage = _life.busy(character, CUSTOM_SETTINGS)
        if stage: return current_activity.get(character, stage)
    prompt = load_character_prompt(character)

    period = get_time_period()

    if place is None:
        place = current_place.get(
            character
        )

    if place is None:
        place = "알 수 없는 장소"

    period_names = {
        "morning": "아침",
        "day": "낮",
        "evening": "저녁",
        "night": "밤",
        "late_night": "늦은 밤/새벽",
    }

    if character_state[character]["sleeping"]:
        current_activity[character] = "자는 중"
        return f"{place}에서 자는 중"

    if character_state[character]["away"]:
        current_activity[character] = "외출 중"
        return "외출 중"
    entry = weekly_entry(CUSTOM_SETTINGS, config, now_kst(), place)
    if entry and not get_movement_busy_reason(character, clients.get(character)):
        current_activity[character] = entry['activity']
        return entry['activity']

    life_context = get_character_context(character)

    input_text = f"""
현재 시간대:
{period_names[period]}

{life_context}

현재 장소:
{place}

캐릭터:
{config["name"]}

Discord 상태메시지에 표시할
현재 활동을 하나 정한다.

규칙:
- 현재 장소와 자연스럽게 어울리는 활동이어야 한다.
- 아주 짧게 쓴다.
- "~하는 중" 형태가 자연스럽다.
- 캐릭터 성격과 생활에 어울려야 한다.
- 거창한 사건이나 임무를 만들지 않는다.
- 존재하지 않는 구체적인 과거 사건을 만들지 않는다.
- 장소와 명백하게 모순되는 행동은 피한다.
- 상태메시지에 들어갈 실제 문구만 출력한다.
- 활동 문구만 작성한다.
- 장소 이름은 출력하지 않는다.
- 18자 이내로 작성한다.

예:
카페 → 차 마시는 중
옥상 → 야경 구경하는 중
거실 → 소파에서 쉬는 중
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        instructions=prompt,
        input=input_text
    )

    activity = (
        response.output_text
        .strip()
    )

    status_text = (
        f"{place}에서 {activity}"
    )

    if len(status_text) > 25:
        status_text = status_text[:25]

    current_activity[
        character
    ] = activity

    return status_text

# 상태메세지 결정 루프(30분~2시간)
async def activity_loop(
    client,
    character
):
    config = CHARACTERS[
        character
    ]

    while True:

        # 30분 ~ 2시간
        wait_seconds = random.randint(
            60 * 30,
            60 * 60 * 2
        )

        log_message(
            config["name"],
            '상태 갱신까지',
            wait_seconds // 60,
            '분',
        )

        await asyncio.sleep(
            wait_seconds
        )

        try:
            if character_state[character]["sleeping"] or character_state[character]["away"]:
                continue

            place = current_place.get(
                character
            )

            activity = await run_ai(choose_activity,
                character,
                place
            )

            await safe_change_presence(
                client,
                character=character,
                activity=discord.CustomActivity(
                    name=get_presence_location_text(character, place)
                ),
                reason="주기적 활동 상태 갱신",
            )

            log_message(
                config["name"],
                '상태 변경:',
                activity,
            )

        except Exception as e:
            log_message(
                config["name"],
                '상태 갱신 오류:',
                type(e).__name__,
                '-',
                e,
            )


# Discord 멤버 목록 커스텀 상태를 주기적으로 현재 상태와 다시 동기화한다.
# GPT를 다시 호출하지 않고, 이미 저장된 current_place/current_activity 값만 재전송한다.
async def presence_sync_loop():
    await asyncio.sleep(15)

    while True:
        try:
            for character, client in clients.items():
                if not client or not client.is_ready():
                    continue

                state = character_state.get(character, {})
                place = current_place.get(character)

                if state.get("sleeping"):
                    status = discord.Status.idle
                elif state.get("away"):
                    status = discord.Status.dnd
                else:
                    status = discord.Status.online

                await safe_change_presence(
                    client,
                    character=character,
                    status=status,
                    activity=discord.CustomActivity(
                        name=get_presence_location_text(character, place)
                    ),
                    reason="presence 재동기화",
                )

        except Exception as e:
            log_message(
                "General",
                "Presence 동기화 오류:",
                type(e).__name__,
                "-",
                e,
            )

        # Discord 상태 갱신을 과도하게 보내지 않도록 90초 간격으로 재동기화한다.
        await asyncio.sleep(90)


# 자율 발언 생성
def generate_autonomous_message(
    character,
    place
):
    config = CHARACTERS[character]
    prompt = load_character_prompt(character)

    activity = current_activity.get(
        character,
        "특별히 하는 일 없음"
    )
    
    place_description, place_objects = (
        get_place_info(
            place
        )
    )
    persistent_place_state = get_active_place_state(place)
    life_context = get_character_context(character)
    place_items_text = get_place_items_text(place)
    knowledge_context = get_knowledge_context(character)
    repetition_context = get_recent_expression_context(character)
    autonomous_history_context = get_recent_autonomous_context(
        character,
        scope="world",
    )
    autonomous_variety_rule = get_character_autonomous_variety_rule(character)

    input_text = f"""
{COMMON_ROLEPLAY_RULES}

최근 이 캐릭터의 자율발언:
{autonomous_history_context}

{autonomous_variety_rule}

현재 장소:
{place}

현재 장소 환경:
{place_description}

주변에서 자연스럽게 사용할 수 있는 사물:
{place_objects}

장소에 남아 있는 상황:
{persistent_place_state}

이 장소에 놓여 있는 개인 소지품:
{place_items_text}

현재 이 캐릭터만 알고 있는 정보/목격:
{knowledge_context}

최근 표현 재사용 방지 참고:
{repetition_context}

현재 {config["name"]}의 활동:
{activity}

Discord 채널에 짧은 혼잣말이나 잡담을
하나 남길지 판단한다.

먼저 아래 둘 중 하나를 선택한다.

POST
NO_POST

POST라면 다음 줄에 실제 메시지를 쓴다.

형식:

POST
메시지

또는:

NO_POST

규칙:
- 현재 활동과 자연스럽게 관련될 수 있다.
- 반드시 현재 활동을 직접 언급할 필요는 없다.
- 사소한 생각이나 불평, 관찰도 가능하다.
- 누군가에게 질문할 필요는 없다.
- 억지로 캐릭터성을 과장하지 않는다.
- 구체적인 새 사건이나 과거를 지어내지 않는다.
- 짧고 자연스럽게 쓴다.
- 현재 장소에서 자연스럽게 떠올릴 법한 말을 할 수 있다.
- 장소를 매번 직접 언급할 필요는 없다.
- 개인 목표는 관련 있을 때만 사소한 선택이나 생각에 약하게 드러낼 수 있다.
- 다른 장소에서 직접 듣지 못한 일은 아는 척하지 않는다.
- 최근 자신의 자율발언과 지나치게 비슷한 행동이나 문장 구조를 재사용하지 않는다.
- 다른 캐릭터의 최근 말투나 비유를 그대로 흉내 내지 않는다.
- 최근 대화에 확인되지 않은 과거 반복 행동이나 반복 발언을 새 사실처럼 만들지 않는다.
- 특별히 침묵할 이유가 없다면 POST를 약간 더 선호한다.
- 사소한 관찰, 현재 활동에 대한 짧은 감상, 주변 사람에게 건네는 가벼운 말도 POST할 가치가 있다.
- 반드시 중요한 사건이나 특별한 주제가 있어야 말하는 것은 아니다.

- 현재 장소의 주변 환경이나 사물에 자연스럽게 반응할 수 있다.
- 반드시 주변 사물을 언급할 필요는 없다.
- 장소 환경에 없는 특별한 시설이나 물건을 함부로 만들어내지 않는다.

{ROLEPLAY_OUTPUT_RULES}
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        instructions=prompt,
        input=input_text
    )

    result = response.output_text.strip()

    if result.upper() == "NO_POST":
        return None

    if result.upper().startswith("POST"):
        parts = result.split(
            "\n",
            1
        )

        if len(parts) == 2:
            text = clean_generated_text(
                    parts[1]
                )

            if text:
                text = sanitize_ungrounded_repeat_count(
                    text,
                    evidence_text=autonomous_history_context,
                )

                if expression_was_recent(character, text):
                    return None

                remember_expression(character, text)
                return text

    return None

# 자율 발언 루프(1시간~3시간)
# 현재 위치에 따라 다른 캐릭터에게 먼저 말 걸수있음(확률)

async def _get_rp_channel_recent_context(channel, limit=10):
    lines = []
    try:
        async for msg in channel.history(limit=limit, oldest_first=False):
            if not msg.content:
                continue
            speaker = getattr(msg.author, "display_name", str(msg.author))
            content = msg.content.strip()
            if len(content) > 300:
                content = content[:300] + "…"
            lines.append(f"{speaker}: {content}")
    except (discord.Forbidden, discord.HTTPException):
        return "(최근 대화를 읽을 수 없음)"

    lines.reverse()
    return "\n".join(lines) if lines else "(최근 대화 없음)"


def _personal_rp_bot_characters_in_guild(guild, exclude_character=None):
    result = []
    member_ids = {member.id for member in guild.members}

    for candidate, bot_id in bot_character_ids.items():
        if candidate == exclude_character:
            continue
        if bot_id in member_ids:
            result.append(candidate)

    return result


def generate_personal_rp_autonomous_message(
    character,
    recent_context,
    target_character=None,
    target_user_name=None,
    recent_autonomous_context=None,
):
    config = CHARACTERS[character]
    prompt = load_character_prompt(character)
    autonomous_variety_rule = get_character_autonomous_variety_rule(character)

    if not recent_autonomous_context:
        recent_autonomous_context = "(최근 자율발언 없음)"

    target_instruction = ""
    if target_character is not None:
        target_name = CHARACTERS[target_character]["name"]
        target_instruction = (
            f"\n이번에는 같은 개인 RP 공간에 있는 {target_name}에게 "
            "먼저 자연스럽게 말을 건다."
        )
    elif target_user_name:
        target_instruction = (
            f"\n이번에는 최근 대화했던 사용자 {target_user_name}에게 "
            "먼저 자연스럽게 말을 건다."
        )
    else:
        target_instruction = (
            "\n이번에는 특정 상대를 강제로 정하지 않는다. "
            "혼잣말, 가벼운 관찰, 대화를 열 수 있는 한마디 중 자연스러운 것을 고른다."
        )

    input_text = f"""
{COMMON_ROLEPLAY_RULES}

이곳은 본서버의 장소/생활 시뮬레이션과 분리된 개인 RP 공간이다.
본서버의 현재 위치, 수면, 외출, 약속, 장소 상태를 여기의 사실로 가져오지 않는다.
이 개인 RP 공간에서 실제로 확인된 최근 대화만 현재 상황의 근거로 사용한다.

최근 대화:
{recent_context}

최근 이 캐릭터의 자율발언:
{recent_autonomous_context}

{autonomous_variety_rule}

너는 {config["name"]}다.
{target_instruction}

규칙:
- 사용자가 먼저 말하지 않아도 캐릭터답게 대화를 시작할 수 있다.- 사소한 잡담, 농담, 질문, 관찰, 관심 표현도 좋다.
- 매번 큰 사건, 장기 목표, 임무, 복수, 수사 이야기로 연결하지 않는다.
- 최근 대화에 없는 사건이나 약속을 만들어내지 않는다.
- 상대가 다른 캐릭터라면 실제로 그 캐릭터에게 말을 거는 문장으로 작성한다.
- 상대가 사용자라면 자연스럽게 사용자를 향해 말한다.
- Discord, 서버, 채팅, 메시지 같은 메타 표현을 역할극 대사에 넣지 않는다.
- Discord 멘션 문자열은 출력하지 않는다.
- 너무 길지 않게 1~3개의 짧은 대사/행동으로 작성한다.
- 설명이나 해설 없이 실제 역할극 출력만 작성한다.

{ROLEPLAY_OUTPUT_RULES}
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        instructions=prompt,
        input=input_text,
    )

    result = clean_generated_text(response.output_text)
    result = sanitize_ungrounded_repeat_count(
        result,
        evidence_text=(
            f"{recent_context}\n{recent_autonomous_context or ''}"
        ),
    )
    remember_expression(character, result)
    return result


async def personal_rp_autonomous_loop(client, character):
    """
    개인 RP 서버 전용 자율발언 루프.
    본서버의 장소/수면/외출/월드 상태와 독립적으로 동작한다.
    """
    config = CHARACTERS[character]
    await client.wait_until_ready()

    # 여러 캐릭터가 프로그램 시작 직후 동시에 검사하지 않도록 약간 분산한다.
    await asyncio.sleep(random.randint(20, 90))

    while not client.is_closed():
        wait_minutes = random.randint(
            RP_AUTONOMOUS_CHECK_MIN_MINUTES,
            RP_AUTONOMOUS_CHECK_MAX_MINUTES,
        )
        await asyncio.sleep(wait_minutes * 60)

        for guild in list(client.guilds):
            if is_main_world_guild_id(guild.id):
                continue

            channel_id, enabled = get_personal_rp_settings(guild.id)
            if not enabled or not channel_id:
                continue

            try:
                channel = guild.get_channel(int(channel_id))
            except (TypeError, ValueError):
                channel = None

            if not isinstance(channel, discord.TextChannel):
                continue

            me = guild.me
            if me is None:
                continue

            permissions = channel.permissions_for(me)
            if not (
                permissions.view_channel
                and permissions.send_messages
                and permissions.read_message_history
            ):
                continue

            set_log_context(
                "rp",
                guild_id=guild.id,
                guild_name=getattr(guild, "name", None),
            )

            cooldown_key = (str(guild.id), character)
            allowed_at = rp_autonomous_cooldown_until.get(cooldown_key)
            now = datetime.now()

            if allowed_at is not None and now < allowed_at:
                continue

            try:
                recent_context = await _get_rp_channel_recent_context(channel)

                # 같은 개인 서버에 있는 다른 캐릭터에게 먼저 말 걸기.
                target_character = None
                target_user = None

                possible_characters = _personal_rp_bot_characters_in_guild(
                    guild,
                    exclude_character=character,
                )

                if (
                    possible_characters
                    and random.random() < RP_AUTONOMOUS_CHARACTER_START_CHANCE
                ):
                    target_character = random.choice(possible_characters)

                # 캐릭터에게 먼저 말하지 않는 경우에는 최근 사용자에게 가끔 먼저 말 건다.
                recent_human = recent_human_by_channel.get(channel.id)
                if (
                    target_character is None
                    and recent_human is not None
                    and random.random() < RP_AUTONOMOUS_USER_MENTION_CHANCE
                ):
                    last_seen = recent_human.get("last_seen")
                    if (
                        last_seen is not None
                        and (datetime.now() - last_seen).total_seconds() <= 72 * 3600
                    ):
                        target_user = recent_human

                recent_autonomous_context = get_recent_autonomous_context(
                    character,
                    scope="rp",
                    guild_id=guild.id,
                )

                message_text = await run_ai(generate_personal_rp_autonomous_message,
                    character,
                    recent_context,
                    target_character=target_character,
                    target_user_name=(
                        target_user.get("display_name")
                        if target_user is not None
                        else None
                    ),
                    recent_autonomous_context=recent_autonomous_context,
                )

                if not message_text:
                    continue

                # 다른 캐릭터를 선택한 경우 실제 Discord 멘션을 앞에 붙여
                # 그 캐릭터의 개인 RP 반응 루프가 직접 대화로 인식하게 한다.
                if target_character is not None:
                    target_bot_id = bot_character_ids.get(target_character)
                    if target_bot_id is not None:
                        message_text = f"<@{target_bot_id}> {message_text}"
                        reset_bot_chain(
                            f"rp:{guild.id}:{channel.id}",
                            f"{config['name']}의 개인 RP 자율 대화 시작",
                        )

                # 최근 사용자에게 먼저 말을 거는 경우.
                elif target_user is not None:
                    message_text = f'<@{target_user["user_id"]}> {message_text}'

                await channel.send(message_text)

                remember_autonomous_message(
                    character,
                    message_text,
                    scope="rp",
                    guild_id=guild.id,
                )

                # 서버/캐릭터별 독립 쿨다운.
                cooldown_minutes = random.randint(
                    RP_AUTONOMOUS_CHECK_MIN_MINUTES,
                    RP_AUTONOMOUS_CHECK_MAX_MINUTES,
                )
                rp_autonomous_cooldown_until[cooldown_key] = (
                    datetime.now() + timedelta(minutes=cooldown_minutes)
                )

                log_message(
                    config["name"],
                    "개인 RP 자율발언:",
                    message_text,
                    "| 다음 가능:",
                    f"{cooldown_minutes}분 후",
                )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                log_message(
                    config["name"],
                    "개인 RP 자율발언 오류:",
                    type(e).__name__,
                    "-",
                    e,
                )


async def autonomous_message_loop(
    client,
    character
):
    config = CHARACTERS[
        character
    ]

    await client.wait_until_ready()

    while not client.is_closed():

        # 20~60분마다 한 번 자율발언 가능 여부를 판단한다.
        wait_seconds = random.randint(
            20 * 60,
            60 * 60
        )

        log_message(
            config["name"],
            '자율 발언 판단까지',
            wait_seconds // 60,
            '분',
        )

        _monitor.characters.setdefault(character,{})['next_check'] = datetime.now(timezone.utc).timestamp()+wait_seconds
        await asyncio.sleep(
            wait_seconds
        )

        try:
            if autonomous_global_paused or character in autonomous_paused_characters:
                log_message(config["name"], "자율 발언 일시정지 중")
                continue
            update_character_needs(character)
            if character_state[character]["sleeping"]:
                log_message(config["name"], "자는 중이라 자율 발언 건너뜀")
                continue
            if character_state[character]["away"]:
                log_message(config["name"], "외출 중이라 자율 발언 건너뜀")
                continue

            # 먼저 현재 위치 결정
            place = current_place.get(
                character
            )

            if place is None:
                place = choose_place(
                    character
                )

            # ------------------------------------------
            # 새 자율발언 쿨다운 확인
            # ------------------------------------------
            now = datetime.now()

            character_allowed_at = (
                autonomous_character_cooldown_until.get(
                    character
                )
            )

            if (
                character_allowed_at is not None
                and now < character_allowed_at
            ):
                remaining_seconds = max(
                    0,
                    int(
                        (
                            character_allowed_at
                            - now
                        ).total_seconds()
                    )
                )

                log_message(
                    config["name"],
                    "개인 자율발언 쿨다운:",
                    f"{remaining_seconds // 60}분 "
                    f"{remaining_seconds % 60}초 남음",
                )
                continue

            place_allowed_at = (
                autonomous_place_cooldown_until.get(
                    place
                )
            )

            if (
                place_allowed_at is not None
                and now < place_allowed_at
            ):
                remaining_seconds = max(
                    0,
                    int(
                        (
                            place_allowed_at
                            - now
                        ).total_seconds()
                    )
                )

                log_message(
                    config["name"],
                    "장소 자율발언 쿨다운:",
                    place,
                    "|",
                    f"{remaining_seconds // 60}분 "
                    f"{remaining_seconds % 60}초 남음",
                )
                continue

            # ------------------------------------------
            # 다른 캐릭터에게 먼저 말 걸기
            # 같은 장소면 확률 높음
            # 다른 장소면 확률 낮음
            # ------------------------------------------

            # 실제 같은 장소에 있는 캐릭터만 자율 대화 상대 후보로 삼는다.
            # 관리자 지정 관계/축적된 관계가 있으면 상대 선택 가중치가 올라간다.
            my_place = current_place.get(
                character
            )

            possible_targets = [
                c
                for c in CHARACTERS
                if (
                    c != character
                    and c in bot_character_ids
                    and current_place.get(c) == my_place
                )
            ]

            start_conversation = False
            other_character = None

            if possible_targets:
                target_weights = []
                target_profiles = {}

                for candidate in possible_targets:
                    weight, chance, label = (
                        get_character_interaction_profile(
                            character,
                            candidate
                        )
                    )
                    target_weights.append(weight)
                    target_profiles[candidate] = (
                        chance,
                        label,
                        weight
                    )

                other_character = random.choices(
                    possible_targets,
                    weights=target_weights,
                    k=1
                )[0]

                conversation_chance, relation_label, relation_weight = (
                    target_profiles[other_character]
                )

                start_conversation = (
                    random.random()
                    < conversation_chance
                )

                log_message(
                    config["name"],
                    '대화 상대 선택:',
                    CHARACTERS[other_character]["name"],
                    '| 관계:',
                    relation_label,
                    '| 선택 가중치:',
                    f'{relation_weight:.1f}',
                    '| 대화 시작 확률:',
                    f'{conversation_chance:.0%}',
                )

            if (
                start_conversation
                and other_character is not None
            ):

                other_name = CHARACTERS[
                    other_character
                ]["name"]

                other_user_id = bot_character_ids[
                    other_character
                ]

                starter = await run_ai(generate_conversation_starter,
                    character,
                    other_character,
                    place
                )

                message_text = (
                    f"<@{other_user_id}> "
                    f"{starter}"
                )
                if not starter: continue

                log_message(
                    config["name"],
                    '→',
                    other_name,
                    '대화 시작:',
                    starter,
                )

            else:
                message_text = (
                    await run_ai(generate_autonomous_message,
                        character,
                        place
                    )
                )

            if not message_text:
                log_message(
                    config["name"],
                    '자율 발언: NO_POST',
                )
                continue

            target_channel = find_place_channel(
                client,
                place
            )

            if target_channel is None:
                log_message(
                    config["name"],
                    '현재 위치 채널을 찾을 수 없음:',
                    place,
                )
                continue

            # 아주 드물게 최근 사용자에게 먼저 말을 건다.
            recent_human = recent_human_by_channel.get(target_channel.id)
            if (
                recent_human
                and random.random() < 0.03
                and (datetime.now() - recent_human["last_seen"]).total_seconds() < 48 * 3600
            ):
                message_text = f'<@{recent_human["user_id"]}> ' + message_text

            # 특정 캐릭터에게 먼저 말을 거는 자율 발언은 완전히 새로운 대화 흐름이다.
            # 이전에 이 채널에서 누적된 MAX_BOT_CHAIN 값이 새 대화를 막지 않게 초기화한다.
            if (
                start_conversation
                and other_character is not None
            ):
                reset_bot_chain(
                    target_channel.id,
                    f"{config['name']}의 새 자율 대화 시작"
                )

            await send_world(target_channel,message_text,character,place)

            remember_autonomous_message(
                character,
                message_text,
                scope="world",
            )

            # 실제 새 자율발언이 전송된 경우에만
            # 캐릭터/장소 쿨다운을 새로 설정한다.
            character_cooldown_minutes = random.randint(
                AUTONOMOUS_CHARACTER_COOLDOWN_MIN,
                AUTONOMOUS_CHARACTER_COOLDOWN_MAX
            )
            place_cooldown_minutes = random.randint(
                AUTONOMOUS_PLACE_COOLDOWN_MIN,
                AUTONOMOUS_PLACE_COOLDOWN_MAX
            )

            cooldown_now = datetime.now()

            autonomous_character_cooldown_until[
                character
            ] = (
                cooldown_now
                + timedelta(
                    minutes=character_cooldown_minutes
                )
            )

            autonomous_place_cooldown_until[
                place
            ] = (
                cooldown_now
                + timedelta(
                    minutes=place_cooldown_minutes
                )
            )

            log_message(
                config["name"],
                "자율발언 쿨다운 설정:",
                f"개인 {character_cooldown_minutes}분",
                "|",
                f"{place} {place_cooldown_minutes}분",
            )

            if start_conversation and other_character is not None:
                maybe_register_room_invitation(
                    character,
                    other_character,
                    message_text
                )
                maybe_update_inventory_from_text(
                    character,
                    message_text,
                    place,
                    other_character
                )

            log_message(
                config["name"],
                '자율 발언:',
                message_text,
            )

        except Exception as e:
            log_message(
                config["name"],
                '자율 발언 오류:',
                type(e).__name__,
                '-',
                e,
            )

# 다른 캐릭터에게 말걸기 
def generate_conversation_starter(
    character,
    other_character,
    place
):
    config = CHARACTERS[
        character
    ]

    other_config = CHARACTERS[
        other_character
    ]

    prompt = load_character_prompt(
        character
    )

    activity = current_activity.get(
        character,
        "특별히 하는 일 없음"
    )

    other_activity = current_activity.get(
        other_character,
        "알 수 없음"
    )

    other_place = current_place.get(
        other_character,
        "알 수 없는 장소"
    )

    input_text = f"""
{COMMON_ROLEPLAY_RULES}

현재 {config["name"]}의 활동:
{activity}

현재 {other_config["name"]}의 상태:
{other_activity}

현재 {config["name"]}의 장소:
{place}

현재 {other_config["name"]}의 장소:
{other_place}

{config["name"]}가 Discord에서
{other_config["name"]}에게 먼저 말을 걸려고 한다.

짧고 자연스러운 첫마디를 하나 작성한다.

규칙:
- 현재 장소와 활동을 대화 소재로 사용할 수 있다.
- 두 캐릭터가 같은 장소라면
  주변 상황이나 상대의 현재 상태를 자연스럽게 언급할 수 있다.
- 서로 다른 장소라면
  바로 옆에 있는 것처럼 말하지 않는다.
- 서로 다른 장소에서는 설정된 대화 방식을 따른다. 현장 RP라면 확인된 연락·관찰 없이 상대 위치나 행동을 아는 것으로 취급하지 않는다.
- 반드시 장소를 직접 언급할 필요는 없다.
- 사소한 질문, 불평, 농담, 관찰도 가능하다.
- 매번 "뭐 해?"처럼 같은 질문만 하지 않는다.
- 상대 캐릭터에게 실제로 말을 거는 문장이어야 한다.
- 존재하지 않는 과거 사건이나 약속을 만들어내지 않는다.
- 대화를 억지로 큰 사건으로 만들지 않는다.
- 짧게 작성한다.
- Discord 멘션은 넣지 않는다.
- 실제 발언만 출력한다.

{ROLEPLAY_OUTPUT_RULES}
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        instructions=prompt,
        input=input_text
    )

    return response.output_text.strip()

# 캐릭터가 이동할 장소 정하기(캐릭터 기본 가중치 + 시간대 가중치)
def choose_place(
    character
):
    period = get_time_period()
    if feature_enabled('meal_stages') and character_state[character].get('hunger',0)>=55:
        foods=[p for p in PLACE_CHANNELS if any(PLACE_INFO.get(p,{}).get('menus',{}).values()) and can_enter_place(character,p)]
        if foods:
            selected=random.choice(foods)
            current_place[character]=selected
            return selected

    candidates = []
    weights = []

    for place in PLACE_CHANNELS:
        
        if not can_enter_place(
            character,
            place
        ):
            continue

        own_room = CHARACTERS[character].get("private_room")
        default_weight = 12 if place == own_room else 1
        base_weight = (
            PLACE_WEIGHTS
            .get(character, {})
            .get(place, default_weight)
        )

        time_multiplier = (
            TIME_PLACE_MULTIPLIERS
            .get(period, {})
            .get(place, 1.0)
        )

        need_multiplier = 1.0
        state = character_state[character]
        own_room = CHARACTERS[character].get("private_room")

        if state["hunger"] >= 70 and place in ("식당", "카페"):
            need_multiplier *= 2.4
        if state["fatigue"] >= 65 and place in ("거실", own_room):
            need_multiplier *= 2.5
        if period == "late_night" and place == own_room:
            need_multiplier *= 4.0

        final_weight = (
            base_weight
            * time_multiplier
            * need_multiplier
        )

        entry = weekly_entry(CUSTOM_SETTINGS, CHARACTERS[character], now_kst())
        if entry and entry.get('place') == place:
            final_weight *= 6

        if final_weight <= 0:
            continue

        candidates.append(
            place
        )

        weights.append(
            final_weight
        )

    if not candidates:
        return current_place.get(character)
    selected = random.choices(
        candidates,
        weights=weights,
        k=1
    )[0]

    current_place[
        character
    ] = selected

    return selected

# 장소에 해당하는 채널 찾기
def find_place_channel(
    client,
    place
):
    if place is None:
        return None

    channel_name = PLACE_CHANNEL_NAMES.get(
        place,
        place
    )

    for guild in client.guilds:
        for channel in guild.text_channels:
            if channel.name == channel_name:
                permissions = channel.permissions_for(guild.me)
                if permissions.view_channel and permissions.send_messages:
                    return channel

    return None

# 개인실에 접근 가능 여부
def can_enter_place(
    character,
    place
):
    if not place_open(CUSTOM_SETTINGS, PLACE_INFO.get(place, {}), now_kst()):
        return False
    # 캐릭터별 고정 출입 제한.
    # 가중치가 0이어도 대화/약속 등 다른 이동 경로가 있을 수 있으므로
    # 실제 입장 단계에서도 한 번 더 막는다.
    if place in RESTRICTED_PLACES_BY_CHARACTER.get(character, set()):
        return False

    # 블랙모어 실험실처럼 설정상 출입이 제한된 공용 내부 장소.
    allowed = PLACE_ALLOWED_CHARACTERS.get(place)
    if allowed is not None:
        if character not in allowed and place not in place_access_permissions.get(character, set()):
            return False

    # 내부 장소는 자연 이동 시 상위 장소를 거쳐 들어간다.
    # 관리자 강제 이동은 can_enter_place를 거치지 않으므로 예외다.
    parent = get_physical_parent_place(place)
    if parent is not None:
        current = current_place.get(character)
        has_special_access = place in place_access_permissions.get(character, set())
        if current not in (parent, place) and not has_special_access:
            return False

    owner = PRIVATE_ROOMS.get(place)
    if owner is None:
        return True
    if owner == character:
        return True
    if place in room_access_permissions.get(character, set()):
        return True
    if place in room_invitations.get(character, set()):
        return True
    return False

# 장소 정보 가져오기
def get_place_info(place):
    info = PLACE_INFO.get(
        place,
        {}
    )

    description = info.get(
        "description",
        "특별한 환경 정보 없음"
    )

    objects = info.get(
        "objects",
        []
    )

    objects_text = ", ".join(
        objects
    )

    if not objects_text:
        objects_text = "특별히 정해진 사물 없음"

    return (
        description,
        objects_text
    )

# 봇의 불필요한 텍스트 제거
def _unicode_script_group(ch):
    """문자 하나를 아주 거친 스크립트 그룹으로 분류한다."""
    if ch.isspace() or ch.isdigit():
        return None

    code = ord(ch)

    if 0xAC00 <= code <= 0xD7A3 or 0x1100 <= code <= 0x11FF:
        return "HANGUL"

    try:
        name = unicodedata.name(ch)
    except ValueError:
        return "UNKNOWN"

    for group in (
        "LATIN",
        "CYRILLIC",
        "GREEK",
        "ARMENIAN",
        "HEBREW",
        "ARABIC",
        "DEVANAGARI",
        "HIRAGANA",
        "KATAKANA",
        "CJK",
        "THAI",
    ):
        if group in name:
            return group

    if unicodedata.category(ch).startswith(("P", "S")):
        return None

    return "OTHER"


def _looks_like_unicode_garbage(fragment):
    """
    정상 다국어 문장을 건드리지 않도록 매우 보수적으로 판정한다.
    짧은 꼬리 안에서 여러 문자 체계가 비정상적으로 섞이거나,
    제어/비할당 문자가 포함된 경우만 찌꺼기로 본다.
    """
    fragment = (fragment or "").strip()
    if not fragment or len(fragment) > 48:
        return False

    # 실제 문장처럼 공백과 단어가 충분히 있으면 건드리지 않는다.
    if fragment.count(" ") >= 3:
        return False

    categories = [unicodedata.category(ch) for ch in fragment]
    if any(cat.startswith("C") for cat in categories):
        return True

    groups = {
        group
        for ch in fragment
        if (group := _unicode_script_group(ch)) is not None
    }

    # 예: u{...아르메니아/기타문자...} 같은 토큰 찌꺼기
    has_brace_noise = any(ch in fragment for ch in "{}[]<>\\")
    if has_brace_noise and len(groups) >= 2:
        return True

    # 공백도 거의 없는 짧은 문자열 안에 3개 이상의 문자체계가 섞이면 비정상 꼬리로 간주.
    if len(groups) >= 3 and fragment.count(" ") <= 1:
        return True

    # 알 수 없는/기타 문자가 여러 개 섞인 짧은 조각
    odd_count = sum(
        1 for ch in fragment
        if _unicode_script_group(ch) in {"UNKNOWN", "OTHER"}
        and not unicodedata.category(ch).startswith(("P", "S"))
    )
    if odd_count >= 3 and fragment.count(" ") <= 1:
        return True

    return False


def _strip_unicode_garbage_tail(line):
    """
    정상 문장 뒤에 붙은 짧은 Unicode 깨짐만 잘라낸다.
    예: '판단의 정확성이다. u{...}' -> '판단의 정확성이다.'
    """
    line = (line or "").rstrip()
    if not line:
        return line

    # 문장 종결부 뒤의 짧은 꼬리를 검사한다.
    # 마지막 종결부부터 뒤쪽만 보기 때문에 정상 본문은 유지한다.
    matches = list(re.finditer(r'[.!?…\)]', line))
    if matches:
        for match in reversed(matches):
            cut = match.end()
            tail = line[cut:].strip()
            if tail and _looks_like_unicode_garbage(tail):
                return line[:cut].rstrip()

    # 공백 뒤 마지막 토큰이 별도의 깨진 문자열인 경우
    parts = line.rsplit(None, 1)
    if len(parts) == 2 and _looks_like_unicode_garbage(parts[1]):
        return parts[0].rstrip()

    return line


def clean_generated_text(text):
    if not text:
        return ""

    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]

    cleaned = []

    for line in lines:

        if line.upper().startswith('THOUGHT|'):
            if globals().get('_generation_thought') and (_generation_scope.get() or CURRENT_LOG_SCOPE.get())=='world' and feature_enabled('inner_thoughts'):
                _generation_thought.set(line.split('|',1)[1].strip()[:1000])
            continue
        # 모델이 가끔 내놓는 불필요한 invalid 제거
        if line.lower() == "invalid":
            continue

        # 한국어 문장 끝에 갑자기 붙은 랜덤 영문 문자열 제거.
        # 예: "보류해 두자.bhIkfap" -> "보류해 두자."
        #
        # 정상 영문 고유명사(Blackmore, Professor Dynamo 등)를 최대한 보존하기 위해
        # 문장 끝의 6자 이상 영문 덩어리만 제한적으로 제거한다.
        line = re.sub(
            r'(?<=[가-힣.!?…\)])([A-Za-z]{6,})$',
            '',
            line
        )

        # 공백 뒤에 독립적으로 붙은 랜덤 영문 문자열도 제거.
        # 예: "좋아. xKqPzLm" -> "좋아."
        line = re.sub(
            r'\s+[A-Za-z]{6,}$',
            '',
            line
        )

        # 여러 Unicode 문자 체계가 비정상적으로 섞인 생성 찌꺼기 제거.
        line = _strip_unicode_garbage_tail(line)

        line = line.rstrip()

        # 이전 문장이 이미 정상적으로 끝났는데 다음 줄 전체가
        # 짧은 Unicode 찌꺼기라면 그 줄 자체를 버린다.
        if (
            cleaned
            and _looks_like_unicode_garbage(line)
            and re.search(r'[.!?…\)]$', cleaned[-1])
        ):
            continue

        # 바로 앞 문장과 완전히 같으면 중복 제거
        if cleaned and line == cleaned[-1]:
            continue

        if line:
            cleaned.append(line)

    return "\n".join(cleaned).strip()


# 장소 이동 결정
def decide_place_movement(
    character
):
    config = CHARACTERS[
        character
    ]

    prompt = load_character_prompt(
        character
    )

    current = current_place.get(
        character
    )

    activity = current_activity.get(
        character,
        "특별히 하는 일 없음"
    )

    period = get_time_period()

    period_names = {
        "morning": "아침",
        "day": "낮",
        "evening": "저녁",
        "night": "밤",
        "late_night": "늦은 밤/새벽",
    }

    input_text = f"""
현재 캐릭터:
{config["name"]}

현재 장소:
{current}

현재 활동:
{activity}

현재 시간대:
{period_names[period]}

현재 시각:
{datetime.now().strftime("%H:%M")}

생활 상태:
{get_needs_text(character)}

이 캐릭터가 지금 장소에
계속 머물지,
다른 장소로 이동할지만 판단한다.
반드시 아래 둘 중 하나만 출력한다.

STAY

또는

MOVE

판단 기준:
- 너무 자주 장소를 옮기지는 않지만 시간 흐름과 생활 리듬을 우선한다.
- 낮에는 카페/거리/광장/식당 같은 활동 공간에 머무는 것이 자연스럽다.
- 제제벨과 도박장은 주로 저녁과 밤에 활발해지며, 아침과 낮에는 방문 가능성이 낮다.
- Apollo, Graves, Rem은 미성년자이므로 제제벨과 도박장에 출입하지 않는다.
- 저녁에는 식사 후 거실이나 개인실로 돌아가는 흐름이 자연스럽다.
- 밤에는 거리/광장/식당 같은 공용 장소에 오래 남아 있는 것을 덜 선호한다.
- 00:00~05:00의 늦은 밤/새벽에는 특별한 이유가 없으면 개인실로 돌아가는 쪽을 강하게 선호한다.
- 피로가 높을수록 거실이나 자기 방으로 돌아갈 가능성이 높다.
- 배고픔이 높으면 식당이나 카페로 이동할 이유가 생길 수 있다.
- 현재 활동이 끝났거나 장소와 시간이 어울리지 않으면 MOVE를 적극 고려한다.
- 목적지는 선택하지 않는다.
- 새로운 사건을 만들어내지 않는다.
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        instructions=prompt,
        input=input_text
    )

    result = (
        response.output_text
        .strip()
        .upper()
    )

    if result == "MOVE":
        return True

    return False

# 메세지로 장소 이동 결정하지
def decide_movement_from_message(
    character,
    username,
    message_text
):
    config = CHARACTERS[
        character
    ]

    current = current_place.get(
        character
    )

    places_text = ", ".join(
        PLACE_CHANNELS
    )

    prompt = load_character_prompt(
        character
    )

    input_text = f"""
현재 캐릭터:
{config["name"]}

현재 장소:
{current}

사용자:
{username}

사용자 메시지:
{message_text}

이 메시지를 들은 뒤,
캐릭터가 실제로 다른 장소로 이동할 이유가 생겼는지 판단한다.

가능한 장소:
{places_text}

반드시 아래 형식 중 하나만 출력한다.

STAY

또는

MOVE|장소이름

판단 기준:
- 단순히 장소가 언급됐다고 이동하지 않는다.
- 누가 오라고 했다는 전달, 약속, 초대, 직접적인 요청 등이 있으면 이동할 수 있다.
- 캐릭터 성격상 거절하거나 의심할 수도 있다.
- 현재 대화 맥락과 캐릭터 설정을 따른다.
- 개인실은 아무 이유 없이 들어가면 안 된다.
- 상대방이 초대했다는 말이 분명하면 그 방으로 이동할 수 있다.
- 가능한 장소 목록에 없는 곳은 선택하지 않는다.
- 실제로 이동하지 않는다면 STAY를 출력한다.
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        instructions=prompt,
        input=input_text
    )

    result = (
        response.output_text
        .strip()
    )

    if result.upper() == "STAY":
        return None

    if result.upper().startswith(
        "MOVE|"
    ):
        destination = result.split(
            "|",
            1
        )[1].strip()

        if destination in PLACE_CHANNELS:
            return destination

    return None

# 목적지 선택(가중치)
def choose_weighted_destination(
    character
):
    current = current_place.get(
        character
    )

    period = get_time_period()
    if feature_enabled('meal_stages') and character_state[character].get('hunger',0)>=55:
        foods=[p for p in PLACE_CHANNELS if p!=current and any(PLACE_INFO.get(p,{}).get('menus',{}).values()) and can_enter_place(character,p)]
        if foods: return random.choice(foods)


    candidates = []
    weights = []

    for place in PLACE_CHANNELS:
        
        if not can_enter_place(
            character,
            place
        ):
            continue

        if place == current:
            continue

        own_room = CHARACTERS[character].get("private_room")
        default_weight = 12 if place == own_room else 1
        base_weight = (
            PLACE_WEIGHTS
            .get(character, {})
            .get(place, default_weight)
        )

        time_multiplier = (
            TIME_PLACE_MULTIPLIERS
            .get(period, {})
            .get(place, 1.0)
        )

        final_weight = (
            base_weight
            * time_multiplier
        )

        # 생활 욕구가 실제 이동 성향에 영향을 준다.
        state = character_state[character]
        own_room = CHARACTERS[character].get("private_room")
        if state.get("hunger", 0) >= 70 and place in ("식당", "카페"):
            final_weight *= 4.0
        elif state.get("hunger", 0) >= 50 and place in ("식당", "카페"):
            final_weight *= 2.0

        if state.get("fatigue", 0) >= 80 and place == own_room:
            final_weight *= 6.0
        elif state.get("fatigue", 0) >= 65 and place in (own_room, "거실"):
            final_weight *= 3.0

        routine = get_routine_context(character)
        if "아침 루틴" in routine and place in ("식당", "카페", "거실"):
            final_weight *= 2.2
        if ("저녁 루틴" in routine or "늦은 밤 루틴" in routine) and place == own_room:
            final_weight *= 3.5

        entry = weekly_entry(CUSTOM_SETTINGS, CHARACTERS[character], now_kst())
        if entry and entry.get('place') == place:
            final_weight *= 6

        if final_weight <= 0:
            continue

        candidates.append(
            place
        )

        weights.append(
            final_weight
        )

    if not candidates:
        return None

    return random.choices(
        candidates,
        weights=weights,
        k=1
    )[0]

# 장소 이동 결정 루프
async def place_movement_loop(
    client,
    character
):
    config = CHARACTERS[character]

    await client.wait_until_ready()

    while not client.is_closed():

        # 평상시에는 40~100분마다 이동 여부를 판단한다.
        wait_seconds = random.randint(
            40 * 60,
            100 * 60
        )

        log_message(
            config["name"],
            '이동 판단까지',
            wait_seconds // 60,
            '분',
        )

        await asyncio.sleep(wait_seconds)

        try:
            update_character_needs(character)

            # 이동 시점에 대화/이벤트가 겹치면 긴 주기를 다시 뽑지 않고
            # 10~20분 뒤 짧게 재확인한다.
            while True:
                busy_reason = get_movement_busy_reason(character, client)
                if not busy_reason:
                    break

                retry_minutes = random.randint(10, 20)
                log_message(
                    config["name"],
                    '이동 보류:',
                    busy_reason,
                    '| 재확인:',
                    f'{retry_minutes}분 후',
                )
                await asyncio.sleep(retry_minutes * 60)
                update_character_needs(character)

            old_place = current_place.get(character)

            # GPT는 이동 여부만 판단
            should_move = await run_ai(decide_place_movement, character)

            if not should_move:
                log_message(
                    config["name"],
                    "이동 판단: STAY",
                    "| 현재 위치:",
                    old_place
                )
                continue

            destination = choose_weighted_destination(character)

            if destination is None:
                log_message(config["name"], '이동 목적지 없음')
                continue

            current_place[character] = destination

            if not feature_enabled("meal_stages") and destination in ("식당", "카페"):
                character_state[character]["hunger"] = max(0, character_state[character]["hunger"] - 35)
            if destination in ("거실", CHARACTERS[character].get("private_room")):
                character_state[character]["fatigue"] = max(0, character_state[character]["fatigue"] - 10)

            log_message(
                config["name"],
                '이동:',
                old_place,
                '→',
                destination,
            )

            new_activity = await run_ai(choose_activity, character, destination)

            await safe_change_presence(
                client,
                character=character,
                activity=discord.CustomActivity(
                    name=get_presence_location_text(character, destination)
                ),
                reason="장소 이동",
            )

            log_message(
                config["name"],
                '이동 후 활동:',
                current_activity.get(character) or new_activity,
            )

            await maybe_deliver_unread_note(character)

        except Exception as e:
            log_message(
                config["name"],
                '이동 판단 오류:',
                type(e).__name__,
                '-',
                e,
            )

# 다이나믹 이벤트 상황 설정
def generate_dynamic_event_seed(
    place,
    participants
):
    participant_lines = []
    for character in participants:
        participant_lines.append(
            f"{CHARACTERS[character]['name']}: "
            f"{current_activity.get(character, '특별히 하는 일 없음')}"
        )
    participants_text = "\n".join(participant_lines)
    
    place_description, place_objects = (
        get_place_info(
            place
        )
    )

    input_text = f"""
현재 장소:
{place}

장소 환경:
{place_description}

이 장소에 자연스럽게 존재할 수 있는 사물:
{place_objects}

현재 참여 캐릭터와 활동:
{participants_text}

이번 이벤트 유형:
{random.choice(["일상", "발견", "사소한 사고", "오해", "관계", "작은 선택"])}

이 장소에 아주 작고 일상적인 상황 하나를 만든다.

중요:
이것은 줄거리나 대화 내용을 만드는 것이 아니다.
캐릭터들이 발견하거나 반응할 수 있는
'환경 속 작은 상황'만 만든다.

좋은 예:
- 카페 창가 근처에 손님이 두고 간 듯한 우산이 오래 놓여 있다.
- 테이블 한쪽에 설탕 통 뚜껑이 제대로 닫혀 있지 않다.
- 거실 창문이 조금 열려 있어 커튼이 계속 흔들린다.
- 옥상 난간 근처에 정체를 알 수 없는 종이 한 장이 떨어져 있다.
- 거리 한쪽 가게의 간판 불빛이 계속 깜빡인다.

규칙:
- 참여 캐릭터들이 무엇을 하는지는 정하지 않는다.
- 누가 먼저 발견하는지도 정하지 않는다.
- 누가 무슨 말을 하는지도 정하지 않는다.
- 두 사람의 감정이나 반응을 미리 결정하지 않는다.
- 해결 방법을 정하지 않는다.
- 중요한 과거 사건이나 새로운 설정을 만들지 않는다.
- 위험한 사건, 전투, 큰 사고는 만들지 않는다.
- 세계관을 바꾸는 사건은 만들지 않는다.
- 현재 장소와 어울리는 사소한 상황이어야 한다.
- 캐릭터들이 여러 방식으로 반응할 수 있는 열린 상황이어야 한다.
- 한 문장, 길어도 두 문장으로 쓴다.
- 상황 설명만 출력한다.
- 가능하면 현재 장소의 환경이나 사물을 활용한다.
- 목록에 없는 사물도 장소에 자연스럽다면 사용할 수 있다.
- 장소에 어울리지 않는 물건이나 시설을 갑자기 만들어내지 않는다.
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        input=input_text
    )

    return clean_generated_text(
        response.output_text
    )

# 다이나믹 이벤트 답글 달기
def generate_dynamic_event_reply(
    character,
    other_character,
    event_seed,
    place,
    history,
    turn_number,
    max_turns
):
    config = CHARACTERS[
        character
    ]

    other_name = CHARACTERS[
        other_character
    ]["name"]

    prompt = load_character_prompt(
        character
    )

    history_text = "\n".join(
        history
    )

    activity = current_activity.get(
        character,
        "특별히 하는 일 없음"
    )

    if turn_number == 1:
        turn_rule = """
이번은 이벤트의 첫 턴이다.

첫 행동지문은 이 이벤트의 도입부 역할을 해야 한다.

- 현재 환경 상황의 핵심이 무엇인지 행동만 읽어도 알 수 있게 한다.
- 캐릭터가 그것을 발견하거나 바라보거나 만지는 등
  실제 행동으로 상황을 화면에 보여준다.
- 단순히 "무언가를 본다"로 끝내지 말고
  무엇을 보고 있는지 구체적으로 드러낸다.
- 필요하면 행동지문을 최대 2문장까지 사용할 수 있다.
- 첫 턴에는 대사를 하지 않아도 된다.
- 환경 상황을 해설문처럼 설명하지 말고
  캐릭터가 실제로 행동하는 모습으로 보여준다.
"""
    else:
        turn_rule = """
이벤트가 이미 진행 중이다.

- 직전 행동과 대화를 알고 자연스럽게 이어간다.
- 반드시 이벤트의 핵심 소재만 계속 언급할 필요는 없다.
- 주변 물건을 만지거나, 시선을 돌리거나,
  음료를 마시는 등의 사소한 행동이 자연스럽게 끼어들 수 있다.
- 기존 상황과 무관한 완전히 새로운 사건을 갑자기 만들지는 않는다.
- 행동만으로 충분한 턴이라면 대사를 생략해도 된다.
"""

    input_text = f"""
현재 장소:
{place}

이벤트의 전체 상황:
{event_seed}

현재 {config["name"]}의 활동:
{activity}

최근 진행:
{history_text}

현재 이벤트 진행:
{turn_number} / 최대 {max_turns}턴

너는 {config["name"]}다.

{other_name}와 같은 공간에서 현재 상황이 진행 중이다.

{turn_rule}

출력 규칙:
- 행동지문은 (행동) 형식으로 작성한다.
- 다이나믹 이벤트에서는 행동지문을 반드시 하나 포함한다.
- 행동지문은 보통 1문장으로 쓴다.
- 필요한 경우에만 최대 2문장까지 사용할 수 있다.
- 2문장 행동지문은 드물게 사용한다.
- 행동 뒤에 할 말이 있다면 다음 줄에 대사를 작성한다.
- 행동만 자연스럽다면 대사를 생략할 수 있다.
- 현재 장소와 이벤트 상황이 눈에 보이도록 행동한다.
- 상대방의 생각이나 감정을 대신 서술하지 않는다.
- 상대방이 하지 않은 행동을 했다고 확정하지 않는다.
- 같은 행동이나 표현을 반복하지 않는다.
- 모든 턴이 반드시 이벤트 핵심 소재만 다룰 필요는 없다.
- 대사의 앞뒤에 큰따옴표(")나 작은따옴표(')를 붙이지 않는다.
- 화자 이름은 출력하지 않는다.
- 출력에는 행동지문과 필요한 경우의 대사만 넣는다.

좋은 첫 턴 예시:
(창가 쪽 손님이 두고 간 우산을 발견한다. Pocket에게 우산이 꽤 오래 방치된 듯하다고 살짝 눈짓을 준다.)

좋은 이후 턴 예시:
(찻잔을 내려놓다가 테이블 위에 설탕을 조금 쏟는다.)

또 다른 예시:
(Pocket이 쏟은 설탕을 바라본다.)
조심성 없기는.
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        instructions=prompt,
        input=input_text
    )

    return clean_generated_text(
        response.output_text
    )

# 다이나믹 이벤트 종료 판단
def should_end_dynamic_event(
    event_seed,
    history,
    turn_count,
    max_turns
):
    if turn_count >= max_turns:
        return True

    if turn_count < DYNAMIC_EVENT_MIN_TURNS:
        return False

    history_text = "\n".join(
        history
    )

    input_text = f"""
현재 상황:
{event_seed}

최근 대화:
{history_text}

현재 {turn_count}턴째다.

이 대화가 지금 자연스럽게 끝나도 되는지 판단한다.

반드시 둘 중 하나만 출력한다.

CONTINUE
END

END:
- 서로 할 말을 충분히 했다.
- 자연스럽게 대화가 마무리되었다.
- 더 이어가면 반복될 가능성이 높다.

CONTINUE:
- 아직 자연스럽게 이어질 내용이 있다.
- 질문이나 갈등, 주제가 아직 남아 있다.
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        input=input_text
    )

    result = (
        response.output_text
        .strip()
        .upper()
    )

    return result == "END"

# 다이나믹 이벤트 실행
async def run_dynamic_event(
    place,
    participants
):
    if len(participants) < 2:
        return
    first_client = clients.get(participants[0])
    if first_client is None:
        return
    channel = find_place_channel(
        first_client,
        place
    )

    if channel is None:
        return

    channel_id = channel.id

    if dynamic_event_channels.get(
        channel_id
    ):
        return

    event_seed = await run_ai(generate_dynamic_event_seed,
        place,
        participants
    )
    set_place_state(place, event_seed, hours=random.randint(2, 6))

    max_turns = random.randint(
        DYNAMIC_EVENT_MIN_TURNS,
        DYNAMIC_EVENT_MAX_TURNS
    )

    dynamic_event_channels[
        channel_id
    ] = True

    print()
    log_message(
        "General",
        "=== 다이나믹 이벤트 시작 ==="
    )

    log_message(
        "General",
        "장소:",
        place
    )

    log_message(
        "General",
        "상황:",
        event_seed
    )
    log_message(
        "General",
        '최대 턴:',
        max_turns,
    )

    try:
        participants = list(participants[:2])

        speaker_index = random.randint(
            0,
            1
        )

        event_history = []

        for turn in range(
            1,
            max_turns + 1
        ):
            character = participants[
                speaker_index
            ]

            other_character = participants[
                1 - speaker_index
            ]

            client = clients[character]

            speaker_channel = find_place_channel(
                client,
                place
            )

            if speaker_channel is None:
                log_message(
                    CHARACTERS[character]["name"],
                    '이벤트 채널을 찾지 못함:',
                    place,
                )
                break

            reply = await run_ai(generate_dynamic_event_reply,
                character,
                other_character,
                event_seed,
                place,
                event_history,
                turn,
                max_turns
            )

            if not reply:
                break

            await send_world(speaker_channel,reply,character,place)

            name = CHARACTERS[
                character
            ]["name"]

            event_history.append(
                f"{name}: {reply}"
            )
            maybe_update_place_state_from_text(place, reply)
            for listener in participants:
                if listener != character:
                    maybe_store_overheard_knowledge(listener, character, reply, place)

            conversation_history[
                channel_id
            ].append(
                f"{name}: {reply}"
            )

            save_message(
                channel_id,
                name,
                reply
            )
            maybe_update_inventory_from_text(
                character,
                reply,
                place,
                other_character
            )

            log_message(
                name,
                f"이벤트 {turn}턴 →",
                reply
            )

            if await run_ai(should_end_dynamic_event,
                event_seed,
                event_history,
                turn,
                max_turns
            ):
                log_message(
                    "General",
                    '이벤트 자연 종료',
                )
                break

            speaker_index = (
                1 - speaker_index
            )

            # 실제 대화처럼 약간 기다림
            await asyncio.sleep(
                random.randint(
                    10,
                    35
                )
            )

    except Exception as e:
        log_message(
            "General",
            '다이나믹 이벤트 오류:',
            type(e).__name__,
            '-',
            e,
        )

    else:
        # 정상 종료했을 때 기억 판단
        for character in participants:
            try:
                memory = (
                    await run_ai(generate_dynamic_event_memory,
                        character,
                        place,
                        event_seed,
                        event_history
                    )
                )

                if memory:
                    subject = (
                        f"다이나믹 이벤트 - {place}"
                    )

                    save_memory(
                        character,
                        subject,
                        memory
                    )
                    other_for_memory = next((c for c in participants if c != character), None)
                    if other_for_memory is not None:
                        save_memory(
                            character,
                            f"관계:{CHARACTERS[other_for_memory]['name']}",
                            memory
                        )

                    log_message(
                        CHARACTERS[character]["name"],
                        '이벤트 기억 저장:',
                        memory,
                    )

                else:
                    log_message(
                        CHARACTERS[character]["name"],
                        '이벤트 기억: IGNORE',
                    )

            except Exception as e:
                log_message(
                    CHARACTERS[character]["name"],
                    '이벤트 기억 저장 오류:',
                    type(e).__name__,
                    '-',
                    e,
                )

    finally:
        dynamic_event_channels[
            channel_id
        ] = False

        reset_bot_chain(
            channel_id,
            "사용자 대화 시작"
        )

        # 사용자가 새로 대화에 참여하면 이전 봇끼리 대화 피로를 초기화한다.
        reset_conversation_fatigue(channel_id)

        for event_character in participants:
            defer_character_movement(
                event_character,
                5,
                15,
                "다이나믹 이벤트 직후 여유"
            )

        log_message(
            "General",
            '=== 다이나믹 이벤트 종료 ===',
        )

# 다이나믹 이벤트 발생 루프(3시간~6시간, 15%)
async def dynamic_event_loop():
    while True:
        wait_seconds = random.randint(
            60 * 60 * 3,
            60 * 60 * 6
        )
        log_message(
            "General",
            "다이나믹 이벤트 판단까지",
            wait_seconds // 60,
            "분"
        )
        await asyncio.sleep(wait_seconds)

        # 같은 장소에 있고, 깨어 있고, 외출 중이 아닌 캐릭터만 후보
        by_place = defaultdict(list)
        for character in CHARACTERS:
            state = character_state[character]
            place = current_place.get(character)
            if place and not state["sleeping"] and not state["away"]:
                by_place[place].append(character)

        candidate_groups = [
            (place, chars)
            for place, chars in by_place.items()
            if len(chars) >= 2
        ]

        if not candidate_groups:
            log_message("General", "다이나믹 이벤트: 함께 있는 깨어 있는 캐릭터가 없어 건너뜀")
            continue

        if random.random() >= 0.15:
            log_message("General", "다이나믹 이벤트: 발생 안 함")
            continue

        place, chars = random.choice(candidate_groups)
        participants = random.sample(chars, 2)
        await run_dynamic_event(place, participants)

# 다이나믹 이벤트 장기 기억 저장 판단
def generate_dynamic_event_memory(
    character,
    place,
    event_seed,
    event_history
):
    config = CHARACTERS[
        character
    ]

    prompt = load_character_prompt(
        character
    )

    history_text = "\n".join(
        event_history
    )

    input_text = f"""
현재 캐릭터:
{config["name"]}

장소:
{place}

이벤트의 시작 상황:
{event_seed}

이벤트 진행:
{history_text}

이 사건이 끝났다.

이 캐릭터의 장기기억으로 남길 만큼
의미 있는 사건인지 매우 엄격하게 판단한다.

기본 판단은 IGNORE다.

반드시 아래 형식 중 하나만 출력한다.

IGNORE

또는

MEMORY|기억 내용

MEMORY를 선택하려면 아래 조건 중
최소 하나가 분명하게 충족되어야 한다.

1. 두 캐릭터의 관계에 의미 있는 변화가 있었다.
   예:
   - 서로에게 새로운 면을 알게 됨
   - 갈등이 생기거나 해소됨
   - 신뢰, 호감, 불신 등에 영향을 줄 만한 일이 있었음

2. 나중에 다시 언급할 가치가 있는
   특이하거나 인상적인 사건이 실제로 있었다.

3. 캐릭터가 상대방 또는 사용자의
   새로운 중요한 정보를 알게 되었다.

4. 이후 행동이나 대화에 영향을 줄 만한
   약속, 결정, 계획이 생겼다.

IGNORE해야 하는 경우:
- 평범한 잡담만 했다.
- 작은 물건을 발견하거나 정리했다.
- 음식을 먹거나 음료를 마셨다.
- 물건을 떨어뜨리거나 조금 쏟았다.
- 가벼운 농담이나 사소한 말다툼만 있었다.
- 특별한 결과 없이 일상적인 일이 끝났다.
- 나중에 다시 기억하지 않아도 아무 영향이 없는 사건이다.
- 단지 다이나믹 이벤트였다는 이유만으로 저장하면 안 된다.

중요:
- 애매하면 반드시 IGNORE한다.
- 대부분의 일상 이벤트는 IGNORE가 정상이다.
- MEMORY는 드물게 선택해야 한다.
- MEMORY를 만들기 위해 사건의 의미를 과장하지 않는다.
- 실제 이벤트에 없던 감정 변화나 관계 변화를 만들어내지 않는다.

MEMORY를 선택한다면:
- 이 캐릭터의 관점에서 작성한다.
- 이벤트에서 실제로 일어난 내용만 사용한다.
- 1문장으로 짧게 작성한다.
"""

    response = openai_client.responses.create(
        model="gpt-5.6-luna",
        instructions=prompt,
        input=input_text
    )

    result = (
        response.output_text
        .strip()
    )

    if result.upper() == "IGNORE":
        return None

    if result.upper().startswith(
        "MEMORY|"
    ):
        memory = result.split(
            "|",
            1
        )[1].strip()

        if memory:
            return memory

    return None

# ===================================
# 상태 대시보드
# -----------------------------------

def get_status_message_id(status_key):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT message_id FROM status_messages WHERE status_key = ?",        (status_key,)
    )
    row = cursor.fetchone()
    conn.close()
    return int(row[0]) if row else None


def save_status_message_id(status_key, message_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO status_messages (status_key, message_id)
        VALUES (?, ?)
        ON CONFLICT(status_key)
        DO UPDATE SET message_id = excluded.message_id
    """, (status_key, str(message_id)))
    conn.commit()
    conn.close()


def get_status_meta(meta_key):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT meta_value FROM status_meta WHERE meta_key=?", (meta_key,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None


def set_status_meta(meta_key, meta_value):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO status_meta (meta_key, meta_value)
        VALUES (?, ?)
        ON CONFLICT(meta_key)
        DO UPDATE SET meta_value=excluded.meta_value
    """, (meta_key, str(meta_value)))
    conn.commit()
    conn.close()




def _level_text(value, kind="normal"):
    value = float(value)
    if kind == "fatigue":
        if value >= 80:
            return "매우 높음"
        if value >= 60:
            return "높음"
        if value >= 30:
            return "보통"
        return "낮음"
    if kind == "hunger":
        if value >= 80:
            return "매우 높음"
        if value >= 60:
            return "높음"
        if value >= 30:
            return "보통"
        return "낮음"
    return str(int(value))


def get_character_status_text(character):
    state = character_state[character]
    if state["sleeping"]:
        return "자는 중"
    if state["away"]:
        return "외출 중"
    return current_activity.get(character) or "쉬는 중"


def get_sleep_window_datetimes(character, now=None):
    """현재 진행 중이거나 다음에 예정된 수면 구간을 설정된 생활 시간대의 aware datetime으로 반환한다."""
    now = now or now_kst()
    sched = ensure_daily_sleep_schedule(character)
    sleep_min = sched["sleep_minute"]
    wake_min = sched["wake_minute"]
    minute_now = now.hour * 60 + now.minute

    # 새벽~기상 전에는 오늘의 수면 구간, 기상 이후에는 다음 날 새벽 구간을 표시한다.
    target_date = now.date() if minute_now < wake_min else now.date() + timedelta(days=1)

    sleep_dt = datetime.combine(
        target_date,
        time(hour=sleep_min // 60, minute=sleep_min % 60),
        tzinfo=KST,
    )
    wake_dt = datetime.combine(
        target_date,
        time(hour=wake_min // 60, minute=wake_min % 60),
        tzinfo=KST,
    )
    return sleep_dt, wake_dt


def build_world_status_embed():
    ensure_daily_weather()
    period_names = {
        "morning": "아침",
        "day": "낮",
        "evening": "저녁",
        "night": "밤",
        "late_night": "새벽",
    }
    embed = discord.Embed(title="🌍 현재 환경")
    for label,value in calendar_display(CUSTOM_SETTINGS, now_kst()).items():
        embed.add_field(name=label, value=value, inline=True)
    events = visible_events(CUSTOM_SETTINGS, now_kst(), public_only=True)
    if events:
        embed.add_field(name="오늘의 공개 기념일·행사", value="\n".join(e['name'] for e in events)[:1024], inline=False)
    embed.add_field(
        name="🌦️ 날씨",
        value=daily_world["weather"],
        inline=True,
    )
    embed.add_field(
        name="🌡️ 기온",
        value=f'{daily_world["temperature"]}°C',
        inline=True,
    )
    embed.add_field(
        name="🕒 시간대",
        value=period_names.get(get_time_period(), get_time_period()),
        inline=True,
    )
    embed.add_field(
        name="🌆 현재 도시 사건",
        value=get_city_event_status_text(),
        inline=False,
    )
    embed.set_footer(text="범용 환경 상태 · 모든 캐릭터에게 동일하게 적용")
    return embed


def build_character_status_embed(character):
    config = CHARACTERS[character]
    update_character_needs(character)
    state = character_state[character]
    sleep_dt, wake_dt = get_sleep_window_datetimes(character)
    sleep_ts = int(sleep_dt.timestamp())
    wake_ts = int(wake_dt.timestamp())

    embed = discord.Embed(title=config["name"])
    place = current_place.get(character)
    parent_place = get_parent_place(place)
    if parent_place and parent_place != place:
        location_value = (
            f"상위 장소: **{parent_place}**\n"
            f"현재 위치: **{place}**"
        )
    else:
        location_value = (
            f"현재 위치: **{place or '외출 중 / 알 수 없음'}**"
        )

    embed.add_field(
        name="📍 위치",
        value=location_value,
        inline=True,
    )
    embed.add_field(
        name="💬 상태",
        value=get_character_status_text(character),
        inline=True,
    )
    embed.add_field(
        name="🎭 기분",
        value=state.get("mood", "평온"),
        inline=True,
    )
    embed.add_field(
        name="🥱 피로도",
        value=_level_text(state.get("fatigue", 0), "fatigue"),
        inline=True,
    )
    embed.add_field(
        name="🍽️ 배고픔",
        value=_level_text(state.get("hunger", 0), "hunger"),
        inline=True,
    )
    embed.add_field(
        name="🛏️ 수면시간",
        value=(f"{sleep_dt:%H:%M} ~ {wake_dt:%H:%M}" if feature_enabled("world_calendar") and CUSTOM_SETTINGS.get("calendar",{}).get("mode")=="virtual" else f"<t:{sleep_ts}:t> ~ <t:{wake_ts}:t>"),
        inline=False,
    )
    if state["sleeping"]:
        embed.add_field(
            name="⏰ 기상까지",
            value=(f"약 {max(0, int((wake_dt-now_kst()).total_seconds()/60))}분" if feature_enabled("world_calendar") and CUSTOM_SETTINGS.get("calendar",{}).get("mode")=="virtual" else f"<t:{wake_ts}:R>"),
            inline=False,
        )
    embed.set_footer(text="상태가 바뀌면 이 메시지가 자동으로 갱신됩니다.")
    return embed


status_client = discord.Client(intents=discord.Intents.default())
status_tree = app_commands.CommandTree(status_client)
status_last_payloads = {}
_status_commands_synced = False

CHARACTER_COMMAND_CHOICES = [
    app_commands.Choice(name=CHARACTERS[key]["name"], value=key)
    for key in STATUS_CHARACTER_ORDER
    if key in CHARACTERS
]

RELATION_COMMAND_CHOICES = [
    app_commands.Choice(name=label, value=key)
    for key, (label, _instruction) in SIMPLE_RELATIONS.items()
]

PLACE_COMMAND_CHOICES = [
    app_commands.Choice(name=place, value=place)
    for place in PLACE_CHANNELS[:25]
]


async def character_command_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    query = (current or "").strip().lower()
    matches = []

    for key in STATUS_CHARACTER_ORDER:
        if key not in CHARACTERS:
            continue

        name = CHARACTERS[key]["name"]
        searchable = f"{name} {key}".lower()

        if not query or query in searchable:
            matches.append(app_commands.Choice(name=name, value=key))

        if len(matches) >= 25:
            break

    return matches


async def place_command_autocomplete(
    interaction: discord.Interaction,
    current: str,
):
    query = (current or "").strip().lower()
    matches = []

    for place in PLACE_CHANNELS:
        if not query or query in place.lower():
            matches.append(app_commands.Choice(name=place, value=place))

        if len(matches) >= 25:
            break

    return matches


def find_status_channel():
    for guild in status_client.guilds:
        if DISCORD_GUILD_ID and str(guild.id) != str(DISCORD_GUILD_ID):
            continue
        for channel in guild.text_channels:
            if channel.name == STATUS_CHANNEL_NAME:
                permissions = channel.permissions_for(guild.me)
                if permissions.view_channel and permissions.send_messages:
                    return channel
    return None


async def upsert_status_message(channel, status_key, embed):
    payload = embed.to_dict()
    if status_last_payloads.get(status_key) == payload:
        return

    message = None
    message_id = get_status_message_id(status_key)
    if message_id:
        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            message = None

    if message is None:
        message = await channel.send(embed=embed)
        save_status_message_id(status_key, message.id)
    else:
        await message.edit(embed=embed)

    status_last_payloads[status_key] = payload


async def ensure_status_character_order(channel):
    enabled_order = [
        character
        for character in STATUS_CHARACTER_ORDER
        if character in CHARACTERS and CHARACTERS[character].get("token")
    ]
    signature = "|".join(enabled_order)
    if get_status_meta("character_order") == signature:
        return

    # Discord 메시지는 edit만으로 순서를 바꿀 수 없으므로,
    # 캐릭터 구성이/순서가 바뀐 첫 실행에 한해 기존 캐릭터 임베드를 지우고 다시 만든다.
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT status_key, message_id FROM status_messages WHERE status_key LIKE 'character:%'"
    )
    rows = cursor.fetchall()
    conn.close()

    for status_key, message_id in rows:
        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            pass
        status_last_payloads.pop(status_key, None)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM status_messages WHERE status_key LIKE 'character:%'")
    conn.commit()
    conn.close()

    set_status_meta("character_order", signature)
    log_message("Status", "캐릭터 상태 순서 재구성:", " → ".join(CHARACTERS[c]["name"] for c in enabled_order))


async def refresh_status_dashboard():
    channel = find_status_channel()
    if channel is None:
        log_message(
            "Status",
            f'상태 채널을 찾을 수 없음: #{STATUS_CHANNEL_NAME}',
        )
        return False

    await upsert_status_message(
        channel,
        "world",
        build_world_status_embed(),
    )

    await ensure_status_character_order(channel)

    for character in STATUS_CHARACTER_ORDER:
        if character in CHARACTERS and CHARACTERS[character].get("token"):
            await upsert_status_message(
                channel,
                f"character:{character}",
                build_character_status_embed(character),
            )
    return True



def _is_admin_interaction(interaction):
    permissions = getattr(interaction.user, "guild_permissions", None)
    return bool(permissions and permissions.administrator)


def _is_command_channel(interaction):
    # .env에서 채널을 지정하지 않은 경우에는 기존처럼 어디서든 관리자 명령 사용 가능.
    if not COMMAND_CHANNEL_ID:
        return True
    return interaction.channel_id == COMMAND_CHANNEL_ID


async def _check_admin_command_access(interaction):
    if not _is_admin_interaction(interaction):
        await interaction.response.send_message(
            "관리자만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return False

    if not _is_command_channel(interaction):
        channel_mention = f"<#{COMMAND_CHANNEL_ID}>" if COMMAND_CHANNEL_ID else "명령어 전용 채널"
        await interaction.response.send_message(
            f"이 명령은 {channel_mention}에서만 사용할 수 있습니다.",
            ephemeral=True,
        )
        return False

    return True


def _normalize_discord_user_id(user_id_text):
    """
    서버에 아직 없는 사용자도 관계를 미리 등록할 수 있도록
    Discord snowflake ID를 문자열로 검증한다.
    """
    value = str(user_id_text or "").strip()
    if not value.isdigit():
        return None

    # Discord snowflake는 현재 보통 17~20자리지만,
    # 지나치게 빡빡하게 막지 않도록 안전한 범위만 검사한다.
    if not (15 <= len(value) <= 22):
        return None

    try:
        numeric = int(value)
    except ValueError:
        return None

    if numeric <= 0:
        return None

    return value



@status_tree.command(
    name="setcityevent",
    description="현재 도시 사건을 수동으로 설정합니다."
)
@app_commands.describe(
    event="도시에서 벌어지는 사건 내용",
    duration_minutes="지속시간(분, 10~1440)",
    place="사건이 벌어지는 장소. 비우면 도시 전체",
)
@app_commands.autocomplete(place=place_command_autocomplete)
async def setcityevent_command(
    interaction: discord.Interaction,
    event: str,
    duration_minutes: int,
    place: str | None = None,
):
    if not await _check_admin_command_access(interaction):
        return

    event = (event or "").strip()
    if not event:
        await interaction.response.send_message(
            "도시 사건 내용을 입력해주세요.",
            ephemeral=True,
        )
        return

    if len(event) > 1200:
        await interaction.response.send_message(
            "도시 사건 내용은 1200자 이하로 입력해주세요.",
            ephemeral=True,
        )
        return

    if not (10 <= duration_minutes <= 1440):
        await interaction.response.send_message(
            "지속시간은 10~1440분 사이로 입력해주세요.",
            ephemeral=True,
        )
        return

    if place and place not in PLACE_CHANNELS:
        await interaction.response.send_message(
            "알 수 없는 장소입니다. 자동완성 목록에서 선택해주세요.",
            ephemeral=True,
        )
        return

    set_city_event_state(
        event,
        duration_minutes,
        place=place,
        source="manual",
    )

    await interaction.response.send_message(
        (
            f"🌆 도시 사건을 설정했습니다.\n"
            f"장소: **{place or '도시 전체'}**\n"
            f"내용: {event}\n"
            f"지속시간: **{duration_minutes}분**"
        ),
        ephemeral=True,
    )


@status_tree.command(
    name="cityevent",
    description="현재 진행 중인 도시 사건을 확인합니다."
)
async def cityevent_command(interaction: discord.Interaction):
    if not await _check_admin_command_access(interaction):
        return

    await interaction.response.send_message(
        get_city_event_status_text(),
        ephemeral=True,
    )


@status_tree.command(
    name="clearcityevent",
    description="현재 진행 중인 도시 사건을 즉시 종료합니다."
)
async def clearcityevent_command(interaction: discord.Interaction):
    if not await _check_admin_command_access(interaction):
        return

    cleanup_city_event()
    if not daily_world.get("city_event"):
        await interaction.response.send_message(
            "현재 종료할 도시 사건이 없습니다.",
            ephemeral=True,
        )
        return

    previous = daily_world.get("city_event")
    clear_city_event_state("관리자 명령")

    await interaction.response.send_message(
        f"🌆 도시 사건을 종료했습니다.\n이전 사건: {previous}",
        ephemeral=True,
    )


@status_tree.command(
    name="setuserprofile",
    description="사용자의 역할극 캐릭터 고정 프로필을 저장합니다."
)
@app_commands.describe(
    user="프로필을 설정할 사용자",
    profile="이름/나이/출신/외형/성격/배경 등 역할극 캐릭터 설정(최대 3500자)",
)
async def setuserprofile_command(
    interaction: discord.Interaction,
    user: discord.Member,
    profile: str,
):
    if not await _check_admin_command_access(interaction):
        return

    profile = (profile or "").strip()
    if not profile:
        await interaction.response.send_message(
            "프로필 내용을 입력해주세요.",
            ephemeral=True,
        )
        return

    if len(profile) > 3500:
        await interaction.response.send_message(
            "사용자 캐릭터 프로필은 3500자 이하로 입력해주세요.",
            ephemeral=True,
        )
        return

    upsert_user_profile(user.id, user.display_name)
    set_user_roleplay_profile(user.id, profile)

    await interaction.response.send_message(
        f"{user.mention}의 역할극용 사용자 캐릭터 프로필을 저장했습니다.",
        ephemeral=True,
    )
    log_message("Status", "사용자 캐릭터 프로필 저장:", user.id, user.display_name)


@status_tree.command(
    name="getuserprofile",
    description="사용자의 역할극 캐릭터 고정 프로필을 확인합니다."
)
@app_commands.describe(user="프로필을 확인할 사용자")
async def getuserprofile_command(
    interaction: discord.Interaction,
    user: discord.Member,
):
    if not await _check_admin_command_access(interaction):
        return

    profile = get_user_roleplay_profile(user.id)
    if not profile:
        await interaction.response.send_message(
            f"{user.mention}에게 등록된 역할극용 사용자 캐릭터 프로필이 없습니다.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"**{user.display_name}의 역할극 캐릭터 프로필**\n{profile}",
        ephemeral=True,
    )


@status_tree.command(
    name="clearuserprofile",
    description="사용자의 역할극 캐릭터 고정 프로필을 삭제합니다."
)
@app_commands.describe(user="프로필을 삭제할 사용자")
async def clearuserprofile_command(
    interaction: discord.Interaction,
    user: discord.Member,
):
    if not await _check_admin_command_access(interaction):
        return

    changed = clear_user_roleplay_profile(user.id)
    await interaction.response.send_message(
        (
            f"{user.mention}의 역할극 캐릭터 프로필을 삭제했습니다."
            if changed else
            "삭제할 역할극 캐릭터 프로필이 없습니다."
        ),
        ephemeral=True,
    )


@status_tree.command(
    name="setuserprofileid",
    description="Discord ID로 역할극 캐릭터 고정 프로필을 저장합니다."
)
@app_commands.describe(
    user_id="대상 사용자의 Discord 사용자 ID",
    profile="이름/나이/출신/외형/성격/배경 등 역할극 캐릭터 설정(최대 3500자)",
)
async def setuserprofileid_command(
    interaction: discord.Interaction,
    user_id: str,
    profile: str,
):
    if not await _check_admin_command_access(interaction):
        return

    normalized_id = _normalize_discord_user_id(user_id)
    if normalized_id is None:
        await interaction.response.send_message(
            "올바른 Discord 사용자 ID를 입력해주세요.",
            ephemeral=True,
        )
        return

    profile = (profile or "").strip()
    if not profile:
        await interaction.response.send_message(
            "프로필 내용을 입력해주세요.",
            ephemeral=True,
        )
        return

    if len(profile) > 3500:
        await interaction.response.send_message(
            "사용자 캐릭터 프로필은 3500자 이하로 입력해주세요.",
            ephemeral=True,
        )
        return

    set_user_roleplay_profile(normalized_id, profile)

    await interaction.response.send_message(
        f"User ID `{normalized_id}`의 역할극용 사용자 캐릭터 프로필을 저장했습니다.",
        ephemeral=True,
    )
    log_message("Status", "ID 사용자 캐릭터 프로필 저장:", normalized_id)


@status_tree.command(
    name="getuserprofileid",
    description="Discord ID의 역할극 캐릭터 고정 프로필을 확인합니다."
)
@app_commands.describe(user_id="확인할 Discord 사용자 ID")
async def getuserprofileid_command(
    interaction: discord.Interaction,
    user_id: str,
):
    if not await _check_admin_command_access(interaction):
        return

    normalized_id = _normalize_discord_user_id(user_id)
    if normalized_id is None:
        await interaction.response.send_message(
            "올바른 Discord 사용자 ID를 입력해주세요.",
            ephemeral=True,
        )
        return

    profile = get_user_roleplay_profile(normalized_id)
    if not profile:
        await interaction.response.send_message(
            f"User ID `{normalized_id}`에 등록된 역할극 캐릭터 프로필이 없습니다.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"**User ID {normalized_id} 역할극 캐릭터 프로필**\n{profile}",
        ephemeral=True,
    )


@status_tree.command(
    name="clearuserprofileid",
    description="Discord ID의 역할극 캐릭터 고정 프로필을 삭제합니다."
)
@app_commands.describe(user_id="프로필을 삭제할 Discord 사용자 ID")
async def clearuserprofileid_command(
    interaction: discord.Interaction,
    user_id: str,
):
    if not await _check_admin_command_access(interaction):
        return

    normalized_id = _normalize_discord_user_id(user_id)
    if normalized_id is None:
        await interaction.response.send_message(
            "올바른 Discord 사용자 ID를 입력해주세요.",
            ephemeral=True,
        )
        return

    changed = clear_user_roleplay_profile(normalized_id)
    await interaction.response.send_message(
        (
            f"User ID `{normalized_id}`의 역할극 캐릭터 프로필을 삭제했습니다."
            if changed else
            "삭제할 역할극 캐릭터 프로필이 없습니다."
        ),
        ephemeral=True,
    )


@status_tree.command(name="setrelation", description="사용자와 캐릭터의 간단한 고정 관계를 설정합니다.")
@app_commands.describe(user="관계를 설정할 사용자", character="캐릭터", relation="간단 관계")
@app_commands.choices(relation=RELATION_COMMAND_CHOICES)
@app_commands.autocomplete(character=character_command_autocomplete)
async def setrelation_command(
    interaction: discord.Interaction,
    user: discord.Member,
    character: str,
    relation: app_commands.Choice[str],
):
    if not await _check_admin_command_access(interaction):
        return
    set_user_character_relation(user.id, character, relation_type=relation.value)
    label = SIMPLE_RELATIONS[relation.value][0]
    await interaction.response.send_message(
        f"{user.mention} ↔ **{CHARACTERS[character]['name']}** 관계를 **{label}**(으)로 설정했습니다.",
        ephemeral=True,
    )
    log_message("Status", "간단 관계 설정:", user.id, CHARACTERS[character]["name"], label)


@status_tree.command(name="setspecialrelation", description="사용자와 캐릭터의 복잡한 고정 관계/설정을 저장합니다.")
@app_commands.describe(user="관계를 설정할 사용자", character="캐릭터", context="자유형 관계 설정(최대 1800자)")
@app_commands.autocomplete(character=character_command_autocomplete)
async def setspecialrelation_command(
    interaction: discord.Interaction,
    user: discord.Member,
    character: str,
    context: str,
):
    if not await _check_admin_command_access(interaction):
        return
    context = (context or "").strip()
    if not context:
        await interaction.response.send_message("관계 설정 내용을 입력해주세요.", ephemeral=True)
        return
    if len(context) > 1800:
        await interaction.response.send_message("복잡한 관계 설정은 1800자 이하로 입력해주세요.", ephemeral=True)
        return
    set_user_character_relation(user.id, character, custom_context=context)
    await interaction.response.send_message(
        f"{user.mention} ↔ **{CHARACTERS[character]['name']}** 복잡 관계 설정을 저장했습니다.",
        ephemeral=True,
    )
    log_message("Status", "복잡 관계 설정:", user.id, CHARACTERS[character]["name"])


@status_tree.command(name="getrelation", description="사용자와 캐릭터의 고정 관계 설정을 확인합니다.")
@app_commands.describe(user="확인할 사용자", character="캐릭터")
@app_commands.autocomplete(character=character_command_autocomplete)
async def getrelation_command(
    interaction: discord.Interaction,
    user: discord.Member,
    character: str,
):
    if not await _check_admin_command_access(interaction):
        return
    relation_type, custom_context = get_user_character_relation(user.id, character)
    if not relation_type and not custom_context:
        await interaction.response.send_message(
            f"{user.mention} ↔ **{CHARACTERS[character]['name']}** 고정 관계 설정이 없습니다.",
            ephemeral=True,
        )
        return
    label = SIMPLE_RELATIONS.get(relation_type, (relation_type or "없음", ""))[0] if relation_type else "없음"
    custom = custom_context or "없음"
    await interaction.response.send_message(
        f"**{CHARACTERS[character]['name']} / {user.display_name}**\\n"
        f"간단 관계: **{label}**\\n"
        f"복잡 관계: {custom}",
        ephemeral=True,
    )


@status_tree.command(name="clearrelation", description="사용자와 캐릭터의 고정 관계 설정을 모두 삭제합니다.")
@app_commands.describe(user="관계를 지울 사용자", character="캐릭터")
@app_commands.autocomplete(character=character_command_autocomplete)
async def clearrelation_command(
    interaction: discord.Interaction,
    user: discord.Member,
    character: str,
):
    if not await _check_admin_command_access(interaction):
        return
    changed = clear_user_character_relation(user.id, character)
    await interaction.response.send_message(
        (
            f"{user.mention} ↔ **{CHARACTERS[character]['name']}** 고정 관계 설정을 삭제했습니다."
            if changed else
            "삭제할 고정 관계 설정이 없습니다."
        ),
        ephemeral=True,
    )
    if changed:
        log_message("Status", "고정 관계 삭제:", user.id, CHARACTERS[character]["name"])



@status_tree.command(
    name="setrelationid",
    description="서버에 없는 사용자도 Discord ID로 간단한 고정 관계를 설정합니다."
)
@app_commands.describe(
    user_id="대상 사용자의 Discord 사용자 ID",
    character="캐릭터",
    relation="간단 관계",
)
@app_commands.choices(
    relation=RELATION_COMMAND_CHOICES,
)
@app_commands.autocomplete(
    character=character_command_autocomplete,
)
async def setrelationid_command(
    interaction: discord.Interaction,
    user_id: str,
    character: str,
    relation: app_commands.Choice[str],
):
    if not await _check_admin_command_access(interaction):
        return

    normalized_id = _normalize_discord_user_id(user_id)
    if normalized_id is None:
        await interaction.response.send_message(
            "올바른 Discord 사용자 ID를 입력해주세요. 예: `123456789012345678`",
            ephemeral=True,
        )
        return

    set_user_character_relation(
        normalized_id,
        character,
        relation_type=relation.value,
    )

    label = SIMPLE_RELATIONS[relation.value][0]
    await interaction.response.send_message(
        f"User ID `{normalized_id}` ↔ **{CHARACTERS[character]['name']}** "
        f"관계를 **{label}**(으)로 설정했습니다.",
        ephemeral=True,
    )
    log_message(
        "Status",
        "ID 간단 관계 설정:",
        normalized_id,
        CHARACTERS[character]["name"],
        label,
    )


@status_tree.command(
    name="setspecialrelationid",
    description="서버에 없는 사용자도 Discord ID로 복잡한 고정 관계를 저장합니다."
)
@app_commands.describe(
    user_id="대상 사용자의 Discord 사용자 ID",
    character="캐릭터",
    context="자유형 관계 설정(최대 1800자)",
)
@app_commands.autocomplete(character=character_command_autocomplete)
async def setspecialrelationid_command(
    interaction: discord.Interaction,
    user_id: str,
    character: str,
    context: str,
):
    if not await _check_admin_command_access(interaction):
        return

    normalized_id = _normalize_discord_user_id(user_id)
    if normalized_id is None:
        await interaction.response.send_message(
            "올바른 Discord 사용자 ID를 입력해주세요. 예: `123456789012345678`",
            ephemeral=True,
        )
        return

    context = (context or "").strip()
    if not context:
        await interaction.response.send_message(
            "관계 설정 내용을 입력해주세요.",
            ephemeral=True,
        )
        return

    if len(context) > 1800:
        await interaction.response.send_message(
            "복잡한 관계 설정은 1800자 이하로 입력해주세요.",
            ephemeral=True,
        )
        return

    set_user_character_relation(
        normalized_id,
        character,
        custom_context=context,
    )

    await interaction.response.send_message(
        f"User ID `{normalized_id}` ↔ **{CHARACTERS[character]['name']}** "
        "복잡 관계 설정을 저장했습니다.",
        ephemeral=True,
    )
    log_message(
        "Status",
        "ID 복잡 관계 설정:",
        normalized_id,
        CHARACTERS[character]["name"],
    )


@status_tree.command(
    name="getrelationid",
    description="Discord 사용자 ID와 캐릭터의 고정 관계 설정을 확인합니다."
)
@app_commands.describe(
    user_id="확인할 사용자의 Discord 사용자 ID",
    character="캐릭터",
)
@app_commands.autocomplete(character=character_command_autocomplete)
async def getrelationid_command(
    interaction: discord.Interaction,
    user_id: str,
    character: str,
):
    if not await _check_admin_command_access(interaction):
        return

    normalized_id = _normalize_discord_user_id(user_id)
    if normalized_id is None:
        await interaction.response.send_message(
            "올바른 Discord 사용자 ID를 입력해주세요. 예: `123456789012345678`",
            ephemeral=True,
        )
        return

    relation_type, custom_context = get_user_character_relation(
        normalized_id,
        character,
    )

    if not relation_type and not custom_context:
        await interaction.response.send_message(
            f"User ID `{normalized_id}` ↔ **{CHARACTERS[character]['name']}** "
            "고정 관계 설정이 없습니다.",
            ephemeral=True,
        )
        return

    label = (
        SIMPLE_RELATIONS.get(
            relation_type,
            (relation_type or "없음", ""),
        )[0]
        if relation_type
        else "없음"
    )
    custom = custom_context or "없음"

    await interaction.response.send_message(
        f"**{CHARACTERS[character]['name']} / User ID {normalized_id}**\n"
        f"간단 관계: **{label}**\n"
        f"복잡 관계: {custom}",
        ephemeral=True,
    )


@status_tree.command(
    name="clearrelationid",
    description="Discord 사용자 ID와 캐릭터의 고정 관계 설정을 모두 삭제합니다."
)
@app_commands.describe(
    user_id="관계를 지울 사용자의 Discord 사용자 ID",
    character="캐릭터",
)
@app_commands.autocomplete(character=character_command_autocomplete)
async def clearrelationid_command(
    interaction: discord.Interaction,
    user_id: str,    character: str,
):
    if not await _check_admin_command_access(interaction):
        return

    normalized_id = _normalize_discord_user_id(user_id)
    if normalized_id is None:
        await interaction.response.send_message(
            "올바른 Discord 사용자 ID를 입력해주세요. 예: `123456789012345678`",
            ephemeral=True,
        )
        return

    changed = clear_user_character_relation(
        normalized_id,
        character,
    )

    await interaction.response.send_message(
        (
            f"User ID `{normalized_id}` ↔ **{CHARACTERS[character]['name']}** "
            "고정 관계 설정을 삭제했습니다."
            if changed
            else "삭제할 고정 관계 설정이 없습니다."
        ),
        ephemeral=True,
    )

    if changed:
        log_message(
            "Status",
            "ID 고정 관계 삭제:",
            normalized_id,
            CHARACTERS[character]["name"],
        )



@status_tree.command(
    name="setcharrelation",
    description="두 캐릭터 사이의 장기 관계 발전 방향을 설정합니다."
)
@app_commands.describe(
    character_a="첫 번째 캐릭터",
    character_b="두 번째 캐릭터",
    context="장기 관계 방향/가능한 발전/금지할 발전 등을 자유롭게 입력 (최대 1800자)",
)
@app_commands.autocomplete(
    character_a=character_command_autocomplete,
    character_b=character_command_autocomplete,
)
async def setcharrelation_command(
    interaction: discord.Interaction,
    character_a: str,
    character_b: str,
    context: str,
):
    if not await _check_admin_command_access(interaction):
        return

    if character_a == character_b:
        await interaction.response.send_message(
            "같은 캐릭터끼리는 관계를 설정할 수 없습니다.",
            ephemeral=True,
        )
        return

    context = (context or "").strip()
    if not context:
        await interaction.response.send_message(
            "관계 방향 설정 내용을 입력해주세요.",
            ephemeral=True,
        )
        return

    if len(context) > 1800:
        await interaction.response.send_message(
            "캐릭터 관계 방향은 1800자 이하로 입력해주세요.",
            ephemeral=True,
        )
        return

    set_character_relationship_goal(
        character_a,
        character_b,
        context,
    )

    name_a = CHARACTERS[character_a]["name"]
    name_b = CHARACTERS[character_b]["name"]

    await interaction.response.send_message(
        f"**{name_a} ↔ {name_b}** 장기 관계 방향을 저장했습니다.",
        ephemeral=True,
    )
    log_message(
        "Status",
        "캐릭터 장기 관계 설정:",
        name_a,
        "↔",
        name_b,
    )


@status_tree.command(
    name="getcharrelation",
    description="두 캐릭터 사이의 장기 관계 방향을 확인합니다."
)
@app_commands.describe(
    character_a="첫 번째 캐릭터",
    character_b="두 번째 캐릭터",
)
@app_commands.autocomplete(
    character_a=character_command_autocomplete,
    character_b=character_command_autocomplete,
)
async def getcharrelation_command(
    interaction: discord.Interaction,
    character_a: str,
    character_b: str,
):
    if not await _check_admin_command_access(interaction):
        return

    if character_a == character_b:
        await interaction.response.send_message(
            "같은 캐릭터끼리는 관계를 조회할 수 없습니다.",
            ephemeral=True,
        )
        return

    context = get_character_relationship_goal(
        character_a,
        character_b,
    )

    name_a = CHARACTERS[character_a]["name"]
    name_b = CHARACTERS[character_b]["name"]

    if not context:
        await interaction.response.send_message(
            f"**{name_a} ↔ {name_b}** 장기 관계 방향 설정이 없습니다.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"**{name_a} ↔ {name_b}**\n{context}",
        ephemeral=True,
    )


@status_tree.command(
    name="clearcharrelation",
    description="두 캐릭터 사이의 장기 관계 방향 설정을 삭제합니다."
)
@app_commands.describe(
    character_a="첫 번째 캐릭터",
    character_b="두 번째 캐릭터",
)
@app_commands.autocomplete(
    character_a=character_command_autocomplete,
    character_b=character_command_autocomplete,
)
async def clearcharrelation_command(
    interaction: discord.Interaction,
    character_a: str,
    character_b: str,
):
    if not await _check_admin_command_access(interaction):
        return

    if character_a == character_b:
        await interaction.response.send_message(
            "같은 캐릭터끼리는 관계를 삭제할 수 없습니다.",
            ephemeral=True,
        )
        return

    changed = clear_character_relationship_goal(
        character_a,
        character_b,
    )

    name_a = CHARACTERS[character_a]["name"]
    name_b = CHARACTERS[character_b]["name"]

    await interaction.response.send_message(
        (
            f"**{name_a} ↔ {name_b}** 장기 관계 방향을 삭제했습니다."
            if changed
            else "삭제할 장기 관계 방향 설정이 없습니다."
        ),
        ephemeral=True,
    )

    if changed:
        log_message(
            "Status",
            "캐릭터 장기 관계 삭제:",
            name_a,
            "↔",
            name_b,
        )




@status_tree.command(name="movechar", description="캐릭터를 지정한 장소로 즉시 강제 이동시킵니다.")
@app_commands.describe(character="이동할 캐릭터", place="이동할 장소")
@app_commands.autocomplete(character=character_command_autocomplete, place=place_command_autocomplete)
async def movechar_command(interaction: discord.Interaction, character: str, place: str):
    if not await _check_admin_command_access(interaction):
        return
    key = character
    destination = place
    old = current_place.get(key)
    current_place[key] = destination
    character_state[key]["away"] = False
    activity_text = await run_ai(choose_activity, key, destination)
    client = clients.get(key)
    if client and client.is_ready():
        await safe_change_presence(
            client,
            character=key,
            status=discord.Status.online,
            activity=discord.CustomActivity(name=get_presence_location_text(key, destination)),
            reason="관리자 강제 이동",
        )
    defer_character_movement(key, 5, 15, "관리자 강제 이동 직후")
    await interaction.response.send_message(f"**{CHARACTERS[key]['name']}**: `{old or '알 수 없음'}` → **{get_location_path_text(destination)}**", ephemeral=True)
    log_message("Status", "관리자 강제 이동:", CHARACTERS[key]["name"], old, "→", destination)


@status_tree.command(name="forceevent", description="두 캐릭터를 지정 장소로 이동시키고 다이나믹 이벤트를 강제로 시작합니다.")
@app_commands.describe(place="이벤트 장소", character_a="첫 번째 캐릭터", character_b="두 번째 캐릭터")
@app_commands.autocomplete(place=place_command_autocomplete, character_a=character_command_autocomplete, character_b=character_command_autocomplete)
async def forceevent_command(interaction: discord.Interaction, place: str, character_a: str, character_b: str):
    if not await _check_admin_command_access(interaction):
        return
    if character_a == character_b:
        await interaction.response.send_message("서로 다른 두 캐릭터를 선택해주세요.", ephemeral=True)
        return
    destination = place
    participants = [character_a, character_b]
    for key in participants:
        current_place[key] = destination
        character_state[key]["away"] = False
        await run_ai(choose_activity, key, destination)
        client = clients.get(key)
        if client and client.is_ready():
            await safe_change_presence(
            client,
            character=key,
            status=discord.Status.online,
            activity=discord.CustomActivity(name=get_presence_location_text(key, destination)),
            reason="관리자 강제 이동",
        )
        defer_character_movement(key, 10, 20, "강제 다이나믹 이벤트")
    await interaction.response.send_message(f"**{destination}**에서 **{CHARACTERS[participants[0]]['name']} ↔ {CHARACTERS[participants[1]]['name']}** 다이나믹 이벤트를 시작합니다.", ephemeral=True)
    log_message("Status", "강제 다이나믹 이벤트:", CHARACTERS[participants[0]]["name"], "↔", CHARACTERS[participants[1]]["name"], "@", destination)
    asyncio.create_task(run_dynamic_event(destination, participants))


@status_tree.command(name="forceconversation", description="두 캐릭터를 같은 장소로 이동시키고 일반 대화를 시작시킵니다.")
@app_commands.describe(place="대화 장소", character_a="먼저 말할 캐릭터", character_b="상대 캐릭터")
@app_commands.autocomplete(place=place_command_autocomplete, character_a=character_command_autocomplete, character_b=character_command_autocomplete)
async def forceconversation_command(interaction: discord.Interaction, place: str, character_a: str, character_b: str):
    if not await _check_admin_command_access(interaction):
        return
    if character_a == character_b:
        await interaction.response.send_message("서로 다른 두 캐릭터를 선택해주세요.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    destination = place
    a, b = character_a, character_b
    for key in (a, b):
        current_place[key] = destination
        character_state[key]["away"] = False
        await run_ai(choose_activity, key, destination)
        client = clients.get(key)
        if client and client.is_ready():
            await safe_change_presence(
            client,
            character=key,
            status=discord.Status.online,
            activity=discord.CustomActivity(name=get_presence_location_text(key, destination)),
            reason="관리자 강제 이동",
        )
        defer_character_movement(key, 10, 20, "강제 캐릭터 대화")
    starter = await run_ai(generate_conversation_starter, a, b, destination)
    channel = find_place_channel(clients[a], destination)
    if channel is None:
        await interaction.followup.send("해당 장소의 Discord 채널을 찾지 못했습니다.", ephemeral=True)
        return
    target_id = bot_character_ids.get(b)
    sent_text = f"<@{target_id}> {starter}" if target_id else starter
    await send_world(channel,sent_text,a,destination)
    save_message(channel.id, CHARACTERS[a]["name"], starter)
    conversation_history[channel.id].append(f"{CHARACTERS[a]['name']} → {CHARACTERS[b]['name']}: {starter}")
    await interaction.followup.send(f"**{destination}**에서 대화를 시작했습니다: **{CHARACTERS[a]['name']} → {CHARACTERS[b]['name']}**", ephemeral=True)
    log_message("Status", "강제 일반 대화:", CHARACTERS[a]["name"], "→", CHARACTERS[b]["name"], "@", destination)


@status_tree.command(name="재료상자", description="현재 장소에 재료 상자를 놓고 발견·정리합니다.")
@app_commands.choices(종류=[app_commands.Choice(name=k,value=k) for k in ('놓기','발견','정리')])
@app_commands.describe(내용="놓기: 빵:3,우유:2 / 발견·정리: 상자 ID")
async def ingredient_box_command(interaction: discord.Interaction, 종류: str, 내용: str):
    if not interaction.guild or not is_main_world_guild_id(interaction.guild.id) or not feature_enabled('ingredient_stock'):
        await interaction.response.send_message('본서버에서 식재료 재고 기능을 켠 뒤 사용하세요.',ephemeral=True);return
    place=normalize_place_name_from_channel(getattr(interaction.channel,'name',''))
    if place not in PLACE_INFO:
        await interaction.response.send_message('등록 장소 채널에서 사용하세요.',ephemeral=True);return
    try:
        if 종류=='놓기':
            contents={}
            for pair in 내용.split(','):
                name,count=pair.rsplit(':',1);contents[name.strip()]=int(count.strip())
            identifier=_world_actions.supply_box(interaction.id,f'user:{interaction.user.id}',place,contents)
            result='재료 상자 #'+identifier+' 도착 (발견 후 정리하면 재고에 추가됩니다.)'
        else:result=_world_actions.handle_box(내용.strip(),f'user:{interaction.user.id}',place,종류)
        await interaction.response.send_message(result,ephemeral=True)
    except ValueError as error:await interaction.response.send_message('상자 입력을 확인하세요: '+str(error),ephemeral=True)


@status_tree.command(name="음식준비", description="현재 장소에 음식 수량·함께 준비한 세트를 등록합니다.")
@app_commands.describe(음식="음식 이름. 같은 세트 구성품은 쉼표로 구분", 수량="준비한 세트 수")
async def prepare_food_command(interaction: discord.Interaction, 음식: str, 수량: app_commands.Range[int,1,100] = 1):
    if not interaction.guild or not is_main_world_guild_id(interaction.guild.id):
        await interaction.response.send_message('본서버의 등록 장소에서 사용하세요.',ephemeral=True);return
    place=normalize_place_name_from_channel(getattr(interaction.channel,'name',''))
    if place not in PLACE_INFO:
        await interaction.response.send_message('등록 장소 채널에서 사용하세요.',ephemeral=True);return
    try:
        names=[n.strip()[:100] for n in 음식.split(',') if n.strip()]
        if len(names)>10:raise ValueError('한 세트 구성품은 최대 10개로 입력하세요.')
        result=_world_actions.prepare_food(interaction.id,f'user:{interaction.user.id}',place,names,int(수량),PLACE_INFO[place],CUSTOM_SETTINGS)
        await interaction.response.send_message(result,ephemeral=True)
    except ValueError as error:await interaction.response.send_message(str(error),ephemeral=True)


@status_tree.command(name="운영진단", description="공개 월드 자료를 AI로 검토하고 관리자 PC에 보고서를 저장합니다.")
async def diagnose_command(interaction: discord.Interaction):
    if not await _check_admin_command_access(interaction):return
    await interaction.response.defer(ephemeral=True,thinking=True)
    try:
        path,_=await asyncio.to_thread(diagnose,DATA_DIR,diagnostic_payload(),_monitor.secrets)
        await interaction.followup.send('관리자 PC에 보고서를 저장했습니다: '+path.name,ephemeral=True)
    except Exception as error:
        await interaction.followup.send('운영 진단 실패: '+str(error),ephemeral=True)


@status_tree.command(name="생활행동", description="본서버 장소에 부탁·쪽지·물건을 남기거나 등록 물건을 관리합니다.")
@app_commands.describe(종류="행동 종류", 이름="물건 이름 또는 기존 부탁·물건 ID", 캐릭터="수신·대여 캐릭터 키", 내용="부탁·쪽지 내용", 비밀="지정 수신자만 참고하는 내용")
@app_commands.choices(종류=[app_commands.Choice(name=k,value=k) for k in ('부탁','쪽지','부탁취소','물건놓기','분실신고','발견','반환','물건회수','대여')])
async def world_action_command(interaction: discord.Interaction, 종류: str, 이름: str = '', 캐릭터: str = '', 내용: str = '', 비밀: bool = False):
    if not interaction.guild or not is_main_world_guild_id(interaction.guild.id):
        await interaction.response.send_message('본서버의 등록 장소에서 사용하세요.',ephemeral=True);return
    flag='world_requests' if 종류 in ('부탁','쪽지','부탁취소') else 'lost_found'
    if not feature_enabled(flag):
        await interaction.response.send_message('셋업에서 해당 생활 기능을 먼저 켜세요.',ephemeral=True);return
    place=normalize_place_name_from_channel(getattr(interaction.channel,'name',''))
    if place not in PLACE_INFO:
        await interaction.response.send_message('등록된 장소 채널에서 사용하세요.',ephemeral=True);return
    if 캐릭터 and 캐릭터 not in CHARACTERS:
        await interaction.response.send_message('설정에 등록된 캐릭터 키를 입력하세요.',ephemeral=True);return
    try:
        result=_world_actions.command(interaction.id,f'user:{interaction.user.id}',place,종류,이름,캐릭터,내용,비밀)
    except ValueError as error:
        await interaction.response.send_message(str(error),ephemeral=True);return
    await interaction.response.send_message(result,ephemeral=True)


@status_tree.command(name="setmood", description="캐릭터의 기분과 이유를 일정 시간 강제로 지정합니다.")
@app_commands.describe(character="캐릭터", mood="기분", reason="기분의 이유", hours="유지 시간(시간)")
@app_commands.autocomplete(character=character_command_autocomplete)
async def setmood_command(interaction: discord.Interaction, character: str, mood: str, reason: str, hours: app_commands.Range[int, 1, 24] = 2):
    if not await _check_admin_command_access(interaction):
        return
    set_mood(character, mood[:60], reason[:300], hours=int(hours))
    await interaction.response.send_message(f"**{CHARACTERS[character]['name']}** 기분을 **{mood[:60]}** 상태로 {hours}시간 설정했습니다.", ephemeral=True)
    log_message("Status", "기분 강제 설정:", CHARACTERS[character]["name"], mood[:60], "|", reason[:120])


@status_tree.command(name="setactivity", description="캐릭터의 현재 활동을 강제로 지정합니다.")
@app_commands.describe(character="캐릭터", activity="현재 활동")
@app_commands.autocomplete(character=character_command_autocomplete)
async def setactivity_command(interaction: discord.Interaction, character: str, activity: str):
    if not await _check_admin_command_access(interaction):
        return
    value = (activity or "").strip()[:120]
    if not value:
        await interaction.response.send_message("활동 내용을 입력해주세요.", ephemeral=True)
        return
    current_activity[character] = value
    client = clients.get(character)
    if client and client.is_ready():
        await safe_change_presence(
            client,
            character=character,
            activity=discord.CustomActivity(name=get_presence_location_text(character)),
            reason="관리자 활동 강제 설정",
        )
    await interaction.response.send_message(f"**{CHARACTERS[character]['name']}** 활동: {value}", ephemeral=True)
    log_message("Status", "활동 강제 설정:", CHARACTERS[character]["name"], value)


@status_tree.command(name="setplaceevent", description="특정 장소에 일정 시간 유지되는 환경 사건/상태를 설정합니다.")
@app_commands.describe(place="장소", event="장소 상태/사건", hours="유지 시간(시간)")
@app_commands.autocomplete(place=place_command_autocomplete)
async def setplaceevent_command(interaction: discord.Interaction, place: str, event: str, hours: app_commands.Range[int, 1, 24] = 4):
    if not await _check_admin_command_access(interaction):
        return
    value = (event or "").strip()[:500]
    if not value:
        await interaction.response.send_message("장소 사건 내용을 입력해주세요.", ephemeral=True)
        return
    set_place_state(place, value, hours=int(hours))
    await interaction.response.send_message(f"**{place}** 장소 사건을 {hours}시간 설정했습니다.\n{value}", ephemeral=True)
    log_message("Status", "장소 사건 설정:", place, "|", value)


@status_tree.command(name="clearevent", description="특정 장소의 환경 사건/상태를 제거합니다.")
@app_commands.describe(place="장소")
@app_commands.autocomplete(place=place_command_autocomplete)
async def clearevent_command(interaction: discord.Interaction, place: str):
    if not await _check_admin_command_access(interaction):
        return
    existed = place_state.pop(place, None)
    await interaction.response.send_message((f"**{place}** 장소 사건을 제거했습니다." if existed else "현재 제거할 장소 사건이 없습니다."), ephemeral=True)
    if existed:
        log_message("Status", "장소 사건 제거:", place)


@status_tree.command(name="wakechar", description="캐릭터를 즉시 깨웁니다.")
@app_commands.describe(character="캐릭터")
@app_commands.autocomplete(character=character_command_autocomplete)
async def wakechar_command(interaction: discord.Interaction, character: str):
    if not await _check_admin_command_access(interaction):
        return
    character_state[character]["sleeping"] = True
    await set_sleeping_state(character, False)
    await interaction.response.send_message(f"**{CHARACTERS[character]['name']}**을(를) 깨웠습니다.", ephemeral=True)
    log_message("Status", "강제 기상:", CHARACTERS[character]["name"])


@status_tree.command(name="sleepchar", description="캐릭터를 즉시 재웁니다.")
@app_commands.describe(character="캐릭터")
@app_commands.autocomplete(character=character_command_autocomplete)
async def sleepchar_command(interaction: discord.Interaction, character: str):
    if not await _check_admin_command_access(interaction):
        return
    character_state[character]["sleeping"] = False
    await set_sleeping_state(character, True)
    await interaction.response.send_message(f"**{CHARACTERS[character]['name']}**을(를) 재웠습니다.", ephemeral=True)
    log_message("Status", "강제 취침:", CHARACTERS[character]["name"])


@status_tree.command(name="pauseauto", description="전체 캐릭터의 새 자율발언을 일시정지합니다.")
async def pauseauto_command(interaction: discord.Interaction):
    global autonomous_global_paused
    if not await _check_admin_command_access(interaction):
        return
    autonomous_global_paused = True
    await interaction.response.send_message("전체 새 자율발언을 일시정지했습니다.", ephemeral=True)
    log_message("Status", "전체 자율발언 일시정지")


@status_tree.command(name="resumeauto", description="전체 캐릭터의 새 자율발언을 다시 허용합니다.")
async def resumeauto_command(interaction: discord.Interaction):
    global autonomous_global_paused
    if not await _check_admin_command_access(interaction):
        return
    autonomous_global_paused = False
    await interaction.response.send_message("전체 새 자율발언을 다시 허용했습니다.", ephemeral=True)
    log_message("Status", "전체 자율발언 재개")


@status_tree.command(name="pausecharauto", description="특정 캐릭터의 새 자율발언만 일시정지합니다.")
@app_commands.describe(character="캐릭터")
@app_commands.autocomplete(character=character_command_autocomplete)
async def pausecharauto_command(interaction: discord.Interaction, character: str):
    if not await _check_admin_command_access(interaction):
        return
    autonomous_paused_characters.add(character)
    await interaction.response.send_message(f"**{CHARACTERS[character]['name']}** 자율발언을 일시정지했습니다.", ephemeral=True)
    log_message("Status", "캐릭터 자율발언 일시정지:", CHARACTERS[character]["name"])


@status_tree.command(name="resumecharauto", description="특정 캐릭터의 새 자율발언을 다시 허용합니다.")
@app_commands.describe(character="캐릭터")
@app_commands.autocomplete(character=character_command_autocomplete)
async def resumecharauto_command(interaction: discord.Interaction, character: str):
    if not await _check_admin_command_access(interaction):
        return
    autonomous_paused_characters.discard(character)
    await interaction.response.send_message(f"**{CHARACTERS[character]['name']}** 자율발언을 다시 허용했습니다.", ephemeral=True)
    log_message("Status", "캐릭터 자율발언 재개:", CHARACTERS[character]["name"])


@status_tree.command(name="cooldownstatus", description="자율발언 캐릭터/장소 쿨다운 현황을 확인합니다.")
async def cooldownstatus_command(interaction: discord.Interaction):
    if not await _check_admin_command_access(interaction):
        return
    now = datetime.now()
    lines = ["**캐릭터 자율발언 쿨다운**"]
    active = False
    for key in STATUS_CHARACTER_ORDER:
        until = autonomous_character_cooldown_until.get(key)
        if until and until > now:
            active = True
            mins = max(0, int((until - now).total_seconds() // 60))
            lines.append(f"• {CHARACTERS[key]['name']}: {mins}분")
    if not active:
        lines.append("• 없음")
    lines.append("\n**장소 자율발언 쿨다운**")
    active = False
    for place, until in sorted(autonomous_place_cooldown_until.items()):
        if until and until > now:
            active = True
            mins = max(0, int((until - now).total_seconds() // 60))
            lines.append(f"• {place}: {mins}분")
    if not active:
        lines.append("• 없음")
    await interaction.response.send_message("\n".join(lines)[:1900], ephemeral=True)


@status_tree.command(name="deletenote", description="캐릭터가 남긴 실제 메모/쪽지를 ID로 삭제합니다.")
@app_commands.describe(note_id="삭제할 메모 ID (CMD 로그의 id=숫자)")
async def deletenote_command(
    interaction: discord.Interaction,
    note_id: int,
):
    if not await _check_admin_command_access(interaction):
        return

    if note_id <= 0:
        await interaction.response.send_message(
            "메모 ID는 1 이상의 숫자여야 합니다.",
            ephemeral=True,
        )
        return

    deleted = delete_character_note(note_id)
    if deleted is None:
        await interaction.response.send_message(
            f"ID **{note_id}**인 메모를 찾지 못했습니다.",
            ephemeral=True,
        )
        return

    _id, from_character, to_character, place, content, read_at = deleted
    from_name = CHARACTERS.get(from_character, {}).get("name", from_character)
    to_name = CHARACTERS.get(to_character, {}).get("name", to_character)

    await interaction.response.send_message(
        f"메모 **#{note_id}** 삭제 완료\n"
        f"**{from_name} → {to_name}** · {place or '장소 미정'}\n"
        f"내용: {content[:300]}",
        ephemeral=True,
    )
    log_message(
        "Status",
        "메모 삭제:",
        f"id={note_id}",
        "|",
        from_name,
        "→",
        to_name,
        "| 위치:",
        place or "장소 미정",
    )


@status_tree.command(name="clearcooldown", description="캐릭터 또는 장소의 자율발언 쿨다운을 강제로 해제합니다.")
@app_commands.describe(character="해제할 캐릭터(선택)", place="해제할 장소(선택)")
@app_commands.autocomplete(character=character_command_autocomplete, place=place_command_autocomplete)
async def clearcooldown_command(interaction: discord.Interaction, character: str | None = None, place: str | None = None):
    if not await _check_admin_command_access(interaction):
        return
    if character is None and place is None:
        await interaction.response.send_message("character 또는 place 중 하나는 선택해주세요.", ephemeral=True)
        return
    parts = []
    if character is not None:
        autonomous_character_cooldown_until.pop(character, None)
        parts.append(CHARACTERS[character]["name"])
    if place is not None:
        autonomous_place_cooldown_until.pop(place, None)
        parts.append(place)
    await interaction.response.send_message("쿨다운 해제: " + " / ".join(parts), ephemeral=True)
    log_message("Status", "자율발언 쿨다운 해제:", *parts)


@status_tree.command(
    name="overview",
    description="메모, 약속, 장기기억의 전체 현황 또는 캐릭터 상세 현황을 확인합니다."
)
@app_commands.describe(
    character="특정 캐릭터의 상세 현황을 볼 때 선택합니다. 비우면 전체 요약을 표시합니다."
)
@app_commands.autocomplete(character=character_command_autocomplete)
async def overview_command(
    interaction: discord.Interaction,
    character: str | None = None,
):
    if not await _check_admin_command_access(interaction):
        return

    # 캐릭터 미지정: 전체 요약
    if character is None:
        lines = []
        total_memories = 0
        total_appointments = 0
        total_notes = 0
        total_unread = 0

        for key in STATUS_CHARACTER_ORDER:
            if key not in CHARACTERS:
                continue

            counts = get_character_overview_counts(key)
            total_memories += counts["memories"]
            total_appointments += counts["appointments"]
            total_notes += counts["incoming_notes"] + counts["outgoing_notes"]
            total_unread += counts["unread_notes"]

            lines.append(
                f"**{CHARACTERS[key]['name']}** — "
                f"기억 {counts['memories']} · "
                f"약속 {counts['appointments']} · "
                f"받은 메모 {counts['incoming_notes']}"
                f"(미확인 {counts['unread_notes']}) · "
                f"보낸 메모 {counts['outgoing_notes']}"
            )

        embed = discord.Embed(
            title="📚 캐릭터 데이터 전체 현황",
            description="\n".join(lines) or "표시할 캐릭터가 없습니다.",
        )
        embed.add_field(
            name="합계",
            value=(
                f"장기기억 **{total_memories}**개\n"
                f"진행 중 약속 **{total_appointments}**개\n"
                f"메모/쪽지 기록 **{total_notes}**건 "
                f"(미확인 수신 **{total_unread}**건)"
            ),
            inline=False,
        )
        embed.set_footer(
            text="특정 캐릭터를 선택하면 최근 장기기억·약속·메모 내용을 확인할 수 있습니다."
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )
        return

    # 캐릭터 지정: 상세
    key = character
    config = CHARACTERS[key]
    counts = get_character_overview_counts(key)
    memories_text, appointments_text, notes_text = (
        get_character_overview_details(key)
    )

    embed = discord.Embed(
        title=f"📚 {config['name']} 데이터 현황",
        description=(
            f"장기기억 **{counts['memories']}**개 · "
            f"진행 중 약속 **{counts['appointments']}**개 · "
            f"받은 메모 **{counts['incoming_notes']}**개 "
            f"(미확인 **{counts['unread_notes']}**) · "
            f"보낸 메모 **{counts['outgoing_notes']}**개"
        ),
    )
    embed.add_field(
        name="🧠 최근 장기기억",
        value=_truncate_embed_field(memories_text),
        inline=False,
    )
    embed.add_field(
        name="📅 진행 중 약속",
        value=_truncate_embed_field(appointments_text),
        inline=False,
    )
    embed.add_field(
        name="📝 최근 메모 / 쪽지",
        value=_truncate_embed_field(notes_text),
        inline=False,
    )
    embed.set_footer(
        text="메모 번호는 /deletenote note_id:<번호>에서 사용할 수 있습니다."
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
    )


@status_tree.command(name="whereis", description="모든 캐릭터의 현재 위치를 확인합니다.")
async def whereis_command(interaction: discord.Interaction):
    if not await _check_admin_command_access(interaction):
        return
    lines = [f"• **{CHARACTERS[key]['name']}** — {get_location_path_text(current_place.get(key))}" for key in STATUS_CHARACTER_ORDER if key in CHARACTERS]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@status_tree.command(name="whoishere", description="특정 장소에 있는 캐릭터를 확인합니다.")
@app_commands.describe(place="확인할 장소")
@app_commands.autocomplete(place=place_command_autocomplete)
async def whoishere_command(interaction: discord.Interaction, place: str):
    if not await _check_admin_command_access(interaction):
        return
    people = [CHARACTERS[key]["name"] for key in STATUS_CHARACTER_ORDER if current_place.get(key) == place]
    await interaction.response.send_message(f"**{place}**: " + (", ".join(people) if people else "현재 캐릭터 없음"), ephemeral=True)


@status_tree.error
async def status_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log_message("Status", "관리 명령 오류:", type(error).__name__, "-", error)
    message = "명령 처리 중 오류가 발생했습니다. CMD 로그를 확인해주세요."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


@status_client.event
async def on_ready():
    set_log_context("world", guild_id=DISCORD_GUILD_ID)
    global _status_commands_synced
    log_message("Status", "로그인 성공:", status_client.user)

    if not _status_commands_synced:
        try:
            if DISCORD_GUILD_ID:
                guild_obj = discord.Object(id=int(DISCORD_GUILD_ID))
                status_tree.copy_global_to(guild=guild_obj)
                synced = await status_tree.sync(guild=guild_obj)
                log_message("Status", f"관리 슬래시 명령 서버 동기화 완료: {len(synced)}개")
            else:
                synced = await status_tree.sync()
                log_message("Status", f"관리 슬래시 명령 글로벌 동기화 완료: {len(synced)}개")
            _status_commands_synced = True
        except Exception as e:
            log_message("Status", "관리 슬래시 명령 동기화 오류:", type(e).__name__, "-", e)


async def status_dashboard_loop():
    await status_client.wait_until_ready()
    while not status_client.is_closed():
        try:
            await refresh_status_dashboard()
        except Exception as e:
            log_message(
                "Status",
                "상태 대시보드 갱신 오류:",
                type(e).__name__,
                "-",
                e,
            )
        await asyncio.sleep(20)



# ------------------------------------------------------------
# 범용 RP 서버 자동 초기화
# ------------------------------------------------------------

def _get_channel_setup_settings():
    raw = CUSTOM_SETTINGS.get("channel_setup", {}) if isinstance(CUSTOM_SETTINGS, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    aliases = raw.get("category_names", {})
    if not isinstance(aliases, dict):
        aliases = {}

    return {
        "create_categories": bool(raw.get("create_categories", True)),
        "create_status_channel": bool(raw.get("create_status_channel", True)),
        "category_names": {
            str(k): str(v)
            for k, v in aliases.items()
            if str(k).strip() and str(v).strip()
        },
    }


async def initialize_rp_server_channels(guild, create_categories=True, create_status_channel=True):
    result = {
        "created_categories": [],
        "created_channels": [],
        "skipped_channels": [],
        "created_status": False,
        "skipped_status": False,
    }

    settings = _get_channel_setup_settings()
    aliases = settings["category_names"]

    category_cache = {c.name: c for c in guild.categories}
    existing_channels = {c.name: c for c in guild.text_channels}

    async def get_category(group_name):
        if not create_categories:
            return None

        group_name = str(group_name or "").strip()
        if not group_name:
            return None

        category_name = aliases.get(group_name, group_name)[:100]
        if category_name in category_cache:
            return category_cache[category_name]

        category = await guild.create_category(
            category_name,
            reason="Generic RP Bot server setup",
        )
        category_cache[category_name] = category
        result["created_categories"].append(category_name)
        return category

    for category_key in aliases:
        await get_category(category_key)

    for place in PLACE_CHANNELS:
        channel_name = PLACE_CHANNEL_NAMES.get(
            place,
            str(place).replace(" ", "-"),
        )

        if channel_name in existing_channels:
            result["skipped_channels"].append(channel_name)
            continue

        category = await get_category(PLACE_GROUPS.get(place))

        channel = await guild.create_text_channel(
            channel_name,
            category=category,
            reason=f"Generic RP Bot place setup: {place}",
        )
        existing_channels[channel.name] = channel
        result["created_channels"].append(channel.name)

    if create_status_channel and STATUS_BOT_TOKEN and feature_enabled("status_dashboard"):
        status_name = STATUS_CHANNEL_NAME or "캐릭터-상태"

        if status_name in existing_channels:
            result["skipped_status"] = True
        else:
            category = await get_category("system")
            channel = await guild.create_text_channel(
                status_name,
                category=category,
                reason="Generic RP Bot status setup",
            )
            existing_channels[channel.name] = channel
            result["created_status"] = True

    return result


# 디스코드 클라이언트
def create_client(character):

    intents = discord.Intents.default()
    intents.message_content = True

    client = discord.Client(
        intents=intents
    )
    character_tree = app_commands.CommandTree(client)
    character_commands_synced = False

    config = CHARACTERS[
        character
    ]

    @client.event
    async def on_ready():
        nonlocal character_commands_synced

        bot_user_characters[
            client.user.id
        ] = character

        bot_character_ids[
            character
        ] = client.user.id

        place = current_place.get(
            character
        )

        if place is None:
            place = choose_place(
                character
            )

        activity = await run_ai(choose_activity,
            character,
            place
        )

        await safe_change_presence(
            client,
            character=character,
            activity=discord.CustomActivity(
                name=get_presence_location_text(character, place)
            ),
            reason="로그인 직후 상태 설정",
        )

        if not character_commands_synced:
            try:
                await character_tree.sync()
                character_commands_synced = True
                log_message(config["name"], "개인 RP 슬래시 명령 동기화 완료")
            except Exception as e:
                log_message(
                    config["name"],
                    "개인 RP 슬래시 명령 동기화 오류:",
                    type(e).__name__,
                    "-",
                    e,
                )

        log_message(
            config["name"],
            "로그인 성공:",
            client.user,
        )

        log_message(
            config["name"],
            "현재 활동:",
            current_activity.get(character) or activity,
        )

        log_message(
            config["name"],
            "현재 위치:",
            place,
        )



    @character_tree.command(
        name="서버초기화",
        description="설정 파일을 기준으로 RP 장소 채널과 카테고리를 자동 생성합니다."
    )
    async def server_initialize_command(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "서버 안에서만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        if not GENERIC_CONFIG_ACTIVE:
            await interaction.response.send_message(
                "범용 RP 설정 모드에서만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        permissions = getattr(interaction.user, "guild_permissions", None)
        if not permissions or not (
            permissions.administrator
            or permissions.manage_guild
            or permissions.manage_channels
        ):
            await interaction.response.send_message(
                "서버 관리 또는 채널 관리 권한이 필요합니다.",
                ephemeral=True,
            )
            return

        me = interaction.guild.me
        if me is None or not me.guild_permissions.manage_channels:
            await interaction.response.send_message(
                "이 봇에게 **채널 관리** 권한을 부여해주세요.",
                ephemeral=True,
            )
            return

        settings = _get_channel_setup_settings()
        await interaction.response.defer(ephemeral=True, thinking=True)

        try:
            result = await initialize_rp_server_channels(
                interaction.guild,
                create_categories=settings["create_categories"],
                create_status_channel=settings["create_status_channel"],
            )

            await interaction.followup.send(
                "**서버 초기화 완료**\\n"
                f"새 카테고리: **{len(result['created_categories'])}개**\\n"
                f"새 장소 채널: **{len(result['created_channels'])}개**\\n"
                f"기존 채널: **{len(result['skipped_channels'])}개**",
                ephemeral=True,
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "채널 생성 권한이 없습니다. 봇의 **채널 관리** 권한을 확인해주세요.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(
                f"초기화 오류: `{type(e).__name__}: {e}`",
                ephemeral=True,
            )

    @character_tree.command(
        name="기능설정보기",
        description="현재 settings.json의 기능 ON/OFF 상태를 확인합니다."
    )
    async def feature_settings_command(interaction: discord.Interaction):
        if not GENERIC_CONFIG_ACTIVE:
            await interaction.response.send_message(
                "범용 RP 설정 모드에서만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        labels = {
            "world_simulation": "월드 시뮬레이션",
            "place_movement": "장소 자동 이동",
            "autonomous_messages": "자율발언",
            "bot_to_bot_chat": "캐릭터끼리 대화",
            "long_term_memory": "장기 기억",
            "relationships": "관계 변화",
            "personal_rp": "개인 RP 서버",
            "dm": "DM",
            "offline_mention_recovery": "오프라인 멘션 복구",
            "sleep_system": "수면",
            "away_system": "외출",
            "appointments": "약속",
            "dynamic_events": "다이나믹 이벤트",
            "city_events": "도시 이벤트",
            "status_dashboard": "상태 대시보드",
            "presence": "Discord 상태메시지",
        }

        lines = ["**기능 설정**"]
        for key, label in labels.items():
            lines.append(
                f"{'✅' if feature_enabled(key) else '❌'} {label}"
            )

        await interaction.response.send_message(
            "\\n".join(lines),
            ephemeral=True,
        )


    @character_tree.command(name="관계설정", description="이 서버에서만 적용되는 사용자-캐릭터 관계를 설정합니다.")
    @app_commands.describe(
        relation="관계 유형 또는 이름",
        description="이 서버에서만 적용할 관계 설명"
    )
    async def personal_relation_set(
        interaction: discord.Interaction,
        relation: str,
        description: str = "",    ):
        if GENERIC_CONFIG_ACTIVE and not feature_enabled("relationships"):
            await interaction.response.send_message(
                "관계 기능이 settings.json에서 비활성화되어 있습니다.",
                ephemeral=True,
            )
            return
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령은 서버 안에서만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        relation = (relation or "").strip()[:80]
        description = (description or "").strip()[:1800]

        if not relation and not description:
            await interaction.response.send_message(
                "관계 유형이나 설명 중 하나는 입력해주세요.",
                ephemeral=True,
            )
            return

        set_guild_user_character_relation(
            interaction.guild.id,
            interaction.user.id,
            character,
            relation_type=relation,
            custom_context=description,
        )

        await interaction.response.send_message(
            f"이 서버에서 **{config['name']}**과(와)의 관계를 저장했습니다.\n"
            f"관계: **{relation or '직접 설명'}**"
            + (f"\n설명: {description}" if description else ""),
            ephemeral=True,
        )

        log_message(
            config["name"],
            "개인 RP 관계 설정:",
            interaction.guild.id,
            interaction.user.id,
            relation or "(설명형)",
        )

    @character_tree.command(name="관계보기", description="이 서버에서만 적용되는 관계를 확인합니다.")
    async def personal_relation_view(interaction: discord.Interaction):
        if interaction.guild is not None:
            set_log_context(
                "world" if is_main_world_guild_id(interaction.guild.id) else "rp",
                guild_id=interaction.guild.id,
                guild_name=getattr(interaction.guild, "name", None),
            )
        else:
            set_log_context("dm")
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령은 서버 안에서만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        relation_type, custom_context = get_guild_user_character_relation(
            interaction.guild.id,
            interaction.user.id,
            character,
        )

        if not relation_type and not custom_context:
            await interaction.response.send_message(
                f"이 서버에서 **{config['name']}**과(와) 따로 설정된 관계가 없습니다.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"**{config['name']}** · 이 서버 전용 관계\n"
            f"관계: **{relation_type or '직접 설명'}**"
            + (f"\n설명: {custom_context}" if custom_context else ""),
            ephemeral=True,
        )

    @character_tree.command(name="관계초기화", description="이 서버 전용 사용자-캐릭터 관계를 초기화합니다.")
    async def personal_relation_clear(interaction: discord.Interaction):
        if interaction.guild is not None:
            set_log_context(
                "world" if is_main_world_guild_id(interaction.guild.id) else "rp",
                guild_id=interaction.guild.id,
                guild_name=getattr(interaction.guild, "name", None),
            )
        else:
            set_log_context("dm")
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령은 서버 안에서만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        changed = clear_guild_user_character_relation(
            interaction.guild.id,
            interaction.user.id,
            character,
        )

        await interaction.response.send_message(
            (
                f"이 서버에서 **{config['name']}**과(와)의 관계 설정을 초기화했습니다."
                if changed
                else "초기화할 서버 전용 관계가 없습니다."
            ),
            ephemeral=True,
        )


    def _can_manage_personal_rp(interaction):
        permissions = getattr(interaction.user, "guild_permissions", None)
        return bool(
            permissions
            and (
                permissions.administrator
                or permissions.manage_guild
            )
        )

    @character_tree.command(
        name="rp채널설정",
        description="이 개인 서버에서 캐릭터들이 자율발언할 채널을 현재 채널로 지정합니다."
    )
    async def personal_rp_channel_set(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령은 서버 안에서만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        set_log_context(
            "world" if is_main_world_guild_id(interaction.guild.id) else "rp",
            guild_id=interaction.guild.id,
            guild_name=getattr(interaction.guild, "name", None),
        )

        if is_main_world_guild_id(interaction.guild.id):
            await interaction.response.send_message(
                "본서버의 자율발언은 기존 월드 시스템이 관리합니다.",
                ephemeral=True,
            )
            return

        if not _can_manage_personal_rp(interaction):
            await interaction.response.send_message(
                "서버 관리 권한이 있는 사용자만 RP 채널을 설정할 수 있습니다.",
                ephemeral=True,
            )
            return

        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "일반 텍스트 채널에서 실행해주세요.",
                ephemeral=True,
            )
            return

        set_personal_rp_channel(
            interaction.guild.id,
            interaction.channel.id,
        )

        await interaction.response.send_message(
            f"개인 RP 자율발언 채널을 {interaction.channel.mention}(으)로 설정했습니다.\n"
            "아직 자율발언은 꺼져 있습니다. `/자율발언켜기`로 활성화할 수 있습니다.",
            ephemeral=True,
        )

        log_message(
            config["name"],
            "개인 RP 채널 설정:",
            interaction.channel.id,
            getattr(interaction.channel, "name", ""),
        )

    @character_tree.command(
        name="자율발언켜기",
        description="이 개인 서버에서 캐릭터들의 자율발언을 활성화합니다."
    )
    async def personal_rp_autonomous_on(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령은 서버 안에서만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        set_log_context(
            "world" if is_main_world_guild_id(interaction.guild.id) else "rp",
            guild_id=interaction.guild.id,
            guild_name=getattr(interaction.guild, "name", None),
        )

        if is_main_world_guild_id(interaction.guild.id):
            await interaction.response.send_message(
                "본서버의 자율발언은 기존 월드 시스템이 관리합니다.",
                ephemeral=True,
            )
            return

        if not _can_manage_personal_rp(interaction):
            await interaction.response.send_message(
                "서버 관리 권한이 있는 사용자만 자율발언을 켤 수 있습니다.",
                ephemeral=True,
            )
            return

        channel_id, _ = get_personal_rp_settings(interaction.guild.id)
        if not channel_id:
            await interaction.response.send_message(
                "먼저 자율발언을 사용할 채널에서 `/rp채널설정`을 실행해주세요.",
                ephemeral=True,
            )
            return

        set_personal_rp_autonomous_enabled(
            interaction.guild.id,
            True,
        )

        await interaction.response.send_message(
            "이 개인 서버의 캐릭터 자율발언을 켰습니다.\n"
            "캐릭터가 혼잣말을 하거나, 다른 캐릭터/최근 사용자에게 먼저 말을 걸 수 있습니다.",
            ephemeral=True,
        )

        log_message(
            config["name"],
            "개인 RP 자율발언 활성화",
        )

    @character_tree.command(
        name="자율발언끄기",
        description="이 개인 서버에서 캐릭터들의 자율발언을 비활성화합니다."
    )
    async def personal_rp_autonomous_off(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령은 서버 안에서만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        set_log_context(
            "world" if is_main_world_guild_id(interaction.guild.id) else "rp",
            guild_id=interaction.guild.id,
            guild_name=getattr(interaction.guild, "name", None),
        )

        if is_main_world_guild_id(interaction.guild.id):
            await interaction.response.send_message(
                "본서버의 자율발언은 기존 월드 시스템이 관리합니다.",
                ephemeral=True,
            )
            return

        if not _can_manage_personal_rp(interaction):
            await interaction.response.send_message(
                "서버 관리 권한이 있는 사용자만 자율발언을 끌 수 있습니다.",
                ephemeral=True,
            )
            return

        set_personal_rp_autonomous_enabled(
            interaction.guild.id,
            False,
        )

        await interaction.response.send_message(
            "이 개인 서버의 캐릭터 자율발언을 껐습니다.",
            ephemeral=True,
        )

        log_message(
            config["name"],
            "개인 RP 자율발언 비활성화",
        )

    @character_tree.command(
        name="rp설정보기",
        description="이 개인 서버의 RP 자율발언 설정을 확인합니다."
    )
    async def personal_rp_settings_view(interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "이 명령은 서버 안에서만 사용할 수 있습니다.",
                ephemeral=True,
            )
            return

        channel_id, enabled = get_personal_rp_settings(interaction.guild.id)
        channel = None
        if channel_id:
            try:
                channel = interaction.guild.get_channel(int(channel_id))
            except (TypeError, ValueError):
                channel = None

        channel_text = channel.mention if channel is not None else "미설정"
        enabled_text = "켜짐" if enabled else "꺼짐"

        await interaction.response.send_message(
            f"**개인 RP 설정**\n"
            f"자율발언 채널: {channel_text}\n"
            f"자율발언: **{enabled_text}**",
            ephemeral=True,
        )


    @client.event
    async def on_error(event, *args, **kwargs):
        import traceback
        log_message(
            config["name"],
            "Discord 이벤트 오류:",
            event,
        )
        traceback.print_exc()


    @client.event
    async def on_message(message):

        is_offline_recovery = OFFLINE_RECOVERY_ACTIVE.get()

        # 이 이벤트에서 발생하는 모든 하위 로그를 서버/DM별 파일로 자동 분리한다.
        if message.guild is None:
            set_log_context("dm")
        elif is_main_world_guild_id(message.guild.id):
            set_log_context(
                "world",
                guild_id=message.guild.id,
                guild_name=getattr(message.guild, "name", None),
            )
        else:
            set_log_context(
                "rp",
                guild_id=message.guild.id,
                guild_name=getattr(message.guild, "name", None),
            )

        # 자기 자신의 메시지는 무시
        if (
            client.user is not None
            and message.author.id
            == client.user.id
        ):
            return

        is_dm = message.guild is None

        # 같은 사용자가 여러 캐릭터와 DM하더라도 기록이 절대 섞이지 않도록
        # 캐릭터 + 사용자 ID를 DM 전용 대화 키로 사용한다.
        if is_dm:
            channel_id = f"dm:{character}:{message.author.id}"
            raw_channel_name = current_place.get(character) or "개인 대화"
            place = current_place.get(character) or "개인 대화"
        else:
            channel_id = message.channel.id
            raw_channel_name = getattr(
                message.channel,
                "name",
                "알 수 없는 장소"
            )
            place = normalize_place_name_from_channel(raw_channel_name)

        is_dm_message = message.guild is None
        guild_id = message.guild.id if message.guild is not None else None
        personal_rp_mode = (
            message.guild is not None
            and not is_main_world_guild_id(guild_id)
        )

        if GENERIC_CONFIG_ACTIVE:
            if is_dm and not feature_enabled("dm"):
                return
            if personal_rp_mode and not feature_enabled("personal_rp"):
                return

        history_key = scoped_history_key(
            channel_id,
            guild_id=guild_id,
            character=character,
            user_id=message.author.id,
            is_dm=is_dm_message,
        )

        history = conversation_history[
            history_key
        ]

        if not history and not personal_rp_mode and not is_dm_message:
            saved_history = load_recent_history(
                channel_id,
                limit=10
            )

            for item in saved_history:
                history.append(
                    item
                )

        # ------------------------------------------
        # 다른 캐릭터 봇이 보낸 메시지
        # ------------------------------------------

        if message.author.bot:
            source_character=bot_user_characters.get(message.author.id)
            if not personal_rp_mode and not is_dm and source_character and current_place.get(source_character)==place and not character_state[source_character].get('sleeping') and not character_state[source_character].get('away'):
                record_world_sent(message,source_character,place,message.content or '')

            if GENERIC_CONFIG_ACTIVE and not feature_enabled("bot_to_bot_chat"):
                return

            # 캐릭터끼리의 DM 자동 대화는 지원하지 않는다.
            if is_dm:
                return

            # 개인 RP 서버는 본서버의 수면/외출/다이나믹 이벤트 상태와 독립적이다.
            if not personal_rp_mode:
                if character_state[character]["sleeping"] or character_state[character]["away"]:
                    return

                if dynamic_event_channels.get(
                    channel_id,
                    False
                ):
                    return

            other_character = (
                bot_user_characters.get(
                    message.author.id
                )
            )

            # 우리가 관리하는 봇이 아니면 무시
            if other_character is None:
                return

            # 자기 캐릭터는 무시
            if other_character == character:
                return

            other_name = CHARACTERS[
                other_character
            ]["name"]

            message_place = normalize_place_name_from_channel(raw_channel_name)
            if not personal_rp_mode:
                maybe_store_overheard_knowledge(
                    character,
                    other_character,
                    message.content,
                    message_place
                )

            reply_target = None

            if client.user in message.mentions:
                reply_target = config["name"]

            if (
                message.reference is not None
                and isinstance(
                    message.reference.resolved,
                    discord.Message
                )
            ):
                referenced_message = (
                    message.reference.resolved
                )

                target_character = (
                    bot_user_characters.get(
                        referenced_message.author.id
                    )
                )

                if target_character is not None:
                    reply_target = CHARACTERS[
                        target_character
                    ]["name"]
                else:
                    reply_target = (
                        referenced_message.author.display_name
                    )

            if reply_target is not None:
                history.append(
                    f"{other_name} → "
                    f"{reply_target}: "
                    f"{message.content}"
                )
            else:
                history.append(
                    f"{other_name}: "
                    f"{message.content}"
                )

            # 실제 같은 장소에 있는 캐릭터만 반응 후보가 된다.
            # 자신에게 직접 멘션/답글한 경우는 후보 제한과 무관하게 판단한다.
            directly_addressed = (reply_target == config["name"])

            # 개인 RP 서버에서는 같은 채널에 있다는 사실 자체가 같은 RP 공간이라는 뜻이다.
            # 본서버의 current_place 값을 사용하면 서로 다른 장소로 오판하므로 적용하지 않는다.
            if not personal_rp_mode:
                if not should_consider_bot_reaction(
                    message.id,
                    character,
                    other_character,
                    message_place,
                    directly_addressed=directly_addressed,
                    channel_id=channel_id,
                ):
                    log_message(
                        config["name"],
                        "봇 반응 생략:",
                        "다른 장소 또는 반응 후보 제한",
                        "| 장소:",
                        message_place,
                    )
                    return
            elif not directly_addressed:
                # 직접 호출되지 않은 제3자 캐릭터가 매 메시지마다 모두 끼어들지 않게 제한.
                if random.random() >= 0.18:
                    return

            if not personal_rp_mode:
                defer_character_movement(
                    character,
                    5,
                    12,
                    "캐릭터끼리 대화 중"
                )

            # 봇끼리 너무 오래 같은 흐름을 반복하지 않도록 피로도 누적.
            # 직접 멘션/답글된 캐릭터는 피로도 때문에 응답을 생략하지 않는다.
            fatigue_value = touch_conversation_fatigue(channel_id)
            if (
                not directly_addressed
                and fatigue_value >= 5
                and random.random() < 0.45
            ):
                log_message(config["name"], "대화 피로로 이번 반응 생략")
                return

            # 봇끼리 너무 오래 대화하지 않음.
            # 단, 오래전에 누적된 횟수는 get_bot_chain_count()에서 자동 감쇠/초기화된다.
            current_chain_count = get_bot_chain_count(channel_id)
            if (
                current_chain_count
                >= MAX_BOT_CHAIN
            ):
                log_message(
                    config["name"],
                    '→ 봇 대화 최대 횟수 도달',
                    f'({current_chain_count}/{MAX_BOT_CHAIN})',
                )
                return

            try:
                delay = random.randint(
                    10,
                    45
                )

                log_message(
                    config["name"],
                    '→',
                    other_name,
                    '메시지 반응까지',
                    delay,
                    '초',
                )

                await asyncio.sleep(
                    delay
                )

                if directly_addressed:
                    action = "REPLY"
                else:
                    action = await run_ai(decide_bot_reaction,
                        character,
                        other_name,
                        message.content,
                        history,
                        reply_target
                    )

                log_message(
                    config["name"],
                    '봇 반응 판단:',
                    action,
                )

                if action == "IGNORE":
                    return

                await asyncio.sleep(
                    random.randint(
                        2,
                        8
                    )
                )

                reply_language = conversation_language[channel_id]
                reply = await run_ai(generate_bot_reply,
                    character,
                    other_name,
                    message.content,
                    history,
                    reply_target,
                    reply_language,
                    external_rp=personal_rp_mode,
                )

                if not reply: return

                increment_bot_chain(
                    channel_id
                )

                history.append(
                    f'{config["name"]}: '
                    f'{reply}'
                )

                if not personal_rp_mode:
                    save_message(
                        channel_id,
                        config["name"],
                        reply
                    )

                log_message(
                    config["name"],
                    '→',
                    other_name,
                    ':',
                    reply,
                )

                await message.reply(
                    reply
                )
                if not personal_rp_mode:
                    defer_character_movement(
                        character,
                        5,
                        15,
                        "캐릭터 대화 직후 여유"
                    )

                    maybe_register_room_invitation(
                        character,
                        other_character,
                        reply
                    )
                    register_appointment_from_exchange(
                        character,
                        message.content,
                        reply,
                        current_place.get(character),
                        other_character
                    )
                    await run_ai(maybe_analyze_relationship_update,
                        character,
                        other_character,
                        message.content,
                        reply
                    )
                    maybe_update_inventory_from_text(
                        character,
                        reply,
                        current_place.get(character),
                        other_character
                    )
                    maybe_create_note_from_exchange(character, message.content, reply)
                    maybe_update_place_state_from_text(current_place.get(character), reply)

            except Exception as e:
                log_message(
                    config["name"],
                    '봇 대화 오류:',
                    type(e).__name__,
                    '-',
                    e,
                )

            return

        # ------------------------------------------
        # 여기부터 사람 메시지
        # ------------------------------------------
        if not personal_rp_mode and not is_dm and not is_offline_recovery and place in PLACE_INFO:
            outcomes = _world_actions.apply(message.id, f'user:{message.author.id}', place,
                message.content or '', PLACE_INFO[place], CUSTOM_SETTINGS)
            for outcome in outcomes: _monitor.event('생활행동', '', outcome, place)
            if outcomes and any(not o.startswith(('식재료 부족','음식 재고 부족','설거지 보류','음식 수량')) for o in outcomes):
                try: await message.add_reaction('✅')
                except (discord.HTTPException, discord.Forbidden):
                    _monitor.event('오류', '', '생활 행동은 저장됐지만 확인 반응을 남기지 못했습니다.', place)


        targeted_to_me = (
            True
            if is_dm
            else await message_targets_client(message, client)
        )

        if not targeted_to_me:
            # 개인 RP 서버의 일반 대화는 본서버 지식/목격으로 저장하지 않는다.
            if personal_rp_mode:
                return

            # 같은 장소에서 다른 캐릭터에게 향한 사용자 발언 중 중요한 내용은 들을 수 있다.
            message_place = normalize_place_name_from_channel(raw_channel_name)
            if current_place.get(character) == message_place:
                mentioned_other = any(
                    bot_id in [u.id for u in message.mentions]
                    for bot_id in bot_character_ids.values()
                )
                if mentioned_other:
                    low = (message.content or "").lower()
                    cues = ["비밀", "사실", "약속", "미안", "고마", "걱정", "secret", "promise", "sorry", "thank"]
                    if any(k in low for k in cues):
                        save_knowledge(
                            character,
                            f"{message.author.display_name}가 {message_place}에서 다른 사람에게 이렇게 말했다: {message.content[:180]}",
                            "목격/엿들음",
                            message.author.display_name,
                            user_subject_key(message.author.id),
                            "비밀" in low or "secret" in low,
                        )
            return

        # 동일 메시지를 실시간 이벤트와 시작 시 복구 스캔이 동시에 보더라도
        # 한 번만 답한다.
        if is_discord_mention_processed(character, message.id):
            if is_offline_recovery:
                log_message(
                    config["name"],
                    "밀린 멘션 이미 처리됨:",
                    message.id,
                )
            return

        bot_chain_count[
            channel_id
        ] = 0

        if is_dm:
            clean_text = (message.content or "").strip()
        else:
            clean_text = (
                message.content
                .replace(
                    f"<@{client.user.id}>",
                    ""
                )
                .replace(
                    f"<@!{client.user.id}>",
                    ""
                )
                .strip()
            )

        if not clean_text:
            clean_text = "..."

        log_message(
            config["name"],
            "사용자 DM 수신:" if is_dm else "사용자 멘션 수신:",
            message.author.display_name,
            "→",
            clean_text,
        )

        reply_language = detect_message_language(clean_text)
        conversation_language[channel_id] = reply_language
        if not is_offline_recovery:
            recent_human_by_channel[channel_id] = {
                "user_id": message.author.id,
                "display_name": message.author.display_name,
                "last_seen": datetime.now(),
            }

        if not personal_rp_mode and not is_dm and not is_offline_recovery:
            defer_character_movement(
                character,
                10,
                20,
                "사용자와 대화 중"
            )

        conversation_fatigue[channel_id] = max(0, conversation_fatigue[channel_id] - 2)

        base_user_subject = user_subject_key(message.author.id)

        try:
            base_user_subject = migrate_legacy_user_memories(
                character,
                message.author.id,
                message.author.display_name
            )
        except Exception as e:
            log_message(
                config["name"],
                "사용자 기억 마이그레이션 오류:",
                type(e).__name__,
                "-",
                e,
            )

        # 서버 대화 장기기억과 DM 장기기억은 별도 subject에 저장한다.
        # 고정 관계/사용자 캐릭터 프로필은 같은 사용자를 가리키므로 base subject를 유지한다.
        if is_dm:
            user_subject = f"DM:{character}:{message.author.id}"
        elif personal_rp_mode:
            user_subject = scoped_user_subject(
                message.author.id,
                guild_id=guild_id,
                character=character,
                is_dm=False,
            )
        else:
            user_subject = base_user_subject

        try:
            upsert_user_profile(
                message.author.id,
                message.author.display_name
            )
        except Exception as e:
            log_message(
                config["name"],
                "사용자 프로필 저장 오류:",
                type(e).__name__,
                "-",
                e,
            )

        if not is_dm and not personal_rp_mode and not is_offline_recovery:
            try:
                maybe_store_user_knowledge(
                    character,
                    message.author.id,
                    message.author.display_name,
                    clean_text
                )
            except Exception as e:
                log_message(
                    config["name"],
                    "사용자 지식 저장 오류:",
                    type(e).__name__,
                    "-",
                    e,
                )

        if not personal_rp_mode and not is_dm and not is_offline_recovery and current_place.get(character)==place and 6<=now_kst().hour<12 and any(word in clean_text for word in ('일어나','기상','깨워')):
            wake_choice=_life.wake_response(character,character_state[character],config,CUSTOM_SETTINGS,datetime.now(timezone.utc).timestamp())
            if wake_choice:
                try:await message.add_reaction('⏰' if wake_choice=='일어남' else '💤')
                except discord.HTTPException:pass
                if wake_choice=='일어남':await set_sleeping_state(character,False)
                else:
                    mark_discord_mention_processed(character,message.id)
                    _monitor.event('수면',character,'아침 기상 요청: '+wake_choice,place);return

        if not personal_rp_mode and not is_dm and _life.record(character).get('nap'):
            character_state[character]['sleeping']=False
            place=current_place.get(character)
            current_activity[character]=_life.tick(character,place,PLACE_INFO.get(place,{}),character_state[character],current_activity.get(character,''),CUSTOM_SETTINGS,datetime.now(timezone.utc).timestamp(),now_kst().hour,busy=True)
            _life.save()

        # 자는 동안 사용자가 직접 불렀을 때,
        # 10분 안에 이어지는 대화는 같은 "깨움 세션"으로 취급한다.
        if (
            not personal_rp_mode
            and not is_offline_recovery
            and character_state[character]["sleeping"]
        ):
            state = character_state[character]
            now = datetime.now()
            last_interaction = state.get("last_sleep_interaction")

            new_interruption = (
                last_interaction is None
                or now - last_interaction >= timedelta(minutes=10)
            )

            if new_interruption:
                state["sleep_interruptions"] += 1
                state["fatigue"] = min(
                    100,
                    state["fatigue"] + 3
                )
                if state["sleep_interruptions"] >= 3:
                    set_mood(character, "짜증남", f"자는 중 {state['sleep_interruptions']}번 깨움")
                elif state["sleep_interruptions"] >= 2:
                    set_mood(character, "조금 예민함", f"자는 중 {state['sleep_interruptions']}번 깨움")
                else:
                    set_mood(character, "졸림", "자는 중 한 번 깨움")

                log_message(
                    config["name"],
                    "수면 중 깨움:",
                    state["sleep_interruptions"],
                    "회"
                )
            else:
                log_message(
                    config["name"],
                    "수면 중 대화 계속:",
                    state["sleep_interruptions"],
                    "번째 깨움 세션"
                )

            state["last_sleep_interaction"] = now

        history.append(
            f"{message.author.display_name} → "
            f"{config['name']}: "
            f"{clean_text}"
        )

        try:
            if not personal_rp_mode:
                save_message(
                    channel_id,
                    f"{message.author.display_name} → {config['name']}",
                    clean_text
                )
        except Exception as e:
            log_message(
                config["name"],
                "사용자 메시지 저장 오류:",
                type(e).__name__,
                "-",
                e,
            )

        if not is_dm and not personal_rp_mode and not is_offline_recovery:
            try:
                maybe_update_shared_object_from_user(clean_text, place)
            except Exception as e:
                log_message(
                    config["name"],
                    "공용 물건 처리 오류:",
                    type(e).__name__,
                    "-",
                    e,
                )

        try:
            memory_result = await run_ai(
                analyze_memory,
                character,
                user_subject,
                message.author.display_name,
                clean_text
            )
        except Exception as e:
            memory_result = "IGNORE"
            log_message(
                config["name"],
                "장기 기억 판단 오류:",
                type(e).__name__,
                "-",
                e,
            )

        log_message(
            config["name"],
            '장기 기억 판단:',
            memory_result,
        )

        if memory_result.startswith(
            "NEW|"
        ):
            new_memory = memory_result.split(
                "|",
                1
            )[1].strip()

            save_memory(
                character,
                user_subject,
                new_memory
            )

            log_message(
                config["name"],
                '장기 기억 추가:',
                message.author.display_name,
                '→',
                new_memory,
            )

        elif memory_result.startswith(
            "UPDATE|"
        ):
            parts = memory_result.split(
                "|",
                2
            )

            if len(parts) == 3:

                old_memory = parts[1].strip()
                new_memory = parts[2].strip()

                update_memory(                    character,
                    user_subject,
                    old_memory,
                    new_memory
                )
                log_message(
                    config["name"],
                    '장기 기억 수정:',
                    old_memory,
                    '→',
                    new_memory,
                )

        elif memory_result == "IGNORE":
            pass

        log_message(
            config["name"],
            message.author.display_name,
            '→',
            config['name'],
            ':',
            clean_text,
        )

        try:
            current_character_place = (
                current_place.get(
                    character
                )
            )

            current_character_channel = (
                find_place_channel(
                    client,
                    current_character_place
                )
            )

            movement_allowed = (
                not is_dm
                and not personal_rp_mode
                and not is_offline_recovery
                and (
                    not GENERIC_CONFIG_ACTIVE
                    or feature_enabled("place_movement")
                )
                and (
                    not GENERIC_CONFIG_ACTIVE
                    or feature_enabled("world_simulation")
                )
                and not (
                    character_state[character]["sleeping"]
                    or character_state[character]["away"]
                    or (
                        current_character_channel is not None
                        and dynamic_event_channels.get(
                            current_character_channel.id,
                            False
                        )
                    )
                )
            )

            if _life.busy(character, CUSTOM_SETTINGS): movement_allowed = False

            guild_relation_context = None
            if not is_dm:
                guild_relation_context = get_guild_user_relation_context(
                    guild_id,
                    message.author.id,
                    character,
                )

            reply_place = (
                "개인 RP 공간"
                if personal_rp_mode
                else (current_character_place or place)
            )

            job_id=f'reply:{character}:{message.id}'
            _world_actions.submit(job_id,'사용자 답변',character,place,{'message_id':str(message.id),'channel_id':str(channel_id),'scope':'dm' if is_dm else 'rp' if personal_rp_mode else 'world'})
            if not _world_actions.begin_job(job_id):
                _monitor.event('발언 보류',character,'중복 또는 전송 결과 확인이 필요한 작업',channel_id);return
            reply_snapshot = (current_place.get(character), character_state[character].get('sleeping'), character_state[character].get('away'))
            _monitor.request(character, message.id, '답변 생성 중', channel_id,
                             f'{message.author.display_name}: {message.content}')
            reply, destination = await run_ai(
                generate_character_reply,
                character,
                message.author.display_name,
                user_subject,
                clean_text if is_dm else message.content,
                history,
                channel_id,
                reply_place,
                movement_allowed,
                reply_language,
                private_chat=is_dm,
                relation_subject=(
                    user_subject
                    if personal_rp_mode
                    else base_user_subject
                ),
                profile_subject=base_user_subject,
                fixed_relation_override=guild_relation_context,
                external_rp=personal_rp_mode,
                priority=0,
            )
            if not reply:
                _world_actions.job_phase(job_id,'취소','대화 검사에서 보류됨')
                _monitor.request(character, message.id, '발언 보류', channel_id, '대화 검사에서 보류됨')
                _monitor.requests.pop(f'{character}:{message.id}', None)
                return

            if (
                    destination is not None
                    and destination in PRIVATE_ROOMS
                ):
                    owner = PRIVATE_ROOMS[
                        destination
                    ]

                    if (
                        owner != character
                        and destination not in room_invitations.get(
                            character,
                            set()
                        )
                    ):
                        destination = None
                        
            reply_length = len((reply or "").strip())

            if is_offline_recovery:
                reply_delay = random.randint(1, 3)
            elif reply_length <= 40:
                reply_delay = random.randint(3, 12)
            elif reply_length <= 100:
                reply_delay = random.randint(8, 25)
            elif reply_length <= 200:
                reply_delay = random.randint(15, 40)
            else:
                reply_delay = random.randint(25, 60)

            log_message(
                config["name"],
                "사용자 답장 대기:",
                f"{reply_delay}초",
                "| 길이:",
                f"{reply_length}자",
            )

            _monitor.request(character, message.id, '응답 대기 중', channel_id)
            await asyncio.sleep(reply_delay)

            if not personal_rp_mode and not is_dm and reply_snapshot != (current_place.get(character), character_state[character].get('sleeping'), character_state[character].get('away')):
                _world_actions.job_phase(job_id,'취소','생성 중 생활 상태 변경')
                _monitor.request(character, message.id, '발언 보류', channel_id, '생성 중 장소·수면·외출 상태가 변경됨')
                return
            _world_actions.job_phase(job_id,'전송 중')
            if destination is not None and movement_allowed and not can_enter_place(character,destination):
                _world_actions.job_phase(job_id,'취소','전송 전 목적지 출입 조건 변경')
                _monitor.request(character,message.id,'발언 보류',channel_id,'목적지 출입 조건이 변경됨');return
            sent_reply = await message.reply(reply)
            mark_discord_mention_processed(character,message.id)
            _world_actions.job_phase(job_id,'완료')
            if not personal_rp_mode and not is_dm:
                record_world_sent(sent_reply,character,place,reply,apply_effects=not is_offline_recovery)

            if (
                destination is not None
                and movement_allowed
            ):
                old_place = current_place.get(
                    character
                )

                if can_enter_place(
                    character,
                    destination
                ):
                    current_place[
                        character
                    ] = destination

                    new_activity = await run_ai(choose_activity,
                        character,
                        destination
                    )

                    await safe_change_presence(
                        client,
                        character=character,
                        activity=discord.CustomActivity(
                            name=get_presence_location_text(character, destination)
                        ),
                        reason="대화로 인한 이동",
                    )

                    log_message(
                        config["name"],
                        '대화로 인한 이동:',
                        old_place,
                        '→',
                        destination,
                    )
                    await maybe_deliver_unread_note(character)

                consume_room_invitation(character, destination)

            if (
                not is_dm
                and not personal_rp_mode
                and not is_offline_recovery
            ):
                register_appointment_from_exchange(
                    character,
                    clean_text,
                    reply,
                    current_character_place or place,
                    None
                )
                maybe_update_inventory_from_text(
                    character,
                    reply,
                    current_place.get(character) or place,
                    None
                )
                maybe_create_note_from_exchange(character, clean_text, reply, message.author.display_name)
                maybe_update_place_state_from_text(current_place.get(character) or place, reply)

            if not personal_rp_mode and not is_offline_recovery:
                low_exchange = f"{clean_text} {reply}".lower()
                if any(k in low_exchange for k in ["고마", "thank"]):
                    character_state[character]["mood_reason"] = f"{message.author.display_name}와 고마움을 주고받음"
                elif any(k in low_exchange for k in ["미안", "sorry"]):
                    character_state[character]["mood_reason"] = f"{message.author.display_name}와 사과가 오감"

            history.append(
                f'{config["name"]}: {reply}'
            )

            if not personal_rp_mode and not is_dm_message:
                save_message(
                    channel_id,
                    config["name"],
                    reply
                )

            log_message(
                config["name"],
                'DM →' if is_dm else '→',
                reply,
            )

            # 사용자에게 답할 때 답변 길이에 따라 약간의 랜덤 텀을 둔다.
            # 짧은 답은 비교적 빨리, 긴 답은 최대 60초까지 늦게 보낸다.
            _world_actions.job_phase(job_id,'완료')
            _monitor.request(character, message.id, '전송 완료', channel_id, reply)
            mark_discord_mention_processed(
                character,
                message.id,
            )

            if is_offline_recovery:
                log_message(
                    config["name"],
                    "밀린 멘션 답변 완료:",
                    message.id,
                )

            if not is_dm and not personal_rp_mode and not is_offline_recovery:
                defer_character_movement(
                    character,
                    5,
                    15,
                    "사용자 대화 직후 여유"
                )

        except Exception as e:
            if 'job_id' in locals(): _world_actions.job_phase(job_id,'불확정' if isinstance(e, (asyncio.TimeoutError, OSError)) else '오류', type(e).__name__)
            _monitor.request(character, message.id, '오류', message.channel.id, str(e))
            log_message(
                config["name"],
                'GPT 응답 오류:',
                type(e).__name__,
                '-',
                e,
            )

    _monitor_commands(character_tree, character)
    return client

# -----------------------------
# 수면 / 외출 / 생활 상태 루프
# -----------------------------

def generate_dream(character):
    config = CHARACTERS[character]
    prompt = load_character_prompt(character)
    input_text = f"""
{config['name']}가 잠에서 막 깼다.
아주 짧고 모호한 꿈의 잔상 하나를 만든다.
실제 사건, 예언, 새로운 설정, 확정된 과거사로 취급하면 안 된다.
캐릭터 분위기와 어울리되 1문장만 출력한다.
"""
    try:
        response = openai_client.responses.create(model="gpt-5.6-luna", instructions=prompt, input=input_text)
        return clean_generated_text(response.output_text)[:180]
    except Exception:
        return None


def record_world_sent(sent, character, place, text, apply_effects=True):
    try:
        _world_actions.public_dialogue(sent.id,character,place,redact(text,_monitor.secrets))
        _world_actions.sent(character,text,place)
        if apply_effects and current_place.get(character)==place and not character_state[character].get('sleeping') and not character_state[character].get('away'):
            for outcome in _world_actions.apply(sent.id,character,place,text,PLACE_INFO.get(place,{}),CUSTOM_SETTINGS,character=True):
                _monitor.event('생활행동',character,outcome,place)
    except Exception as error:
        _monitor.event('오류',character,'전송은 완료했으나 생활 자료 기록 실패: '+str(error),place)


async def send_world(channel, text, character, place):
    sent=await channel.send(text)
    record_world_sent(sent,character,place,text)
    return sent


async def run_ai(fn,*args,priority=10,**kwargs):
    token=GENERATION_PRIORITY.set(priority)
    try:return await asyncio.to_thread(fn,*args,**kwargs)
    finally:GENERATION_PRIORITY.reset(token)


async def set_sleeping_state(character, sleeping):
    client = clients.get(character)
    if client is None or not client.is_ready():
        return

    state = character_state[character]
    if state["sleeping"] == sleeping:
        return

    state["sleeping"] = sleeping
    if sleeping and '_life' in globals():
        r = _life.record(character)
        for field in ('nap', 'meal', 'task', 'seat'): r.pop(field, None)
    config = CHARACTERS[character]

    if sleeping:
        # 새 수면 주기가 시작될 때 깨움 횟수와 깨움 세션을 초기화한다.
        state["sleep_interruptions"] = 0
        state["last_sleep_interaction"] = None

        room = config.get("private_room")
        if room:
            current_place[character] = room
        current_activity[character] = "자는 중"
        state["sleep_started_at"] = now_kst()
        _life.begin_sleep(character, state, CUSTOM_SETTINGS, datetime.now(timezone.utc).timestamp())
        state["dream"] = None
        state["dream_expires_at"] = None
        await safe_change_presence(
            client,
            character=character,
            status=discord.Status.idle,
            activity=discord.CustomActivity(
                name=get_presence_location_text(character, room)
            ),
            reason="취침 상태 반영",
        )
        set_mood(character, "졸림", "수면 시간이라 잠자리에 듦")
        log_message(config["name"], "취침")
    else:
        if not feature_enabled('actual_sleep'): state["fatigue"] = max(0, state["fatigue"] - 55)
        _life.wake(character, state, CUSTOM_SETTINGS, datetime.now(timezone.utc).timestamp(), config)
        state["last_wake_at"] = now_kst()
        set_mood(character, "평온", "잠에서 깨어 피로가 회복됨", hours=1)
        if random.random() < 0.20:
            dream = await run_ai(generate_dream,character)
            if dream:
                state["dream"] = dream
                state["dream_expires_at"] = datetime.now() + timedelta(hours=4)
                log_message(config["name"], "꿈 기억:", dream)
        place = current_place.get(character) or config.get("private_room") or choose_place(character)
        status_text = await run_ai(choose_activity, character, place)
        await safe_change_presence(
            client,
            character=character,
            status=discord.Status.online,
            activity=discord.CustomActivity(name=get_presence_location_text(character, place)),
            reason="외출 복귀 상태 반영",
        )
        log_message(config["name"], "기상 |", status_text)


def life_should_sleep(character):
    if not feature_enabled('sleep_system'):return False
    now=now_kst();stamp=datetime.now(timezone.utc).timestamp();record=_life.record(character)
    if record.get('wake_override_until',0)>stamp:return False
    if record.get('wake_delayed_until',0)>stamp and character_state[character].get('sleeping'):return True
    if is_sleep_time(character,now):return True
    if feature_enabled('weekend_rest') and now.weekday() in (5,6) and character_state[character].get('sleeping'):
        wake=ensure_daily_sleep_schedule(character)['wake_minute'];minute=now.hour*60+now.minute
        return wake<=minute<wake+CHARACTERS[character].get('weekend_extra_sleep_hours',1)*60
    return False


async def life_state_loop(character):
    client = clients[character]
    await client.wait_until_ready()

    # 시작 시 현재 시간에 맞춰 즉시 수면 상태 정렬
    sched = ensure_daily_sleep_schedule(character)
    initial_sleeping = life_should_sleep(character)
    if not initial_sleeping and character_state[character].get("last_wake_at") is None:
        now = now_kst()
        wake_today = datetime.combine(
            now.date(),
            time(sched["wake_minute"] // 60, sched["wake_minute"] % 60),
            tzinfo=KST
        )
        # 기상한 지 두 시간 이내라면 재시작 후에도 아침 루틴을 이어간다.
        if timedelta(0) <= now - wake_today <= timedelta(hours=2):
            character_state[character]["last_wake_at"] = wake_today
    try:
        await set_sleeping_state(character, initial_sleeping)
    except Exception as e:
        # 초기 상태 정렬 실패가 life_state_loop 자체를 죽이지 않게 한다.
        log_message(
            CHARACTERS[character]["name"],
            "초기 생활 상태 정렬 오류:",
            type(e).__name__,
            "-",
            e,
        )

    while not client.is_closed():
        try:
            ensure_daily_sleep_schedule(character)
            update_character_needs(character)
            apply_place_need_effects(character)
            state = character_state[character]
            place = current_place.get(character)
            r = _life.record(character)
            state['temperature'] = get_world_temperature()
            state['outfit_set'] = bool(CHARACTERS[character].get('current_outfit') or CHARACTERS[character].get('default_outfit'))
            _world_actions.tick_objects(place or '',PLACE_INFO.get(place,{}),str(ensure_daily_weather()),CUSTOM_SETTINGS)
            stage_channel=find_place_channel(client,place)
            in_stage_event=bool(stage_channel and dynamic_event_channels.get(stage_channel.id,False))
            scheduled_sleep = life_should_sleep(character)
            stage_busy = in_stage_event or bool(movement_defer_until.get(character) and datetime.now() < movement_defer_until[character]) or bool(_life_appointment_due(character))
            morning_target=_life.morning_destination(character,CHARACTERS[character],CUSTOM_SETTINGS,datetime.now(timezone.utc).timestamp(),place,PLACE_INFO,current_place,lambda target:can_enter_place(character,target),busy=stage_busy or scheduled_sleep)
            if morning_target:
                current_place[character]=morning_target;r.pop('seat',None);place=morning_target
                _monitor.event('이동',character,'몸단장 장소로 이동: '+morning_target)
            destination = scheduled_destination(CUSTOM_SETTINGS, CHARACTERS[character], now_kst(), place,
                lambda target: can_enter_place(character, target),
                busy=stage_busy or bool(get_movement_busy_reason(character, client)) or scheduled_sleep,
                hungry=feature_enabled('meal_stages') and state.get('hunger',0)>=55)
            if destination:
                current_place[character] = destination
                r.pop('seat', None)
                current_activity[character] = await run_ai(choose_activity, character, destination)
                log_message(CHARACTERS[character]['name'], '요일 일정으로 이동:', place, '→', destination)
                place = destination
            # Nap recovery is handled only once by LifeEngine, not by night-sleep recovery.
            meal_definition=dict(PLACE_INFO.get(place, {}),_food_weather_ok=not PLACE_INFO.get(place,{}).get('outdoor') or (not any(w in str(ensure_daily_weather()) for w in ('비','눈','폭풍')) and 10<=state['temperature']<=28))
            current_activity[character] = _life.tick(
                character, place, meal_definition, state,
                current_activity.get(character, ''), CUSTOM_SETTINGS, datetime.now(timezone.utc).timestamp(),
                now_kst().hour, str(ensure_daily_weather()),
                busy=stage_busy or not place_open(CUSTOM_SETTINGS, PLACE_INFO.get(place,{}), now_kst()),
                scheduled_sleep=scheduled_sleep,
                scheduled_activity=(weekly_entry(CUSTOM_SETTINGS, CHARACTERS[character], now_kst(), place) or {}).get("activity", ""))
            if r.get('finished_meal_at') and datetime.now(timezone.utc).timestamp()-r['finished_meal_at']>=300 and not stage_busy and not scheduled_sleep and not get_movement_busy_reason(character, client):
                if feature_enabled('place_movement'):
                    destination=choose_weighted_destination(character)
                    if destination:
                        current_place[character]=destination
                        r.pop('seat',None); r.pop('finished_meal_at',None)
                        current_activity[character]=await run_ai(choose_activity, character,destination)
                        log_message(CHARACTERS[character]['name'],'식사 후 이동:',place,'→',destination)
                else: r.pop('finished_meal_at',None)
            if feature_enabled('opening_hours') and feature_enabled('place_movement') and not place_open(CUSTOM_SETTINGS, PLACE_INFO.get(current_place.get(character), {}), now_kst()) and not stage_busy and not scheduled_sleep and not get_movement_busy_reason(character, client):
                old = current_place.get(character)
                destination = choose_weighted_destination(character)
                if destination:
                    current_place[character] = destination
                    r.pop('seat', None)
                    current_activity[character] = await run_ai(choose_activity, character, destination)
                    log_message(CHARACTERS[character]['name'], '영업 종료 후 이동:', old, '→', destination)
            _world_actions.sync_leisure_seats(CUSTOM_SETTINGS)
            _world_actions.discover(character,current_place.get(character) or '',CUSTOM_SETTINGS)
            _life.save()
            cleanup_appointments()
            cleanup_room_invitations()
            await maybe_deliver_unread_note(character)
            should_sleep = life_should_sleep(character)

            # 진행 중인 다이나믹 이벤트가 있으면 장소 이동을 끝날 때까지 미룬다.
            current_channel = find_place_channel(client, current_place.get(character))
            in_event = (
                current_channel is not None
                and dynamic_event_channels.get(current_channel.id, False)
            )
            if not in_event and not _life.record(character).get("nap"):
                await set_sleeping_state(character, should_sleep)
        except Exception as e:
            log_message(CHARACTERS[character]["name"], "생활 상태 오류:", type(e).__name__, "-", e)
        await asyncio.sleep(60)


async def appointment_loop(character):
    """약속 시간이 되면 깨어 있고 이동 가능한 캐릭터를 약속 장소로 보낸다."""
    client = clients[character]
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            cleanup_appointments()
            cleanup_room_invitations()
            now = now_kst()
            state = character_state[character]
            if not state["sleeping"] and not state["away"]:
                due_items = [
                    x for x in pending_appointments
                    if x["character"] == character
                    and not x.get("completed")
                    and x["due_at"] <= now <= x["expires_at"]
                ]
                for item in due_items:
                    destination = item.get("place")
                    if destination and destination != current_place.get(character) and can_enter_place(character, destination):
                        old = current_place.get(character)
                        current_place[character] = destination
                        if destination in PRIVATE_ROOMS and PRIVATE_ROOMS[destination] != character:
                            consume_room_invitation(character, destination)
                        status_text = await run_ai(choose_activity, character, destination)
                        await safe_change_presence(
                            client,
                            character=character,
                            status=discord.Status.online,
                            activity=discord.CustomActivity(name=get_presence_location_text(character, destination)),
                            reason="약속 장소 이동",
                        )
                        log_message(CHARACTERS[character]["name"], "약속으로 이동:", old, "→", destination)
                        await maybe_deliver_unread_note(character)
                    item["completed"] = True
                    if item.get("with_character"):
                        set_mood(character, "기대함", f"{CHARACTERS[item['with_character']]['name']}와의 약속 시간이 됨")
                    log_message(CHARACTERS[character]["name"], "약속 시간 도달:", item.get("text"))
            cleanup_appointments()
        except Exception as e:
            log_message(CHARACTERS[character]["name"], "약속 처리 오류:", type(e).__name__, "-", e)
        await asyncio.sleep(30)


async def away_loop(character):
    """아주 드물게 짧은 외출 상태를 만든다. 수면 중에는 실행하지 않는다."""
    client = clients[character]
    await client.wait_until_ready()
    while not client.is_closed():
        await asyncio.sleep(random.randint(4 * 60 * 60, 8 * 60 * 60))
        state = character_state[character]
        if state["sleeping"] or state["away"] or random.random() >= 0.12:
            continue
        state["away"] = True
        set_mood(character, "외출 중", "잠시 바깥에 나감")
        return_place = current_place.get(character)
        current_place[character] = None
        current_activity[character] = "외출 중"
        await safe_change_presence(
            client,
            character=character,
            status=discord.Status.dnd,
            activity=discord.CustomActivity(name="외출 중"),
            reason="외출 상태 반영",
        )
        log_message(CHARACTERS[character]["name"], "잠시 외출")
        await asyncio.sleep(random.randint(30 * 60, 90 * 60))
        state["away"] = False
        set_mood(character, "평온", "외출에서 돌아옴")
        current_place[character] = return_place
        place = current_place.get(character) or choose_place(character)
        status_text = await run_ai(choose_activity, character, place)
        await safe_change_presence(
            client,
            character=character,
            status=discord.Status.online,
            activity=discord.CustomActivity(name=get_presence_location_text(character, place)),
            reason="외출 복귀 상태 반영",
        )
        log_message(CHARACTERS[character]["name"], "외출 종료 |", status_text)



_generation_scope = contextvars.ContextVar('generation_scope', default='')
_generation_feedback = contextvars.ContextVar('generation_feedback', default='')
_generation_thought = contextvars.ContextVar('generation_thought', default='')
_life = LifeEngine(DATA_DIR, lambda kind, key, text: _monitor.event(kind, key, text))
validate_extensions(CHARACTERS,PLACE_INFO)
_world_actions = WorldActions(DATA_DIR, DISCORD_GUILD_ID or 'unconfigured')
_life.world = _world_actions
_world_actions.life = _life
if feature_enabled('world_simulation'):
    for _key in CHARACTERS:
        _checkpoint=_life.record(_key).get('checkpoint',{})
        if _checkpoint:
            if _checkpoint.get('place') in PLACE_INFO: current_place[_key]=_checkpoint['place']
            current_activity[_key]=_checkpoint.get('activity',current_activity.get(_key,''))
            if feature_enabled('sleep_system') and _life.record(_key).get('night_sleep'):
                character_state[_key]['sleeping']=True
            for _field in ('hunger','fatigue'):
                if isinstance(_checkpoint.get(_field),(int,float)): character_state[_key][_field]=max(0,min(100,_checkpoint[_field]))
for _loan in _life.loans.values():
    if _loan['owner'] in character_inventory and _loan['borrower'] in character_inventory:
        character_inventory[_loan['owner']].discard(_loan['item'])
        character_inventory[_loan['borrower']].add(_loan['item'])

def _life_appointment_due(character):
    now = datetime.now()
    return any(x['character'] == character and not x.get('completed') and
               abs((x['due_at']-now).total_seconds()) < 1800 for x in pending_appointments)



def _monitor_commands(tree, label):
    for command in tree.walk_commands():
        if not isinstance(command, app_commands.Command): continue
        original = command.callback
        @functools.wraps(original)
        async def callback(*args, _original=original, _name=command.name, **kwargs):
            interaction = next((x for x in args if isinstance(x, discord.Interaction)), kwargs.get('interaction'))
            identifier = f"command:{interaction.id}" if interaction else f"command:{_name}"
            channel = str(getattr(interaction,'channel_id',''))
            _monitor.request(label, identifier, '명령 실행 중', channel, '/'+_name)
            try:
                result = await _original(*args, **kwargs)
                _monitor.request(label, identifier, '전송 완료', channel, '/'+_name+' · 처리 종료 (권한 안내 포함)')
                return result
            except Exception as error:
                _monitor.request(label, identifier, '오류', channel, '/'+_name+' · '+str(error))
                raise
        command._callback = callback

def _checked_generation(fn):
    signature = inspect.signature(fn)
    @functools.wraps(fn)
    def checked(*args, **kwargs):
        bound = signature.bind(*args, **kwargs); bound.apply_defaults()
        data = bound.arguments; character = data['character']
        scope = ('dm' if CURRENT_LOG_SCOPE.get() == 'dm' else 'rp' if data.get('external_rp') or 'personal_rp' in fn.__name__ else CURRENT_LOG_SCOPE.get() or 'world')
        scope_token = _generation_scope.set(scope)
        feedback_token = None
        try:
            history = data.get('history') or data.get('recent_context') or [x[1] if isinstance(x,tuple) else str(x) for x in recent_autonomous_messages.get(_autonomous_history_key(character, scope, CURRENT_LOG_GUILD_ID.get()), [])]
            if isinstance(history, str): history = history.splitlines()
            relation = str(data.get('fixed_relation_override') or '')
            if not relation and data.get('user_subject'):
                relation = get_user_relation_context(character, data.get('relation_subject') or data['user_subject'])
            other = data.get('other_character') or data.get('target_character')
            if other:
                relation += get_character_relationship_goal(character, other)
            result = None
            for attempt in range(2):
                if globals().get('_generation_thought'): _generation_thought.set('')
                _monitor.event('생성', character, fn.__name__+' · 생성 중')
                result = fn(*args, **kwargs)
                _monitor.event('생성', character, fn.__name__+' · 생성 완료')
                text = result[0] if isinstance(result, tuple) else result
                requested_repeat = any(w in str(data.get('message_text','')) for w in ('다시 말해','반복해','한 번 더 알려','repeat that'))
                reason = dialogue_reason(text or '', history) if feature_enabled('dialogue_guard') and not requested_repeat else ''
                if not reason and feature_enabled('relationship_guard'):
                    reason = relationship_reason(text or '', relation)
                if not reason:
                    if scope=='world' and globals().get('_world_actions') and feature_enabled('inner_thoughts') and _generation_thought.get():
                        _world_actions.stage_thought(character,text or '',_generation_thought.get())
                    return result
                _monitor.event('대화 검사', character, f'{reason} · '+('재생성' if attempt == 0 else '발언 보류'))
                if attempt == 0:
                    feedback_token = _generation_feedback.set('[이번 초안 수정 지시]\n'+reason+' 때문에 이전 초안은 사용하지 않는다. 새로운 내용으로 답한다. 관계와 현재 상태를 유지한다.')
            return ('', None) if isinstance(result, tuple) else ''
        finally:
            if feedback_token is not None: _generation_feedback.reset(feedback_token)
            _generation_scope.reset(scope_token)
    return checked

for _name in ('generate_character_reply','generate_bot_reply','generate_autonomous_message',
              'generate_personal_rp_autonomous_message','generate_conversation_starter','generate_dynamic_event_reply'):
    globals()[_name] = _checked_generation(globals()[_name])


# 캐릭터 클라이언트 자동 생성
for character in CHARACTERS:
    clients[character] = create_client(character)



async def city_event_loop():
    """활성 사건이 없을 때만 세계 전체에 작은 도시 사건을 드물게 만든다."""
    while True:
        await asyncio.sleep(random.randint(4 * 60 * 60, 8 * 60 * 60))

        cleanup_city_event()

        # 수동/자동 사건이 아직 진행 중이면 새 사건으로 덮어쓰지 않는다.
        if daily_world.get("city_event"):
            continue

        if random.random() < 0.45:
            roll_small_city_event()


async def start_discord_client_resilient(label, client, token):
    """
    Discord Gateway 연결이 시작/재연결 단계에서 일시적으로 실패해도
    전체 프로그램을 종료하지 않고 같은 클라이언트를 정리한 뒤 재시도한다.

    잘못된 토큰(LoginFailure)처럼 재시도로 해결되지 않는 오류는 그대로 올린다.
    """
    attempt = 0

    while True:
        try:
            await client.start(token, reconnect=True)
            return

        except asyncio.CancelledError:
            raise

        except discord.LoginFailure:
            # 토큰 오류는 기다려도 해결되지 않으므로 main에 전달한다.
            raise

        except Exception as e:
            attempt += 1

            # discord.py가 Gateway 연결 timeout 뒤 내부 ws를 None으로 만든 상태에서
            # reconnect를 시도할 때 발생할 수 있는 'NoneType.sequence'도
            # 일시적인 연결 실패로 취급한다.
            transient = isinstance(
                e,
                (
                    TimeoutError,
                    asyncio.TimeoutError,
                    aiohttp.ClientError,
                    ConnectionResetError,
                    OSError,
                    discord.ConnectionClosed,
                    discord.GatewayNotFound,
                ),
            )

            if isinstance(e, AttributeError) and "sequence" in str(e):
                transient = True

            # 이미 닫힌 aiohttp 세션은 같은 discord.Client 인스턴스에서
            # 안전하게 되살릴 수 없으므로 무한 재시도하지 않는다.
            if isinstance(e, RuntimeError) and "session is closed" in str(e).lower():
                log_message(
                    label,
                    "Discord HTTP 세션이 이미 닫혀 있어 현재 Client로 재시도할 수 없음",
                )
                raise

            if not transient:
                raise

            wait_seconds = min(60, 5 + attempt * 5)
            log_message(
                label,
                "Discord Gateway 연결 실패 - 재시도 예정:",
                type(e).__name__,
                "-",
                e,
                "|",
                f"{wait_seconds}초 후 재시도",
            )

            # IMPORTANT:
            # 여기서 client.close()를 호출하면 discord.py 내부 aiohttp 세션까지 닫힌다.
            # 같은 Client 인스턴스로 client.start()를 다시 호출하면
            # RuntimeError: Session is closed 가 발생할 수 있다.
            #
            # reconnect=True 자체가 Gateway 재연결을 담당하므로,
            # 일시적인 시작 실패에서는 HTTP session을 닫지 않고 그대로 재시도한다.
            await asyncio.sleep(wait_seconds)


def diagnostic_payload():
    snapshot=_world_actions.snapshot(CUSTOM_SETTINGS)
    public_kinds={'생활행동','음식 수령','섭취','생활 명령','쪽지 발견','관리자 수정','오브젝트 변화'}
    return dict(world=CUSTOM_SETTINGS.get('world_name',''),environment=calendar_display(CUSTOM_SETTINGS,now_kst()),
        characters={k:{f:v.get(f) for f in ('hunger','fatigue','sleeping','away','mood')} for k,v in character_state.items()},
        locations=dict(current_place),activities=dict(current_activity),
        objects=snapshot['states'].get('object',{}),food=snapshot['states'].get('food',{}),
        dialogue=[e for e in snapshot['events'] if e['kind']=='공개 대화'],
        events=[e for e in snapshot['events'] if e['kind'] in public_kinds and e['kind']!='관리자 수정'])


async def diagnostic_loop():
    while True:
        await asyncio.sleep(3600)
        if feature_enabled('hourly_diagnostics'):
            try:
                path,_=await asyncio.to_thread(diagnose,DATA_DIR,diagnostic_payload(),_monitor.secrets)
                _monitor.event('운영 진단','','보고서 저장: '+path.name)
            except Exception as error:_monitor.event('오류','','운영 진단 실패: '+str(error))


async def desktop_monitor_loop(main_task):
    while True:
        for key, config in CHARACTERS.items():
            client = clients.get(key)
            state = _monitor.characters.setdefault(key, {})
            state.update(name=config['name'], connected=bool(client and client.is_ready()),
                         place=current_place.get(key), activity=current_activity.get(key),
                         outfit=_life.outfit(key,config,CUSTOM_SETTINGS,character_state.get(key,{}).get('sleeping'),str(ensure_daily_weather())) or '미지정',
                         **{k: character_state.get(key, {}).get(k) for k in ('mood','hunger','fatigue','sleeping','away')},
                         life=_life.context(key, CUSTOM_SETTINGS),
                         seat=_life.record(key).get('seat'),
                         cooldown=str(autonomous_character_cooldown_until.get(_autonomous_history_key(key)) or '대기 없음'),
                         paused=autonomous_global_paused or key in autonomous_paused_characters,
                         movement_reason=get_movement_busy_reason(key, client))
        _monitor.environment = dict(calendar_display(CUSTOM_SETTINGS, now_kst()), 날씨=ensure_daily_weather(), 기온=f'{get_world_temperature()}°C')
        events = visible_events(CUSTOM_SETTINGS, now_kst(), public_only=True)
        if events: _monitor.environment['오늘의 공개 기념일·행사'] = ', '.join(e['name'] for e in events)
        if feature_enabled('world_simulation'):
            for key in CHARACTERS:
                _life.record(key)['checkpoint']={**{f:character_state.get(key,{}).get(f) for f in ('hunger','fatigue')},'place':current_place.get(key),'activity':current_activity.get(key)}
            _life.save()
        _world_actions.record_samples(character_state,current_place,{k:bool(c.is_ready()) for k,c in clients.items()},{k:bool(_life.record(k).get('nap')) for k in CHARACTERS},CUSTOM_SETTINGS)
        _monitor.world = _world_actions.snapshot(CUSTOM_SETTINGS)
        _monitor.flush()
        stop_file = DATA_DIR / 'desktop_stop.request'
        if stop_file.exists():
            stop_file.unlink(missing_ok=True)
            main_task.cancel()
            return
        await asyncio.sleep(2)


async def main():
    (DATA_DIR / 'desktop_stop.request').unlink(missing_ok=True)
    monitor_task=asyncio.create_task(desktop_monitor_loop(asyncio.current_task()))
    # 토큰이 설정된 캐릭터만 실행한다.
    enabled_characters = [
        character
        for character, config in CHARACTERS.items()
        if config.get("token")
    ]

    # Discord Developer Portal에서 아직 봇을 만들지 못했거나
    # .env에 토큰을 넣지 않은 캐릭터는 비활성 목록으로 따로 표시한다.
    disabled_characters = [
        character
        for character, config in CHARACTERS.items()
        if not config.get("token")
    ]

    log_message(
        "General",
        f"캐릭터 봇 상태 | 활성 {len(enabled_characters)} / 전체 {len(CHARACTERS)}"
    )

    if enabled_characters:
        log_message(
            "General",
            "활성:",
            ", ".join(
                CHARACTERS[c]["name"]
                for c in enabled_characters
            )
        )

    if disabled_characters:
        log_message(
            "General",
            "비활성 - 토큰 없음:",
            ", ".join(
                CHARACTERS[c]["name"]
                for c in disabled_characters
            )
        )
    else:
        log_message(
            "General",
            "비활성 - 토큰 없음: 없음"
        )

    if not enabled_characters:
        raise RuntimeError("실행 가능한 Discord 봇 토큰이 없습니다.")

    login_tasks = []
    background_tasks = [asyncio.create_task(diagnostic_loop())]

    try:
        # 상태 봇은 토큰이 있으면 함께 실행한다.
        if STATUS_BOT_TOKEN:
            login_tasks.append(
                asyncio.create_task(
                    start_discord_client_resilient(
                        "Status",
                        status_client,
                        STATUS_BOT_TOKEN,
                    )
                )
            )
            await asyncio.sleep(0.5)

        for index, character in enumerate(enabled_characters):
            login_tasks.append(
                asyncio.create_task(
                    start_discord_client_resilient(
                        CHARACTERS[character]["name"],
                        clients[character],
                        CHARACTERS[character]["token"],
                    )
                )
            )

            # 수십 개의 봇이 같은 순간 Discord 로그인/게이트웨이 연결을 시도하면
            # 연결 초기화가 겹칠 수 있으므로 시작 시점만 짧게 분산한다.
            if index < len(enabled_characters) - 1:
                await asyncio.sleep(0.35)

        while not all(clients[c].is_ready() for c in enabled_characters):
            # 로그인 task가 먼저 실패했다면 무한 대기하지 않고 원래 오류를 올린다.
            for task in login_tasks:
                if task.done() and not task.cancelled():
                    exc = task.exception()
                    if exc is not None:
                        raise exc
                    raise RuntimeError("Discord 로그인 task가 ready 전에 종료되었습니다.")
            await asyncio.sleep(0.5)

        if STATUS_BOT_TOKEN:
            while not status_client.is_ready():
                for task in login_tasks:
                    if task.done() and not task.cancelled():
                        exc = task.exception()
                        if exc is not None:
                            raise exc
                await asyncio.sleep(0.5)

        log_message(
            "General",
            f"로그인 완료 ({len(enabled_characters)}명):",
            ", ".join(CHARACTERS[c]["name"] for c in enabled_characters)
        )

        for character in enabled_characters:
            client = clients[character]

            if not GENERIC_CONFIG_ACTIVE or feature_enabled("presence"):
                background_tasks.append(
                    asyncio.create_task(activity_loop(client, character))
                )

            if (
                not GENERIC_CONFIG_ACTIVE
                or feature_enabled("offline_mention_recovery")
            ):
                background_tasks.append(
                    asyncio.create_task(bot_runtime_heartbeat_loop(character))
                )
                background_tasks.append(
                    asyncio.create_task(recover_offline_mentions(client, character))
                )

            if (
                not GENERIC_CONFIG_ACTIVE
                or (
                    feature_enabled("world_simulation")
                    and feature_enabled("autonomous_messages")
                )
            ):
                background_tasks.append(
                    asyncio.create_task(autonomous_message_loop(client, character))
                )

            if (
                not GENERIC_CONFIG_ACTIVE
                or (
                    feature_enabled("personal_rp")
                    and feature_enabled("autonomous_messages")
                )
            ):
                background_tasks.append(
                    asyncio.create_task(personal_rp_autonomous_loop(client, character))
                )

            if (
                not GENERIC_CONFIG_ACTIVE
                or (
                    feature_enabled("world_simulation")
                    and feature_enabled("place_movement")
                )
            ):
                background_tasks.append(
                    asyncio.create_task(place_movement_loop(client, character))
                )

            if (
                not GENERIC_CONFIG_ACTIVE
                or (
                    feature_enabled("world_simulation")
                    and (feature_enabled("sleep_system") or any(feature_enabled(k) for k in ("seating", "meal_stages", "naps", "activity_stages", "outfit_condition", "weekly_schedules", "opening_hours")))
                )
            ):
                background_tasks.append(
                    asyncio.create_task(life_state_loop(character))
                )

            if (
                not GENERIC_CONFIG_ACTIVE
                or (
                    feature_enabled("world_simulation")
                    and feature_enabled("appointments")
                )
            ):
                background_tasks.append(
                    asyncio.create_task(appointment_loop(character))
                )

            if (
                not GENERIC_CONFIG_ACTIVE
                or (
                    feature_enabled("world_simulation")
                    and feature_enabled("away_system")
                )
            ):
                background_tasks.append(
                    asyncio.create_task(away_loop(character))
                )

        if STATUS_BOT_TOKEN and (
            not GENERIC_CONFIG_ACTIVE
            or feature_enabled("status_dashboard")
        ):
            background_tasks.append(
                asyncio.create_task(status_dashboard_loop())
            )
        else:
            log_message(
                "Status",
                "DISCORD_STATUS_TOKEN이 없어 상태 대시보드는 비활성화됨",
            )

        if (
            not GENERIC_CONFIG_ACTIVE
            or (
                feature_enabled("world_simulation")
                and feature_enabled("dynamic_events")
            )
        ):
            background_tasks.append(
                asyncio.create_task(dynamic_event_loop())
            )

        if (
            not GENERIC_CONFIG_ACTIVE
            or (
                feature_enabled("world_simulation")
                and feature_enabled("city_events")
            )
        ):
            background_tasks.append(
                asyncio.create_task(city_event_loop())
            )

        if (
            not GENERIC_CONFIG_ACTIVE
            or feature_enabled("presence")
        ):
            background_tasks.append(
                asyncio.create_task(presence_sync_loop())
            )

        await asyncio.gather(
            *login_tasks,
            *background_tasks,
        )

    finally:
        _monitor.environment['실행 상태'] = '종료 중: 생활 상태 저장 및 Discord 연결 정리'
        _monitor.flush()
        _life.save()
        openai_client.close_requests()
        # Only currently running calls count; historical failure rows never block exit.
        shutdown_started=asyncio.get_running_loop().time()
        while openai_client.active and asyncio.get_running_loop().time()-shutdown_started<30:
            _monitor.environment['실행 상태']=f'종료 대기: AI 실행 {openai_client.active}건 · {int(asyncio.get_running_loop().time()-shutdown_started)}초'
            _monitor.world=_world_actions.snapshot(CUSTOM_SETTINGS);_monitor.flush()
            await asyncio.sleep(1)
        for job in _world_actions.snapshot(CUSTOM_SETTINGS)['jobs']:
            if job['phase'] in ('생성 중','전송 중'):_world_actions.job_phase(job['id'],'불확정','종료 시 진행 결과 확인 필요')
        _monitor.environment['실행 상태']='종료 중: Discord 연결 정리';_monitor.flush()
        # 어느 한 task에서 예외가 나더라도 나머지 background task와 Discord 세션을
        # 정상적으로 닫아 aiohttp의 'Unclosed client session' 경고를 줄인다.
        for task in background_tasks:
            if not task.done():
                task.cancel()

        if background_tasks:
            await asyncio.gather(*background_tasks, return_exceptions=True)

        for character in enabled_characters:
            try:
                set_bot_last_seen_at(character)
            except Exception:
                pass

            client = clients.get(character)
            if client is not None and not client.is_closed():
                try:
                    await client.close()
                except Exception as e:
                    log_message(
                        CHARACTERS[character]["name"],
                        "Discord 클라이언트 종료 오류:",
                        type(e).__name__,
                        "-",
                        e,
                    )

        if STATUS_BOT_TOKEN and not status_client.is_closed():
            try:
                await status_client.close()
            except Exception as e:
                log_message(
                    "Status",
                    "상태봇 종료 오류:",
                    type(e).__name__,
                    "-",
                    e,
                )

        for task in login_tasks:
            if not task.done():
                task.cancel()

        if login_tasks:
            await asyncio.gather(*login_tasks, return_exceptions=True)

        monitor_task.cancel()
        await asyncio.gather(monitor_task,return_exceptions=True)
        _monitor.environment['실행 상태']='종료 완료'
        _monitor.world=_world_actions.snapshot(CUSTOM_SETTINGS)
        log_message("General", "Discord 클라이언트 정리 완료")
        for state in _monitor.characters.values(): state['connected'] = False
        _monitor.requests.clear()
        _monitor.flush()



_monitor_commands(status_tree, "관리자")
init_db()
ensure_personal_rp_settings_table()
ensure_offline_mention_tables()
ensure_daily_weather()
for _character in CHARACTERS:
    ensure_daily_sleep_schedule(_character)

# 공개 직전에 테스트 데이터를 지우고 싶으면 아래 한 줄의 주석만 잠깐 해제하고
# 한 번 실행한 뒤 다시 주석 처리한다.
# reset_test_data()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print()
        log_message("General", "Discord 봇 종료 완료")
