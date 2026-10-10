import concurrent.futures
import json
import random
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from world_actions import WorldActions, completed, quantity, validate_extensions
from life_engine import LifeEngine
from model_client import ModelClient, GenerationError, GENERATION_PRIORITY
from rp_policy import FEATURES, enabled
from feature_dependencies import toggle_changes, validate_features
from feature_groups import grouped_features
from setup_wizard import FEATURE_QUESTIONS

class WorldTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.now=100000.;self.engine=WorldActions(self.temp.name,'1',clock=lambda:self.now)
        self.settings={'features':{k:True for k,_,_ in FEATURES}}
        self.place={'food_definitions':{'토스트':{'aliases':['토스트 빵'],'hunger':35,'expires_minutes':180},'차':{'hunger':5,'dishes':1}},'object_states':{'창문':{'properties':{'열림':False},'actions':{'열었다':{'열림':True},'닫았다':{'열림':False}}}},'leisure':{'체스':{'capacity':2,'minutes':30,'seat':'테이블'}},'seats':{'테이블':2}}
    def act(self,id,text,actor='user:1',character=False):return self.engine.apply(id,actor,'주방',text,self.place,self.settings,character)
    def test_question_negation_future_quote_past_do_not_apply(self):
        for text in ['토스트 두 인분 준비할까?','토스트를 준비하지 않았다','토스트를 만들게','"토스트를 만들었다"','어제 토스트를 준비했다','토스트를 만들려고 한다','토스트를 안 만들었다','토스트를 못 준비했다']:
            self.assertFalse(self.act(text,text),text)
        self.assertFalse(self.engine.states('food'))
    def test_korean_quantity_whitespace_alias_and_dedup_across_restart(self):
        self.assertTrue(self.act('1','토스트 빵 두 인분을 준비했다'))
        self.assertEqual(self.engine.states('food')['주방'][0]['remaining'],2)
        other=WorldActions(self.temp.name,'1',clock=lambda:self.now)
        self.assertEqual(other.apply('1','user:1','주방','토스트 빵 두 인분을 준비했다',self.place,self.settings),[])
        self.assertEqual(other.states('food')['주방'][0]['remaining'],2)
    def test_concurrent_same_message_is_atomic(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _: self.act('same','토스트 세 인분을 준비했다'),range(4)))
        self.assertEqual(sum(bool(r) for r in results),1)
        self.assertEqual(self.engine.states('food')['주방'][0]['remaining'],3)
    def test_receipt_is_not_consumption_and_eating_is_exactly_once(self):
        self.act('1','토스트 두 인분을 준비했다')
        self.act('2','토스트 한 인분을 받았다','a',True)
        self.assertEqual(self.engine.states('food')['주방'][0]['remaining'],1)
        self.assertFalse(self.engine.states('sink'))
        effect=self.engine.consume('eat','a','주방',self.place,self.settings)
        self.assertEqual(effect['hunger'],35)
        self.assertIsNone(self.engine.consume('eat','a','주방',self.place,self.settings))
        self.assertEqual(self.engine.states('sink')['주방']['dirty'],1)
    def test_expired_food_cannot_be_received_or_eaten(self):
        self.act('1','토스트 한 인분을 준비했다');self.now+=20000
        self.assertIsNone(self.engine.reserve_meal('a','주방',self.place,self.settings))
        self.assertEqual(self.engine.snapshot(self.settings)['states']['food']['주방'],[])
    def test_stock_missing_never_negative_and_ingredients_use_recipe(self):
        self.place['food_definitions']['토스트']['ingredients']={'빵':1}
        self.assertIn('부족',self.act('1','토스트 두 인분을 조리했다')[0])
        self.assertFalse(self.engine.states('food'))
        self.engine.correct('ingredients','주방',{'빵':3})
        self.act('2','토스트 두 인분을 조리했다')
        self.assertEqual(self.engine.states('ingredients')['주방']['빵'],1)
    def test_object_uses_registered_properties_only_and_scopes_are_separate(self):
        self.act('1','창문을 열었다')
        self.assertTrue(self.engine.states('object')['주방:창문']['열림'])
        self.assertEqual(self.act('2','난방을 켰다'),[])
        other=WorldActions(self.temp.name,'2',clock=lambda:self.now)
        self.assertFalse(other.states('object'))
    def test_washing_reservation_blocks_others_and_expires(self):
        self.engine.correct('sink','주방',{'dirty':3,'reserved_by':'','until':0})
        self.act('1','설거지 담당을 예약했다')
        self.assertIn('보류',self.act('2','설거지 두 개를 했다','user:2')[0])
        self.assertEqual(self.engine.states('sink')['주방']['dirty'],3)
        self.now+=1801;self.act('3','설거지 두 개를 했다','user:2')
        self.assertEqual(self.engine.states('sink')['주방']['dirty'],1)
    def test_leisure_reuses_group_caps_participants_watchers_and_expires(self):
        for actor in ('a','b','c'):self.act(actor,'체스에 참여한다',actor,True)
        group=self.engine.states('leisure')['주방:체스']
        self.assertEqual(group['participants'],['a','b']);self.assertEqual(group['watchers'],['c'])
        self.act('again','체스에 참여한다','a',True)
        self.assertEqual(len(self.engine.states('leisure')['주방:체스']['participants']),2)
        self.now+=1900;self.assertEqual(self.engine.snapshot(self.settings)['states']['leisure'],{})
    def test_new_flags_default_off_and_turning_off_hides_but_retains_state(self):
        for key in ('food_stock','object_states','actual_sleep','group_leisure'):self.assertFalse(enabled({},key))
        self.act('1','창문을 열었다');self.settings['features']['object_states']=False
        self.assertNotIn('object',self.engine.snapshot(self.settings)['states'])
        self.assertTrue(self.engine.states('object')['주방:창문']['열림'])
    def test_note_discovery_and_secrecy_and_request_transitions(self):
        self.engine.command('letter','user:1','주방','쪽지',target='a',text='개인 비밀',secret=True)
        self.assertNotIn('개인 비밀',self.engine.context('a','주방',self.settings))
        self.engine.discover('b','주방',self.settings)
        self.assertNotIn('개인 비밀',self.engine.context('b','주방',self.settings))
        self.engine.discover('a','주방',self.settings)
        self.assertIn('개인 비밀',self.engine.context('a','주방',self.settings))
        self.engine.command('request','user:1','주방','부탁',target='a',text='컵 정리')
        self.act('done-too-early','#request 완료했다','a',True)
        self.assertEqual(self.engine.states('request')['request']['phase'],'제안')
        for id,verb in [('accept','수락한다'),('run','진행한다'),('done','완료했다')]:self.act(id,'#request '+verb,'a',True)
        self.assertEqual(self.engine.states('request')['request']['phase'],'완료')
    def test_found_items_do_not_change_owner_and_only_custodian_returns(self):
        self.engine.command('item','user:1','주방','물건놓기',name='우산')
        self.engine.command('found','user:2','주방','발견',name='item')
        item=self.engine.states('item')['item'];self.assertEqual(item['owner'],'user:1');self.assertEqual(item['custodian'],'user:2')
        with self.assertRaises(ValueError):self.engine.command('wrong','user:3','주방','반환',name='item')
        self.engine.command('returned','user:2','주방','반환',name='item')
        self.assertEqual(self.engine.states('item')['item']['custodian'],'user:1')
    def test_queue_priority_failed_jobs_do_not_block_and_sending_is_uncertain(self):
        self.engine.submit('auto','자율','a','주방',{},user=False);self.engine.submit('user','사용자','b','주방',{})
        self.assertEqual(self.engine.claim()['id'],'user');self.engine.job_phase('user','오류','실패')
        self.assertEqual(self.engine.snapshot(self.settings)['active'],0)
        self.assertEqual(self.engine.claim()['id'],'auto');self.engine.job_phase('auto','전송 중')
        other=WorldActions(self.temp.name,'1',clock=lambda:self.now)
        self.assertEqual(next(j['phase'] for j in other.snapshot(self.settings)['jobs'] if j['id']=='auto'),'불확정');self.assertIsNone(other.claim())
    def test_read_only_dashboard_open_does_not_reset_running_job(self):
        self.engine.submit('x','답변','a','주방',{});self.engine.begin_job('x')
        WorldActions(self.temp.name,'1',clock=lambda:self.now,recover=False)
        self.assertEqual(self.engine.snapshot(self.settings)['active'],1)
    def test_thought_is_committed_only_after_send_and_csv_is_separate(self):
        self.engine.stage_thought('a','안녕','생각')
        self.assertFalse(self.engine.states('thought'));self.engine.sent('a','안녕','주방')
        self.assertEqual(self.engine.states('thought')['a']['text'],'생각')
    def test_life_engine_stock_meal_recovers_once_and_records_real_dishes(self):
        self.act('1','토스트 한 인분을 준비했다');engine=LifeEngine(self.temp.name,rng=random.Random(1));engine.world=self.engine
        settings={'features':dict(self.settings['features'],naps=False,morning_routine=False)}
        state={'hunger':80,'fatigue':10,'sleeping':False}
        for stamp in (self.now,self.now+360,self.now+420):engine.tick('a','주방',self.place,state,'',settings,stamp,12)
        self.assertEqual(state['hunger'],45);self.assertEqual(self.engine.states('sink')['주방']['dirty'],1)
    def test_real_sleep_survives_restart_short_sleep_does_not_reset(self):
        engine=LifeEngine(self.temp.name);settings={'features':{'actual_sleep':True,'sleep_quality':False,'morning_routine':True}}
        state={'fatigue':90};engine.begin_sleep('a',state,settings,100000)
        loaded=LifeEngine(self.temp.name);loaded.wake('a',state,settings,101800,{'required_sleep_hours':8,'sleep_recovery_per_hour':7})
        self.assertEqual(state['fatigue'],86.5);self.assertEqual(loaded.record('a')['sleep_debt'],7.5)
        self.assertIn('기상',loaded.tick('a','방',{},state,'',settings,101860,8))
    def test_sleep_outfits_are_registered_and_scope_free_policy(self):
        engine=LifeEngine(self.temp.name);config={'sleep_outfit':'잠옷','day_outfits':[{'outfit':'비옷','weather':['비']},{'outfit':'셔츠','weather':[]}]}
        self.assertEqual(engine.outfit('a',config,self.settings,True),'잠옷')
        self.assertEqual(engine.outfit('a',config,self.settings,False,'비'),'비옷')
        self.assertEqual(engine.outfit('a',config,self.settings,False,'맑음'),'셔츠')
    def test_food_sets_are_received_together_without_merging_separate_sets(self):
        self.engine.prepare_food('set1','user:1','주방',['토스트','차'],2,self.place,self.settings)
        self.engine.prepare_food('set2','user:2','주방',['차'],1,self.place,self.settings)
        name=self.engine.reserve_meal('a','주방',self.place,self.settings)
        self.assertEqual(name,'토스트 + 차')
        result=self.engine.consume('eat','a','주방',self.place,self.settings,name)
        self.assertEqual(result['hunger'],40);self.assertEqual(self.engine.states('sink')['주방']['dirty'],2)
        self.assertEqual(next(s['remaining'] for s in self.engine.states('food')['주방'] if s['set']=='set2'),1)
    def test_object_drift_limits_manager_reason_and_indoor_seats_not_wet(self):
        self.place['object_states']={'화분':{'properties':{'수분':40},'drift':{'수분':-60},'limits':{'수분':[0,100]},'manage_below':{'수분':35},'auto_manage':True,'manager':'a','actions':{'물을 줬다':{'수분':100}},'manage_action':'물을 줬다'},'의자':{'properties':{'젖음':False},'outdoor':False}}
        self.engine.tick_objects('주방',self.place,'비',self.settings);self.now+=600
        self.engine.tick_objects('주방',self.place,'비',self.settings)
        self.assertEqual(self.engine.states('object')['주방:화분']['수분'],30)
        self.assertFalse(self.engine.states('object')['주방:의자']['젖음'])
        self.assertIn('지정 담당자',self.engine.context('b','주방',self.settings,self.place))
        self.act('water','화분에 물을 줬다')
        self.assertEqual(self.engine.states('object')['주방:화분']['수분'],100)
    def test_statistics_one_sample_per_minute_filters_gaps_and_no_private_export(self):
        state={'a':{'hunger':30,'fatigue':20,'sleeping':True}}
        self.engine.record_samples(state,{'a':'주방'},{'a':True},{},self.settings)
        self.engine.record_samples(state,{'a':'주방'},{'a':True},{},self.settings)
        self.now+=180;self.engine.record_samples(state,{'a':'주방'},{'a':True},{},self.settings)
        stats=self.engine.statistics(24,'a','주방');self.assertEqual(len(stats['samples']),2)
        self.assertEqual(stats['samples'][1]['minute']-stats['samples'][0]['minute'],3)
        self.assertEqual(self.engine.statistics(24,'b')['samples'],[])
        self.engine.stage_thought('a','응답','내부 생각');self.engine.sent('a','응답','주방')
        out=Path(self.temp.name)/'export.csv';self.engine.export(out)
        self.assertNotIn('내부 생각',out.read_text(encoding='utf-8-sig'))
    def test_short_alias_does_not_match_inside_unrelated_noun(self):
        self.assertEqual(self.act('car','자동차를 준비했다'),[])
        self.assertFalse(self.engine.states('food'))

    def test_wake_response_delays_or_overrides_sleep_without_fabricated_recovery(self):
        engine=LifeEngine(self.temp.name);engine.rng=Mock();engine.rng.choices.return_value=['미룸']
        state={'sleeping':True,'fatigue':90};engine.begin_sleep('a',state,self.settings,self.now-1800)
        self.assertEqual(engine.wake_response('a',state,{},self.settings,self.now),'미룸')
        self.assertEqual(engine.record('a')['wake_delayed_until'],self.now+600);self.assertEqual(state['fatigue'],90)
        engine.rng.choices.return_value=['일어남'];engine.wake_response('a',state,{},self.settings,self.now)
        self.assertEqual(engine.record('a')['wake_override_until'],self.now+7200)
    def test_grooming_destination_capacity_and_conversation_pause(self):
        engine=LifeEngine(self.temp.name);engine.record('a')['morning']={'start':self.now-360,'last_tick':self.now-60}
        definitions={'세면실':{'routine_capacity':1}}
        target=engine.morning_destination('a',{'grooming_place':'세면실'},self.settings,self.now,'방',definitions,{'b':'세면실'},lambda _:True)
        self.assertIsNone(target)
        target=engine.morning_destination('a',{'grooming_place':'세면실'},self.settings,self.now,'방',definitions,{},lambda _:True)
        self.assertEqual(target,'세면실')
        state={'hunger':0,'fatigue':0};engine.tick('a','방',{},state,'대화',self.settings,self.now,8,busy=True)
        self.assertEqual(engine.record('a')['morning']['start'],self.now-300)
    def test_ingredient_box_find_store_is_separate_and_not_duplicated(self):
        self.engine.supply_box('box','user:1','주방',{'빵':3})
        self.assertFalse(self.engine.states('ingredients'))
        self.engine.handle_box('box','a','주방','발견');self.assertFalse(self.engine.states('ingredients'))
        self.engine.handle_box('box','a','주방','정리');self.assertEqual(self.engine.states('ingredients')['주방']['빵'],3)
        with self.assertRaises(ValueError):self.engine.handle_box('box','a','주방','정리')
    def test_historical_failed_jobs_do_not_hide_running_jobs(self):
        for n in range(105):self.engine.submit(str(n),'과거 실패','a','주방',{});self.engine.job_phase(str(n),'오류')
        self.engine.submit('live','사용자 답변','b','주방',{});self.engine.begin_job('live')
        snap=self.engine.snapshot(self.settings);self.assertEqual(snap['active'],1);self.assertEqual(snap['jobs'][0]['id'],'live')
        self.engine.job_phase('live','완료');self.engine.job_phase('live','오류','후처리 실패')
        self.assertEqual(self.engine.snapshot(self.settings)['active'],0)
        self.assertFalse(any(j['id']=='live' for j in self.engine.snapshot(self.settings)['jobs']))

    def test_validation_and_feature_groups_cover_every_toggle_once(self):
        validate_extensions({}, {'주방':self.place})
        bad={'food_definitions':{'차':{'hunger':-1}}}
        with self.assertRaises(ValueError):validate_extensions({}, {'주방':bad})
        grouped=[key for _,items in grouped_features(FEATURE_QUESTIONS) for key,_,_ in items]
        self.assertEqual(len(grouped),len(set(grouped)));self.assertEqual(set(grouped),{k for k,_,_ in FEATURE_QUESTIONS})
        off={'features':{k:False for k,_,_ in FEATURE_QUESTIONS}}
        changes=toggle_changes(off,'food_stock',True);off['features'].update(changes);validate_features(off)
        self.assertTrue(changes['world_simulation']);self.assertTrue(changes['meal_stages'])

class ModelTests(unittest.TestCase):
    def test_model_override_empty_and_truncated_rejected(self):
        sdk=Mock();sdk.responses.create.return_value=SimpleNamespace(status='completed',output_text='응답')
        client=ModelClient('fake',client=sdk,env={'RPBOT_MODEL':'chosen'})
        self.assertEqual(client.responses.create(model='old',input='x').output_text,'응답')
        self.assertEqual(sdk.responses.create.call_args.kwargs['model'],'chosen')
        for result in [SimpleNamespace(status='incomplete',output_text='중단'),SimpleNamespace(status='completed',output_text='')]:
            sdk.responses.create.return_value=result
            with self.assertRaises(GenerationError):client.responses.create(model='old',input='x')
    def test_chat_adapter_and_timeout_concurrency_validation(self):
        sdk=Mock();sdk.chat.completions.create.return_value=SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content='응답'))])
        client=ModelClient('fake',client=sdk,env={'RPBOT_API_STYLE':'chat'})
        self.assertEqual(client.create(model='m',instructions='s',input='u').output_text,'응답')
        self.assertEqual(sdk.chat.completions.create.call_args.kwargs['messages'],[{'role':'system','content':'s'},{'role':'user','content':'u'}])
        with self.assertRaises(ValueError):ModelClient('fake',client=sdk,env={'RPBOT_GENERATION_CONCURRENCY':'0'})
    def test_pending_user_generation_precedes_autonomous_generation(self):
        client=ModelClient('fake',client=Mock(),env={'RPBOT_GENERATION_CONCURRENCY':'1'})
        order=[];held=threading.Event();release=threading.Event();ready=threading.Barrier(3)
        def occupy():
            with client.slot():held.set();release.wait(2)
        def wait(name,priority):
            token=GENERATION_PRIORITY.set(priority)
            try:
                ready.wait()
                with client.slot():order.append(name)
            finally:GENERATION_PRIORITY.reset(token)
        with concurrent.futures.ThreadPoolExecutor(3) as pool:
            blocker=pool.submit(occupy);self.assertTrue(held.wait(2))
            a=pool.submit(wait,'auto',10);b=pool.submit(wait,'user',0);ready.wait()
            with client.condition:self.assertTrue(client.condition.wait_for(lambda:len(client.waiters)==2,timeout=2))
            release.set();blocker.result(timeout=2);a.result(timeout=2);b.result(timeout=2)
        self.assertEqual(order,['user','auto'])
        client.close_requests()
        with self.assertRaises(GenerationError):
            with client.slot():pass

if __name__=='__main__':unittest.main()
