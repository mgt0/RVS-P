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
_torch_error = None
try:
    import torch  # noqa: F401
except (ImportError, OSError) as _exc:
    # OSError: Windows 스마트 앱 컨트롤 같은 정책이 torch DLL 자체를 막는 경우
    # (WinError 4551, 2026-10-28 실측). 선점은 순서 보정일 뿐이라 서버는 torch 없이도 떠야 한다.
    # 예외는 버리지 않고 남겨 둔다 — 알림·진단 화면이 '왜 CNN/rollout 이 안 되는지'를 보여줄 근거다.
    _torch_error = _exc

import atexit
import socket
import sys
import tempfile
from pathlib import Path

from backend.app import create_app, enable_file_logging
from backend.config import load_config
from backend.core import system_check   # import 가 가벼워 선점 순서와 무관하다

system_check.record_torch_import(_torch_error)

_LOCK_PATH = Path(tempfile.gettempdir()) / "omx-scan-dashboard.lock"
_lock_handle = None


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


def _acquire_single_instance_lock() -> None:
    """하드웨어를 열기 전에 배타 잠금을 잡는다.

    _abort_if_already_running() 만으로는 부족하다. 두 인스턴스가 거의 동시에
    출발하면 둘 다 '아무도 안 듣고 있다'를 보고 통과한 뒤, 카메라를 서로
    뺏는다(실측: 5대가 전부 open 실패, 증상은 '캠이 안 나온다'). 포트 바인딩은
    카메라를 다 연 뒤에야 일어나므로 그 사이가 통째로 경합 구간이다.

    파일 잠금은 프로세스가 죽으면 OS 가 알아서 풀어주므로, 강제 종료된
    인스턴스가 잠금을 들고 죽는 일이 없다.
    """
    global _lock_handle
    import msvcrt  # Windows 전용. 이 리그는 Windows 다.

    _lock_handle = open(_LOCK_PATH, "a+")  # noqa: SIM115 — 프로세스 수명 동안 열어둔다
    try:
        msvcrt.locking(_lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        # 잠금 실패뿐 아니라, 잠긴 파일에 대한 쓰기 거부(PermissionError)도 여기로 온다.
        sys.exit(
            "이미 다른 대시보드가 실행 중입니다.\n"
            "두 개를 동시에 띄우면 카메라와 시리얼 포트를 서로 뺏어 둘 다 오동작합니다.\n"
            "기존 창을 닫거나 종료한 뒤 다시 실행하세요."
        )
    atexit.register(_lock_handle.close)


def _serve_streams(app, cfg) -> None:
    """MJPEG 프리뷰를 두 번째 포트에서도 서빙한다(같은 app, 다른 포트).

    브라우저는 오리진당 동시 연결을 6개로 묶는다. 카메라 6대의 MJPEG 은 연결을
    하나씩 영구히 물고 있어서, 한 포트로 다 받으면 그 6개를 스트림이 전부 차지하고
    API 요청이 큐에서 무한 대기한다 — 화면상으로는 '버튼을 눌러도 아무 반응이 없고
    잠긴 채로 굳는' 증상이다(실측). 포트가 다르면 오리진이 달라 연결 예산이 따로 잡힌다.
    """
    import threading

    if not cfg.server.stream_port or cfg.server.stream_port == cfg.server.port:
        return
    threading.Thread(
        target=lambda: app.run(
            host=cfg.server.host, port=cfg.server.stream_port,
            debug=False, threaded=True, use_reloader=False,
        ),
        daemon=True,   # 메인 서버가 내려가면 같이 죽는다
        name="stream-server",
    ).start()


def main() -> None:
    cfg = load_config()
    _acquire_single_instance_lock()
    _abort_if_already_running(cfg.server.port)
    app = create_app(cfg)
    # 터미널을 띄운 사람만 로그를 볼 수 있으면 문제가 생겼을 때 옮겨 붙여야 한다.
    # 파일로도 남겨 누가 띄웠든 같이 본다: python scripts/server_status.py
    log_path = enable_file_logging()
    if log_path:
        print(f"로그: {log_path}   (상태 확인: python scripts/server_status.py)")
    _serve_streams(app, cfg)
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