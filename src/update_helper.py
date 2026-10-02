"""Independent Windows updater, copied outside the binaries it replaces."""
import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from updater import FILES, BotLock


def replace_binaries(root, stage):
    root, stage = Path(root).resolve(), Path(stage).resolve()
    if stage.parent != root or not stage.name.startswith('rpbot-update-'):
        raise ValueError('잘못된 업데이트 경로입니다.')
    lock = BotLock(root)
    if not lock.acquire():
        raise RuntimeError('RPBot이 실행 중입니다. 봇을 종료한 뒤 다시 업데이트해주세요.')
    replaced = []
    backup = stage / 'previous'
    backup.mkdir(exist_ok=True)
    try:
        for name in FILES:
            if not (stage / name).is_file() or (stage / name).read_bytes()[:2] != b'MZ':
                raise ValueError(f'{name}: 검증된 실행 파일이 없습니다.')
        for name in FILES:
            target = root / name
            if target.exists():
                shutil.copy2(target, backup / name)
            os.replace(stage / name, target)
            replaced.append(name)
    except Exception:
        for name in reversed(replaced):
            previous = backup / name
            if previous.exists():
                os.replace(previous, root / name)
            else:
                (root / name).unlink(missing_ok=True)
        raise
    finally:
        lock.release()


def wait_for_parent(pid):
    if os.name != 'nt':
        return
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if handle:
        try:
            if kernel.WaitForSingleObject(handle, 120000) != 0:
                raise RuntimeError('설정 프로그램 종료를 기다리는 중 시간이 초과되었습니다.')
        finally:
            kernel.CloseHandle(handle)
    # Onefile bootloader can briefly retain the old executable after its child exits.
    time.sleep(2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--stage', required=True)
    parser.add_argument('--parent', type=int, required=True)
    args = parser.parse_args()
    try:
        wait_for_parent(args.parent)
        replace_binaries(args.root, args.stage)
        subprocess.Popen([str(Path(args.root) / 'RPBot_Setup.exe')], cwd=args.root)
    except Exception as error:
        if os.name == 'nt':
            ctypes.windll.user32.MessageBoxW(None, str(error), 'RPBot 업데이트 실패', 0x10)
        else:
            print(error, file=sys.stderr)
        return 1
    # Keep previous binaries in the staging folder as a recovery copy.
    return 0


if __name__ == '__main__':
    sys.exit(main())
