"""Required feature combinations shared by GUI, CLI and runtime startup."""
from rp_policy import FEATURES, enabled

DEPENDENCIES = {
    'place_movement': ('world_simulation',),
    'sleep_system': ('world_simulation',),
    'away_system': ('world_simulation',),
    'appointments': ('world_simulation',),
    'dynamic_events': ('world_simulation',),
    'city_events': ('world_simulation',),
    'seating': ('world_simulation',),
    'meal_stages': ('world_simulation',),
    'naps': ('world_simulation', 'seating'),
    'sleep_quality': ('sleep_system',),
    'activity_stages': ('world_simulation',),
    'outfit_condition': ('world_simulation',),
    'seasonal_weather': ('world_calendar',),
    'weekly_schedules': ('world_calendar', 'world_simulation'),
    'opening_hours': ('world_calendar',),
    'anniversaries': ('world_calendar',),
}
LABELS = dict((key,label) for key,label,_ in FEATURES)
LABELS.update(world_simulation='본서버 월드 시뮬레이션',place_movement='캐릭터 장소 자동 이동',sleep_system='수면 시스템',away_system='외출 시스템',appointments='약속 시스템',dynamic_events='캐릭터 다이나믹 이벤트',city_events='도시/세계 이벤트')

def required_features(key):
    result=set()
    for parent in DEPENDENCIES.get(key,()):
        result.add(parent); result.update(required_features(parent))
    return result

def dependency_errors(settings):
    return [(key,parent) for key,parents in DEPENDENCIES.items() if enabled(settings,key)
            for parent in parents if not enabled(settings,parent)]

def validate_features(settings):
    if not isinstance(settings,dict) or not isinstance(settings.get('features',{}),dict):
        raise ValueError('기능 설정은 이름과 true/false 값을 가진 객체로 입력하세요.')
    errors=dependency_errors(settings)
    if errors:
        lines=[f'• {LABELS.get(key,key)} → {LABELS.get(parent,parent)} 필요' for key,parent in errors]
        raise ValueError('함께 켜야 하는 기능이 꺼져 있습니다.\n'+'\n'.join(lines)+'\n세계관 · 기능에서 관련 설정을 함께 켜거나 사용하는 기능을 끄세요.')

def toggle_changes(settings,key,on):
    """Return all required changes without mutating caller state."""
    result={key:on}
    if on:
        result.update({p:True for p in required_features(key) if not enabled(settings,p)})
    else:
        result.update({child:False for child in DEPENDENCIES if key in required_features(child) and enabled(settings,child)})
    return result
