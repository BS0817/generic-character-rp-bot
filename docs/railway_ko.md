---
layout: guide
title: Railway 24시간 클라우드 설치 가이드
permalink: /railway_ko.html
---

# Railway 24시간 클라우드 설치 가이드

이 문서는 **Generic Character RP Bot**을 Railway에서 상시 실행하는 방법을 안내합니다. Windows PC를 계속 켜둘 필요는 없지만, Railway 서비스가 실행 중이고 요금·사용량 한도를 넘지 않아야 합니다. 유지보수와 배포 때에는 짧게 중단될 수 있으므로 100% 무중단을 보장하는 것은 아닙니다.

> **비용:** Railway Hobby 요금제는 월 **$5**이고 매월 $5 상당의 자원 사용량을 포함합니다. 실제 자원 사용량이 $5를 초과하면 초과분이 청구될 수 있습니다. Free는 월 $1 상당의 자원 사용량을 제공하지만 이 봇의 장기적인 24시간 운영용으로 보장할 수 없습니다. OpenAI API 요금은 Railway와 **별도**입니다. 최신 조건은 [Railway 요금](https://railway.com/pricing)에서 확인하세요.

## 시작 전에 준비할 것

- [GitHub](https://github.com/)와 [Railway](https://railway.com/) 계정.
- Discord 봇 토큰(캐릭터별, 상태봇을 사용하는 경우 상태봇도), Discord 서버 ID, [OpenAI API 키](https://platform.openai.com/api-keys).
- 컴퓨터에서 설정 마법사로 만든 **본인만의** `config/`와 `prompts/`. 처음부터 직접 JSON과 프롬프트를 작성해도 됩니다.
- 결제·사용량 설정을 확인할 수 있는 Railway 계정.

**중요:** `.env`, 실제 Discord 토큰, OpenAI API 키, `discord_memory.db`, 로그를 GitHub에 올리지 마세요. 반드시 Railway의 **Variables**에 비밀값을 넣습니다.

## 1. Windows에서 캐릭터 설정 만들기

1. [GitHub Releases](https://github.com/BS0817/generic-character-rp-bot/releases)에서 최신 Windows ZIP을 다운로드하고 압축을 풉니다.
2. `RPBot_Setup.exe`를 실행해 서버 ID, API 키, 캐릭터와 장소를 설정합니다.
3. `prompts/` 안의 각 캐릭터 프롬프트를 수정합니다.
4. 준비가 끝나면 **`config/` 폴더와 `prompts/` 폴더만** 다음 단계에서 사용할 수 있게 준비합니다. 로컬 `.env`에 적은 실제 키와 토큰은 **Railway Variables**에 다시 입력합니다.

설정 마법사가 만든 `characters.json`의 각 `token_env` 값을 확인하세요. 예를 들어 `DISCORD_CHARACTER_1_TOKEN`이라면 Railway에도 정확히 그 변수 이름으로 해당 캐릭터 토큰을 등록해야 합니다.

## 2. 비공개 GitHub 저장소 만들기

공개된 예시 저장소를 그대로 사용하면 모든 사용자에게 같은 예시 캐릭터만 배포됩니다. 개인 설정을 넣기 위해 **비공개 저장소**를 만드는 절차입니다.

1. GitHub의 [Import repository](https://github.com/new/import)에서 원본 주소 `https://github.com/BS0817/generic-character-rp-bot`를 입력합니다.
2. 새 저장소 이름을 정하고 **Private**을 선택해 가져옵니다. GitHub 화면과 권한에 따라 가져오기 절차가 달라질 수 있습니다.
3. 새 저장소의 `config/`와 `prompts/`에 1단계에서 만든 **본인의 설정 및 프롬프트 파일**을 반영합니다. 기존 같은 이름의 예시 파일은 교체합니다.
4. `Dockerfile`, `requirements.txt`, `src/bot.py`가 저장소에 있는지 확인합니다.

설정 파일이나 프롬프트에도 개인 정보와 비밀번호를 적지 않는 편이 안전합니다. GitHub의 파일 수정·업로드는 브라우저에서 할 수 있지만, 폴더별 파일이 많다면 GitHub Desktop을 이용해 커밋·푸시해도 됩니다.

**이미 기존 봇을 운영 중이라면:** 로컬 `discord_memory.db`는 GitHub에 올리지 않습니다. 이 가이드는 **새 클라우드 DB로 시작하는 방법**입니다. 기존 기억을 이전하려면 Railway Volume으로 별도 복사가 필요합니다.

## 3. Railway에서 GitHub 저장소 연결하기

1. [Railway](https://railway.com/)에 로그인하고 `New Project` → `Deploy from GitHub repo`를 선택합니다.
2. GitHub 연결을 승인하고 **2단계의 비공개 저장소**를 선택합니다. 목록에 없으면 GitHub의 Railway 앱 저장소 접근 권한을 확인합니다.
3. 서비스의 `Settings`에서 **Root Directory**를 저장소 루트(`/`)로 둡니다.
4. 저장소의 `Dockerfile`이 감지되면 Python 3.12 이미지에서 패키지를 설치하고 `python src/bot.py`를 실행합니다. `Start Command`를 별도로 지정할 필요는 없습니다.
5. 이 봇은 Discord로 **외부 연결**하므로 **Public Networking / 도메인 / HTTP Healthcheck**를 설정하지 않아도 됩니다. `Cron Schedule`도 설정하지 마세요.

첫 배포를 눌렀더라도 변수와 저장소를 설정하기 전에는 토큰이 없어 실패할 수 있습니다. 다음 단계를 마친 후 다시 배포하면 됩니다.

## 4. Variables에 비밀키와 토큰 등록하기

Railway 서비스 → `Variables`에서 다음 값을 각각 추가합니다. 설정 후 Railway가 변경 사항 배포를 요청하면 적용합니다.

```env
OPENAI_API_KEY=실제_OpenAI_API_키
DISCORD_GUILD_ID=본서버의_숫자_ID
DISCORD_CHARACTER_1_TOKEN=첫_캐릭터의_실제_Discord_토큰
DISCORD_CHARACTER_2_TOKEN=둘째_캐릭터의_실제_Discord_토큰
DISCORD_STATUS_TOKEN=상태봇을_사용한다면_상태봇_토큰
```

위의 토큰 변수 이름은 **예시**입니다. 사용자가 직접 만든 `config/characters.json`의 `token_env` 값에 맞추세요. 상태봇을 사용하지 않으면 `DISCORD_STATUS_TOKEN`은 생략할 수 있습니다. `DISCORD_PERSONAL_RP_GUILD_IDS` 등 기타 선택 변수는 사용하는 기능에 따라서만 추가합니다.

`.env` 파일을 GitHub로 옮길 필요는 **없습니다**. Railway Variables는 실행 프로그램에서 환경변수로 읽습니다.

## 5. Volume으로 장기 기억·로그 보존하기

이 단계를 건너뛰면 재배포나 컨테이너 교체로 SQLite 기억과 로그가 사라질 수 있습니다.

1. Railway 프로젝트에서 봇 서비스에 `Volume`을 추가합니다(`Attach Volume` 메뉴 또는 서비스의 Volume 설정).
2. **Mount Path를 `/data`로 지정**합니다.
3. 서비스 배포를 적용합니다.

이 버전의 `src/bot.py`는 Railway가 자동 제공하는 `RAILWAY_VOLUME_MOUNT_PATH`를 확인하고, 그 경로에 `discord_memory.db`와 `logs/`를 저장합니다. 따로 설정하고 싶다면 `BOT_DATA_DIR=/data`를 Variables에 넣을 수도 있지만 필수는 아닙니다. **Volume이 붙었는지 확인한 뒤 봇을 정상 운영하세요.**

`config/`와 `prompts/`는 비공개 GitHub 저장소에서 관리합니다. 이 파일은 새 커밋을 배포하면 업데이트되고, **DB/로그는 Volume에 남는** 구조입니다. Volume은 별도로 백업하는 것을 권장합니다.

## 6. 24시간 실행 설정

- 서비스 `Settings`에서 **Serverless를 비활성화**합니다. 서버리스는 비활동 시 서비스를 중지할 수 있습니다.
- 유료 요금제를 사용하는 경우 **Restart Policy → Always**로 설정하면 프로세스가 종료되었을 때 다시 시작하도록 설정할 수 있습니다. 무료/체험 계정에서는 Always가 지원되지 않을 수 있으며, 기본 `On Failure`는 재시도 횟수 제한이 있습니다.
- **Replicas는 1개**로 유지합니다. 여러 복제본을 실행하면 같은 Discord 토큰의 중복 접속과 SQLite 동시 접근 문제가 생길 수 있습니다.
- **Cron을 사용하지 않습니다.** 주기적으로 시작해서 끝나는 작업이 아니라 항상 연결된 프로세스가 필요합니다.

Railway의 `Deploy`를 실행하고 로그에서 Discord 연결 완료 메시지를 확인합니다. Discord에서 캐릭터를 멘션해 답하는지 확인하고, 상태봇을 사용하는 경우 대시보드도 확인하세요.

**같은 토큰으로 로컬 `RPBot.exe`와 Railway를 동시에 실행하지 마세요.** 클라우드가 정상 구동되면 로컬 프로그램은 종료합니다.

## 7. 설정 변경과 업데이트

- 캐릭터·장소·프롬프트 변경: 로컬 설정 마법사에서 수정 → **비공개 GitHub 저장소의 `config/`, `prompts/`에 반영** → 새 커밋 배포.
- API 키·토큰 변경: Railway의 `Variables`에서 교체 → 변경 사항 배포.
- 엔진 업데이트: 공개 원본 저장소의 새 버전 파일을 본인의 비공개 저장소에 반영하되, 개인 설정을 덮어쓰지 않도록 주의합니다.
- DB/로그: Volume에 남지만, Volume 제거와 계정 만료에 대비해 정기 백업합니다.

## 8. 자주 발생하는 문제

| 증상 | 확인할 내용 |
| --- | --- |
| `OPENAI_API_KEY` 관련 오류 | `Variables`에 키가 올바르게 등록되었는지, OpenAI API 잔액이 있는지 확인 |
| Discord 로그인 실패 | `token_env` 이름과 실제 토큰이 일치하는지 확인 |
| `prompts/*.txt` 파일을 찾지 못함 | GitHub에 해당 프롬프트를 올렸는지와 `prompt_file` 경로 확인 |
| 재배포했더니 기억이 없어짐 | Volume이 `/data`에 연결되어 있었는지, 실행 전에 연결했는지 확인 |
| 프로그램이 잠시 실행되다 종료됨 | `Deploy Logs`와 `Restart Policy`, RAM·CPU 사용량 확인 |
| GitHub 변경이 반영되지 않음 | 수정한 비공개 저장소와 Railway 연결 저장소가 일치하는지 확인 |
| 예상보다 요금이 많이 나옴 | Railway의 `Usage`에서 메모리·CPU·Volume 사용량을 확인하고 비용 상한 설정 |
| Discord 봇이 두 번 반응함 | 같은 토큰으로 로컬 실행 또는 복제본이 동시에 돌아가는지 확인 |

## 요금과 보안

Railway Hobby의 월 $5는 **고정된 무제한 24시간 사용료가 아니라** 첫 $5 상당의 리소스 이용분을 포함하는 기본 요금입니다. 실행하는 캐릭터 수와 메모리·CPU 사용량에 따라 추가 비용이 발생할 수 있습니다. Railway 계정의 `Usage`와 **사용량 알림 및 상한**을 설정하세요. 상한에 도달하면 서비스가 중단될 수 있습니다.

OpenAI API 사용료는 별도이며 ChatGPT Plus 구독으로 대신할 수 없습니다. OpenAI에서도 사용량을 확인하세요.

실제 토큰은 GitHub 커밋, 스크린샷, 배포 로그에 공유하지 마세요. 실수로 공개했다면 Discord 토큰과 OpenAI API 키를 재발급하세요.

## 참고 문서

- [Railway 요금 안내](https://docs.railway.com/pricing/plans)
- [Railway Volume](https://docs.railway.com/volumes/reference)
- [Railway 재시작 정책](https://docs.railway.com/deployments/restart-policy)
- [Railway Serverless](https://docs.railway.com/deployments/serverless)
- [Railway 비용 제한](https://docs.railway.com/pricing/cost-control)

[← 일반 Windows 설치 가이드](guide_ko.html) · [프로젝트 GitHub](https://github.com/BS0817/generic-character-rp-bot)
