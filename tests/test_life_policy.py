import ast
import asyncio
import contextvars
import functools
import inspect
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from rp_policy import FEATURES, policy_prompt, mode_rule, build_prompt, dialogue_reason, relationship_reason, completed_transfer
from life_engine import LifeEngine
from desktop_monitor import Monitor
from test_rp_extensions import functions

class LifeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.engine=LifeEngine(self.temp.name,rng=random.Random(1))
        self.settings={'features':{key:True for key,_,_ in FEATURES}}
        self.state={'hunger':80,'fatigue':85,'sleeping':False,'away':False,'outfit_set':True}
        self.place={'seats':{'소파':1},'menus':{'전체':['차','토스트']},'nap_allowed':True}
    def tick(self,t,place='카페',definition=None,busy=False,night=False):
        return self.engine.tick('a',place,definition or self.place,self.state,'독서 중',self.settings,t,12,busy=busy,scheduled_sleep=night)
    def test_meal_only_consumption_reduces_hunger_and_finishes(self):
        self.settings['features']['naps']=False
        self.tick(100000); menu=self.engine.record('a')['meal']['menu']; h=self.state['hunger']
        self.tick(100060);self.assertEqual(self.state['hunger'],h)
        self.tick(100360);self.assertLess(self.state['hunger'],h)
        self.assertEqual(menu,self.engine.record('a')['meal']['menu'])
        self.tick(100960);self.assertNotIn('meal',self.engine.record('a'));self.assertIn('finished_meal_at',self.engine.record('a'))
        self.assertFalse(self.engine.busy('a',self.settings))
    def test_seat_capacity_and_release(self):
        self.settings['features']['meal_stages']=False;self.settings['features']['naps']=False
        self.tick(100000)
        b={'hunger':0,'fatigue':0}
        self.engine.tick('b','카페',self.place,b,'',self.settings,100000,12)
        self.assertEqual(self.engine.record('a')['seat'],'소파');self.assertNotIn('seat',self.engine.record('b'))
        self.tick(100060,'거리',{'seats':{}});self.assertNotIn('seat',self.engine.record('a'))
        self.engine.tick('b','카페',self.place,b,'',self.settings,100120,12)
        self.assertEqual(self.engine.record('b')['seat'],'소파')
    def test_nap_interrupt_recovers_once_and_night_is_blocked(self):
        self.settings['features']['meal_stages']=False
        self.tick(100000);self.assertTrue(self.state['sleeping']);self.assertIn('nap',self.engine.record('a'))
        self.state['sleeping']=False;self.tick(100600,busy=True)
        fatigue=self.state['fatigue'];self.assertLess(fatigue,85)
        self.tick(100660,busy=True);self.assertEqual(fatigue,self.state['fatigue'])
        self.tick(200000,night=True);self.assertNotIn('nap',self.engine.record('a'))
    def test_flags_cancel_stages_and_hide_context(self):
        self.tick(100000)
        self.settings['features']={key:False for key,_,_ in FEATURES}
        self.tick(100060)
        self.assertFalse(self.engine.context('a',self.settings));self.assertFalse(self.engine.busy('a',self.settings))
        self.assertNotIn('meal',self.engine.record('a'));self.assertNotIn('seat',self.engine.record('a'))
    def test_loans_return_and_reload(self):
        self.engine.transfer('우산','a','b',True)
        loaded=LifeEngine(self.temp.name);self.assertIn('a:우산',loaded.loans)
        loaded.transfer('우산','b','a');self.assertFalse(loaded.loans)
    def test_sleep_quality_formula_and_disable(self):
        self.settings['features']['actual_sleep']=False
        self.engine.rng=Mock();self.engine.rng.choices.return_value=['악몽']
        self.state['sleep_interruptions']=2
        self.engine.wake('a',self.state,self.settings);self.assertEqual(self.state['fatigue'],50)
        self.settings['features']['sleep_quality']=False
        self.engine.wake('a',self.state,self.settings);self.assertEqual(self.state['fatigue'],50)
    def test_task_blocks_move_and_ends(self):
        self.state['hunger']=0;self.state['fatigue']=10
        definition={'activities':['훈련']}
        self.tick(100000,definition=definition);self.assertEqual(self.engine.busy('a',self.settings),'활동 진행 중')
        self.tick(100660,definition=definition);self.assertGreater(self.state['fatigue'],10)
        self.tick(101860,definition=definition);self.assertFalse(self.engine.busy('a',self.settings))
    def test_wet_clothes_only_when_defined(self):
        definition={'outdoor':True};self.state['hunger']=0;self.state['fatigue']=0
        self.engine.tick('a','밖',definition,self.state,'',self.settings,100000,12,weather='비')
        self.assertEqual(self.engine.record('a')['outfit_condition'],'젖음')
        self.engine.tick('a','실내',{},self.state,'',self.settings,100100,12)
        self.engine.tick('a','실내',{},self.state,'',self.settings,102000,12)
        self.assertEqual(self.engine.record('a')['outfit_condition'],'건조됨')

class PolicyTests(unittest.TestCase):
    def test_no_discord_assumption_in_common_and_scope_mode(self):
        from rp_policy import DEFAULT_COMMON_PROMPT
        self.assertNotIn('Discord',DEFAULT_COMMON_PROMPT)
        settings={'conversation_modes':{'world':'scene','dm':'messenger','rp':'inherit'}}
        self.assertIn('같은 장소',mode_rule(settings,'rp'));self.assertIn('메신저',mode_rule(settings,'dm'))
        settings['features']={'conversation_mode':False};self.assertEqual(mode_rule(settings,'dm'),'')
    def test_outfit_preferences_require_both_switches(self):
        settings={'features':{'outfit_preferences':True}}
        config={'outfit_preferences_enabled':True,'preferred_style':'취향테스트'}
        self.assertIn('취향테스트',policy_prompt(settings,config))
        config['outfit_preferences_enabled']=False;self.assertNotIn('취향테스트',policy_prompt(settings,config))
        config['outfit_preferences_enabled']=True;settings['features']['outfit_preferences']=False
        self.assertNotIn('취향테스트',policy_prompt(settings,config))
    def test_guards_and_return_exception(self):
        self.assertTrue(dialogue_reason('좋아.',['a: 그래.','b: 맞아.']))
        self.assertFalse(dialogue_reason('좋아.',['a: 처음 해보자']))
        self.assertTrue(dialogue_reason('이미 한 문장입니다',['a: 이미 한 문장입니다']))
        self.assertTrue(relationship_reason('담요를 덮어 준다','과거 암살 사건으로 불신'))
        self.assertFalse(relationship_reason('담요를 돌려준다','과거 암살 사건으로 불신'))
    def test_transfer_offer_is_not_a_completed_loan(self):
        self.assertFalse(completed_transfer('우산을 빌려줄까?'))
        self.assertFalse(completed_transfer('(우산을 건네려다 멈춘다.)'))
        self.assertTrue(completed_transfer('(우산을 건네준다.)'))
        self.assertTrue(completed_transfer('(우산을 돌려준다.)'))

    def test_builder_uses_only_input_no_placeholder(self):
        prompt=build_prompt('테스트',{'speech':'반말'})
        self.assertIn('반말',prompt);self.assertNotIn('예:',prompt);self.assertNotIn('[외형]',prompt)
    def test_checked_generation_retry_limit_and_scope_reset(self):
        from collections import defaultdict
        log=contextvars.ContextVar('log',default='dm');scope=contextvars.ContextVar('scope',default='');feedback=contextvars.ContextVar('feedback',default='')
        monitor=Mock(); ns=functions({'_checked_generation'},dict(inspect=inspect,functools=functools,_generation_scope=scope,_generation_feedback=feedback,CURRENT_LOG_SCOPE=log,CURRENT_LOG_GUILD_ID=Mock(get=lambda:None),_monitor=monitor,feature_enabled=lambda k:True,dialogue_reason=dialogue_reason,relationship_reason=relationship_reason,CONFIG_RELATIONSHIPS={},recent_autonomous_messages=defaultdict(list),_autonomous_history_key=lambda *a:tuple(a)))
        calls=[]
        def generate(character,history):
            calls.append((scope.get(),feedback.get()));return '동일 문장',None
        output=ns['_checked_generation'](generate)('a',['a: 동일 문장'])
        self.assertEqual(output,('',None));self.assertEqual(len(calls),2);self.assertEqual(calls[0][0],'dm');self.assertTrue(calls[1][1]);self.assertEqual(scope.get(),'');self.assertEqual(feedback.get(),'')
