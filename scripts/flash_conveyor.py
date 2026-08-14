#!/usr/bin/env python3
"""컨베이어 보드(BlackPill) 펌웨어를 빌드해서 올린다. BOOT0 버튼 없이.

    python scripts/flash_conveyor.py            # 빌드 + 업로드
    python scripts/flash_conveyor.py --build    # 빌드만

절차는 세 가지 걸림돌을 자동으로 넘는다. 손으로 하면 매번 여기서 막힌다.

1) 서버가 COM 포트를 잡고 있으면 업로드가 안 된다 -> /api/conveyor/release 를 먼저 부른다
   (서버가 안 떠 있으면 그냥 건너뛴다).
2) 보드를 DFU 로 넣어야 한다 -> 1200bps 로 포트를 열었다 닫는다. 펌웨어가 이 신호를
   보고 스스로 부트로더로 넘어간다(conveyor_blackpill.ino 의 DFU_TOUCH_BAUD).
   그 기능이 없는 펌웨어가 올라가 있으면 BOOT0+NRST 를 눌러야 한다 — 그때는 안내만 하고 기다린다.
3) Arduino 에 딸린 dfu-util 은 0.1 이라 STM32 내장 DFU 를 못 굽고, CubeProgrammer 는
   한글 경로("바탕 화면")를 못 읽는다 -> CubeProgrammer 를 쓰되 .bin 을 임시 ASCII
   경로로 복사해서 넘긴다.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SKETCH = PROJECT_ROOT / "firmware" / "conveyor_blackpill"
FQBN = "STMicroelectronics:stm32:GenF4:pnum=BLACKPILL_F411CE,usb=CDCgen"
BUILD_BIN = SKETCH / "build" / "STMicroelectronics.stm32.GenF4" / "conveyor_blackpill.ino.bin"

DFU_TOUCH_BAUD = 1200          # 펌웨어의 DFU_TOUCH_BAUD 와 같아야 한다
FLASH_ADDR = "0x08000000"


def _run(cmd: list[str]) -> str:
    """외부 도구 실행. stdout+stderr 를 한 문자열로 돌려준다.

    CubeProgrammer 는 진행률 막대에 콘솔 제어문자를 섞어 보내서 디코딩이 깨지기
    쉽다. errors="replace" 로 받고, 캡처가 비는 경우까지 감안해 빈 문자열로 맞춘다.
    """
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    return (res.stdout or "") + (res.stderr or "")


def _find(*globs: str) -> Path | None:
    """설치 위치가 버전마다 달라지므로 glob 로 찾는다."""
    for pattern in globs:
        root, _, rest = pattern.partition("*")
        base = Path(root)
        if not base.exists():
            continue
        hits = sorted(base.glob("*" + rest)) if rest else sorted(base.glob("*"))
        for hit in hits:
            if hit.is_file():
                return hit
    return None


def find_arduino_cli() -> Path:
    local = Path.home() / "AppData/Local/Programs/Arduino IDE/resources/app/lib/backend/resources/arduino-cli.exe"
    if local.exists():
        return local
    found = shutil.which("arduino-cli")
    if found:
        return Path(found)
    sys.exit("arduino-cli 를 찾지 못했습니다. Arduino IDE 를 설치하거나 PATH 에 추가하세요.")


def find_programmer() -> Path:
    """STM32CubeProgrammer CLI. STM32CubeIDE 안에 딸려 오는 것도 그대로 쓴다."""
    for base in (Path("C:/ST"), Path("C:/Program Files/STMicroelectronics"),
                 Path("C:/Program Files (x86)/STMicroelectronics")):
        if not base.exists():
            continue
        hits = sorted(base.rglob("STM32_Programmer_CLI.exe"))
        if hits:
            return hits[-1]
    sys.exit(
        "STM32_Programmer_CLI.exe 를 찾지 못했습니다.\n"
        "STM32CubeProgrammer 또는 STM32CubeIDE 를 설치하세요.\n"
        "(Arduino 에 딸린 dfu-util 0.1 은 STM32 내장 DFU 를 굽지 못합니다)"
    )


def build() -> Path:
    cli = find_arduino_cli()
    print("빌드 중...")
    res = subprocess.run(
        [str(cli), "compile", "-b", FQBN, "--export-binaries", str(SKETCH)],
        capture_output=True, text=True,
    )
    print(res.stdout.strip()[-500:] or res.stderr.strip()[-500:])
    if res.returncode != 0:
        sys.exit("빌드 실패")
    if not BUILD_BIN.exists():
        sys.exit(f"빌드 결과물이 없습니다: {BUILD_BIN}")
    return BUILD_BIN


def release_port(base_url: str) -> None:
    """서버가 포트를 잡고 있으면 놓아준다. 서버가 없으면 조용히 넘어간다."""
    try:
        req = urllib.request.Request(
            f"{base_url}/api/conveyor/status", method="GET")
        belts = json.load(urllib.request.urlopen(req, timeout=3))["conveyors"]
    except Exception:
        print("서버가 떠 있지 않습니다 — 포트 해제는 건너뜁니다.")
        return
    name = belts[0]["name"] if belts else None
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"{base_url}/api/conveyor/{name}/release", data=b"{}",
            headers={"Content-Type": "application/json"}), timeout=10)
        print(f"서버가 COM 포트를 놓았습니다 ({name} 경유).")
    except Exception as exc:
        print(f"포트 해제 실패(계속 진행): {exc}")


def dfu_touch(port: str) -> None:
    """1200bps 로 열었다 닫아 보드를 부트로더로 보낸다."""
    try:
        import serial
    except ImportError:
        print("pyserial 이 없어 1200bps 터치를 건너뜁니다.")
        return
    try:
        ser = serial.Serial(port, DFU_TOUCH_BAUD, timeout=0.5)
        time.sleep(0.3)
        ser.close()
        print(f"{port} 에 1200bps 터치 — 보드가 DFU 로 넘어갑니다.")
    except Exception as exc:
        print(f"1200bps 터치 실패({exc}). BOOT0+NRST 로 직접 DFU 에 넣어주세요.")


def wait_for_dfu(programmer: Path, timeout_sec: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout_sec
    announced = False
    while time.monotonic() < deadline:
        if "No STM32 device in DFU mode" not in _run([str(programmer), "-l", "usb"]):
            return True
        if not announced:
            print("DFU 장치를 기다리는 중... (안 잡히면 BOOT0 누른 채 NRST)")
            announced = True
        time.sleep(2)
    return False


def flash(programmer: Path, binary: Path) -> None:
    # CubeProgrammer 가 한글 경로를 못 읽는다. ASCII 임시 경로로 복사해서 넘긴다.
    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "conveyor.bin"
        shutil.copy2(binary, staged)
        cmd = [str(programmer), "-c", "port=usb1",
               "-w", str(staged), FLASH_ADDR, "-v", "-g", FLASH_ADDR]
        if "Download verified successfully" in _run(cmd):
            print("업로드 완료 — 검증 통과, 보드 재시작됨.")
            return
        # 한 번은 실측으로 Sector[0] 다운로드가 깨졌다. 전체 소거 후 재시도로 복구됐다.
        print("업로드 실패. 전체 소거 후 재시도합니다.")
        _run([str(programmer), "-c", "port=usb1", "-e", "all"])
        out = _run(cmd)
        if "Download verified successfully" in out:
            print("업로드 완료(전체 소거 후) — 검증 통과.")
            return
        print(out[-1500:])
        sys.exit("업로드 실패")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--build", action="store_true", help="빌드만 하고 끝낸다")
    ap.add_argument("--port", default="COM13", help="보드의 COM 포트 (기본 COM13)")
    ap.add_argument("--server", default="http://127.0.0.1:5000", help="대시보드 주소")
    args = ap.parse_args()

    binary = build()
    if args.build:
        print(f"빌드 산출물: {binary}")
        return

    programmer = find_programmer()
    release_port(args.server)
    dfu_touch(args.port)
    time.sleep(2)
    if not wait_for_dfu(programmer):
        sys.exit("DFU 장치가 나타나지 않았습니다. BOOT0 누른 채 NRST 를 누르고 다시 실행하세요.")
    flash(programmer, binary)


if __name__ == "__main__":
    main()
