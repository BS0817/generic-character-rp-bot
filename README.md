# Generic Character RP Bot

사용자가 직접 캐릭터, 장소, 세계관을 정의할 수 있는 **범용 멀티 캐릭터 Discord RP 봇 프레임워크**입니다.

여러 캐릭터 봇이 하나의 세계 안에서 각자의 생활 상태를 가지며 이동하고, 자율적으로 말하고, 서로 대화하고, 사용자와 관계와 기억을 쌓도록 구성되어 있습니다.

> 현재 공개 초기 버전: **v0.1.0**

## 주요 기능

- 여러 Discord 캐릭터 봇을 하나의 프로그램에서 동시 실행
- 캐릭터별 프롬프트와 생활 습관 설정
- 장소별 Discord 채널 및 방문 가중치
- 수면 / 기상 / 외출 / 약속 / 자동 이동
- 캐릭터 자율발언 및 캐릭터끼리의 대화
- 사용자 및 캐릭터 관계 장기 기억
- 본서버 / 개인 RP 서버 / DM 상태 분리
- 종료 중 받은 사람의 멘션과 답글 복구
- 기능별 ON/OFF 설정
- RP 생활 시간대 설정
- `/서버초기화`를 통한 장소 채널 자동 생성
- Windows EXE 빌드 지원

## Windows 사용자

GitHub Releases에 Windows 배포 ZIP이 올라온 경우 Python 설치 없이 사용할 수 있습니다.

1. 배포 ZIP 압축을 풉니다.
2. `RPBot_Setup.exe`를 실행합니다.
3. 질문에 따라 API 키, Discord 서버, 캐릭터, 장소를 설정합니다.
4. `prompts/`의 캐릭터 프롬프트를 원하는 만큼 수정합니다.
5. `RPBot.exe`를 실행합니다.
6. 필요하면 Discord에서 `/서버초기화`를 실행합니다.

## 소스에서 실행

Python 3.11 또는 3.12 권장.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python src/setup_wizard.py
python src/bot.py
```

## 설정 파일

### `config/characters.json`
캐릭터 이름, Discord 토큰 환경변수, 프롬프트 파일, 개인실, 수면/기상 범위, 소지품, 습관, 목표, 장소 선호 등을 정의합니다.

### `config/places.json`
장소 이름, Discord 채널, 장소 설명, 주변 사물, 시간대별 방문 배율을 정의합니다.

### `config/relations.json`
캐릭터 사이에 시작 시점부터 이미 존재하는 관계만 정의합니다. 설정하지 않은 관계는 실제 상호작용을 통해 발전시킬 수 있습니다.

### `config/settings.json`
세계 이름과 설명, 공통 RP 규칙, 시간대, 기능별 ON/OFF를 관리합니다. 예시 파일의 `_설명` 항목은 엔진이 무시하므로 안내문으로 사용할 수 있습니다.

## 생활 시간

```json
"timezone": "Asia/Seoul"
```

`sleep_start_range`, `wake_range`, 약속, 시간대별 장소 이동은 이 시간대를 기준으로 계산합니다.

예:

```json
"sleep_start_range": [[1, 0], [3, 0]],
"wake_range": [[8, 0], [10, 0]]
```

위 설정은 생활 시간대 기준으로 01:00~03:00 사이에 취침하고 08:00~10:00 사이에 기상하는 스케줄을 매일 정합니다.

## 기능 ON/OFF

`config/settings.json`의 `features`에서 주요 기능을 켜거나 끌 수 있습니다.

```json
"features": {
  "world_simulation": true,
  "place_movement": true,
  "autonomous_messages": true,
  "bot_to_bot_chat": true,
  "long_term_memory": true,
  "relationships": true,
  "personal_rp": true,
  "dm": true
}
```

Discord에서 `/기능설정보기`로 현재 상태를 확인할 수 있습니다.

## Discord 서버 초기화

봇에 **채널 관리** 권한을 준 뒤 `/서버초기화`를 실행하면 `places.json`과 캐릭터 개인실 설정을 기준으로 없는 카테고리/채널을 생성합니다. 기존 채널은 삭제하거나 덮어쓰지 않습니다.

## EXE 직접 빌드

Windows에서 `build_exe.bat`을 실행합니다. 빌드 PC에는 Python이 필요하지만, 완성된 EXE를 사용하는 PC에는 Python이 필요하지 않습니다.

완료되면 `release/`에 다음 파일이 만들어집니다.

```text
release/
├─ RPBot.exe
├─ RPBot_Setup.exe
├─ README_KO.md
├─ config/
└─ prompts/
```

## 보안 주의

- `.env`에는 OpenAI API Key와 Discord Bot Token이 들어가므로 절대 GitHub에 올리지 마세요.
- 실제 토큰이 들어간 설정, 로그, DB도 공개 저장소에 커밋하지 않는 것을 권장합니다.
- 예시 `config/`와 `prompts/`에는 실제 비밀값을 넣지 마세요.

## 프로젝트 상태

현재 초기 공개 버전입니다. 설정 마법사와 Windows EXE 배포 흐름을 다듬고 있습니다.