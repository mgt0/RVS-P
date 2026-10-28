#!/usr/bin/env python3
"""서버가 떠 있는지, 무슨 일이 있었는지 한 번에 본다.

    python scripts/server_status.py            # 상태 + 최근 로그 30줄
    python scripts/server_status.py --log 100  # 최근 로그 100줄
    python scripts/server_status.py --errors   # 경고/에러만

서버는 카메라와 시리얼 포트를 잡으므로 한 번에 하나만 뜬다(run.py 의 단일
인스턴스 잠금). 그래서 '누가 띄웠는지' 와 무관하게 이 스크립트로 같은 것을 본다.
터미널 스크롤백은 그 창을 띄운 사람만 볼 수 있지만 logs/server.log 는 둘 다 읽는다.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = PROJECT_ROOT / "logs" / "server.log"


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


def get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=5) as res:
            return json.load(res)
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", type=int, default=30, help="보여줄 로그 줄 수 (기본 30)")
    ap.add_argument("--errors", action="store_true", help="WARNING/ERROR 줄만 본다")
    ap.add_argument("--base", default="http://127.0.0.1:5000", help="대시보드 주소")
    args = ap.parse_args()

    host = args.base.split("//")[-1].split(":")[0]
    port = int(args.base.rsplit(":", 1)[-1])

    running = port_open(host, port)
    print(f"서버: {'실행 중' if running else '꺼져 있음'}  ({args.base})")

    if running:
        meta = get(f"{args.base}/api/meta") or {}
        stream_port = meta.get("stream_port")
        if stream_port:
            ok = port_open(host, int(stream_port))
            print(f"프리뷰 스트림 포트 {stream_port}: {'정상' if ok else '응답 없음'}")

        conv = get(f"{args.base}/api/conveyor/status")
        if conv:
            print("\n컨베이어(DC 모터):")
            for b in conv.get("conveyors", []):
                print(f"  M{b.get('motor')} {b['name']:5} {b.get('label',''):12} "
                      f"port={b.get('port') or '미설정':6} "
                      f"연결={'O' if b.get('connected') else 'X'} "
                      f"구동={'O' if b.get('running') else 'X'}")

        st = get(f"{args.base}/api/status")
        if st:
            cams = st.get("cameras", [])
            live = sum(1 for c in cams if c.get("connected"))
            print(f"\n카메라: {live}/{len(cams)} 연결   "
                  f"녹화: {st.get('recording', {}).get('state', '?')}   "
                  f"텔레오퍼: {'ON' if st.get('teleop', {}).get('running') else 'OFF'}")
    else:
        print("띄우려면:  python run.py")

    if not LOG_PATH.exists():
        print(f"\n로그 파일이 아직 없습니다: {LOG_PATH}")
        return

    lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    if args.errors:
        lines = [ln for ln in lines if " WARNING" in ln or " ERROR" in ln or " CRITICAL" in ln]
        title = "최근 경고/에러"
    else:
        title = "최근 로그"
    print(f"\n--- {title} ({LOG_PATH}) ---")
    for ln in lines[-args.log:]:
        print(" ", ln)
    if not lines:
        print("  (없음)")


if __name__ == "__main__":
    main()
