"""Presentation groups only: internal setting keys and dependencies stay stable."""
GROUPS = [
    ('대화 · 말투 · 공통 규칙', ('common_prompt','dialogue_guard','relationship_guard','conversation_mode','autonomous_messages','bot_to_bot_chat')),
    ('기억 · 관계 · 대화 범위', ('long_term_memory','relationships','personal_rp','dm','offline_mention_recovery')),
    ('월드 · 장소 · 이동', ('world_simulation','place_movement','seating','away_system','appointments','dynamic_events','city_events')),
    ('수면 · 기상 · 복장', ('sleep_system','actual_sleep','sleep_quality','wake_calls','weekend_rest','naps','morning_routine','outfit_preferences','outfit_schedule','outfit_condition')),
    ('식사 · 조리 · 정리', ('meal_stages','food_stock','ingredient_stock','dishwashing')),
    ('활동 · 사용자 행동 · 물건', ('activity_stages','user_world_actions','object_states','group_leisure','world_requests','lost_found','item_loans')),
    ('날짜 · 일정 · 기념일', ('world_calendar','seasonal_weather','weekly_schedules','opening_hours','anniversaries')),
    ('상태 표시 · 운영', ('status_dashboard','presence','inner_thoughts','hourly_diagnostics','life_statistics')),
]


def grouped_features(features):
    remaining={key:(key,label,default) for key,label,default in features}
    result=[]
    for title,keys in GROUPS:
        items=[remaining.pop(key) for key in keys if key in remaining]
        if items:result.append((title,items))
    if remaining:result.append(('기타 기능',list(remaining.values())))
    return result
