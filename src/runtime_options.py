"""User-facing runtime knobs and validation shared with the bot."""
OPTIONS = {
    'MAX_BOT_CHAIN': ('캐릭터 자동 대화 최대 턴', 8, 1, 50),
    'AUTONOMOUS_CHARACTER_COOLDOWN_MIN': ('자율발언 최소 간격 (분)', 90, 1, 1440),
    'AUTONOMOUS_CHARACTER_COOLDOWN_MAX': ('자율발언 최대 간격 (분)', 150, 1, 1440),
    'RP_AUTONOMOUS_CHECK_MIN_MINUTES': ('개인 RP 자율 확인 최소 간격 (분)', 30, 1, 1440),
    'RP_AUTONOMOUS_CHECK_MAX_MINUTES': ('개인 RP 자율 확인 최대 간격 (분)', 90, 1, 1440),
    'RP_AUTONOMOUS_USER_MENTION_CHANCE': ('개인 RP 사용자 말걸기 확률 (0~1)', 0.18, 0., 1.),
    'RP_AUTONOMOUS_CHARACTER_START_CHANCE': ('개인 RP 캐릭터 대화 시작 확률 (0~1)', 0.55, 0., 1.),
}


def validate_options(raw):
    result = {}
    for key, (label, default, minimum, maximum) in OPTIONS.items():
        value = raw.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int,float)) or not minimum <= value <= maximum:
            raise ValueError(f'{label}: {minimum}~{maximum} 범위로 입력해주세요.')
        if isinstance(default,int) and int(value) != value:
            raise ValueError(f'{label}: 정수를 입력해주세요.')
        result[key] = int(value) if isinstance(default,int) else float(value)
    for low,high in [('AUTONOMOUS_CHARACTER_COOLDOWN_MIN','AUTONOMOUS_CHARACTER_COOLDOWN_MAX'),('RP_AUTONOMOUS_CHECK_MIN_MINUTES','RP_AUTONOMOUS_CHECK_MAX_MINUTES')]:
        if result[low] > result[high]: raise ValueError('최소 간격은 최대 간격보다 클 수 없습니다.')
    return result
