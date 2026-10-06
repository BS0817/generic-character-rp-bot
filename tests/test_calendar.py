"""Calendar boundaries, scene privacy, life priority and dependency transactions."""
import copy
import contextvars
import json
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from world_calendar import *
from feature_dependencies import validate_features, toggle_changes
from life_engine import LifeEngine
from rp_policy import policy_prompt
from settings_store import DraftStore
from test_rp_extensions import functions

class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.settings={'timezone':'Asia/Seoul','features':{'world_calendar':True,'weekly_schedules':True,'opening_hours':True,'anniversaries':True},'calendar':copy.deepcopy(CALENDAR_DEFAULT)}
        self.now=datetime(2026,10,5,10,0,tzinfo=timezone(timedelta(hours=9))) # Monday
    def test_real_clock_and_disabled_virtual_clock_use_local_real_time(self):
        with tempfile.TemporaryDirectory() as folder:
            clock=WorldClock(folder);physical=datetime(2026,10,5,1,tzinfo=timezone.utc)
            self.assertEqual(clock.now(self.settings,physical),self.now)
            self.settings['calendar']['mode']='virtual';self.settings['features']['world_calendar']=False
            self.assertEqual(clock.now(self.settings,physical),self.now)
            self.assertFalse(clock.path.exists())
    def test_virtual_clock_survives_restart_and_resets_on_changed_start(self):
        with tempfile.TemporaryDirectory() as folder:
            physical=datetime(2026,10,5,1,tzinfo=timezone.utc)
            self.settings['calendar'].update(mode='virtual',start_date='1940-12-31',start_time='23:59')
            first=WorldClock(folder).now(self.settings,physical)
            later=WorldClock(folder).now(self.settings,physical+timedelta(minutes=2))
            self.assertEqual(first.date().isoformat(),'1940-12-31');self.assertEqual(later.date().isoformat(),'1941-01-01')
            self.assertEqual((later-first).total_seconds(),120)
            self.settings['calendar']['start_date']='2000-01-01'
            self.assertEqual(WorldClock(folder).now(self.settings,physical+timedelta(days=5)).date().isoformat(),'2000-01-01')
    def test_corrupt_clock_state_recovers(self):
        with tempfile.TemporaryDirectory() as folder:
            self.settings['calendar']['mode']='virtual'
            path=Path(folder)/'world_clock.json'
            for value in ['[]',json.dumps({'fingerprint':['2026-01-01','09:00','Asia/Seoul']})]:
                path.write_text(value);self.assertEqual(WorldClock(folder).now(self.settings,self.now).date().isoformat(),'2026-01-01')
    def test_overnight_hours_belong_to_opening_day_and_close_boundary(self):
        hours={'opening_hours':dict(enabled=True,days=['월'],opens='22:00',closes='02:00')}
        for instant,expected in [(self.now.replace(hour=21),False),(self.now.replace(hour=22),True),((self.now+timedelta(days=1)).replace(hour=1),True),((self.now+timedelta(days=1)).replace(hour=2),False),((self.now+timedelta(days=1)).replace(hour=22),False)]:
            self.assertEqual(place_open(self.settings,hours,instant),expected)
        self.settings['features']['opening_hours']=False
        self.assertTrue(place_open(self.settings,hours,self.now))
    def test_same_start_end_is_twenty_four_hours_and_empty_days_closed(self):
        self.assertTrue(in_slot(self.now,['월'],'00:00','00:00'));self.assertFalse(in_slot(self.now,[],'00:00','00:00'))
    def test_seasonal_weather_matches_config_and_display_flags_do_not_hide_ai_context(self):
        weather,temp=seasonal_weather(self.settings,self.now,random.Random(2))
        self.assertIn(weather,SEASONS['가을']['weather_weights']);self.assertTrue(8<=temp<=23)
        self.settings['calendar'].update(show_date=False,show_weekday=False,show_season=False)
        self.assertEqual(calendar_display(self.settings,self.now),{});self.assertIn('2026-10-05',world_context(self.settings,self.now,{},'a'))
    def test_leap_day_policies_future_start_and_hundredth_day(self):
        event=dict(EVENT_DEFAULT,date='2000-02-29')
        self.assertEqual(event_date(event,2025).isoformat(),'2025-02-28')
        event['leap_day']='mar1';self.assertEqual(event_date(event,2025).isoformat(),'2025-03-01')
        event['leap_day']='leap_only';self.assertIsNone(event_date(event,2025));self.assertEqual(event_date(event,2028).day,29)
        self.assertIsNone(event_date(event,1996))
        event.update(kind='milestone',date='2026-01-01',offset_days=100)
        self.assertEqual(event_date(event,2026).isoformat(),'2026-04-10')
    def test_event_visibility_and_year_rollover(self):
        self.settings['calendar_events']={'secret':dict(EVENT_DEFAULT,name='비밀 생일',date='2000-01-01',known_by=['a']), 'public':dict(EVENT_DEFAULT,name='공개 행사',date='2000-01-01',public=True), 'once':dict(EVENT_DEFAULT,name='한번',kind='once',date='2026-01-01',public=True)}
        now=self.now.replace(month=12,day=31)
        self.assertEqual([e['name'] for e in visible_events(self.settings,now,character='b',window=1)],['공개 행사'])
        self.assertEqual(len(visible_events(self.settings,now,character='a',window=1)),2)
        self.assertEqual(len(visible_events(self.settings,now,public_only=True,window=1)),1)
        self.assertNotIn('비밀 생일',world_context(self.settings,now,{},'b'))
    def test_private_and_dm_prompts_do_not_receive_world_calendar(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'a.txt').write_text('character')
            scope=contextvars.ContextVar('calendar-test',default='world')
            ns=functions({'load_character_prompt'},dict(Path=Path,APP_ROOT=root,CHARACTERS={'a':dict(prompt_file='a.txt')},CUSTOM_SETTINGS=self.settings,policy_prompt=policy_prompt,_generation_scope=scope,_generation_feedback=Mock(get=lambda:''),CURRENT_LOG_SCOPE=Mock(get=lambda:'world'),_world_clock=True,now_kst=lambda:self.now,world_context=world_context))
            self.assertIn('[세계 날짜·일정]',ns['load_character_prompt']('a'))
            for value in ('rp','dm'):
                scope.set(value);self.assertNotIn('[세계 날짜·일정]',ns['load_character_prompt']('a'))
    def test_schedule_movement_respects_hunger_busy_closed_zero_weight_and_toggle(self):
        config={'weekly_schedule':[dict(days=['월'],start='09:00',end='12:00',activity='독서',place='도서관')]}
        args=(self.settings,config,self.now,'거실',lambda p:True)
        self.assertEqual(scheduled_destination(*args),'도서관')
        self.assertIsNone(scheduled_destination(*args,busy=True));self.assertIsNone(scheduled_destination(*args,hungry=True))
        self.assertIsNone(scheduled_destination(self.settings,config,self.now,'거실',lambda p:False))
        config['place_weights']={'도서관':0};self.assertIsNone(scheduled_destination(*args))
        config.pop('place_weights');self.settings['features']['place_movement']=False;self.assertIsNone(scheduled_destination(*args))
        self.settings['features']['weekly_schedules']=False;self.assertIsNone(weekly_entry(self.settings,config,self.now))
    def test_schedule_does_not_interrupt_meal_task_sleep_or_conversation(self):
        with tempfile.TemporaryDirectory() as folder:
            engine=LifeEngine(folder);state=dict(hunger=10,fatigue=10,sleeping=False,away=False)
            settings={'features':{'activity_stages':True,'meal_stages':True}}
            def tick(now,busy=False):return engine.tick('a','집',{},state,'휴식',settings,now,10,busy=busy,scheduled_activity='독서')
            self.assertEqual(tick(1000),'독서');self.assertEqual(tick(1001,True),'휴식')
            engine.record('a')['meal']=dict(start=1000,menu='차',label='아침',phase='준비')
            self.assertIn('차',tick(1002));engine.record('a').pop('meal')
            engine.record('a')['task']=dict(start=1000,name='운동');self.assertIn('운동',tick(1003))
            state['sleeping']=True;self.assertEqual(tick(1004),'휴식')
    def test_invalid_calendar_schedule_and_hours_are_korean_validation_errors(self):
        changes=[lambda s:s['calendar'].update(mode='bad'),lambda s:s['calendar'].update(start_date='2026-02-30'),lambda s:s['calendar']['season_profiles']['봄'].update(months=[3,4,5,6]),lambda s:s['calendar']['season_profiles']['봄'].update(weather_weights={'비':float('nan')}),lambda s:s.update(calendar_events={'a':dict(EVENT_DEFAULT,known_by=['missing'])})]
        for change in changes:
            settings=copy.deepcopy(self.settings);change(settings)
            with self.assertRaises(ValueError):validate_calendar(settings)
        with self.assertRaisesRegex(ValueError,'일정 장소'):validate_calendar(self.settings,{'a':{'weekly_schedule':[dict(activity='독서',place='없는곳')]}},{})
        with self.assertRaisesRegex(ValueError,'HH:MM'):validate_calendar(self.settings,{}, {'카페':{'opening_hours':dict(opens='25:00')}})
    def test_remove_character_and_place_clean_calendar_references(self):
        with tempfile.TemporaryDirectory() as folder:
            store=DraftStore(folder);store.data['characters']={'a':{},'b':{'weekly_schedule':[dict(place='카페',activity='독서')]}}
            store.data['places']={'카페':{}};store.data['settings']['calendar_events']={'birthday':dict(EVENT_DEFAULT,known_by=['a'],participants=['a','b'])}
            store.remove_character('a');event=store.data['settings']['calendar_events']['birthday'];self.assertEqual(event['known_by'],[]);self.assertEqual(event['participants'],['b'])
            store.remove_place('카페');self.assertIsNone(store.data['characters']['b']['weekly_schedule'][0]['place'])

class DependencyTests(unittest.TestCase):
    def test_enable_and_disable_include_transitive_requirements(self):
        settings={'features':{'world_simulation':False,'sleep_system':False,'sleep_quality':False}}
        changes=toggle_changes(settings,'sleep_quality',True)
        self.assertEqual(changes,dict(sleep_quality=True,sleep_system=True,world_simulation=True));self.assertFalse(settings['features']['world_simulation'])
        settings['features'].update(changes)
        self.assertFalse(toggle_changes(settings,'world_simulation',False)['sleep_quality'])
    def test_invalid_combinations_block_and_private_autonomous_is_independent(self):
        with self.assertRaisesRegex(ValueError,'본서버 월드'):validate_features({'features':{'world_simulation':False}})
        off={'world_simulation':False,'place_movement':False,'sleep_system':False,'away_system':False,'appointments':False,'dynamic_events':False,'city_events':False,'autonomous_messages':True}
        validate_features({'features':off})
        self.assertNotIn('autonomous_messages',toggle_changes({'features':off},'world_simulation',False))

class BotCalendarIntegrationTests(unittest.TestCase):
    def test_opening_hours_gate_ordinary_entry_and_disabled_flag_restores_it(self):
        now=datetime(2026,10,5,8,tzinfo=timezone.utc)
        settings={'features':{'opening_hours':True}}
        ns=functions({'can_enter_place'},dict(CUSTOM_SETTINGS=settings,PLACE_INFO={'카페':{'opening_hours':dict(enabled=True,days=['월'],opens='09:00',closes='18:00')}},now_kst=lambda:now,place_open=place_open,RESTRICTED_PLACES_BY_CHARACTER={},PLACE_ALLOWED_CHARACTERS={},PRIVATE_ROOMS={},get_physical_parent_place=lambda p:None))
        self.assertFalse(ns['can_enter_place']('a','카페'))
        settings['features']['opening_hours']=False;self.assertTrue(ns['can_enter_place']('a','카페'))
    def test_scheduled_activity_uses_no_ai_and_does_not_override_sleep(self):
        now=datetime(2026,10,5,10,tzinfo=timezone.utc)
        settings={'features':{'weekly_schedules':True}}
        config={'weekly_schedule':[dict(days=['월'],start='09:00',end='12:00',place='도서관',activity='독서')]}
        state={'a':dict(sleeping=False,away=False)}
        ns=functions({'choose_activity'},dict(CHARACTERS={'a':config},CUSTOM_SETTINGS=settings,character_state=state,current_place={'a':'도서관'},current_activity={},_life=Mock(busy=lambda *a:''),load_character_prompt=lambda c:'prompt',get_time_period=lambda:'day',weekly_entry=weekly_entry,now_kst=lambda:now,get_movement_busy_reason=lambda *a:None,clients={}))
        self.assertEqual(ns['choose_activity']('a','도서관'),'독서')
        state['a']['sleeping']=True;self.assertIn('자는 중',ns['choose_activity']('a','도서관'))
    def test_weekly_schedule_survives_character_normalization(self):
        config={'name':'A','weekly_schedule':[dict(activity='독서',days=['월'])]}
        ns=functions({'_normalize_character_config','_normalize_time_range'}, {})
        self.assertEqual(ns['_normalize_character_config']({'a':config})['a']['weekly_schedule'],config['weekly_schedule'])
