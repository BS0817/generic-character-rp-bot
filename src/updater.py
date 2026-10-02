"""Release discovery and verified staging. Only application binaries are updated."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path
from app_version import VERSION, REPOSITORY

ASSET = 'Generic-Character-RP-Bot-Windows.zip'
FILES = ('RPBot.exe', 'RPBot_Setup.exe', 'RPBot_Updater.exe')
MAX_DOWNLOAD = 600 * 1024 * 1024


def version_tuple(value):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)', value)
    if not match:
        raise ValueError('정식 버전 형식이 아닙니다.')
    return tuple(map(int, match.groups()))


def request(url):
    if not url.startswith('https://'):
        raise ValueError('HTTPS 다운로드만 허용합니다.')
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'RPBot/' + VERSION}), timeout=30)


def latest_release():
    with request(f'https://api.github.com/repos/{REPOSITORY}/releases/latest') as response:
        release = json.loads(response.read(2 * 1024 * 1024))
    if release.get('draft') or release.get('prerelease'):
        return None
    if version_tuple(release['tag_name']) <= version_tuple(VERSION):
        return None
    assets = {asset['name']: asset for asset in release.get('assets', [])}
    if ASSET not in assets or ASSET + '.sha256' not in assets:
        raise ValueError('이 릴리스에는 자동 업데이트용 검증 파일이 없습니다.')
    return {'version': release['tag_name'], 'notes': release.get('body') or '변경 사항이 등록되지 않았습니다.',
            'url': assets[ASSET]['browser_download_url'], 'checksum_url': assets[ASSET + '.sha256']['browser_download_url']}


def stage_release(release, root):
    root = Path(root).resolve()
    with request(release['checksum_url']) as response:
        digest = response.read(1024).decode('ascii').split()[0]
    if not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
        raise ValueError('검증 파일 형식이 올바르지 않습니다.')
    stage = Path(tempfile.mkdtemp(prefix='rpbot-update-', dir=root))
    try:
        archive = stage / 'release.zip'
        hasher = hashlib.sha256()
        size = 0
        with request(release['url']) as response, archive.open('wb') as output:
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_DOWNLOAD:
                    raise ValueError('업데이트 파일이 허용 크기를 초과했습니다.')
                output.write(chunk)
                hasher.update(chunk)
        if hasher.hexdigest().lower() != digest.lower():
            raise ValueError('다운로드 검증에 실패했습니다. 기존 프로그램을 유지합니다.')
        with zipfile.ZipFile(archive) as package:
            for name in FILES:
                matches = [i for i in package.infolist() if i.filename == name]
                if len(matches) != 1 or not 0 < matches[0].file_size < MAX_DOWNLOAD:
                    raise ValueError(f'업데이트 파일에 {name}이 없거나 잘못되었습니다.')
                with package.open(matches[0]) as source, (stage / name).open('wb') as target:
                    shutil.copyfileobj(source, target)
                if (stage / name).read_bytes()[:2] != b'MZ':
                    raise ValueError(f'{name}: Windows 실행 파일이 아닙니다.')
        archive.unlink()
        (stage / 'version.txt').write_text(release['version'], encoding='utf-8')
        return stage
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def launch_update(stage, root):
    if not getattr(sys, 'frozen', False) or os.name != 'nt':
        raise ValueError('자동 설치는 Windows EXE 배포본에서 사용할 수 있습니다.')
    helper = Path(stage) / 'install-helper.exe'
    shutil.copy2(Path(stage) / 'RPBot_Updater.exe', helper)
    subprocess.Popen([str(helper), '--root', str(root), '--stage', str(stage), '--parent', str(os.getpid())], cwd=str(stage))


class BotLock:
    """Held for the lifetime of the bot; updater acquires the same OS lock."""
    def __init__(self, root, filename='.rpbot-running.lock'):
        self.path = Path(root) / filename
        self.stream = None

    def acquire(self):
        self.stream = self.path.open('a+b')
        self.stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            if os.fstat(self.stream.fileno()).st_size == 0:
                self.stream.write(b'0')
                self.stream.flush()
            self.stream.seek(0)
            try:
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                self.stream.close()
                self.stream = None
                return False
        else:
            import fcntl
            try:
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                self.stream.close()
                self.stream = None
                return False
        return True

    def release(self):
        if self.stream:
            self.stream.close()
            self.stream = None


def bot_running(root):
    if os.name == 'nt':
        # Also detect pre-GUI RPBot releases which do not hold our lock.
        import csv
        result = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq RPBot.exe', '/FO', 'CSV', '/NH'],
                                capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
        if result.returncode != 0:
            raise RuntimeError('실행 중인 봇을 확인할 수 없습니다. 잠시 후 다시 시도해주세요.')
        if any(row and row[0].lower() == 'rpbot.exe' for row in csv.reader(result.stdout.splitlines())):
            return True
    lock = BotLock(root)
    acquired = lock.acquire()
    lock.release()
    return not acquired


def startup_notice():
    try:
        release = latest_release()
        if release:
            print(f"[RPBot] 새 버전 {release['version']}이 있습니다. RPBot_Setup.exe의 업데이트 메뉴에서 설치하세요.")
    except Exception:
        pass  # Network failure must never prevent bot startup.
