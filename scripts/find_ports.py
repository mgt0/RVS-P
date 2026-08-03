#!/usr/bin/env python3
"""CLI 포트 식별 도구 (lerobot-find-port 대응).

대시보드를 띄우기 전에 리더/팔로워의 시리얼 넘버를 알아내
config.yaml 에 박아두기 위한 스크립트다. 한 번만 실행하면
이후로는 /dev 번호가 바뀌어도 포트가 뒤바뀌지 않는다.

사용법:
    python scripts/find_ports.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.core.port_finder import scan_serial_ports  # noqa: E402


def identify(role: str) -> None:
    before = {p.device: p for p in scan_serial_ports()}
    if not before:
        print("감지된 시리얼 포트가 없습니다. USB 와 전원을 확인하세요.")
        return

    print(f"\n[{role.upper()}] 현재 포트: {', '.join(before)}")
    input(f"→ {role.upper()} arm 의 USB 를 뽑고 Enter 를 누르세요...")
    time.sleep(0.5)

    after = {p.device for p in scan_serial_ports()}
    removed = set(before) - after

    if len(removed) != 1:
        print(f"  ✗ 특정 실패 (사라진 포트 {len(removed)}개). 하나씩 다시 시도하세요.")
        return

    device = removed.pop()
    info = before[device]
    print(f"  ✓ {role.upper()} = {device}")
    print(f"    serial_number: {info.serial_number or '(없음 — port_hint 를 쓰세요)'}")
    print(f"    → config.yaml 의 arms.{role}.serial_number 에 위 값을 넣으세요.")
    input("  USB 를 다시 연결하고 Enter...")


if __name__ == "__main__":
    print("=" * 56)
    print(" OMX PORT IDENTIFICATION")
    print("=" * 56)
    for role in ("leader", "follower"):
        identify(role)
    print("\n완료. config.yaml 을 수정한 뒤 서버를 재시작하세요.")
