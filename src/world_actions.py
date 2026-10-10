"""Transactional, opt-in world effects. No model calls and no Discord dependency.

The message ID is the idempotency key. Effects and their audit record commit together.
Ambiguous narration is left unchanged; registered aliases and completed actions only.
"""
import csv
import copy
import hashlib
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from rp_policy import enabled

FOOD_DEFAULT = {'aliases': [], 'unit': '인분', 'hunger': 35, 'expires_minutes': 180,
                'dishes': 1, 'ingredients': {}}
OBJECT_DEFAULT = {'aliases': [], 'properties': {}, 'actions': {}, 'manager': '',
                  'auto_manage': False, 'outdoor': False, 'drift': {}, 'limits': {}, 'manage_below': {}, 'manage_action': ''}
LEISURE_DEFAULT = {'aliases': [], 'capacity': 2, 'minutes': 30, 'seat': ''}


def compact(text):
    return re.sub(r'\s+', '', str(text)).casefold()


def completed(text):
    # Questions, quoted dialogue, negation, hypotheticals, plans and historical reports
    # are never evidence of a present, completed physical effect.
    if re.search(r'(?:^|[\s(])(?:안|못)\s+(?:준비|만들|조리|받|먹|마시|열|닫|켜|끄|설거지|참여|진행|완료|돌려|반환|건)',text):return False
    if any(c in text for c in '?？"“”‘’') or re.search(
        r'안\s*했|않|못했|않고|말고|취소|어제|지난|예전에|했다고|했었|한다면|하면|할까|줄까|할게|줄게|하려|할\s*예정|려고|하고\s*싶|할\s*수', text):
        return False
    return bool(re.search(r'했다|했어|하였다|한다|합니다|놓았다|놓았어|놓는다|두었다|뒀어|둔다|건넸다|건넸어|건넨다|받았다|받았어|받는다|먹었다|먹었어|먹는다|마셨다|마셨어|마신다|주었다|줬다|덮었다|닦았다|열었다|열었어|연다|닫았다|닫았어|닫는다|켰다|켰어|켠다|껐다|껐어|끈다|참여한다|참여했다|구경한다|구경했다|수락한다|거절한다|진행한다|완료했다|돌려준다|돌려주었다|반환했다', text))


def quantity(text, default=1):
    match = re.search(r'([+-]?\d+(?:\.\d+)?|한|하나|두|둘|세|셋|네|넷|다섯|여섯|일곱|여덟|아홉|열)\s*(?:인분|개|잔|세트|명)', text)
    if not match:
        return default
    words = {'한':1,'하나':1,'두':2,'둘':2,'세':3,'셋':3,'네':4,'넷':4,'다섯':5,'여섯':6,'일곱':7,'여덟':8,'아홉':9,'열':10}
    if match[1] in words:return words[match[1]]
    return int(match[1]) if '.' not in match[1] else 0


def matched_names(text, definitions):
    matches=[]
    for name,definition in definitions.items():
        lengths=[]
        for alias in [name]+definition.get('aliases',[]):
            value=compact(alias)
            if not value:continue
            pattern=r'(?<![가-힣A-Za-z0-9])'+r'\s*'.join(re.escape(c) for c in value)+r'(?=$|[^가-힣A-Za-z0-9]|[0-9]|을|를|은|는|이|가|도|에|와|과)'
            if re.search(pattern,text,re.I):lengths.append(len(value))
        if lengths:matches.append((max(lengths),name))
    if not matches:return []
    matches.sort(reverse=True)
    if len(matches)>1 and matches[0][0]==matches[1][0]:return []
    return [matches[0][1]]


def validate_extensions(characters, places):
    for key, definition in characters.items():
        if key.startswith('_'): continue
        if not isinstance(definition, dict): continue
        for field in ('required_sleep_hours', 'sleep_recovery_per_hour'):
            value=definition.get(field, 8 if field=='required_sleep_hours' else 7)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not 0 < value <= 24:
                raise ValueError(f'{key}: {field}는 0보다 크고 24 이하인 숫자로 입력하세요.')
        for field,default,lo,hi in [('weekend_extra_sleep_hours',1,0,4),('wake_acceptance',.65,0,1)]:
            v=definition.get(field,default)
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not lo<=v<=hi:raise ValueError(f'{key}: {field}는 {lo}~{hi} 숫자로 입력하세요.')
        if definition.get('grooming_place') and definition['grooming_place'] not in places:raise ValueError(f'{key}: 몸단장 장소를 등록된 장소에서 지정하세요.')
        for field in ('sleep_outfit','day_outfits'):
            value=definition.get(field,'' if field=='sleep_outfit' else [])
            if field=='day_outfits' and (not isinstance(value,list) or not all(isinstance(v,dict) and isinstance(v.get('outfit'),str) and isinstance(v.get('weather',[]),list) and all(isinstance(w,str) for w in v.get('weather',[])) for v in value)):
                raise ValueError(f'{key}: 낮 복장은 outfit과 weather 목록을 가진 JSON 목록으로 입력하세요.')
    for place, definition in places.items():
        if place.startswith('_') or not isinstance(definition,dict): continue
        capacity=definition.get('routine_capacity',0)
        if isinstance(capacity,bool) or not isinstance(capacity,int) or not 0<=capacity<=100:raise ValueError(f'{place}: 몸단장 동시 이용 정원은 0~100 정수로 입력하세요.')
        for field in ('food_definitions','object_states','leisure'):
            entries=definition.get(field,{})
            if not isinstance(entries,dict): raise ValueError(f'{place}: {field}는 JSON 객체로 입력하세요.')
            seen={}
            for name, data in entries.items():
                if not name or not isinstance(data,dict): raise ValueError(f'{place}: {field}의 각 항목은 이름과 설정 객체가 필요합니다.')
                aliases=data.get('aliases',[])
                if not isinstance(aliases,list) or not all(isinstance(a,str) and a.strip() for a in aliases): raise ValueError(f'{place}/{name}: 별칭은 문자열 목록으로 입력하세요.')
                for alias in [name]+aliases:
                    normalized=compact(alias)
                    if normalized in seen and seen[normalized]!=name: raise ValueError(f'{place}: 별칭 {alias}가 여러 항목에 사용됩니다.')
                    seen[normalized]=name
                if field=='food_definitions':
                    for attr,default,limit in [('hunger',35,100),('expires_minutes',180,525600),('dishes',1,100)]:
                        v=data.get(attr,default)
                        if isinstance(v,bool) or not isinstance(v,int) or not 0<=v<=limit: raise ValueError(f'{place}/{name}: {attr}는 0~{limit} 정수로 입력하세요.')
                    recipe=data.get('ingredients',{})
                    if not isinstance(recipe,dict) or not all(isinstance(n,int) and not isinstance(n,bool) and n>0 for n in recipe.values()): raise ValueError(f'{place}/{name}: 식재료 수량은 양의 정수로 입력하세요.')
                if field=='object_states':
                    props=data.get('properties',{}); actions=data.get('actions',{})
                    if not isinstance(props,dict) or not isinstance(actions,dict): raise ValueError(f'{place}/{name}: 속성과 행동은 JSON 객체로 입력하세요.')
                    if not all(isinstance(v,dict) and set(v)<=set(props) for v in actions.values()): raise ValueError(f'{place}/{name}: 행동에는 등록된 속성만 지정하세요.')
                    if not all(isinstance(v,(str,int,float,bool)) for v in props.values()): raise ValueError(f'{place}/{name}: 속성은 문자·숫자·true/false로 입력하세요.')
                    for field in ('drift','manage_below'):
                        values=data.get(field,{})
                        if not isinstance(values,dict) or any(k not in props or isinstance(v,bool) or not isinstance(v,(int,float)) or isinstance(props[k],bool) or not isinstance(props[k],(int,float)) for k,v in values.items()): raise ValueError(f'{place}/{name}: {field}에는 등록된 숫자 속성과 변화량·기준값만 입력하세요.')
                    limits=data.get('limits',{})
                    if not isinstance(limits,dict) or any(k not in props or not isinstance(v,list) or len(v)!=2 or not all(isinstance(n,(int,float)) and not isinstance(n,bool) for n in v) or v[0]>v[1] for k,v in limits.items()): raise ValueError(f'{place}/{name}: 속성 제한은 [최소, 최대] 숫자로 입력하세요.')
                    if data.get('manage_action') and data['manage_action'] not in actions: raise ValueError(f'{place}/{name}: 관리 행동은 actions에 등록된 행동 이름을 사용하세요.')
                if field=='leisure':
                    for attr,default,lo,hi in [('capacity',2,2,100),('minutes',30,30,120)]:
                        v=data.get(attr,default)
                        if isinstance(v,bool) or not isinstance(v,int) or not lo<=v<=hi: raise ValueError(f'{place}/{name}: {attr}는 {lo}~{hi} 정수로 입력하세요.')
                    seat=data.get('seat','')
                    if seat and (seat not in definition.get('seats',{}) or definition['seats'][seat]<2): raise ValueError(f'{place}/{name}: 공동 활동에는 정원 2명 이상의 좌석을 지정하세요.')


class WorldActions:
    def __init__(self, root, scope, clock=time.time, recover=True):
        self.path=Path(root)/'world_actions.db'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.scope=str(scope)
        self.clock=clock
        self.life=None
        with self.transaction() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS effects(scope TEXT, event_id TEXT, actor TEXT, place TEXT, created REAL, outcome TEXT, PRIMARY KEY(scope,event_id));
                CREATE TABLE IF NOT EXISTS state(scope TEXT, kind TEXT, key TEXT, data TEXT, PRIMARY KEY(scope,kind,key));
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, scope TEXT, created REAL, actor TEXT, place TEXT, kind TEXT, content TEXT);
                CREATE TABLE IF NOT EXISTS samples(scope TEXT, minute INTEGER, actor TEXT, place TEXT, hunger REAL, fatigue REAL, sleeping INTEGER, nap INTEGER, away INTEGER, connected INTEGER, PRIMARY KEY(scope,minute,actor));
                CREATE TABLE IF NOT EXISTS jobs(scope TEXT, id TEXT, kind TEXT, actor TEXT, place TEXT, priority INTEGER, phase TEXT, created REAL, updated REAL, payload TEXT, error TEXT, PRIMARY KEY(scope,id));
            ''')
            # A sending job may have reached Discord. Do not automatically replay it.
            if recover: db.execute("UPDATE jobs SET phase='불확정', error='재시작 전 전송 결과 확인 필요' WHERE scope=? AND phase='전송 중'",(self.scope,))
            if recover: db.execute("UPDATE jobs SET phase='대기', error='재시작: 복구된 원문과 최신 상태로 다시 생성' WHERE scope=? AND phase='생성 중'",(self.scope,))

    @contextmanager
    def transaction(self):
        db=sqlite3.connect(self.path,timeout=5)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback(); raise
        finally: db.close()

    def _get(self, db, kind, key, default=None):
        row=db.execute('SELECT data FROM state WHERE scope=? AND kind=? AND key=?',(self.scope,kind,key)).fetchone()
        return json.loads(row[0]) if row else ({} if default is None else copy.deepcopy(default))

    def _put(self, db, kind, key, value):
        db.execute('INSERT INTO state VALUES(?,?,?,?) ON CONFLICT(scope,kind,key) DO UPDATE SET data=excluded.data',(self.scope,kind,key,json.dumps(value,ensure_ascii=False)))

    def _event(self, db, actor, place, kind, content):
        db.execute('INSERT INTO events(scope,created,actor,place,kind,content) VALUES(?,?,?,?,?,?)',(self.scope,self.clock(),actor,place,kind,content))

    def states(self, kind):
        with self.transaction() as db:
            return {r['key']:json.loads(r['data']) for r in db.execute('SELECT key,data FROM state WHERE scope=? AND kind=?',(self.scope,kind))}

    def apply(self, event_id, actor, place, text, definition, settings, character=False):
        if not enabled(settings,'world_simulation') or not enabled(settings,'user_world_actions') and not character:
            return []
        if not completed(text): return []
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM effects WHERE scope=? AND event_id=?',(self.scope,str(event_id))).fetchone(): return []
            outcomes=self._apply(db,str(actor),place,text,definition,settings,character)
            db.execute('INSERT INTO effects VALUES(?,?,?,?,?,?)',(self.scope,str(event_id),str(actor),place,self.clock(),json.dumps(outcomes,ensure_ascii=False)))
            for outcome in outcomes: self._event(db,str(actor),place,'ユーザー行動' if False else '生活行動',outcome)
            return outcomes

    def _apply(self, db, actor, place, text, definition, settings, character):
        output=[]; now=self.clock()
        foods=definition.get('food_definitions',{})
        names=matched_names(text,foods)
        if enabled(settings,'food_stock') and names:
            name=names[0]; food=dict(FOOD_DEFAULT,**foods[name]); n=quantity(text)
            if not 0<n<=100: return ['음식 수량은 1~100으로 입력하세요.']
            stock=self._get(db,'food',place,[])
            stock=[s for s in stock if s['expires']>now and s['remaining']>0]
            if re.search(r'준비|만들|만든|조리|놓|놓았|두었|뒀',text):
                if re.search(r'조리|만들|만든',text) and food['ingredients'] and enabled(settings,'ingredient_stock'):
                    ingredients=self._get(db,'ingredients',place,{})
                    if any(ingredients.get(k,0)<v*n for k,v in food['ingredients'].items()): return ['식재료 부족: 조리 미반영']
                    for k,v in food['ingredients'].items(): ingredients[k]-=v*n
                    self._put(db,'ingredients',place,ingredients)
                stock.append(dict(id=f'{actor}:{now}:{len(stock)}',name=name,remaining=n,source=actor,created=now,expires=now+food['expires_minutes']*60,unit=food['unit'],hunger=food['hunger'],dishes=food['dishes']))
                output.append(f'{name} {n}{food["unit"]} 준비 반영')
            elif re.search(r'받았|받는|가져|챙겼|챙긴|먹었|먹는|마셨|마신',text):
                remaining=n; portions=[]
                for s in stock:
                    if s['name']!=name: continue
                    take=min(remaining,s['remaining']); remaining-=take
                    if take: portions.append(dict(s,remaining=take))
                    if not remaining: break
                if remaining: return ['음식 재고 부족: 수령·섭취 미반영']
                remaining=n
                for s in stock:
                    if s['name']!=name: continue
                    take=min(remaining,s['remaining']); s['remaining']-=take; remaining-=take
                    if not remaining: break
                carried=self._get(db,'carried_food',actor,[]); carried.extend(portions)
                self._put(db,'carried_food',actor,carried)
                output.append(f'{name} {n}{food["unit"]} 수령 반영 (섭취는 별도 기록)')
            self._put(db,'food',place,stock)
        if enabled(settings,'object_states'):
            objects=definition.get('object_states',{})
            for name in matched_names(text,objects):
                spec=objects[name]; current=self._get(db,'object',f'{place}:{name}',spec.get('properties',{}))
                matches=[changes for phrase,changes in spec.get('actions',{}).items() if compact(phrase) in compact(text)]
                if len(matches)==1:
                    current.update(matches[0]); self._put(db,'object',f'{place}:{name}',current)
                    output.append(f'{name} 상태 반영: '+json.dumps(matches[0],ensure_ascii=False))
        if enabled(settings,'dishwashing') and '설거지' in text:
            sink=self._get(db,'sink',place,{'dirty':0,'reserved_by':'','until':0})
            if re.search(r'예약|맡|담당',text):
                if sink.get('until',0)<=now or sink.get('reserved_by')==actor:
                    sink.update(reserved_by=actor,until=now+1800);output.append('설거지 담당 예약: 30분')
            elif '취소' not in text:
                if sink.get('until',0)>now and sink.get('reserved_by')!=actor: return output+['설거지 보류: 다른 담당자의 예약 중']
                requested=quantity(text,sink.get('dirty',0))
                if requested<0:return output+['설거지 보류: 음수 수량은 반영하지 않습니다.']
                n=min(sink.get('dirty',0),requested);sink['dirty']-=n
                sink.update(reserved_by='',until=0);output.append(f'설거지 {n}개 완료')
            self._put(db,'sink',place,sink)
        if enabled(settings,'group_leisure'):
            leisure=definition.get('leisure',{})
            for name in matched_names(text,leisure):
                spec=leisure[name]; group=self._get(db,'leisure',f'{place}:{name}')
                if group and group.get('end',0)<=now: group={}
                if re.search(r'시작|모집|참여|함께|구경',text):
                    seat=spec.get('seat') or next((s for s,c in definition.get('seats',{}).items() if c>=2),'')
                    if not seat:return output+['여가 보류: 공동 좌석 미설정']
                    occupied=[k for k,v in (self.life.records if self.life else {}).items() if v.get('place')==place and v.get('seat')==seat]
                    capacity=definition.get('seats',{}).get(seat,0)
                    if not group: group=dict(name=name,place=place,participants=[],watchers=[],start=None,end=now+300,minutes=spec.get('minutes',30),phase='모집',capacity=min(spec.get('capacity',2),definition.get('seats',{}).get(spec.get('seat'),spec.get('capacity',2))),seat=seat)
                    if actor not in group['participants'] and actor not in group['watchers']:
                        field='watchers' if '구경' in text or len(group['participants'])>=group['capacity'] or actor not in occupied and len(occupied)>=capacity else 'participants'
                        group[field].append(actor)
                        if len(group['participants'])>=2 and group.get('start') is None:group.update(start=now,end=now+group['minutes']*60,phase='진행')
                        output.append(f'{name}: '+('구경 등록' if field=='watchers' else '참여 등록'))
                    self._put(db,'leisure',f'{place}:{name}',group)
                elif re.search(r'종료|그만|나간|나갔',text) and actor in group.get('participants',[])+group.get('watchers',[]):
                    for field in ('participants','watchers'): group[field]=[p for p in group[field] if p!=actor]
                    self._put(db,'leisure',f'{place}:{name}',group);output.append(f'{name}: 참여 종료')
        if enabled(settings,'world_requests') and character:
            for match in re.finditer(r'#([A-Za-z0-9_-]+)\s*(수락한다|거절한다|진행한다|완료했다)',text):
                request=self._get(db,'request',match[1])
                if request and request.get('target')==actor and request.get('place')==place and request['expires']>now:
                    transitions={'수락한다':('제안','수락'),'거절한다':('제안','거절'),'진행한다':('수락','진행'),'완료했다':('진행','완료')}
                    previous,next_phase=transitions[match[2]]
                    if request['phase']==previous:
                        request['phase']=next_phase; request['updated']=now
                        self._put(db,'request',match[1],request);output.append(f'부탁 #{match[1]}: {next_phase}')
        return output

    def supply_box(self, identifier, actor, place, contents):
        if not isinstance(contents,dict) or not contents or any(not k.strip() or isinstance(v,bool) or not isinstance(v,int) or not 1<=v<=1000 for k,v in contents.items()):raise ValueError('상자 내용은 재료 이름과 1~1000 수량으로 입력하세요.')
        with self.transaction() as db:
            if self._get(db,'box',str(identifier)):return str(identifier)
            self._put(db,'box',str(identifier),dict(id=str(identifier),place=place,contents=contents,source=actor,phase='도착',created=self.clock()))
            self._event(db,actor,place,'식재료 상자','상자 #'+str(identifier)+' 도착');return str(identifier)

    def handle_box(self, identifier, actor, place, action):
        with self.transaction() as db:
            box=self._get(db,'box',identifier)
            if not box or box['place']!=place:raise ValueError('현재 장소의 재료 상자 ID를 입력하세요.')
            if action=='발견' and box['phase']=='도착':box['phase']='발견';box['custodian']=actor
            elif action=='정리' and box['phase']=='발견':
                stock=self._get(db,'ingredients',place,{})
                for k,n in box['contents'].items():stock[k]=stock.get(k,0)+n
                self._put(db,'ingredients',place,stock);box['phase']='정리 완료'
            else:raise ValueError('도착한 상자를 발견한 뒤 정리하세요. 완료된 상자는 중복 반영하지 않습니다.')
            self._put(db,'box',identifier,box);self._event(db,actor,place,'식재료 상자',f'상자 #{identifier}: {box["phase"]}')
            return box['phase']

    def command(self, identifier, actor, place, action, name='', target='', text='', secret=False):
        """Explicit user commands avoid guessing ownership or a letter's contents."""
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM effects WHERE scope=? AND event_id=?',(self.scope,str(identifier))).fetchone(): return '이미 처리한 행동입니다.'
            now=self.clock(); record_id=str(identifier)
            if action in ('부탁','쪽지'):
                if not target or not text.strip(): raise ValueError('수신 캐릭터와 내용을 입력하세요.')
                value=dict(id=record_id,type=action,source=actor,target=target,place=place,content=text[:1000],secret=secret,phase='제안' if action=='부탁' else '미발견',created=now,updated=now,expires=now+86400)
                self._put(db,'request',record_id,value);output=f'{action} #{record_id} 등록 (24시간 유효)'
            elif action=='부탁취소':
                value=self._get(db,'request',name)
                if not value or value['source']!=actor: raise ValueError('본인이 등록한 부탁 ID를 입력하세요.')
                if value['phase'] in ('완료','거절','만료','취소'): raise ValueError('이미 종료된 부탁입니다.')
                value['phase']='취소';value['updated']=now;self._put(db,'request',name,value);output=f'부탁 #{name} 취소'
            elif action=='물건놓기':
                if not name.strip():raise ValueError('물건 이름을 입력하세요.')
                value=dict(id=record_id,name=name[:100],owner=actor,custodian='',place=place,status='보관',created=now)
                self._put(db,'item',record_id,value);output=f'{name} 물건 #{record_id} 보관'
            elif action in ('분실신고','발견','반환','물건회수','대여'):
                value=self._get(db,'item',name)
                if not value: raise ValueError('등록된 물건 ID를 입력하세요.')
                if action=='분실신고':
                    if value['owner']!=actor: raise ValueError('주인만 분실 신고할 수 있습니다.')
                    value['status']='분실'
                elif action=='발견':
                    if value['place']!=place or value['custodian']: raise ValueError('현재 장소에 보관자가 없는 물건만 발견할 수 있습니다.')
                    value['custodian']=actor;value['status']='발견·보관'
                elif action=='반환':
                    if value['custodian']!=actor: raise ValueError('현재 보관자만 반환할 수 있습니다.')
                    value['custodian']=value['owner'];value['status']='반환'
                elif action=='물건회수':
                    if value['owner']!=actor or value['place']!=place or value['custodian'] not in ('',actor): raise ValueError('현재 장소의 본인 물건만 회수할 수 있습니다.')
                    value['custodian']=actor;value['status']='회수'
                elif action=='대여':
                    if value['owner']!=actor or value['custodian'] not in ('',actor) or not target: raise ValueError('주인이 보관 중인 물건과 대여 대상을 지정하세요.')
                    value['custodian']=target;value['status']='대여'
                self._put(db,'item',name,value);output=f'{value["name"]} #{name}: {value["status"]}'
            else: raise ValueError('생활 행동 종류를 확인하세요.')
            db.execute('INSERT INTO effects VALUES(?,?,?,?,?,?)',(self.scope,str(identifier),actor,place,now,json.dumps(output,ensure_ascii=False)))
            self._event(db,actor,place,'생활 명령',output)
            return output

    def discover(self, actor, place, settings):
        if not enabled(settings,'world_requests'): return []
        with self.transaction() as db:
            now=self.clock(); found=[]
            rows=db.execute("SELECT key,data FROM state WHERE scope=? AND kind='request'",(self.scope,)).fetchall()
            for row in rows:
                value=json.loads(row['data'])
                if value['expires']<=now and value['phase'] not in ('완료','거절','취소','만료','발견'):
                    value['phase']='만료';self._put(db,'request',row['key'],value)
                elif value['type']=='쪽지' and value['phase']=='미발견' and value['target']==actor and value['place']==place:
                    value['phase']='발견';value['updated']=now;self._put(db,'request',row['key'],value);found.append(row['key'])
                    self._event(db,actor,place,'쪽지 발견',f'쪽지 #{row["key"]} 발견')
            return found

    def sync_leisure_seats(self, settings):
        if not self.life:return
        now=self.clock();active={}
        if enabled(settings,'group_leisure'):
            for group in self.states('leisure').values():
                if group.get('end',0)<=now:continue
                for actor in group.get('participants',[]):
                    record=self.life.record(actor)
                    if (record.get('place')==group['place'] or actor.startswith('user:')) and not record.get('nap') and not record.get('night_sleep'):
                        active[actor]=(group['place'],group['seat'])
        for actor,record in self.life.records.items():
            old=record.pop('leisure_seat',None)
            if old and actor not in active and record.get('seat')==old:record.pop('seat',None)
            if actor in active:
                record.update(place=active[actor][0],seat=active[actor][1],leisure_seat=active[actor][1])
        for actor,(place,seat) in active.items():self.life.record(actor).update(place=place,seat=seat,leisure_seat=seat)

    def leisure_reason(self, actor, settings):
        if enabled(settings,'group_leisure'):
            for group in self.states('leisure').values():
                if group.get('end',0)>self.clock() and actor in group.get('participants',[]):return f"{group['name']} {group.get('phase','진행')} 중"
        return ''

    def tick_objects(self, place, definition, weather, settings):
        if not enabled(settings,'object_states'):return
        now=self.clock()
        with self.transaction() as db:
            previous=self._get(db,'object_clock',place,{'at':now})['at']
            elapsed=max(0,min(600,now-previous))/3600
            self._put(db,'object_clock',place,{'at':now})
            for name,spec in definition.get('object_states',{}).items():
                key=f'{place}:{name}';current=self._get(db,'object',key,spec.get('properties',{}));before=dict(current)
                for prop,rate in spec.get('drift',{}).items():
                    value=current.get(prop)
                    if isinstance(value,(int,float)) and not isinstance(value,bool):
                        lo,hi=spec.get('limits',{}).get(prop,[-1000000,1000000]);current[prop]=max(lo,min(hi,value+rate*elapsed))
                if spec.get('outdoor') and '젖음' in current and isinstance(current['젖음'],bool) and any(w in weather for w in ('비','눈')):current['젖음']=True
                self._put(db,'object',key,current)
                # Continuous numeric drift is sampled; only meaningful boundary changes
                # need their own audit event, keeping long-running worlds compact.
                thresholds=spec.get('manage_below',{})
                crossed=any(before.get(p,t+1)>t and current.get(p,t+1)<=t for p,t in thresholds.items())
                if crossed or before.get('젖음')!=current.get('젖음'):self._event(db,'환경',place,'오브젝트 변화',f'{name}: {json.dumps(current,ensure_ascii=False)}')

    def reserve_meal(self, actor, place, definition, settings):
        if not enabled(settings,'food_stock'): return None
        with self.transaction() as db:
            now=self.clock();carried=self._get(db,'carried_food',actor,[])
            held=next((s for s in carried if s['remaining']>0 and s['expires']>now),None)
            if held:
                portions=[s for s in carried if s['remaining']>0 and s['expires']>now and (s.get('set')==held.get('set') if held.get('set') else s is held)]
                return ' + '.join(s['name'] for s in portions)
            stock=self._get(db,'food',place,[])
            portion=next((s for s in stock if s['remaining']>0 and s['expires']>now),None)
            if not portion: return None
            portions=[s for s in stock if s['remaining']>0 and s['expires']>now and (s.get('set')==portion.get('set') if portion.get('set') else s is portion)]
            for s in portions:s['remaining']-=1;carried.append(dict(s,remaining=1))
            self._put(db,'food',place,stock);self._put(db,'carried_food',actor,carried)
            menu=' + '.join(s['name'] for s in portions);self._event(db,actor,place,'음식 수령',menu)
            return menu

    def prepare_food(self, identifier, actor, place, names, count, definition, settings):
        if not enabled(settings,'food_stock'):raise ValueError('음식 재고 기능을 먼저 켜세요.')
        if not 1<=count<=100:raise ValueError('준비 수량은 1~100으로 입력하세요.')
        names=list(dict.fromkeys(names))
        if not names:raise ValueError('준비한 음식 이름을 입력하세요.')
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM effects WHERE scope=? AND event_id=?',(self.scope,str(identifier))).fetchone():return '이미 등록한 음식입니다.'
            definitions=definition.get('food_definitions',{})
            foods=[(name,dict(FOOD_DEFAULT,**definitions.get(name,{}))) for name in names]
            ingredients=self._get(db,'ingredients',place,{})
            required={}
            if enabled(settings,'ingredient_stock'):
                for _,food in foods:
                    for ingredient,n in food['ingredients'].items():required[ingredient]=required.get(ingredient,0)+n*count
                if any(ingredients.get(k,0)<n for k,n in required.items()):raise ValueError('식재료 재고가 부족합니다. 조리 결과를 반영하지 않았습니다.')
                for k,n in required.items():ingredients[k]-=n
                self._put(db,'ingredients',place,ingredients)
            stock=self._get(db,'food',place,[]);now=self.clock()
            for name,food in foods:
                stock.append(dict(id=f'{identifier}:{name}',set=str(identifier),name=name,remaining=count,source=actor,created=now,expires=now+food['expires_minutes']*60,unit=food['unit'],hunger=food['hunger'],dishes=food['dishes']))
            self._put(db,'food',place,stock)
            output=f"{' + '.join(names)} {count}세트 준비 반영"
            db.execute('INSERT INTO effects VALUES(?,?,?,?,?,?)',(self.scope,str(identifier),actor,place,now,json.dumps(output,ensure_ascii=False)))
            self._event(db,actor,place,'음식 준비',output);return output

    def consume(self, event_id, actor, place, definition, settings, name=None):
        if not enabled(settings,'food_stock'): return None
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM effects WHERE scope=? AND event_id=?',(self.scope,str(event_id))).fetchone(): return None
            now=self.clock();carried=self._get(db,'carried_food',actor,[]);names=name.split(' + ') if name else []
            portion=next((s for s in carried if s['remaining']>0 and s['expires']>now and (not names or s['name'] in names)),None)
            if not portion:return None
            if definition.get('outdoor') and not definition.get('_food_weather_ok',True):return None
            portions=[s for s in carried if s['remaining']>0 and s['expires']>now and (s.get('set')==portion.get('set') if portion.get('set') else s is portion)]
            for s in portions:s['remaining']-=1
            self._put(db,'carried_food',actor,carried)
            if enabled(settings,'dishwashing'):
                sink=self._get(db,'sink',place,{'dirty':0,'reserved_by':'','until':0});sink['dirty']+=sum(s.get('dishes',1) for s in portions);self._put(db,'sink',place,sink)
            menu=' + '.join(s['name'] for s in portions)
            db.execute('INSERT INTO effects VALUES(?,?,?,?,?,?)',(self.scope,str(event_id),actor,place,now,json.dumps({'food':menu},ensure_ascii=False)))
            self._event(db,actor,place,'섭취',menu)
            return dict(name=menu,hunger=min(100,sum(s['hunger'] for s in portions)))

    def correct(self, kind, key, value, actor='관리자'):
        if kind not in ('food','ingredients','object','sink','carried_food'): raise ValueError('수정할 자료 종류를 확인하세요.')
        if kind in ('food','carried_food'):
            if not isinstance(value,list) or any(not isinstance(s,dict) or not isinstance(s.get('remaining'),int) or s['remaining']<0 or not isinstance(s.get('expires'),(int,float)) or not isinstance(s.get('hunger'),(int,float)) or not 0<=s['hunger']<=100 or not isinstance(s.get('name'),str) for s in value): raise ValueError('음식은 이름·남은 수량·만료 시각·허기 회복량을 가진 목록으로 입력하세요.')
        elif not isinstance(value,dict): raise ValueError('수정 값은 JSON 객체로 입력하세요.')
        if kind=='ingredients' and any(not isinstance(v,int) or isinstance(v,bool) or v<0 for v in value.values()): raise ValueError('식재료는 0 이상의 정수 수량으로 입력하세요.')
        if kind=='sink' and (isinstance(value.get('dirty'),bool) or not isinstance(value.get('dirty'),int) or value['dirty']<0 or not isinstance(value.get('until',0),(int,float)) or not isinstance(value.get('reserved_by',''),str)): raise ValueError('식기 수량은 0 이상의 정수, 예약 만료는 숫자, 담당자는 문자로 입력하세요.')
        with self.transaction() as db:
            before=self._get(db,kind,key);self._put(db,kind,key,value);self._event(db,actor,key,'관리자 수정',f'{kind}: {json.dumps(before,ensure_ascii=False)} → {json.dumps(value,ensure_ascii=False)}')

    def submit(self, identifier, kind, actor, place, payload, user=True):
        with self.transaction() as db:
            return db.execute('INSERT OR IGNORE INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)',(self.scope,str(identifier),kind,actor,place,0 if user else 10,'대기',self.clock(),self.clock(),json.dumps(payload,ensure_ascii=False),'')).rowcount==1

    def begin_job(self, identifier):
        with self.transaction() as db:
            return db.execute("UPDATE jobs SET phase='생성 중',updated=? WHERE scope=? AND id=? AND phase='대기'",(self.clock(),self.scope,str(identifier))).rowcount==1

    def claim(self):
        with self.transaction() as db:
            row=db.execute("SELECT * FROM jobs WHERE scope=? AND phase='대기' ORDER BY priority,created LIMIT 1",(self.scope,)).fetchone()
            if not row: return None
            db.execute("UPDATE jobs SET phase='생성 중', updated=? WHERE scope=? AND id=?",(self.clock(),self.scope,row['id']))
            value=dict(row);value['payload']=json.loads(value['payload']);return value

    def job_phase(self, identifier, phase, error=''):
        if phase not in ('전송 중','완료','오류','불확정','취소'): raise ValueError('작업 상태를 확인하세요.')
        with self.transaction() as db:
            db.execute("UPDATE jobs SET phase=?,updated=?,error=? WHERE scope=? AND id=? AND (phase NOT IN ('완료','취소') OR ? IN ('완료','취소'))",(phase,self.clock(),error,self.scope,str(identifier),phase))

    def snapshot(self, settings, limit=100):
        mapping={'food_stock':['food','carried_food'],'ingredient_stock':['ingredients','box'],'dishwashing':['sink'],'object_states':['object'],'group_leisure':['leisure'],'world_requests':['request'],'lost_found':['item'],'inner_thoughts':['thought']}
        kinds=[kind for flag,values in mapping.items() if enabled(settings,flag) for kind in values]
        now=self.clock()
        with self.transaction() as db:
            states={}
            for kind in kinds:
                states[kind]={r['key']:json.loads(r['data']) for r in db.execute('SELECT key,data FROM state WHERE scope=? AND kind=?',(self.scope,kind))}
            for kind in ('food','carried_food'):
                if kind in states: states[kind]={key:[s for s in items if s.get('remaining',0)>0 and s.get('expires',0)>now] for key,items in states[kind].items()}
            if 'leisure' in states: states['leisure']={key:g for key,g in states['leisure'].items() if g.get('end',0)>now and g.get('participants')}
            events=[dict(r) for r in db.execute('SELECT created,actor,place,kind,content FROM events WHERE scope=? ORDER BY id DESC LIMIT ?',(self.scope,limit))]
            jobs=[dict(r) for r in db.execute("SELECT id,kind,actor,place,phase,created,updated,error FROM jobs WHERE scope=? AND phase NOT IN ('완료','취소') ORDER BY CASE WHEN phase IN ('생성 중','전송 중') THEN 0 ELSE 1 END,priority,created LIMIT 100",(self.scope,))]
            active=db.execute("SELECT COUNT(*) FROM jobs WHERE scope=? AND phase IN ('생성 중','전송 중')",(self.scope,)).fetchone()[0]
            return dict(states=states,events=events,jobs=jobs,active=active)

    def context(self, actor, place, settings, definition=None, busy=False):
        snapshot=self.snapshot(settings,0)['states']; lines=[]
        for kind in ('food','ingredients','sink'):
            value=snapshot.get(kind,{}).get(place)
            if value: lines.append(f'{kind}: {json.dumps(value,ensure_ascii=False)}')
        held=snapshot.get('carried_food',{}).get(actor)
        if held: lines.append('수령한 음식 (섭취 전): '+json.dumps(held,ensure_ascii=False))
        for kind in ('object','leisure'):
            for key,value in snapshot.get(kind,{}).items():
                if key.startswith(place+':'): lines.append(f'{key}: {json.dumps(value,ensure_ascii=False)}')
        if enabled(settings,'object_states'):
            for name,spec in (definition or {}).get('object_states',{}).items():
                value=snapshot.get('object',{}).get(f'{place}:{name}',spec.get('properties',{}))
                needs=[p for p,t in spec.get('manage_below',{}).items() if isinstance(value.get(p),(int,float)) and value[p]<=t]
                if needs:
                    reason='진행 중인 일 우선' if busy else '지정 담당자 관리' if spec.get('manager') and spec['manager']!=actor else '자동 관리 대상 아님' if not spec.get('auto_manage') else ''
                    lines.append(f"{name} 관리 필요: {', '.join(needs)} / "+('보류: '+reason if reason else '수행 가능한 등록 행동: '+spec.get('manage_action','미지정')))
        if enabled(settings,'world_requests'):
            for identifier,request in snapshot.get('request',{}).items():
                if request.get('expires',0)<=self.clock() or request['target']!=actor: continue
                if request['type']=='쪽지' and request['phase']!='발견': continue
                if request['type']=='부탁' and request['place']!=place: continue
                lines.append(f"{request['type']} #{identifier} · {request['phase']} · 출처 {request['source']}: {request['content']}")
                if request['type']=='부탁': lines.append('수락·거절·진행·완료 시 지문에 #ID 수락한다 / #ID 거절한다 / #ID 진행한다 / #ID 완료했다 중 해당 단계만 포함한다. 실제로 완료하기 전 완료를 쓰지 않는다.')
        if enabled(settings,'ingredient_stock'):
            for identifier,box in snapshot.get('box',{}).items():
                if box['place']==place and box['phase']!='정리 완료':lines.append(f"식재료 상자 #{identifier}: {box['phase']} / {box['contents']}")
        if enabled(settings,'lost_found'):
            for identifier,item in snapshot.get('item',{}).items():
                if item['place']==place or actor in (item['owner'],item['custodian']):
                    lines.append(f"물건 #{identifier}: {item['name']} / 주인 {item['owner']} / 보관자 {item['custodian'] or '없음'} / {item['status']}")
        return '\n'.join(lines)

    def record_samples(self, states, places, connected, naps, settings):
        if not enabled(settings,'life_statistics'):return
        now=self.clock();minute=int(now//60)
        with self.transaction() as db:
            for actor,state in states.items():
                db.execute('INSERT OR IGNORE INTO samples VALUES(?,?,?,?,?,?,?,?,?,?)',(self.scope,minute,actor,places.get(actor,''),state.get('hunger'),state.get('fatigue'),int(bool(state.get('sleeping'))),int(bool(naps.get(actor))),int(bool(state.get('away'))),int(bool(connected.get(actor)))))
            db.execute('DELETE FROM samples WHERE scope=? AND minute<?',(self.scope,minute-90*24*60))

    def statistics(self, hours=24, actor='', place=''):
        start=self.clock()-hours*3600
        with self.transaction() as db:
            samples=[dict(r) for r in db.execute("SELECT * FROM samples WHERE scope=? AND minute>=? AND (?='' OR actor=?) AND (?='' OR place=?) ORDER BY minute",(self.scope,int(start//60),actor,actor,place,place))]
            counts=[dict(r) for r in db.execute("SELECT kind,COUNT(*) AS count FROM events WHERE scope=? AND created>=? AND kind!='속마음' AND (?='' OR actor=?) AND (?='' OR place=?) GROUP BY kind ORDER BY count DESC",(self.scope,start,actor,actor,place,place))]
            return dict(samples=samples,counts=counts)

    def public_dialogue(self, identifier, actor, place, text):
        with self.transaction() as db:
            if db.execute('INSERT OR IGNORE INTO effects VALUES(?,?,?,?,?,?)',(self.scope,f'dialogue:{identifier}',actor,place,self.clock(),'공개 전송')).rowcount:
                self._event(db,actor,place,'공개 대화',text[:2000])

    def stage_thought(self, actor, reply, thought):
        digest=hashlib.sha256(reply.encode('utf-8')).hexdigest()
        with self.transaction() as db:
            self._put(db,'thought_draft',f'{actor}:{digest}',dict(text=thought[:1000],created=self.clock()))

    def sent(self, actor, reply, place):
        digest=hashlib.sha256(reply.encode('utf-8')).hexdigest();key=f'{actor}:{digest}'
        with self.transaction() as db:
            draft=self._get(db,'thought_draft',key)
            if draft and self.clock()-draft['created']<300:
                self._put(db,'thought',actor,dict(text=draft['text'],created=self.clock(),place=place))
                self._event(db,actor,place,'속마음',draft['text'])
            db.execute("DELETE FROM state WHERE scope=? AND kind='thought_draft' AND key=?",(self.scope,key))

    def export(self, path, start=0, end=float('inf')):
        with self.transaction() as db, Path(path).open('w',encoding='utf-8-sig',newline='') as out:
            writer=csv.writer(out);writer.writerow(['시각(Unix)','행위자','장소','종류','내용'])
            writer.writerows(tuple(r) for r in db.execute("SELECT created,actor,place,kind,content FROM events WHERE scope=? AND created>=? AND created<=? AND kind!='속마음' ORDER BY id",(self.scope,start,end)))
