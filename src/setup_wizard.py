from pathlib import Path
import sys
import json
import os
import re
import shutil
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent

CONFIG_DIR = ROOT / "config"
PROMPTS_DIR = ROOT / "prompts"
ENV_PATH = ROOT / ".env"

CONFIG_DIR.mkdir(exist_ok=True)
PROMPTS_DIR.mkdir(exist_ok=True)

CHARACTERS_PATH = CONFIG_DIR / "characters.json"
PLACES_PATH = CONFIG_DIR / "places.json"
SETTINGS_PATH = CONFIG_DIR / "settings.json"
RELATIONS_PATH = CONFIG_DIR / "relations.json"

from rp_policy import FEATURES
from world_calendar import validate_calendar
from world_actions import validate_extensions
from feature_dependencies import validate_features, dependency_errors, toggle_changes, LABELS as FEATURE_LABELS

FEATURE_QUESTIONS = [
    ("world_simulation", "본서버 월드 시뮬레이션", True),
    ("place_movement", "캐릭터 장소 자동 이동", True),
    ("autonomous_messages", "자율발언 / 먼저 말걸기", True),
    ("bot_to_bot_chat", "캐릭터끼리 자동 대화", True),
    ("long_term_memory", "사용자/캐릭터 장기 기억", True),
    ("relationships", "관계 설정 및 관계 변화", True),
    ("personal_rp", "개인 RP 서버 지원", True),
    ("dm", "캐릭터 DM 대화", True),
    ("offline_mention_recovery", "오프라인 멘션 복구", True),
    ("sleep_system", "수면 시스템", True),
    ("away_system", "외출 시스템", True),
    ("appointments", "약속 시스템", True),
    ("dynamic_events", "캐릭터 다이나믹 이벤트", True),
    ("city_events", "도시/세계 이벤트", True),
    ("status_dashboard", "상태봇 대시보드", True),
    ("presence", "Discord 상태메시지", True),
]
FEATURE_QUESTIONS.extend(FEATURES)


def ask(prompt, default=None, allow_empty=False):
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        value = input(f"{prompt}{suffix}: ").strip()

        if not value and default is not None:
            return str(default)

        if value or allow_empty:
            return value

        print("값을 입력해주세요.")


def ask_yes_no(prompt, default=True):
    default_text = "Y/n" if default else "y/N"
    while True:
        value = input(f"{prompt} [{default_text}]: ").strip().lower()

        if not value:
            return default

        if value in ("y", "yes", "예", "ㅇ", "응"):
            return True

        if value in ("n", "no", "아니오", "ㄴ"):
            return False

        print("y 또는 n으로 입력해주세요.")


def ask_int(prompt, default, minimum=0, maximum=23):
    while True:
        value = ask(prompt, default)
        try:
            number = int(value)
        except ValueError:
            print("숫자로 입력해주세요.")
            continue

        if minimum <= number <= maximum:
            return number

        print(f"{minimum}~{maximum} 범위로 입력해주세요.")


def ask_float(prompt, default, minimum=0.0):
    while True:
        value = ask(prompt, default)
        try:
            number = float(value)
        except ValueError:
            print("숫자로 입력해주세요.")
            continue

        if number >= minimum:
            return number

        print(f"{minimum} 이상의 숫자를 입력해주세요.")


def ask_menu(prompt, choices):
    print(prompt)
    for index, label in enumerate(choices, 1):
        print(f"{index}. {label}")

    while True:
        value = input("선택: ").strip()
        try:
            number = int(value)
        except ValueError:
            print("번호로 입력해주세요.")
            continue

        if 1 <= number <= len(choices):
            return number

        print(f"1~{len(choices)} 사이 번호를 입력해주세요.")


def slugify(value, fallback):
    value = (value or "").strip().lower()
    value = re.sub(r"[^0-9a-zA-Z가-힣_-]+", "_", value)
    value = value.strip("_")
    return value or fallback


def env_key_for_character(key):
    value = re.sub(r"[^0-9A-Za-z]+", "_", key.upper()).strip("_")
    return f"DISCORD_{value}_TOKEN"


def parse_csv(value):
    if not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def csv_text(items):
    if not items:
        return ""
    return ", ".join(str(item) for item in items)


def load_json(path, default):
    if not path.exists():
        return default

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"경고: {path.name}을 읽지 못했습니다: {e}")
        return default


def save_json(path, data):
    if path == SETTINGS_PATH:
        validate_features(data)
        validate_calendar(data,load_json(CHARACTERS_PATH,{}),load_json(PLACES_PATH,{}))
        validate_extensions(load_json(CHARACTERS_PATH,{}),load_json(PLACES_PATH,{}))
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_env_lines():
    result = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip()
    return result


def save_env(values):
    order = [
        "OPENAI_API_KEY",
        "DISCORD_GUILD_ID",
        "DISCORD_STATUS_TOKEN",
        "DISCORD_STATUS_CHANNEL",
        "DISCORD_COMMAND_CHANNEL_ID",
        "DISCORD_PERSONAL_RP_GUILD_IDS",
    ]

    character_keys = sorted(
        key for key in values
        if key.startswith("DISCORD_")
        and key.endswith("_TOKEN")
        and key not in {"DISCORD_STATUS_TOKEN"}
    )

    lines = [
        "# Generated by setup_wizard.py",
        "",
    ]

    written = set()

    for key in order + character_keys:
        if key in values and key not in written:
            lines.append(f"{key}={values[key]}")
            written.add(key)

    for key, value in values.items():
        if key not in written:
            lines.append(f"{key}={value}")

    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def create_prompt_file(path, name):
    template = f"""[정체성]
이름: {name}
나이:
직업 / 역할:
출신:
현재 상황:

[성격]
-

[말투]
-

[외형]
-

[일상]
-

[좋아하는 것]
-

[싫어하는 것]
-

[목표]
-

[약점 / 모순]
-

[다른 캐릭터와의 관계]
- 이미 정해진 관계만 작성한다.
- 적지 않은 관계는 실제 상호작용을 통해 발전시킨다.

[역할의 중심]
- 이 캐릭터가 어떤 상황에서도 유지해야 할 핵심:
- 이 캐릭터가 변질되지 않도록 피해야 할 방향:
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(template, encoding="utf-8")


def ask_timezone(default="Asia/Seoul"):
    while True:
        value = ask(
            "RP 생활 시간대 (IANA 형식, 예: Asia/Seoul / America/New_York)",
            default,
        ).strip()

        try:
            ZoneInfo(value)
            return value
        except ZoneInfoNotFoundError:
            if value.lower() in ("asia/seoul", "kst"):
                return "Asia/Seoul"
            if value.lower() in ("utc", "etc/utc", "gmt"):
                return "UTC"

            print(
                "이 Python 환경에서 해당 시간대를 찾지 못했습니다. "
                "Windows라면 tzdata가 포함된 최신 배포판을 사용하거나 "
                "Asia/Seoul 또는 UTC를 사용해주세요."
            )


def backup_current():
    if not any(path.exists() for path in (
        CHARACTERS_PATH,
        PLACES_PATH,
        SETTINGS_PATH,
        RELATIONS_PATH,
        ENV_PATH,
    )):
        return None

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = ROOT / "backup_setup" / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)

    for path in (
        CHARACTERS_PATH,
        PLACES_PATH,
        SETTINGS_PATH,
        RELATIONS_PATH,
        ENV_PATH,
    ):
        if path.exists():
            shutil.copy2(path, backup_dir / path.name)

    print(f"백업 완료: {backup_dir}")
    return backup_dir


def maybe_backup():
    if ask_yes_no("변경 전에 현재 설정을 백업할까요?", True):
        backup_current()


def choose_character(characters, prompt="캐릭터를 선택하세요"):
    if not characters:
        print("등록된 캐릭터가 없습니다.")
        return None

    keys = list(characters)
    labels = [f"{characters[key].get('name', key)} ({key})" for key in keys]
    index = ask_menu(prompt, labels)
    return keys[index - 1]


def choose_place(places, prompt="장소를 선택하세요"):
    if not places:
        print("등록된 장소가 없습니다.")
        return None

    names = list(places)
    index = ask_menu(prompt, names)
    return names[index - 1]


def build_place(existing=None, default_name="새 장소"):
    existing = dict(existing or {})

    name = ask("장소 이름", existing.get("name", default_name))
    channel_name = ask(
        "Discord 채널 이름",
        existing.get(
            "channel_name",
            slugify(name, "place").replace("_", "-"),
        ),
    )
    description = ask(
        "장소 설명",
        existing.get("description", f"{name}의 공용 공간."),
    )
    objects = parse_csv(
        ask(
            "주변 사물 (쉼표로 구분)",
            csv_text(existing.get("objects", [])),
            allow_empty=True,
        )
    )
    group = ask(
        "장소 그룹",
        existing.get("group", "public"),
        allow_empty=True,
    )
    parent = ask(
        "물리적 상위 장소 (없으면 Enter)",
        existing.get("parent") or "",
        allow_empty=True,
    )

    old_multipliers = existing.get("time_multipliers", {})
    time_multipliers = {
        "morning": ask_float("아침 방문 배율", old_multipliers.get("morning", 1.0)),
        "day": ask_float("낮 방문 배율", old_multipliers.get("day", 1.0)),
        "evening": ask_float("저녁 방문 배율", old_multipliers.get("evening", 1.0)),
        "night": ask_float("밤 방문 배율", old_multipliers.get("night", 1.0)),
        "late_night": ask_float("새벽 방문 배율", old_multipliers.get("late_night", 0.5)),
    }

    data = dict(existing)
    data.update({
        "channel_name": channel_name,
        "description": description,
        "objects": objects,
        "group": group or "public",
        "parent": parent or None,
        "allowed_characters": existing.get("allowed_characters", []),
        "time_multipliers": time_multipliers,
    })
    return name, data


def ask_time_pair_range(label, existing, defaults):
    existing = existing or defaults
    start = existing[0] if len(existing) > 0 else defaults[0]
    end = existing[1] if len(existing) > 1 else defaults[1]

    start_h = ask_int(f"{label} 최소 시", start[0], 0, 23)
    start_m = ask_int(f"{label} 최소 분", start[1], 0, 59)
    end_h = ask_int(f"{label} 최대 시", end[0], 0, 23)
    end_m = ask_int(f"{label} 최대 분", end[1], 0, 59)

    return [[start_h, start_m], [end_h, end_m]]


def build_character(
    key,
    places,
    env_values,
    existing=None,
    allow_prompt_replace=True,
):
    existing = dict(existing or {})
    name = ask("캐릭터 이름", existing.get("name", key))

    token_env = existing.get("token_env") or env_key_for_character(key)
    current_token = env_values.get(token_env, "")
    token = ask(
        f"{name} Discord Bot Token (Enter=기존 값 유지)",
        current_token,
        allow_empty=True,
    )
    if token:
        env_values[token_env] = token

    prompt_file = existing.get("prompt_file") or f"prompts/{key}.txt"
    prompt_path = ROOT / prompt_file

    current_room = existing.get("private_room")
    wants_room = ask_yes_no(
        f"{name}에게 개인실을 사용할까요?",
        current_room is not None,
    )
    private_room_name = (
        ask("개인실 이름", current_room or f"{name} 방")
        if wants_room
        else None
    )

    sleep_start_range = ask_time_pair_range(
        "취침 시작",
        existing.get("sleep_start_range"),
        [[1, 0], [3, 0]],
    )
    wake_range = ask_time_pair_range(
        "기상",
        existing.get("wake_range"),
        [[8, 0], [10, 0]],
    )

    inventory = parse_csv(
        ask(
            "기본 소지품 (쉼표 구분)",
            csv_text(existing.get("inventory", [])),
            allow_empty=True,
        )
    )
    habits = parse_csv(
        ask(
            "일상 습관 (쉼표 구분)",
            csv_text(existing.get("habits", [])),
            allow_empty=True,
        )
    )
    goals = parse_csv(
        ask(
            "장기 목표 (쉼표 구분)",
            csv_text(existing.get("goals", [])),
            allow_empty=True,
        )
    )

    old_weights = dict(existing.get("place_weights", {}))
    place_weights = {}
    print("장소별 방문 가중치를 설정합니다.")
    for place_name in places:
        place_weights[place_name] = ask_float(
            f"  {place_name} 가중치",
            old_weights.get(place_name, 5.0),
        )

    if private_room_name:
        place_weights[private_room_name] = ask_float(
            f"  {private_room_name} 가중치",
            old_weights.get(private_room_name, 14.0),
        )

    restricted = parse_csv(
        ask(
            "출입 금지 장소 (쉼표 구분, 없으면 Enter)",
            csv_text(existing.get("restricted_places", [])),
            allow_empty=True,
        )
    )

    data = dict(existing)
    data.update({
        "name": name,
        "token_env": token_env,
        "prompt_file": prompt_file,
        "private_room": private_room_name,
        "sleep_start_range": sleep_start_range,
        "wake_range": wake_range,
        "inventory": inventory,
        "habits": habits,
        "goals": goals,
        "place_weights": place_weights,
        "restricted_places": restricted,
    })

    if not prompt_path.exists():
        create_prompt_file(prompt_path, name)
        print(f"프롬프트 템플릿 생성: {prompt_file}")
    elif allow_prompt_replace and ask_yes_no(
        f"{prompt_file} 프롬프트 템플릿을 초기화할까요?",
        False,
    ):
        create_prompt_file(prompt_path, name)
        print(f"프롬프트 템플릿 초기화: {prompt_file}")

    return data


def edit_features(settings):
    settings = dict(settings or {})
    old_features = dict(settings.get("features", {}))
    features = {}

    print()
    print("기능 ON/OFF 설정")
    print("-" * 60)

    for key, label, default in FEATURE_QUESTIONS:
        features[key] = ask_yes_no(
            f"{label}을(를) 사용할까요?",
            bool(old_features.get(key, default)),
        )

    settings["features"] = features
    while dependency_errors(settings):
        child,parent=dependency_errors(settings)[0]
        print(f"{FEATURE_LABELS.get(child,child)}에는 {FEATURE_LABELS.get(parent,parent)}이 필요합니다.")
        if ask_yes_no('필수 기능도 함께 켤까요? (아니오: 종속 기능 끄기)',True):
            features.update(toggle_changes(settings,child,True))
        else: features.update(toggle_changes(settings,parent,False))
    validate_features(settings)

    channel_setup = dict(settings.get("channel_setup", {}))
    channel_setup["create_categories"] = ask_yes_no(
        "장소 그룹별 카테고리를 자동 생성할까요?",
        bool(channel_setup.get("create_categories", True)),
    )
    channel_setup["create_status_channel"] = ask_yes_no(
        "상태봇 사용 시 상태 채널도 자동 생성할까요?",
        bool(channel_setup.get("create_status_channel", True)),
    )
    channel_setup.setdefault(
        "category_names",
        {
            "public": "Public",
            "outdoor": "Outdoor",
            "private": "Private",
            "system": "System",
        },
    )
    settings["channel_setup"] = channel_setup
    return settings


def setup_new_environment():
    print()
    print("=" * 60)
    print("새 봇 환경 만들기")
    print("=" * 60)

    if any(path.exists() for path in (
        CHARACTERS_PATH,
        PLACES_PATH,
        SETTINGS_PATH,
        RELATIONS_PATH,
        ENV_PATH,
    )):
        print("기존 설정이 발견되었습니다.")
        if not ask_yes_no("새 설정으로 다시 만들까요?", False):
            print("취소했습니다.")
            return
        maybe_backup()

    env_values = load_env_lines()

    print()
    print("[1/5] 기본 키 / 서버 설정")
    print("-" * 60)

    openai_key = ask(
        "OpenAI API Key",
        env_values.get("OPENAI_API_KEY", ""),
        allow_empty=True,
    )
    if openai_key:
        env_values["OPENAI_API_KEY"] = openai_key

    guild_id = ask(
        "본서버 Discord Guild ID",
        env_values.get("DISCORD_GUILD_ID", ""),
        allow_empty=True,
    )
    if guild_id:
        env_values["DISCORD_GUILD_ID"] = guild_id

    if ask_yes_no("상태봇도 사용할까요?", bool(env_values.get("DISCORD_STATUS_TOKEN"))):
        status_token = ask(
            "상태봇 토큰",
            env_values.get("DISCORD_STATUS_TOKEN", ""),
            allow_empty=True,
        )
        if status_token:
            env_values["DISCORD_STATUS_TOKEN"] = status_token

        env_values["DISCORD_STATUS_CHANNEL"] = ask(
            "상태 채널 이름",
            env_values.get("DISCORD_STATUS_CHANNEL", "캐릭터-상태"),
        )

    print()
    print("[2/5] 세계관 설정")
    print("-" * 60)

    old_settings = load_json(SETTINGS_PATH, {})
    world_name = ask(
        "세계 이름",
        old_settings.get("world_name", "나의 RP 세계"),
    )
    world_description = ask(
        "세계 설명 (한 줄)",
        old_settings.get("world_description", ""),
        allow_empty=True,
    )
    rp_timezone = ask_timezone(
        old_settings.get("timezone", "Asia/Seoul"),
    )

    common_rules = []
    print("공통 규칙을 입력합니다. 빈 줄을 입력하면 끝납니다.")
    print("아무것도 입력하지 않으면 기존/기본 규칙을 사용합니다.")
    while True:
        rule = input("공통 규칙: ").strip()
        if not rule:
            break
        common_rules.append(rule)

    if not common_rules:
        common_rules = old_settings.get(
            "common_rules",
            [
                "설정에 없는 중대한 과거 사건이나 세계관 사실을 임의로 확정하지 않는다.",
                "사용자의 부탁은 명령이 아니며 캐릭터 성격에 따라 거절할 수 있다.",
                "항상 큰 사건만 만들지 말고 평범한 일상 대화도 자연스럽게 한다.",
            ],
        )

    settings = {
        "world_name": world_name,
        "world_description": world_description,
        "timezone": rp_timezone,
        "common_rules": common_rules,
    }
    settings = edit_features(settings)

    print()
    print("[3/5] 장소 설정")
    print("-" * 60)

    places = {}
    place_count = ask_int("공용 장소를 몇 개 만들까요?", 3, 1, 50)

    for index in range(1, place_count + 1):
        print()
        print(f"--- 장소 {index}/{place_count} ---")
        name, data = build_place(default_name=f"장소 {index}")
        while name in places:
            print("같은 이름의 장소가 이미 있습니다.")
            name = ask("다른 장소 이름", f"장소 {index}")
        places[name] = data

    print()
    print("[4/5] 캐릭터 설정")
    print("-" * 60)

    characters = {}
    character_count = ask_int("캐릭터를 몇 명 만들까요?", 2, 1, 50)
    used_keys = set()

    for index in range(1, character_count + 1):
        print()
        print(f"--- 캐릭터 {index}/{character_count} ---")
        name = ask("캐릭터 이름", f"캐릭터 {index}")
        key = slugify(
            ask("내부 캐릭터 키", slugify(name, f"character_{index}")),
            f"character_{index}",
        )

        base_key = key
        suffix = 2
        while key in used_keys:
            key = f"{base_key}_{suffix}"
            suffix += 1
        used_keys.add(key)

        temp_existing = {"name": name}
        characters[key] = build_character(
            key,
            places,
            env_values,
            existing=temp_existing,
            allow_prompt_replace=False,
        )

    print()
    print("[5/5] 캐릭터 초기 관계")
    print("-" * 60)

    relations = {}
    keys = list(characters)
    if len(keys) >= 2 and ask_yes_no(
        "캐릭터끼리 초기 관계도 지금 입력할까요?",
        True,
    ):
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                a_name = characters[a]["name"]
                b_name = characters[b]["name"]
                if ask_yes_no(
                    f"{a_name} ↔ {b_name} 관계를 지정할까요?",
                    False,
                ):
                    context = ask(
                        "관계 설명",
                        "서로 알고 지내는 사이이며 구체적인 관계는 실제 상호작용을 통해 발전한다.",
                    )
                    relations[f"{a}|{b}"] = {"context": context}

    save_json(CHARACTERS_PATH, characters)
    save_json(PLACES_PATH, places)
    save_json(SETTINGS_PATH, settings)
    save_json(RELATIONS_PATH, relations)
    save_env(env_values)

    print()
    print("=" * 60)
    print("새 설정 생성 완료")
    print("=" * 60)
    print(f"- 캐릭터: {len(characters)}명")
    print(f"- 공용 장소: {len(places)}개")
    print(f"- 초기 관계: {len(relations)}개")
    print("- .env 저장 완료")
    print()
    print("다음 단계:")
    print("1. prompts 폴더의 캐릭터 프롬프트를 원하는 만큼 자세히 작성")
    print("2. RPBot.exe 실행")
    print("3. 필요하면 Discord에서 /서버초기화 실행")


def add_character():
    characters = load_json(CHARACTERS_PATH, {})
    places = load_json(PLACES_PATH, {})
    relations = load_json(RELATIONS_PATH, {})
    env_values = load_env_lines()

    if not places:
        print("먼저 장소를 하나 이상 설정해주세요.")
        return

    maybe_backup()

    print()
    print("=" * 60)
    print("캐릭터 추가")
    print("=" * 60)

    name = ask("새 캐릭터 이름", f"캐릭터 {len(characters) + 1}")
    key = slugify(
        ask(
            "내부 캐릭터 키",
            slugify(name, f"character_{len(characters) + 1}"),
        ),
        f"character_{len(characters) + 1}",
    )

    base_key = key
    suffix = 2
    while key in characters:
        key = f"{base_key}_{suffix}"
        suffix += 1

    print(f"사용할 내부 키: {key}")
    characters[key] = build_character(
        key,
        places,
        env_values,
        existing={"name": name},
        allow_prompt_replace=False,
    )

    existing_keys = [c for c in characters if c != key]
    if existing_keys and ask_yes_no(
        "기존 캐릭터와의 초기 관계도 설정할까요?",
        False,
    ):
        for other in existing_keys:
            other_name = characters[other].get("name", other)
            if ask_yes_no(
                f"{name} ↔ {other_name} 관계를 지정할까요?",
                False,
            ):
                context = ask(
                    "관계 설명",
                    "서로 알고 지내는 사이이며 관계는 실제 상호작용을 통해 발전한다.",
                )
                relations[f"{other}|{key}"] = {"context": context}

    save_json(CHARACTERS_PATH, characters)
    save_json(RELATIONS_PATH, relations)
    save_env(env_values)

    print()
    print(f"캐릭터 추가 완료: {characters[key]['name']} ({key})")
    print("프롬프트를 확인한 뒤 RPBot.exe를 다시 실행해주세요.")


def add_place():
    places = load_json(PLACES_PATH, {})
    characters = load_json(CHARACTERS_PATH, {})

    maybe_backup()

    print()
    print("=" * 60)
    print("장소 추가")
    print("=" * 60)

    name, data = build_place(default_name=f"장소 {len(places) + 1}")
    if name in places:
        if not ask_yes_no(
            f"'{name}' 장소가 이미 있습니다. 덮어쓸까요?",
            False,
        ):
            print("취소했습니다.")
            return

    places[name] = data

    if characters:
        print()
        print("각 캐릭터의 새 장소 방문 가중치를 설정합니다.")
        for key, config in characters.items():
            config = dict(config)
            weights = dict(config.get("place_weights", {}))
            weights[name] = ask_float(
                f"{config.get('name', key)}의 {name} 가중치",
                weights.get(name, 5.0),
            )
            config["place_weights"] = weights
            characters[key] = config

    save_json(PLACES_PATH, places)
    save_json(CHARACTERS_PATH, characters)

    print()
    print(f"장소 추가 완료: {name}")
    print("RPBot.exe를 다시 실행한 뒤 /서버초기화를 실행하면 채널을 만들 수 있습니다.")


def edit_relations():
    characters = load_json(CHARACTERS_PATH, {})
    relations = load_json(RELATIONS_PATH, {})

    if len(characters) < 2:
        print("관계를 설정하려면 캐릭터가 2명 이상 필요합니다.")
        return

    maybe_backup()

    print()
    print("=" * 60)
    print("캐릭터 관계 수정")
    print("=" * 60)

    a = choose_character(characters, "첫 번째 캐릭터를 선택하세요")
    if a is None:
        return

    other_characters = {
        key: value
        for key, value in characters.items()
        if key != a
    }
    b = choose_character(other_characters, "두 번째 캐릭터를 선택하세요")
    if b is None:
        return

    possible_keys = [f"{a}|{b}", f"{b}|{a}"]
    existing_key = next((key for key in possible_keys if key in relations), None)
    existing_context = ""
    if existing_key:
        existing_context = relations[existing_key].get("context", "")
        print()
        print("현재 관계:")
        print(existing_context or "(설명 없음)")
    else:
        print()
        print("현재 별도 초기 관계가 없습니다.")

    action = ask_menu(
        "무엇을 할까요?",
        ["관계 추가/수정", "관계 삭제", "취소"],
    )

    if action == 1:
        context = ask(
            "관계 설명",
            existing_context or "서로 알고 지내는 사이이며 관계는 실제 상호작용을 통해 발전한다.",
        )
        for key in possible_keys:
            relations.pop(key, None)
        relations[f"{a}|{b}"] = {"context": context}
        save_json(RELATIONS_PATH, relations)
        print("관계 저장 완료.")
    elif action == 2:
        changed = False
        for key in possible_keys:
            if key in relations:
                relations.pop(key, None)
                changed = True
        save_json(RELATIONS_PATH, relations)
        print("관계를 삭제했습니다." if changed else "삭제할 관계가 없었습니다.")
    else:
        print("취소했습니다.")


def edit_features_menu():
    settings = load_json(SETTINGS_PATH, {})

    if not settings:
        print("settings.json이 없습니다. 먼저 새 봇 환경을 만들어주세요.")
        return

    maybe_backup()
    settings = edit_features(settings)

    if ask_yes_no("RP 생활 시간대도 변경할까요?", False):
        settings["timezone"] = ask_timezone(
            settings.get("timezone", "Asia/Seoul")
        )

    save_json(SETTINGS_PATH, settings)
    print()
    print("기능 설정 저장 완료. RPBot.exe를 다시 실행하면 적용됩니다.")


def edit_character():
    characters = load_json(CHARACTERS_PATH, {})
    places = load_json(PLACES_PATH, {})
    env_values = load_env_lines()

    if not characters:
        print("등록된 캐릭터가 없습니다.")
        return

    key = choose_character(characters, "수정할 캐릭터를 선택하세요")
    if key is None:
        return

    maybe_backup()

    print()
    print("=" * 60)
    print(f"캐릭터 수정: {characters[key].get('name', key)}")
    print("=" * 60)
    print(f"내부 캐릭터 키 '{key}'는 관계/기억 호환성을 위해 유지합니다.")

    characters[key] = build_character(
        key,
        places,
        env_values,
        existing=characters[key],
        allow_prompt_replace=True,
    )

    save_json(CHARACTERS_PATH, characters)
    save_env(env_values)

    print()
    print("캐릭터 수정 완료. RPBot.exe를 다시 실행하면 적용됩니다.")


def edit_world_settings():
    settings = load_json(SETTINGS_PATH, {})

    if not settings:
        print("settings.json이 없습니다. 먼저 새 봇 환경을 만들어주세요.")
        return

    maybe_backup()

    settings["world_name"] = ask(
        "세계 이름",
        settings.get("world_name", "나의 RP 세계"),
    )
    settings["world_description"] = ask(
        "세계 설명",
        settings.get("world_description", ""),
        allow_empty=True,
    )
    settings["timezone"] = ask_timezone(
        settings.get("timezone", "Asia/Seoul")
    )

    print()
    print("공통 규칙을 수정할까요?")
    if ask_yes_no("기존 공통 규칙을 새로 입력할까요?", False):
        common_rules = []
        print("빈 줄을 입력하면 끝납니다.")
        while True:
            rule = input("공통 규칙: ").strip()
            if not rule:
                break
            common_rules.append(rule)
        settings["common_rules"] = common_rules

    save_json(SETTINGS_PATH, settings)
    print("세계관 설정 저장 완료.")


def show_summary():
    characters = load_json(CHARACTERS_PATH, {})
    places = load_json(PLACES_PATH, {})
    relations = load_json(RELATIONS_PATH, {})
    settings = load_json(SETTINGS_PATH, {})

    print()
    print("=" * 60)
    print("현재 설정 요약")
    print("=" * 60)
    print(f"세계: {settings.get('world_name', '(미설정)')}")
    print(f"시간대: {settings.get('timezone', '(미설정)')}")
    print(f"캐릭터: {len(characters)}명")
    for key, config in characters.items():
        print(f"  - {config.get('name', key)} ({key})")
    print(f"공용 장소: {len(places)}개")
    for name in places:
        print(f"  - {name}")
    print(f"초기 관계: {len(relations)}개")
    print()


def main():
    print("=" * 60)
    print(" Generic Character Discord RP Bot - Setup Wizard v66")
    print("=" * 60)
    print()
    print("처음 설치뿐 아니라 기존 설정에 캐릭터/장소를 추가하거나")
    print("관계와 기능을 수정할 수 있습니다.")
    print()

    while True:
        has_existing = any(path.exists() for path in (
            CHARACTERS_PATH,
            PLACES_PATH,
            SETTINGS_PATH,
            RELATIONS_PATH,
            ENV_PATH,
        ))

        if not has_existing:
            print("기존 설정이 없습니다. 먼저 새 봇 환경을 만들어주세요.")
            setup_new_environment()
            print()
            if not ask_yes_no("계속 설정 마법사를 사용할까요?", False):
                break

        choice = ask_menu(
            "무엇을 하시겠습니까?",
            [
                "새 봇 환경 만들기",
                "캐릭터 추가",
                "장소 추가",
                "캐릭터 관계 수정",
                "기능 설정 변경",
                "기존 캐릭터 수정",
                "세계관 / 시간대 설정 변경",
                "현재 설정 요약 보기",
                "종료",
            ],
        )

        print()

        if choice == 1:
            setup_new_environment()
        elif choice == 2:
            add_character()
        elif choice == 3:
            add_place()
        elif choice == 4:
            edit_relations()
        elif choice == 5:
            edit_features_menu()
        elif choice == 6:
            edit_character()
        elif choice == 7:
            edit_world_settings()
        elif choice == 8:
            show_summary()
        elif choice == 9:
            print("설정 마법사를 종료합니다.")
            break

        print()
        input("Enter를 누르면 메인 메뉴로 돌아갑니다...")
        print()


if __name__ == "__main__":
    main()
