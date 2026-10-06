"""Optional deterministic life stages. No AI calls; timestamps supplied by caller."""
import json
import random
from pathlib import Path
from settings_store import atomic_write
from rp_policy import enabled

class LifeEngine:
    def __init__(self, root, emit=lambda *args: None, rng=None):
        self.path = Path(root)/'life_extensions.json'
        self.emit = emit
        self.rng = rng or random.Random()
        self.definitions = {}
        try:
            value = json.loads(self.path.read_text(encoding='utf-8'))
            self.records = value.get('characters', {})
            self.loans = value.get('loans', {})
        except (OSError, ValueError, AttributeError):
            self.records, self.loans = {}, {}

    def save(self):
        atomic_write(self.path, json.dumps({'characters':self.records,'loans':self.loans},ensure_ascii=False))

    def record(self, key): return self.records.setdefault(key,{})

    def seat(self,key,place,definition,settings):
        r=self.record(key)
        if not enabled(settings,'seating'):
            r.pop('seat',None); return ''
        groups=definition.get('seats',{})
        if r.get('seat') in groups:
            occupants=[k for k,v in self.records.items() if v.get('place')==place and v.get('seat')==r['seat']]
            if key in occupants[:int(groups[r['seat']])]: return r['seat']
        r.pop('seat',None)
        for name,capacity in groups.items():
            used=sum(x.get('place')==place and x.get('seat')==name for x in self.records.values())
            if used<int(capacity): r['seat']=name; return name
        return ''

    def tick(self,key,place,definition,state,activity,settings,now,hour,weather='',busy=False,scheduled_sleep=False,scheduled_activity=''):
        self.definitions[place]=definition
        r=self.record(key); elapsed=max(0,min(10,(now-r.get('tick',now))/60)); r['tick']=now
        if r.get('place')!=place:
            for field in ('seat','meal','task'): r.pop(field,None)
            r['place']=place
        if state.get('away') or (state.get('sleeping') and not r.get('nap')):
            for field in ('seat','meal','task'): r.pop(field,None)
            return activity
        self.seat(key,place,definition,settings)
        if not enabled(settings,'naps'):
            if r.pop('nap',None): state['sleeping']=False
        nap=r.get('nap')
        if nap:
            if scheduled_sleep or busy or now>=nap['end'] or not state.get('sleeping'):
                minutes=max(0,min(30,(now-nap['start'])/60))
                state['fatigue']=max(0,state['fatigue']-min(20,minutes*2/3))
                state['sleeping']=False; r.pop('nap',None)
                self.emit('생활',key,'낮잠 종료·스트레칭')
                return '낮잠 후 스트레칭 중'
            return '낮잠 자는 중'
        if enabled(settings,'outfit_condition'):
            if state.get('outfit_set') and definition.get('outdoor') and any(w in weather for w in ('비','눈')): r['outfit_condition']='젖음'
            elif r.get('outfit_condition')=='젖음':
                r.setdefault('dry_at',now+1800)
                if now>=r['dry_at']: r['outfit_condition']='건조됨'; r.pop('dry_at',None)
        else: r.pop('outfit_condition',None); r.pop('dry_at',None)
        meal=r.get('meal')
        if not enabled(settings,'meal_stages'):
            r.pop('meal',None); r.pop('finished_meal_at',None); meal=None
        if meal:
            phase=int((now-meal['start'])//300)
            if phase>=3:
                r.pop('meal',None); r['last_meal']=now; r['last_menu']=meal['menu']
                r['finished_meal_at']=now
                self.emit('생활',key,f"식사 완료: {meal['menu']}")
                return '식사 후 주변을 살피는 중'
            if phase==1:
                state['hunger']=max(0,state['hunger']-7*elapsed)
            meal['phase']=('준비','섭취','정리')[phase]
            return f"{meal['label']} {meal['menu']} {meal['phase']} 중"
        if enabled(settings,'meal_stages') and not busy and not r.get('task') and state.get('hunger',0)>=55 and now-r.get('last_meal',0)>3600:
            label='아침' if 5<=hour<=10 else '점심' if 11<=hour<=16 else '저녁' if 17<=hour<=22 else '야식'
            menus=definition.get('menus',{})
            choices=menus.get(label,menus.get('전체',[])) if isinstance(menus,dict) else menus
            if choices:
                choices=[m for m in choices if m!=r.get('last_menu')] or choices
                r['meal']={'menu':self.rng.choice(choices),'label':label,'start':now,'phase':'준비'}
                self.emit('생활',key,f"식사 시작: {r['meal']['menu']}")
                return f"{label} {r['meal']['menu']} 준비 중"
        if enabled(settings,'naps') and not busy and not scheduled_sleep and 8<=hour<20 and definition.get('nap_allowed') and r.get('seat') and not r.get('meal') and not r.get('task'):
            outdoor=definition.get('outdoor',False)
            suitable=not outdoor or (not any(w in weather for w in ('비','눈','폭풍')) and 10<=state.get('temperature',20)<=28)
            fatigue=state.get('fatigue',0); poor=enabled(settings,'sleep_quality') and r.get('sleep_quality') in ('악몽','뒤척임')
            threshold=55 if poor else 70
            if now-r.get('last_nap',0)>=28800 and now-r.get('nap_check',0)>=1800:
                r['nap_check']=now
                if suitable and fatigue>=threshold and self.rng.random()<(1 if fatigue>=85 else .6 if poor else .35):
                    length=self.rng.randint(15,30)
                    r['nap']={'start':now,'end':now+length*60}; r['last_nap']=now; state['sleeping']=True
                    self.emit('생활',key,f'낮잠 시작: {length}분'); return '낮잠 자는 중'
                self.emit('판단',key,'낮잠 보류: 피로·날씨 조건 또는 시도 확률')
        task=r.get('task')
        if not enabled(settings,'activity_stages'): r.pop('task',None); task=None
        if task:
            phase=int((now-task['start'])//600)
            if phase>=3:
                r.pop('task',None); r['last_task']=now; return f"{task['name']} 마친 뒤 쉬는 중"
            if phase==1 and any(w in task['name'] for w in ('훈련','운동')): state['fatigue']=min(100,state['fatigue']+elapsed*.4)
            return f"{task['name']} {('준비','진행','마무리')[phase]} 중"
        if scheduled_activity and not busy and not scheduled_sleep:
            r['scheduled_activity']=scheduled_activity
            return scheduled_activity
        previous=r.pop('scheduled_activity',None)
        if previous and activity==previous: activity='일정 후 쉬는 중'
        if enabled(settings,'activity_stages') and not busy and not r.get('finished_meal_at') and definition.get('activities') and now-r.get('last_task',0)>=3600:
            r['task']={'name':self.rng.choice(definition['activities']),'start':now}
            return f"{r['task']['name']} 준비 중"
        return activity

    def busy(self,key,settings):
        r=self.record(key)
        for field,flag,label in [('meal','meal_stages','식사 진행 중'),('task','activity_stages','활동 진행 중'),('nap','naps','낮잠 중')]:
            if enabled(settings,flag) and r.get(field): return label
        return ''

    def wake(self,key,state,settings):
        if not enabled(settings,'sleep_quality'): return
        r=self.record(key); quality=self.rng.choices(['정상','꿈','악몽','뒤척임'],[70,15,7,8])[0]
        r['sleep_quality']=quality
        state['fatigue']=min(100,10+10*state.get('sleep_interruptions',0)+(20 if quality in ('악몽','뒤척임') else 0))
        self.emit('생활',key,f'기상·수면 품질: {quality}')

    def transfer(self,item,sender,recipient,borrowed=False):
        identity=f'{sender}:{item}'
        returned=next((k for k,v in self.loans.items() if v['item']==item and v['borrower']==sender and v['owner']==recipient),None)
        if returned:
            del self.loans[returned]; self.emit('물건',sender,f'{item} 반환 → {recipient}')
        elif borrowed:
            self.loans[identity]={'item':item,'owner':sender,'borrower':recipient}
            self.emit('물건',sender,f'{item} 대여 → {recipient}')
        self.save()

    def context(self,key,settings):
        r=self.record(key); lines=[]
        for field,flag,label in [('seat','seating','좌석'),('meal','meal_stages','식사'),('task','activity_stages','진행 활동'),('nap','naps','낮잠'),('sleep_quality','sleep_quality','최근 수면 품질'),('outfit_condition','outfit_condition','복장 상태')]:
            if enabled(settings,flag) and r.get(field): lines.append(f'{label}: {r[field]}')
        if enabled(settings,'item_loans'):
            lines.extend(f"대여 물건: {v['item']} / 소유자 {v['owner']} / 빌린 사람 {v['borrower']}" for v in self.loans.values() if key in (v['owner'],v['borrower']))
        if enabled(settings,'seating'):
            place=r.get('place'); groups=self.definitions.get(place,{}).get('seats',{})
            for seat,capacity in groups.items():
                occupants=[k for k,v in self.records.items() if v.get('place')==place and v.get('seat')==seat]
                lines.append(f"{seat}: 동석자 {', '.join(occupants) or '없음'} / 빈자리 {max(0,int(capacity)-len(occupants))}")
        return '\n'.join(lines)
