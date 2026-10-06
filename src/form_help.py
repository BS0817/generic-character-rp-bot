"""Korean help text shared by setup forms and JSON configuration readers."""
import json

CHECKBOX_HELP = {
 'world_calendar':'현실 또는 가상 세계 날짜·요일·계절을 사용합니다.',
 'seasonal_weather':'세계 계절에 설정한 날씨 가중치와 기온 범위를 사용합니다. 세계 달력이 필요합니다.',
 'weekly_schedules':'요일과 시간에 따른 활동·장소 선호를 반영합니다. 세계 달력과 월드 시뮬레이션이 필요합니다.',
 'opening_hours':'장소의 영업 요일·시간에 맞춰 자동 입장을 제한합니다. 관리자 강제 이동은 허용합니다.',
 'anniversaries':'생일·기념일·정기 행사를 아는 캐릭터에게 전달합니다. 세계 달력이 필요합니다.',
 'show_date':'Discord와 PC 모니터의 환경 영역에 세계 날짜를 표시합니다.',
 'show_weekday':'환경 영역에 세계 요일을 표시합니다.',
 'show_season':'환경 영역에 세계 계절을 표시합니다.',
 'public':'모든 월드 캐릭터가 알고 환경 영역에 표시하는 기념일입니다.',
 'enabled':'이 장소의 영업시간을 적용합니다. 세계관의 영업시간 기능도 켜야 합니다. 시작과 종료가 같으면 해당 요일 24시간 영업입니다.',
 'world_simulation':'본서버에서 장소·활동·생활 상태를 관리합니다. 끄면 월드 생활 판단이 멈춥니다.',
 'place_movement':'생활 상태와 장소 가중치에 따라 이동합니다. 월드 시뮬레이션이 필요합니다.',
 'autonomous_messages':'사용자 메시지 없이도 상황에 맞춰 먼저 발언합니다.',
 'bot_to_bot_chat':'같은 장소의 캐릭터가 서로의 발언에 반응합니다.',
 'long_term_memory':'중요한 대화와 사건을 저장하고 이후 대화에 참고합니다.',
 'relationships':'사용자·캐릭터 관계 설정과 교류에 따른 변화를 반영합니다.',
 'personal_rp':'본서버와 구분되는 개인 RP 서버의 장면을 지원합니다.',
 'dm':'허용된 사용자가 캐릭터와 DM으로 대화할 수 있습니다.',
 'offline_mention_recovery':'오프라인 중 받은 직접 멘션·답글의 복구를 시도합니다.',
 'sleep_system':'설정된 취침·기상 시간에 맞춰 수면 상태를 관리합니다.',
 'away_system':'캐릭터가 일정 시간 외출한 뒤 돌아올 수 있습니다.',
 'appointments':'등록된 약속 시간과 장소를 생활 판단에 반영합니다.',
 'dynamic_events':'같은 장소의 캐릭터 사이에 작은 상황과 대화를 만듭니다.',
 'city_events':'세계나 특정 장소에 영향을 주는 사건을 사용합니다.',
 'status_dashboard':'상태봇을 통해 Discord에 캐릭터 상태를 표시합니다.',
 'presence':'Discord 프로필의 활동 상태에 현재 행동을 표시합니다.',
 'common_prompt':'세계 규칙과 별도로 모든 캐릭터의 공통 행동·대화 원칙을 적용합니다.',
 'dialogue_guard':'반복 발언과 끝난 대화를 불필요하게 이어가는 답변을 검사합니다.',
 'relationship_guard':'원래 관계와 어울리지 않는 친절·돌봄 등의 행동을 검사합니다.',
 'conversation_mode':'범위별 현장 RP·메신저 RP·직접 설정 방식을 반영합니다.',
 'outfit_preferences':'캐릭터별 선호 스타일·색·비선호 복장을 참고합니다. 개인별 사용 설정도 켜야 합니다.',
 'outfit_preferences_enabled':'이 캐릭터의 복장 취향을 사용합니다. 세계관의 복장 취향 반영도 켜야 합니다.',
 'seating':'좌석 정원·착석자·동석자를 관리합니다. 장소에 좌석을 설정하세요.',
 'meal_stages':'식사를 준비·섭취·정리 단계로 관리합니다. 장소에 메뉴를 설정하세요.',
 'naps':'피로와 장소 조건에 따라 짧게 낮잠을 잡니다. 좌석 관리와 낮잠 가능한 장소가 필요합니다.',
 'sleep_quality':'수면 품질과 방해 여부에 따라 회복을 조정합니다. 수면 시스템이 필요합니다.',
 'item_loans':'소지품을 빌려주고 돌려받는 상태를 기록합니다.',
 'activity_stages':'장소의 활동을 단계별로 진행합니다. 장소 활동 목록을 설정하세요.',
 'outfit_condition':'비 등에 젖은 복장과 건조 상태를 관리합니다.',
 'nap_allowed':'낮잠 후보 장소로 허용합니다. 세계관의 낮잠 기능과 생활 조건도 확인합니다.',
 'outdoor':'날씨에 노출되는 야외 장소로 취급합니다.',
 'create_categories':'서버초기화 시 장소 그룹에 맞는 Discord 카테고리를 만듭니다.',
 'create_status_channel':'서버초기화 시 상태 표시 채널을 만듭니다.',
 'show_token':'입력된 비밀 키를 화면에서 확인합니다. 저장 값에는 영향을 주지 않습니다.',
 'check_updates_on_start':'설정 프로그램을 열 때 새 릴리스가 있는지 확인합니다.',
}
JSON_HELP = '중괄호 { }는 이름과 값을 묶고, 대괄호 [ ]는 목록을 만듭니다. 소괄호 ( )는 JSON에 쓰지 않습니다. 이름과 문자는 큰따옴표 " "로 감싸고, 항목 사이는 쉼표로 구분하세요. 들여쓰기는 선택 사항이며 마지막 항목 뒤에는 쉼표를 쓰지 않습니다.'
JSON_EXAMPLES = {'seats':'{"테이블 1": 4, "소파": 2}', 'menus':'{"전체": ["차", "샌드위치"], "아침": ["토스트"]}'}

JSON_EXAMPLES.update(weekly_schedule='[{"days": ["월", "수", "금"], "start": "09:00", "end": "12:00", "activity": "독서", "place": "도서관"}]',season_profiles=json.dumps({'봄':{'months':[3,4,5],'temperature_min':8,'temperature_max':22,'weather_weights':{'맑음':40,'비':25}},'여름':{'months':[6,7,8],'temperature_min':22,'temperature_max':32,'weather_weights':{'맑음':45,'비':30}},'가을':{'months':[9,10,11],'temperature_min':8,'temperature_max':23,'weather_weights':{'맑음':45,'비':15}},'겨울':{'months':[12,1,2],'temperature_min':-5,'temperature_max':8,'weather_weights':{'맑음':35,'눈':20}}},ensure_ascii=False))

def parse_json(text, label='설정', key=None):
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        reasons = {
            'Expecting property name enclosed in double quotes':'항목 이름을 큰따옴표로 감싸세요. 마지막 항목 뒤의 쉼표도 확인하세요.',
            'Expecting value':'값이 빠졌거나 사용할 수 없는 문자가 있습니다.',
            "Expecting ',' delimiter":'항목 사이의 쉼표 또는 닫는 괄호를 확인하세요.',
            "Expecting ':' delimiter":'항목 이름 뒤에 콜론(:)을 넣으세요.',
            'Extra data':'닫는 괄호 뒤에 불필요한 내용이 있습니다.',
            'Unterminated string starting at':'문자열을 닫는 큰따옴표가 빠졌습니다.',
            'Invalid control character at':'문자열 안의 줄바꿈이나 특수문자를 확인하세요.',
            'Invalid \\escape':'역슬래시 뒤의 문자를 확인하세요. 경로에는 /를 사용할 수 있습니다.',
        }
        reason = reasons.get(error.msg,'큰따옴표, 쉼표와 괄호의 짝을 확인하세요.')
        example = JSON_EXAMPLES.get(key,'{"이름": "값"}')
        raise ValueError(f'{label}: JSON 형식이 올바르지 않습니다.\n{error.lineno}번째 줄, {error.colno}번째 글자 부근\n{reason}\n\n{JSON_HELP}\n\n작성 예시: {example}') from None
