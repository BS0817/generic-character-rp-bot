# Generic Character RP Bot

사용자가 직접 캐릭터, 장소, 세계관을 정의할 수 있는 **범용 멀티 캐릭터 Discord RP 봇 프레임워크**입니다.

여러 캐릭터 봇이 하나의 세계 안에서 각자의 생활 상태를 가지며 이동하고, 자율적으로 말하고, 서로 대화하고, 사용자와 관계와 기억을 쌓도록 구성되어 있습니다.

> GUI 업데이트 준비 버전: **v0.1.3** (정식 다운로드는 Releases를 확인하세요)

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
3. 왼쪽 **API · Discord** 메뉴에서 API 키와 서버 ID를 입력하고 저장합니다.
4. **세계관 · 기능**, **장소**, **캐릭터**, **관계** 메뉴에서 설정합니다. 개인실을 사용하려면 장소 메뉴에도 개인실을 추가하세요.
5. 캐릭터 메뉴에서 봇 토큰과 프롬프트를 저장합니다. 프로필 이미지는 관리 화면에 표시되며 Discord 아바타를 바꾸지는 않습니다.
6. **시작하기 → 봇 실행** 또는 `RPBot.exe`로 봇을 실행합니다.
7. 필요하면 Discord에서 `/서버초기화`를 실행합니다.

### 설정 관리와 백업

새 `RPBot_Setup.exe`는 어두운 테마의 GUI입니다. 캐릭터·장소 추가/수정/삭제, 프롬프트, 초기 관계, 세계관, 16개 기능, 자율발언 간격과 대화 최대 턴을 관리할 수 있습니다. 기존 JSON의 추가 필드와 `.env`의 다른 값·주석은 유지합니다. 설정 저장 전에 `backup_setup/`에 설정·프롬프트·관리 화면 프로필·키를 백업합니다. **백업에 API 키와 토큰이 들어 있으므로 공유하지 마세요.**

설정 변경 후에는 봇 콘솔에서 Ctrl+C로 종료하고 다시 실행하세요. 삭제는 초기 관계·출입 제한·장소 참조를 정리하고, 학습된 기억·대화 기록·기존 프롬프트와 토큰은 남깁니다. 백업 복원은 해당 파일을 덮어쓰는 방식이며 이후에 추가된 파일이나 실제 대화 DB를 삭제하지 않습니다.

### 기존 사용자와 자동 업데이트

v0.1.2 이하에서 처음 변경할 때는 봇을 종료한 다음, 새 ZIP에서 `RPBot.exe`, `RPBot_Setup.exe`, `RPBot_Updater.exe` **세 실행 파일만** 기존 폴더로 복사하세요. 기존 `config/`, `prompts/`, `.env`, DB는 덮어쓰지 마세요.

이후에는 시작할 때 새 정식 버전을 확인하고, 설정 프로그램의 **업데이트** 메뉴에서 변경 사항을 읽은 뒤 설치할 수 있습니다. 자동 확인은 끌 수 있습니다. 업데이트 확인에 실패해도 봇 실행에는 영향을 주지 않습니다. 설치 전 봇을 종료해야 하며, SHA-256으로 다운로드를 검증하고 세 실행 파일만 교체합니다. 교체 실패 시 기존 실행 파일로 복구합니다. 완료 후 설정 프로그램이 다시 열리며, 봇은 사용자가 실행합니다.

이전 실행 파일은 설치 폴더의 `rpbot-update-*/previous/`에 보관됩니다. 업데이트가 정상 작동하는 것을 확인한 뒤 이 임시 폴더를 삭제할 수 있습니다. 업데이트는 GitHub 정식 릴리스의 Windows ZIP과 `.sha256`이 함께 있을 때 지원합니다. Python 소스 실행과 Railway에서는 GUI 자동 설치를 사용하지 않습니다.

## Railway 24시간 클라우드 운영

컴퓨터를 계속 켜 두지 않고 실행하려면 [Railway 한국어 배포 가이드](https://bs0817.github.io/generic-character-rp-bot/railway_ko.html)를 참고하세요. 비공개 GitHub 저장소에 본인 config/prompts를 준비하고, Railway Variables에 토큰을 넣고, `/data` Volume으로 SQLite와 로그를 보존합니다. Railway와 OpenAI API 요금이 각각 발생할 수 있습니다.

## 소스에서 실행

Python 3.11 또는 3.12 권장.

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
pip install -r requirements-gui.txt
python src/setup_gui.py
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
## 개발 검증

```bash
python -m unittest discover -s tests -v
```

콘솔 설정 마법사는 `python src/setup_wizard.py`로 계속 사용할 수 있습니다. GUI 의존성은 `requirements-gui.txt`에 분리돼 있으므로 Railway의 봇 실행에는 Qt가 필요하지 않습니다. Windows EXE는 `build_exe.bat` 또는 GitHub Actions에서 만듭니다. 릴리스 태그와 `src/app_version.py`의 VERSION을 일치시켜 배포하세요.
