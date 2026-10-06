"""Non-destructive configuration storage shared by the desktop settings UI."""
import copy
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from dotenv import dotenv_values
from form_help import parse_json


def app_root():
    return Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    try:
        temporary.write_text(text, encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.reload()

    def reload(self):
        self.data = {}
        for name in ('characters', 'places', 'settings', 'relations'):
            path = self.root / 'config' / f'{name}.json'
            value = parse_json(path.read_text(encoding='utf-8-sig'), path.name) if path.exists() else {}
            if not isinstance(value, dict):
                raise ValueError(f'{path.name}: 객체 형식이어야 합니다.')
            self.data[name] = value
        self.env = dict(dotenv_values(self.root / '.env', interpolate=False))

    def safe_path(self, relative):
        path = (self.root / relative).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError('파일 경로는 RPBot 폴더 안이어야 합니다.')
        return path

    def backup(self):
        dest = self.root / 'backup_setup' / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        dest.mkdir(parents=True)
        for name in ('config', 'prompts', 'profiles', '.env', 'desktop_settings.json'):
            source = self.root / name
            if source.is_dir():
                shutil.copytree(source, dest / name)
            elif source.is_file():
                shutil.copy2(source, dest / name)
        return dest

    def write_json(self, name, value):
        self.backup()
        atomic_write(self.root / 'config' / f'{name}.json', json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        self.data[name] = copy.deepcopy(value)

    def write_env(self, changes):
        self.backup()
        path = self.root / '.env'
        lines = path.read_text(encoding='utf-8-sig').splitlines() if path.exists() else []
        remaining = dict(changes)
        result = []
        for line in lines:
            match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
            if match and match[1] in changes:
                key = match[1]
                if key in remaining:
                    result.append(self.env_line(key, remaining.pop(key)))
            else:
                result.append(line)
        result.extend(self.env_line(k, v) for k, v in remaining.items())
        atomic_write(path, '\n'.join(result) + '\n')
        self.env = dict(dotenv_values(path, interpolate=False))

    @staticmethod
    def env_line(key, value):
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            raise ValueError('환경변수 이름이 올바르지 않습니다.')
        value = str(value).replace('\\', '\\\\').replace("'", "\\'")
        if '\n' in value or '\r' in value:
            raise ValueError('키와 토큰에는 줄바꿈을 넣을 수 없습니다.')
        return f"{key}='{value}'"

    def save_prompt(self, relative, text):
        path = self.safe_path(relative)
        if not path.is_relative_to((self.root / 'prompts').resolve()) or path.suffix != '.txt':
            raise ValueError('프롬프트는 prompts 폴더의 .txt 파일로 저장해주세요.')
        self.backup()
        atomic_write(path, text)

    def restore(self, backup):
        backup = Path(backup).resolve()
        if backup.parent != (self.root / 'backup_setup').resolve() or not backup.is_dir():
            raise ValueError('백업 폴더를 선택해주세요.')
        # Validate before any replacement. History/SQLite files are never included.
        for source in (backup / 'config').glob('*.json'):
            parse_json(source.read_text(encoding='utf-8-sig'), source.name)
        self.backup()
        for name in ('config', 'prompts', 'profiles'):
            source = backup / name
            if source.exists():
                for file in source.rglob('*'):
                    if file.is_file() and not file.is_symlink():
                        target = self.safe_path(str(file.relative_to(backup)))
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temporary = target.with_name(target.name + '.tmp')
                        shutil.copy2(file, temporary)
                        os.replace(temporary, target)
        for name in ('.env', 'desktop_settings.json'):
            if (backup / name).is_file():
                atomic_write(self.root / name, (backup / name).read_text(encoding='utf-8-sig'))
        self.reload()

    def remove_character(self, key):
        data = copy.deepcopy(self.data)
        data['characters'].pop(key)
        for pair in list(data['relations']):
            if not pair.startswith('_') and key in pair.split('|'):
                del data['relations'][pair]
        for place in data['places'].values():
            if isinstance(place, dict) and isinstance(place.get('allowed_characters'), list):
                place['allowed_characters'] = [x for x in place['allowed_characters'] if x != key]
        # Token, prompt, and learned history are deliberately retained for recovery.
        self.backup()
        for name in ('characters', 'relations', 'places'):
            atomic_write(self.root / 'config' / f'{name}.json', json.dumps(data[name], ensure_ascii=False, indent=2) + '\n')
        self.data = data

    def remove_place(self, key):
        data = copy.deepcopy(self.data)
        data['places'].pop(key)
        for character in data['characters'].values():
            if isinstance(character, dict):
                character.get('place_weights', {}).pop(key, None)
                character['restricted_places'] = [x for x in character.get('restricted_places', []) if x != key]
                if character.get('private_room') == key:
                    character['private_room'] = None
        for place in data['places'].values():
            if isinstance(place, dict) and place.get('parent') == key:
                place['parent'] = None
        self.backup()
        for name in ('characters', 'places'):
            atomic_write(self.root / 'config' / f'{name}.json', json.dumps(data[name], ensure_ascii=False, indent=2) + '\n')
        self.data = data


class DraftStore(Store):
    """Stage GUI edits in memory; publish only after all forms validate."""
    def __init__(self, root):
        self.pending = {}
        super().__init__(root)

    def stage(self, path, text):
        self.pending[Path(path)] = text.encode('utf-8') if isinstance(text, str) else text

    def write_json(self, name, value):
        self.stage(self.root / 'config' / f'{name}.json', json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        self.data[name] = copy.deepcopy(value)

    def write_env(self, changes):
        path = self.root / '.env'
        raw = self.pending.get(path)
        text = raw.decode('utf-8-sig') if raw is not None else path.read_text(encoding='utf-8-sig') if path.exists() else ''
        remaining = dict(changes); result = []
        for line in text.splitlines():
            match = re.match(r'^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=', line)
            if match and match[1] in changes:
                if match[1] in remaining: result.append(self.env_line(match[1], remaining.pop(match[1])))
            else: result.append(line)
        result.extend(self.env_line(k,v) for k,v in remaining.items())
        self.stage(path, '\n'.join(result) + '\n')
        self.env.update(changes)

    def save_prompt(self, relative, text):
        path = self.safe_path(relative)
        if not path.is_relative_to((self.root/'prompts').resolve()) or path.suffix != '.txt':
            raise ValueError('프롬프트는 prompts 폴더의 .txt 파일로 저장해주세요.')
        self.stage(path, text)

    def remove_character(self, key):
        self.data['characters'].pop(key)
        for pair in list(self.data['relations']):
            if not pair.startswith('_') and key in pair.split('|'): del self.data['relations'][pair]
        for place in self.data['places'].values():
            if isinstance(place,dict) and isinstance(place.get('allowed_characters'),list):
                place['allowed_characters'] = [x for x in place['allowed_characters'] if x != key]
        for name in ('characters','relations','places'): self.write_json(name,self.data[name])

    def remove_place(self, key):
        self.data['places'].pop(key)
        for character in self.data['characters'].values():
            if isinstance(character,dict):
                character.get('place_weights',{}).pop(key,None)
                character['restricted_places'] = [x for x in character.get('restricted_places',[]) if x != key]
                if character.get('private_room') == key: character['private_room'] = None
        for place in self.data['places'].values():
            if isinstance(place,dict) and place.get('parent') == key: place['parent'] = None
        for name in ('characters','places'): self.write_json(name,self.data[name])

    def flush(self):
        if not self.pending: return
        self.backup()
        originals = {p:p.read_bytes() if p.exists() else None for p in self.pending}
        replaced = []
        try:
            for path, content in self.pending.items():
                path.parent.mkdir(parents=True,exist_ok=True)
                temporary = path.with_name(path.name + '.tmp')
                try:
                    temporary.write_bytes(content)
                    os.replace(temporary,path); replaced.append(path)
                finally: temporary.unlink(missing_ok=True)
        except Exception:
            for path in reversed(replaced):
                if originals[path] is None: path.unlink(missing_ok=True)
                else: path.write_bytes(originals[path])
            raise
        self.pending.clear()
