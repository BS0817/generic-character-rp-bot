# 초보자용 설치 및 사용 가이드

이 문서는 **Discord 봇을 처음 만들어보는 사람**도 따라갈 수 있도록 처음부터 차근차근 작성하는 것을 목표로 합니다.

작성할 때는 각 단계마다 아래 순서를 추천합니다.

1. 무엇을 해야 하는지 설명
2. 화면에서 어디를 눌러야 하는지 설명
3. 스크린샷 삽입
4. 주의사항이 있으면 별도 표시
5. 다음 단계에서 왜 필요한지 짧게 설명

---

# Part 1. Discord 서버 준비

## 1. Discord 서버 만들기

여기에 Discord 서버를 만드는 방법을 작성하세요.

![Discord 서버 만들기](images/discord_01_create_server.png)

### 체크할 것
- 서버 이름은 자유롭게 정해도 됩니다.
- RP 봇의 세계 이름과 Discord 서버 이름은 같지 않아도 됩니다.

---

## 2. Discord Developer Portal 열기

여기에 Discord Developer Portal에 들어가는 방법을 작성하세요.

![Discord Developer Portal](images/discord_02_developer_portal.png)

---

## 3. 새 Application 만들기

여기에 `New Application` 버튼을 눌러 애플리케이션을 만드는 방법을 작성하세요.

![새 Application 만들기](images/discord_03_new_application.png)

---

## 4. Bot 만들기

왼쪽 메뉴의 `Bot` 항목에서 Discord 봇을 만드는 과정을 작성하세요.

![Bot 설정](images/discord_04_bot_menu.png)

---

## 5. Bot Token 발급하기

토큰을 발급하는 방법을 작성하세요.

![Bot Token 발급](images/discord_05_reset_token.png)

> [!WARNING]
> Bot Token은 비밀번호와 같습니다.
> 스크린샷, GitHub, Discord 채팅 등에 절대 공개하지 마세요.
> 실수로 공개했다면 즉시 토큰을 재발급하세요.

### 캐릭터 수와 봇 수
- 캐릭터가 1명이면 Discord 봇 1개가 필요합니다.
- 캐릭터가 2명이면 Discord 봇 2개가 필요합니다.
- 상태봇 기능까지 사용할 경우 상태봇용 Discord 봇이 추가로 필요할 수 있습니다.

---

## 6. Privileged Gateway Intents 설정하기

여기에 필요한 Intent를 켜는 방법을 작성하세요.

![Gateway Intents](images/discord_06_intents.png)

### 어떤 항목을 켜야 하나요?
여기에 실제로 필요한 Intent 목록을 적으세요.

---

## 7. 봇 초대 링크 만들기

OAuth2 관련 메뉴에서 봇 초대 링크를 만드는 과정을 작성하세요.

![OAuth2 설정](images/discord_07_oauth_generator.png)

---

## 8. 봇 권한 설정하기

봇에 필요한 권한을 체크하는 과정을 작성하세요.

![Bot 권한](images/discord_08_bot_permissions.png)

### 권한 메모
여기에 필요한 Discord 권한을 정리하세요.

---

## 9. 서버에 봇 초대하기

생성한 초대 링크를 이용해 원하는 Discord 서버에 봇을 추가하는 과정을 작성하세요.

![서버에 봇 초대](images/discord_09_invite_server.png)

---

## 10. 봇이 서버에 들어왔는지 확인하기

![봇 입장 확인](images/discord_10_bot_joined.png)

정상적으로 들어왔다면 다음 단계로 진행합니다.

---

# Part 2. Discord 서버 설정

## 11. 역할과 권한 확인하기

여기에 봇 역할과 서버 권한을 확인하는 방법을 작성하세요.

![역할 권한](images/discord_11_role_permissions.png)

---

## 12. 개발자 모드 켜기

서버 ID나 채널 ID를 복사하려면 Discord 개발자 모드가 필요합니다.

![개발자 모드](images/discord_12_enable_developer_mode.png)

---

## 13. 서버 ID 복사하기

여기에 서버 ID를 복사하는 방법을 작성하세요.

![서버 ID 복사](images/discord_13_copy_server_id.png)

---

# Part 3. OpenAI API 준비

## 14. OpenAI API Key 만들기

여기에 OpenAI API Key를 준비하는 방법을 작성하세요.

> [!WARNING]
> OpenAI API Key도 비밀번호처럼 취급하세요.
> GitHub 저장소나 공개된 스크린샷에 절대 포함하지 마세요.

![OpenAI API Key](images/openai_01_api_key.png)

---

# Part 4. 프로그램 다운로드 및 설치

## 15. Windows ZIP 다운로드하기

GitHub Releases 페이지에서 최신 Windows ZIP을 다운로드하는 방법을 작성하세요.

![Release 다운로드](images/app_01_release_page.png)

---

## 16. 압축 해제하기

ZIP 파일을 원하는 폴더에 압축 해제합니다.

![압축 해제](images/app_02_unzip_folder.png)

> [!IMPORTANT]
> ZIP 파일 안에서 바로 실행하지 말고 반드시 먼저 압축을 해제하세요.

정상적으로 압축을 풀면 대략 아래와 같은 파일이 보입니다.

```text
RPBot.exe
RPBot_Setup.exe
README_KO.md
config/
prompts/
```

---

# Part 5. 최초 설정

## 17. RPBot_Setup.exe 실행하기

`RPBot_Setup.exe`를 실행합니다.

![셋업 실행](images/app_03_setup_start.png)

실행하면 검은색 콘솔 창이 열리고 여러 질문이 표시됩니다.

---

## 18. 기본 정보 입력하기

여기에 실제 Setup Wizard 질문 순서대로 작성하세요.

예:
- OpenAI API Key
- Discord 서버 ID
- 캐릭터 수
- 각 캐릭터 봇 토큰
- 세계 이름
- 세계 설명
- 시간대
- 장소 설정
- 기능 ON/OFF

![셋업 질문](images/app_04_setup_questions.png)

---

# Part 6. 캐릭터와 세계관 설정

## 19. prompts 폴더 수정하기

`prompts` 폴더에 있는 캐릭터별 텍스트 파일을 수정합니다.

![프롬프트 파일](images/app_05_prompt_file.png)

### 작성하면 좋은 내용
- 캐릭터 이름
- 성격
- 말투
- 외형
- 좋아하는 것
- 싫어하는 것
- 목표
- 약점
- 다른 캐릭터와의 초기 관계
- 절대 변하면 안 되는 핵심 성격

---

## 20. config 폴더 이해하기

여기에 각 설정 파일의 역할을 정리하세요.

### characters.json
캐릭터 기본 정보와 생활 설정

### places.json
장소와 Discord 채널 설정

### relations.json
캐릭터 간 초기 관계 설정

### settings.json
세계관 공통 설정과 기능 ON/OFF

---

# Part 7. 봇 실행

## 21. RPBot.exe 실행하기

`RPBot.exe`를 실행합니다.

![봇 실행](images/app_06_bot_run.png)

정상적으로 실행되면 캐릭터 봇들이 Discord에 로그인합니다.

---

## 22. 서버 초기화하기

Discord에서 `/서버초기화` 명령어를 실행합니다.

![서버 초기화 명령어](images/discord_14_server_initialized.png)

### /서버초기화가 하는 일
- 설정된 장소 채널이 없으면 생성
- 필요한 카테고리 생성
- 상태 채널 생성 가능
- 기존 채널은 자동으로 삭제하지 않음

---

# Part 8. 정상 작동 확인

## 23. 기능 설정 확인하기

`/기능설정보기` 명령어를 사용합니다.

![기능 설정 보기](images/app_07_feature_settings.png)

---

## 24. 캐릭터와 대화해보기

캐릭터를 멘션하거나 설정된 방식으로 대화를 시작합니다.

![캐릭터 대화](images/app_08_chat_example.png)

확인할 것:
- 캐릭터가 자신의 프롬프트에 맞게 말하는지
- 다른 캐릭터와의 말투가 자연스러운지
- 장소/상태가 정상적으로 반영되는지

---

# Part 9. 자주 생기는 문제

## 봇이 온라인이 되지 않아요
여기에 원인과 해결 방법을 작성하세요.

## 슬래시 명령어가 안 보여요
여기에 해결 방법을 작성하세요.

## 캐릭터가 대답하지 않아요
여기에 해결 방법을 작성하세요.

## 채널이 생성되지 않아요
여기에 해결 방법을 작성하세요.

## OpenAI 관련 오류가 나요
여기에 해결 방법을 작성하세요.

## Windows에서 실행 경고가 나와요
여기에 Windows SmartScreen 관련 설명을 작성하세요.

---

# Part 10. 고급 설정

## 캐릭터 추가하기
여기에 추가 방법을 작성하세요.

## 장소 추가하기
여기에 추가 방법을 작성하세요.

## 기능 끄고 켜기
여기에 settings.json의 features 설정 방법을 작성하세요.

## 개인 RP 서버 사용하기
여기에 개인 RP 기능 사용법을 작성하세요.

## DM 기능 사용하기
여기에 DM 기능 사용법을 작성하세요.

---

# 스크린샷 파일 이름 예시

```text
docs/images/
├─ discord_01_create_server.png
├─ discord_02_developer_portal.png
├─ discord_03_new_application.png
├─ discord_04_bot_menu.png
├─ discord_05_reset_token.png
├─ discord_06_intents.png
├─ discord_07_oauth_generator.png
├─ discord_08_bot_permissions.png
├─ discord_09_invite_server.png
├─ discord_10_bot_joined.png
├─ discord_11_role_permissions.png
├─ discord_12_enable_developer_mode.png
├─ discord_13_copy_server_id.png
├─ discord_14_server_initialized.png
├─ openai_01_api_key.png
├─ app_01_release_page.png
├─ app_02_unzip_folder.png
├─ app_03_setup_start.png
├─ app_04_setup_questions.png
├─ app_05_prompt_file.png
├─ app_06_bot_run.png
├─ app_07_feature_settings.png
└─ app_08_chat_example.png
```

---

# 작성 팁

- 한 문단은 짧게 작성하는 편이 읽기 쉽습니다.
- 버튼 이름은 가능하면 실제 UI 표기와 똑같이 씁니다.
- 처음 보는 사람 기준으로 "왜 이걸 해야 하는지"도 한 문장씩 적어주세요.
- 긴 설명보다 스크린샷 + 짧은 설명 조합이 이해하기 쉽습니다.
- 토큰, API Key, 개인 서버 ID 등 민감한 정보는 스크린샷에서 반드시 가립니다.
