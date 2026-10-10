"""One configurable OpenAI-compatible adapter for all existing generation calls."""
import os
import threading
import contextvars
from contextlib import contextmanager

GENERATION_PRIORITY=contextvars.ContextVar("generation_priority",default=10)
from types import SimpleNamespace


class GenerationError(RuntimeError):
    pass


class ModelClient:
    def __init__(self, api_key, client=None, env=None):
        env=os.environ if env is None else env
        api=env.get('RPBOT_API_STYLE','responses') or 'responses'
        if api not in ('responses','chat'): raise ValueError('API 방식은 responses 또는 chat으로 입력하세요.')
        self.api=api
        self.model=env.get('RPBOT_MODEL','').strip()
        limit=int(env.get('RPBOT_GENERATION_CONCURRENCY','4') or '4')
        if not 1<=limit<=8: raise ValueError('동시 생성 수는 1~8로 입력하세요.')
        timeout=float(env.get('RPBOT_API_TIMEOUT','60') or '60')
        if not 5<=timeout<=300: raise ValueError('API 제한 시간은 5~300초로 입력하세요.')
        if client is None:
            from openai import OpenAI
            kwargs=dict(api_key=api_key,timeout=timeout,max_retries=2)
            if env.get('RPBOT_API_BASE','').strip(): kwargs['base_url']=env['RPBOT_API_BASE'].strip()
            client=OpenAI(**kwargs)
        self.client=client
        self.limit=limit
        self.condition=threading.Condition()
        self.waiters=[]
        self.active=0
        self.sequence=0
        self.closing=False
        self.responses=self

    @contextmanager
    def slot(self):
        with self.condition:
            if self.closing: raise GenerationError('종료 중이므로 새 AI 생성을 시작하지 않습니다.')
            self.sequence+=1;ticket=(GENERATION_PRIORITY.get(),self.sequence)
            self.waiters.append(ticket)
            self.condition.notify_all()
            try:
                self.condition.wait_for(lambda:self.closing or self.active<self.limit and ticket==min(self.waiters))
                if self.closing:raise GenerationError('종료 중이므로 대기 중인 AI 생성을 취소합니다.')
                self.active+=1
            finally:
                self.waiters.remove(ticket);self.condition.notify_all()
        try:yield
        finally:
            with self.condition:self.active-=1;self.condition.notify_all()

    def close_requests(self):
        with self.condition:self.closing=True;self.condition.notify_all()

    def create(self, **kwargs):
        kwargs=dict(kwargs)
        if self.model: kwargs['model']=self.model
        with self.slot():
            if self.api=='responses':
                result=self.client.responses.create(**kwargs)
                status=getattr(result,'status','completed')
                if status in ('failed','incomplete','cancelled','queued','in_progress'):
                    raise GenerationError(f'AI 응답 미완료: {status}. 전송·생활 효과를 적용하지 않았습니다.')
                text=getattr(result,'output_text','')
            else:
                messages=[]
                if kwargs.get('instructions'): messages.append(dict(role='system',content=kwargs.pop('instructions')))
                raw=kwargs.pop('input','')
                if isinstance(raw,str): messages.append(dict(role='user',content=raw))
                elif isinstance(raw,list): messages.extend(raw)
                else: raise GenerationError('AI 입력 형식을 확인하세요.')
                if 'max_output_tokens' in kwargs: kwargs['max_tokens']=kwargs.pop('max_output_tokens')
                result=self.client.chat.completions.create(messages=messages,**kwargs)
                choices=getattr(result,'choices',[])
                if not choices or choices[0].finish_reason!='stop':
                    raise GenerationError('AI 응답이 중단되거나 비어 있습니다. 전송·생활 효과를 적용하지 않았습니다.')
                text=choices[0].message.content
            if not isinstance(text,str) or not text.strip(): raise GenerationError('AI가 빈 응답을 반환했습니다. 전송·생활 효과를 적용하지 않았습니다.')
            return SimpleNamespace(output_text=text)
