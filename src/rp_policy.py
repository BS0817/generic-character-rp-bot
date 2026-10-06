"""Optional, world-independent roleplay policy and beginner prompt builder."""
import re
from difflib import SequenceMatcher

FEATURES = [
    ('common_prompt', '최우선 공통 프롬프트', True),
    ('dialogue_guard', '대화 반복·종료 검사', True),
    ('relationship_guard', '고정 관계에 맞는 행동 검사', True),
    ('conversation_mode', '대화 방식 설정 반영', True),
    ('outfit_preferences', '복장 취향 반영', False),
    ('seating', '좌석·동석자 관리', False),
    ('meal_stages', '식사 단계·메뉴 유지', False),
    ('naps', '낮잠', False),
    ('sleep_quality', '수면 품질·방해에 따른 회복', False),
    ('item_loans', '물건 대여·반환 기록', False),
    ('activity_stages', '단계별 장소 활동', False),
    ('outfit_condition', '젖은 복장·건조 상태', False),
]
DEFAULT_COMMON_PROMPT = '''[최우선 공통 행동·대화 규칙]
이 규칙은 세계 규칙·캐릭터 설정의 충돌하는 일반 지시보다 우선한다. 캐릭터 고유의 성격과 말투는 유지한다.
최근 발언과 의미·표현이 같은 내용을 불필요하게 반복하지 않는다. 말버릇과 필요한 강조는 허용한다.
단순 동의나 맞장구만으로 대화를 계속 연장하지 않는다. 끝난 주제는 새 용건 없이 다시 꺼내지 않는다.
상투적인 허락 질문 대신 구체적인 용건·관찰로 대화를 시작한다.
이미 완료한 주문·식사·정리·물건 전달을 다시 시작하지 않는다.
명시된 현재 상태·복장·소지품·장소·진행 중 행동과 모순되는 묘사를 하지 않는다.
원래 적대·불신 관계를 몇 번의 잡담이나 친절만으로 해소하지 않는다. 필요한 협력·반환은 가능하다.
직접 알거나 전달받은 정보만 활용한다. 전달받은 주장은 검증된 사실로 단정하지 않는다.
다른 장면의 기억·비밀을 섞지 않는다. 꿈은 현실에서 발생한 확정 사건이 아니다.
상대의 생각·대답·감정·행동을 임의로 확정하지 않는다.
사용자의 직접 질문에는 답하되 자율발언에서는 새 내용이나 행동이 없으면 발언하지 않아도 된다.'''
GUIDE_FIELDS = {
    'identity': ('정체성·배경', '예: 작은 서점의 주인. 과거 사건은 확정된 내용만 적으세요.'),
    'personality': ('핵심 성격', '예: 신중함, 호기심, 낯가림. 3~5개 특징과 행동 예를 적으세요.'),
    'speech': ('말투', '존댓말/반말, 문장 길이, 호칭, 자주 쓰거나 피하는 표현'),
    'examples': ('대화 예시', '인사·기쁨·분노·거절 상황에서 할 법한 대사'),
    'appearance': ('외형', '외모와 기본 복장. 확정할 수 있는 정보만 적으세요.'),
    'likes': ('좋아함·싫어함', '취미, 음식, 가치관 등'),
    'relationships': ('관계별 행동', '낯선 사람·친구·적대 상대에게 보이는 태도'),
    'important': ('중요한 설정', '반드시 유지할 사실과 행동 원칙'),
    'avoid': ('금지할 묘사', '캐릭터에게 어울리지 않는 행동·표현'),
    'extra': ('추가 설명', '위 항목에 없는 설정'),
}

def build_prompt(name, fields):
    return '\n\n'.join([f'[이름]\n{name}'] + [f'[{label}]\n{str(fields.get(key, "")).strip()}' for key, (label, _) in GUIDE_FIELDS.items() if str(fields.get(key, '')).strip()])

def enabled(settings, key):
    value = settings.get('features', {}).get(key, dict((k,d) for k,_,d in FEATURES).get(key, True))
    return value.strip().lower() not in ('false','off','0','no') if isinstance(value,str) else bool(value)

def mode_rule(settings, scope='world'):
    if not enabled(settings, 'conversation_mode'): return ''
    mode = settings.get('conversation_modes', {}).get(scope, 'inherit')
    if mode == 'inherit': mode = settings.get('conversation_modes', {}).get('world', 'custom')
    if mode == 'scene': return '[대화 방식]\n같은 장소에서 직접 말하고 행동하는 장면으로 다룬다. Discord·채널·알림을 작중에서 묘사하지 않는다.'
    if mode == 'messenger': return '[대화 방식]\n메신저로 연락하는 상황이다. 메시지·답장·접속을 자연스럽게 인식한다. 합의된 대면 장면 없이 상대와 물리적으로 접촉하지 않는다.'
    return '[대화 방식]\n' + str(settings.get('custom_conversation_rule', '캐릭터·장면에 지정된 대화 방식을 따른다.'))

def policy_prompt(settings, config, scope='world'):
    parts = []
    if enabled(settings,'common_prompt'): parts.append(settings.get('common_prompt', DEFAULT_COMMON_PROMPT))
    parts.append(mode_rule(settings,scope))
    if enabled(settings,'outfit_preferences') and config.get('outfit_preferences_enabled',False):
        parts.append('[복장 취향]\n취향은 소유·현재 착용 사실이 아니다. 명시된 현재 복장을 우선하며 비선호와 싫어함을 구분한다.\n' + '\n'.join(f'{label}: {config.get(key, "")}' for key,label in [('preferred_style','선호 스타일'),('preferred_colors','선호 색·소재'),('disliked_outfits','별로 좋아하지 않음'),('hated_outfits','싫어함'),('outfit_notes','추가 설명')]))
    return '\n\n'.join(p for p in parts if p)

def normalize(text):
    return re.sub(r'\W+', '', text).lower()

def dialogue_reason(text, history):
    text = re.sub(r'^ACTION\|.*$', '', text, flags=re.M).strip()
    norm = normalize(text)
    recent = [normalize(str(t).split(':',1)[-1]) for t in history[-6:]]
    if norm and any(norm == old or (len(norm)>18 and SequenceMatcher(None,norm,old).ratio()>.88) for old in recent): return '최근 발언 반복'
    if re.fullmatch(r'(그래|좋아|맞아|알겠어|응|그렇군|그러네)[.!…\s]*',text) and sum(any(w in t for w in ('그래','좋아','맞아','알겠어')) for t in recent)>=2: return '단순 동의 연쇄'
    if any(w in text for w in ('잠깐 말 걸어도','말 걸어도 괜찮','이야기해도 괜찮')): return '상투적인 허락 질문'
    return ''

def relationship_reason(text, relation):
    if not any(w in relation for w in ('적대','불신','암살','원수')): return ''
    if any(w in text for w in ('돌려','반환','반납')): return ''
    if any(w in text for w in ('담요를 덮어','외투를 덮어','다정하게','애정 어린','손을 잡아','품에 안')): return '고정 관계와 충돌하는 돌봄·친밀 행동'
    return ''

def completed_transfer(text):
    """Require a completed action, rather than an offer or a proposed future action."""
    if any(w in text for w in ('줄까','줄게','건넬까','빌려줄까','건네려','돌려줄까')): return False
    return bool(re.search(r'건네(?:준다|주었다|줬|고|며|었다)|건넨다|내민다|내밀었다|돌려(?:준다|주었다|줬)|반환(?:한다|했다)|빌려(?:준다|주었다|줬)|\b(?:gives|hands|passes|returns|lends)\b',text,re.I))
