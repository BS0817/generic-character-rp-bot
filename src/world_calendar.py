"""Optional Gregorian world calendar, schedules, opening hours and known events."""
import json
import random
import math
import threading
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from rp_policy import enabled
from settings_store import atomic_write

WEEKDAYS=('월','화','수','목','금','토','일')
SEASONS={
 '봄':{'months':[3,4,5],'temperature_min':8,'temperature_max':22,'weather_weights':{'맑음':40,'흐림':30,'비':25,'바람이 강함':5}},
 '여름':{'months':[6,7,8],'temperature_min':22,'temperature_max':32,'weather_weights':{'맑음':45,'흐림':20,'비':30,'바람이 강함':5}},
 '가을':{'months':[9,10,11],'temperature_min':8,'temperature_max':23,'weather_weights':{'맑음':45,'흐림':25,'비':15,'바람이 강함':15}},
 '겨울':{'months':[12,1,2],'temperature_min':-5,'temperature_max':8,'weather_weights':{'맑음':35,'흐림':35,'눈':20,'바람이 강함':10}},
}
CALENDAR_DEFAULT={'mode':'real','start_date':'2026-01-01','start_time':'09:00',
 'show_date':True,'show_weekday':True,'show_season':True,'season_profiles':SEASONS}
EVENT_DEFAULT={'name':'새 기념일','kind':'annual','date':'2000-01-01','offset_days':100,
 'leap_day':'feb28','participants':[],'known_by':[],'public':False,'note':''}
HOURS_DEFAULT={'enabled':False,'days':list(WEEKDAYS),'opens':'09:00','closes':'18:00'}

def parse_date(value,label='날짜'):
    try: return date.fromisoformat(str(value))
    except (ValueError,TypeError): raise ValueError(f'{label}: YYYY-MM-DD 형식의 실제 날짜를 입력하세요. 예: 2026-10-07') from None

def minute(value,label='시간'):
    try:
        parts=str(value).split(':')
        if len(parts)!=2 or any(len(x)!=2 or not x.isdigit() for x in parts): raise ValueError()
        h,m=map(int,parts)
        if not 0<=h<=23 or not 0<=m<=59:raise ValueError()
        return h*60+m
    except (ValueError,TypeError):raise ValueError(f'{label}: 24시간 형식 HH:MM으로 입력하세요. 예: 09:00') from None

def days_list(values):
    if not isinstance(values,list):raise ValueError('요일은 ["월", "화"]처럼 목록으로 입력하세요.')
    result=[]
    for value in values:
        if isinstance(value,str) and value in WEEKDAYS:result.append(WEEKDAYS.index(value))
        elif isinstance(value,int) and not isinstance(value,bool) and 0<=value<=6:result.append(value)
        else:raise ValueError('요일은 월·화·수·목·금·토·일 또는 0~6으로 입력하세요.')
    return result

def in_slot(now,days,start,end):
    days=days_list(days);start=minute(start);end=minute(end);current=now.hour*60+now.minute
    if start==end:return now.weekday() in days
    if start<end:return now.weekday() in days and start<=current<end
    return (now.weekday() in days and current>=start) or ((now.weekday()-1)%7 in days and current<end)

class WorldClock:
    def __init__(self,root):
        self.path=Path(root)/'world_clock.json';self.lock=threading.RLock()
        try:self.state=json.loads(self.path.read_text(encoding='utf-8'))
        except (OSError,ValueError):self.state={}
        if not isinstance(self.state,dict):self.state={}
    def now(self,settings,real_now=None):
        real=real_now or datetime.now(timezone.utc)
        if real.tzinfo is None:real=real.replace(tzinfo=timezone.utc)
        local=real.astimezone(ZoneInfo(settings.get('timezone','Asia/Seoul')))
        calendar=settings.get('calendar',{})
        if not enabled(settings,'world_calendar') or calendar.get('mode','real')=='real':return local
        fingerprint=[calendar.get('start_date',CALENDAR_DEFAULT['start_date']),calendar.get('start_time','09:00'),settings.get('timezone','Asia/Seoul')]
        with self.lock:
            try:
                anchor=datetime.fromisoformat(self.state['world_anchor'])
                valid=anchor.tzinfo is not None and isinstance(self.state['real_anchor'],(int,float)) and math.isfinite(self.state['real_anchor'])
            except (KeyError,TypeError,ValueError):valid=False
            if self.state.get('fingerprint')!=fingerprint or not valid:
                start=parse_date(fingerprint[0]);mins=minute(fingerprint[1])
                initial=datetime(start.year,start.month,start.day,mins//60,mins%60,tzinfo=local.tzinfo)
                self.state={'fingerprint':fingerprint,'world_anchor':initial.isoformat(),'real_anchor':real.timestamp()}
                atomic_write(self.path,json.dumps(self.state,ensure_ascii=False))
            anchor=datetime.fromisoformat(self.state['world_anchor'])
            try:return (anchor.astimezone(timezone.utc)+timedelta(seconds=real.timestamp()-self.state['real_anchor'])).astimezone(local.tzinfo)
            except (OverflowError,ValueError):raise ValueError('가상 세계 시간이 지원 날짜 범위를 벗어났습니다. 시작 날짜를 다시 지정하세요.') from None

def season_profile(settings,now):
    profiles=settings.get('calendar',{}).get('season_profiles',SEASONS)
    for name,profile in profiles.items():
        if now.month in profile.get('months',[]):return name,profile
    return '미지정',{}

def calendar_display(settings,now):
    if not enabled(settings,'world_calendar'):return {}
    calendar=settings.get('calendar',{})
    result={}
    if calendar.get('show_date',True):result['날짜']=now.date().isoformat()
    if calendar.get('show_weekday',True):result['요일']=WEEKDAYS[now.weekday()]+'요일'
    if calendar.get('show_season',True):result['계절']=season_profile(settings,now)[0]
    return result

def seasonal_weather(settings,now,rng=None):
    rng=rng or random
    _,profile=season_profile(settings,now)
    choices=profile.get('weather_weights',{'맑음':1})
    weather=rng.choices(list(choices),weights=list(choices.values()),k=1)[0]
    return weather,rng.randint(profile.get('temperature_min',10),profile.get('temperature_max',20))

def place_open(settings,definition,now):
    hours=definition.get('opening_hours',{})
    if not enabled(settings,'opening_hours') or not hours.get('enabled',False):return True
    return in_slot(now,hours.get('days',list(WEEKDAYS)),hours.get('opens','09:00'),hours.get('closes','18:00'))

def weekly_entry(settings,config,now,place=None):
    if not enabled(settings,'weekly_schedules'):return None
    for entry in config.get('weekly_schedule',[]):
        if in_slot(now,entry.get('days',list(WEEKDAYS)),entry.get('start','00:00'),entry.get('end','00:00')) and (place is None or not entry.get('place') or entry.get('place')==place):return entry
    return None

def scheduled_destination(settings,config,now,current,accessible,busy=False,hungry=False):
    """Visit the active schedule only when ordinary life and entry conditions allow it."""
    if busy or hungry or not enabled(settings,'place_movement'):return None
    entry=weekly_entry(settings,config,now)
    target=entry.get('place') if entry else None
    if target and target!=current and config.get('place_weights',{}).get(target,1)>0 and accessible(target):return target
    return None

def event_date(event,year):
    original=parse_date(event.get('date','2000-01-01'),'기념일 날짜')
    kind=event.get('kind','annual')
    if kind=='milestone':return original+timedelta(days=int(event.get('offset_days',100))-1)
    if kind=='once':return original
    if year<original.year:return None
    try:return original.replace(year=year)
    except ValueError:
        policy=event.get('leap_day','feb28')
        if policy=='leap_only':return None
        return date(year,3,1) if policy=='mar1' else date(year,2,28)

def visible_events(settings,now,character=None,public_only=False,window=0):
    if not enabled(settings,'anniversaries'):return []
    result=[]
    for key,event in settings.get('calendar_events',{}).items():
        if key.startswith('_'):continue
        if public_only and not event.get('public',False):continue
        if character is not None and not event.get('public',False) and character not in event.get('known_by',[]):continue
        targets=[event_date(event,now.year)]
        if event.get('kind','annual')=='annual':targets.append(event_date(event,now.year+1)) if now.year<9999 else None
        upcoming=[d for d in targets if d and 0<=(d-now.date()).days<=window]
        if upcoming:
            result.append({'key':key,'name':event['name'],'date':min(upcoming).isoformat(),'note':event.get('note',''),'participants':event.get('participants',[])})
    return sorted(result,key=lambda e:(e['date'],e['key']))

def world_context(settings,now,config=None,character=None):
    if not enabled(settings,'world_calendar'):return ''
    lines=['[세계 날짜·일정]',f'{now.date().isoformat()} {now:%H:%M} / {WEEKDAYS[now.weekday()]}요일 / {season_profile(settings,now)[0]}']
    entry=weekly_entry(settings,config or {},now)
    if entry:lines.append('현재 예정된 요일 일정: '+str(entry.get('activity',''))+' / 장소: '+str(entry.get('place') or '자유')+' (계획이며 현재 실제 활동·장소와 다를 수 있음; 수면·식사·약속·진행 중 작업을 우선)')
    for event in visible_events(settings,now,character=character,public_only=character is None,window=7):
        lines.append(f"알고 있는 기념일: {event['date']} {event['name']} / {event['note']}")
    lines.append('기념일은 알고 있는 정보만 활용한다. 관계에 맞게 반응하되 매 발언마다 축하를 반복하지 않는다. 선물이나 약속은 실제 소지품·상대 의사·장소 조건을 따르며, 계획을 이미 완료한 행동으로 말하지 않는다.')
    return '\n'.join(lines)

def validate_calendar(settings,characters=None,places=None):
    characters=characters or {};places=places or {}
    raw_calendar=settings.get('calendar',{})
    if not isinstance(raw_calendar,dict):raise ValueError('세계 달력 설정은 객체로 입력하세요.')
    calendar={**CALENDAR_DEFAULT,**raw_calendar}
    if calendar['mode'] not in ('real','virtual'):raise ValueError('세계 시간 방식은 real(현실) 또는 virtual(가상)이어야 합니다.')
    initial=parse_date(calendar['start_date'],'세계 시작 날짜');minute(calendar['start_time'],'세계 시작 시각')
    if not 2<=initial.year<=9998:raise ValueError('세계 시작 날짜는 0002년~9998년으로 지정하세요.')
    for key in ('show_date','show_weekday','show_season'):
        if not isinstance(calendar[key],bool):raise ValueError('날짜·요일·계절 표시 여부는 true 또는 false로 입력하세요.')
    profiles=calendar['season_profiles']
    if not isinstance(profiles,dict) or not profiles:raise ValueError('계절 설정은 이름별 객체로 입력하세요.')
    months=[]
    for name,profile in profiles.items():
        if not isinstance(profile,dict) or not str(name).strip():raise ValueError('계절 이름과 내용을 확인하세요.')
        values=profile.get('months',[])
        if not isinstance(values,list) or any(isinstance(m,bool) or not isinstance(m,int) or not 1<=m<=12 for m in values):raise ValueError(f'{name}: 계절 월은 1~12의 정수 목록입니다.')
        months.extend(values)
        low=profile.get('temperature_min');high=profile.get('temperature_max')
        if any(isinstance(x,bool) or not isinstance(x,int) or not -100<=x<=100 for x in (low,high)) or low>high:raise ValueError(f'{name}: 기온은 -100~100 정수이며 최저가 최고보다 높을 수 없습니다.')
        weather=profile.get('weather_weights',{})
        if not isinstance(weather,dict) or not weather or any(not str(k).strip() or isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for k,v in weather.items()) or not math.isfinite(sum(weather.values())) or sum(weather.values())<=0:raise ValueError(f'{name}: 날씨 가중치는 0 이상이며 합계가 0보다 커야 합니다.')
    if sorted(months)!=list(range(1,13)):raise ValueError('계절 설정에는 1~12월을 중복 없이 한 번씩 포함하세요.')
    events=settings.get('calendar_events',{})
    if not isinstance(events,dict):raise ValueError('기념일은 이름별 객체로 입력하세요.')
    for key,event in events.items():
        if key.startswith('_'):continue
        if not isinstance(event,dict) or not str(event.get('name','')).strip():raise ValueError('기념일 이름을 입력하세요.')
        if event.get('kind','annual') not in ('annual','milestone','once'):raise ValueError('기념일 종류는 annual(매년), milestone(시작일 기준), once(한 번)입니다.')
        parse_date(event.get('date',''),'기념일 날짜')
        offset=event.get('offset_days',100)
        if isinstance(offset,bool) or not isinstance(offset,int) or not 1<=offset<=365000:raise ValueError('기념일 일수는 1~365000 정수입니다. 시작일을 1일째로 셉니다.')
        if event.get('leap_day','feb28') not in ('feb28','mar1','leap_only'):raise ValueError('윤년 기념일 처리 방식을 확인하세요.')
        if not isinstance(event.get('public',False),bool):raise ValueError('기념일 공개 여부는 true 또는 false로 입력하세요.')
        for field in ('known_by','participants'):
            if not isinstance(event.get(field,[]),list) or any(not isinstance(x,str) for x in event.get(field,[])):raise ValueError('기념일 참여자와 아는 캐릭터는 목록으로 입력하세요.')
        if any(x not in characters for x in event.get('known_by',[])):raise ValueError(f'{event["name"]}: 아는 캐릭터에 등록되지 않은 캐릭터 키가 있습니다.')
        if event.get('kind')=='milestone':
            try:event_date(event,2000)
            except OverflowError:raise ValueError('기념일 계산 결과가 지원 날짜 범위를 벗어납니다.') from None
    for key,config in characters.items():
        if key.startswith('_'):continue
        entries=config.get('weekly_schedule',[])
        if not isinstance(entries,list):raise ValueError(f'{key}: 요일별 일정은 목록으로 입력하세요.')
        for entry in entries:
            if not isinstance(entry,dict):raise ValueError(f'{key}: 일정은 객체로 입력하세요.')
            days_list(entry.get('days',list(WEEKDAYS)));minute(entry.get('start','00:00'));minute(entry.get('end','00:00'))
            if entry.get('place') and entry['place'] not in places:raise ValueError(f'{key}: 일정 장소가 없습니다: {entry["place"]}')
            if not str(entry.get('activity','')).strip():raise ValueError(f'{key}: 일정 활동을 입력하세요.')
    for key,definition in places.items():
        if key.startswith('_'):continue
        hours=definition.get('opening_hours',{})
        if not isinstance(hours,dict):raise ValueError(f'{key}: 영업시간은 객체로 입력하세요.')
        if not isinstance(hours.get('enabled',False),bool):raise ValueError(f'{key}: 영업시간 적용 여부는 true 또는 false로 입력하세요.')
        days_list(hours.get('days',list(WEEKDAYS)));minute(hours.get('opens','09:00'));minute(hours.get('closes','18:00'))
