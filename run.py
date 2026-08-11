#!/usr/bin/env python3
"""개발용 실행 진입점.

주의: Flask 개발 서버는 MJPEG 스트림 5개 + 폴링을 감당하기 빠듯합니다.
threaded=True 로 띄우되, 실제 데모/수집에는 scripts/serve.sh 의
gunicorn+gevent 구성을 사용하세요.
"""

# Windows 한정: 이 서버는 기동 시 cv2/pandas(카메라·저장 모듈)를 먼저 로드하는데,
# rollout 사용 시점에 lerobot_bridge.py가 나중에 torch를 import하면 서로 다른
# 버전의 동명 런타임 DLL(OpenMP 계열)을 cv2/pandas가 먼저 점유해버려서 torch DLL
# 초기화가 WinError 1114로 깨진다(scripts/to_lerobot.py 상단 주석과 동일 원인).
# 프로세스 시작 시점에 아무것도 안 하고 먼저 import만 해두면 슬롯을 선점해 이후
# cv2/pandas/lerobot 전부 정상 동작한다. torch 미설치 환경(=rollout 안 쓰는 경우)
# 에서는 조용히 넘어간다.
try:
    import torch  # noqa: F401
except ImportError:
    pass

import socket
import sys

from backend.app import create_app
from backend.config import load_config


def _abort_if_already_running(port: int) -> None:
    """하드웨어를 잡기 전에 다른 인스턴스가 떠 있는지 확인한다.

    create_app() 은 카메라 5대와 시리얼 포트를 먼저 열고 나서야 app.run() 이
    포트를 바인딩한다. 그래서 실수로 두 번 띄우면 두 번째 인스턴스가 카메라와
    COM 포트를 전부 뺏은 뒤에 "주소가 이미 사용 중" 으로 죽는다. 그 사이
    첫 번째 인스턴스는 장치를 잃고, 증상은 "카메라가 안 잡힌다 / 텔레오퍼가 안 된다 /
    컨베이어가 응답 없다" 로 나타나 원인을 찾기 어렵다(실측으로 세 증상 모두 이것이었다).

    확인 방법은 bind 가 아니라 connect 다. Windows 는 SO_EXCLUSIVEADDRUSE 를
    지정하지 않으면 같은 주소·포트에 두 번 bind 하는 것을 허용해서(실측),
    bind 성공 여부로는 중복 실행을 판별할 수 없다. 연결이 되면 누군가 듣고 있다.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        if sock.connect_ex(("127.0.0.1", port)) != 0:
            return
    sys.exit(
        f"이미 {port} 번 포트를 쓰는 대시보드가 실행 중입니다.\n"
        "두 개를 동시에 띄우면 카메라와 시리얼 포트를 서로 뺏어 둘 다 오동작합니다.\n"
        "기존 창을 닫거나 종료한 뒤 다시 실행하세요."
    )


def main() -> None:
    cfg = load_config()
    _abort_if_already_running(cfg.server.port)
    app = create_app(cfg)
    app.run(
        host=cfg.server.host,
        port=cfg.server.port,
        debug=cfg.server.debug,
        threaded=True,
        # reloader 는 프로세스를 두 번 띄워 카메라를 두 번 연다.
        # 장치가 잠기므로 반드시 끈다.
        use_reloader=False,
    )


if __name__ == "__main__":
    main()