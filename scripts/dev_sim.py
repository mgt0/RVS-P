#!/usr/bin/env python3
"""UI 개발·QA 전용 격리 시뮬레이션 서버.

실기 서버(5000/5001)와 포트·데이터·보정 파일을 전부 분리해서, 사용자가 실물 로봇을
쓰는 동안에도 화면을 띄워볼 수 있다. 하드웨어는 열지 않는다(OMX_SIMULATE=1 고정).

    .venv\\Scripts\\python.exe scripts\\dev_sim.py                          → http://127.0.0.1:5100
    .venv\\Scripts\\python.exe scripts\\dev_sim.py --port 5110 --data .dev_data\\overview

서버를 여럿 띄울 때는 포트와 --data 를 서버마다 다르게 준다(같은 데이터 폴더를 나눠 쓰면
에피소드 ID·보정 파일이 서로 부딪힌다).

격리하는 것
  * 포트: --port(화면·API) / --stream-port(MJPEG). 5000·5001 은 사용자 실기 서버 자리라 거부한다.
  * 데이터: --data 폴더 아래(녹화·검사 촬영·배경·리그 보정·모델). 실제 lerobot_data 는 건드리지 않는다.
  * 끝점 보정: <data>/calibration.json. 시뮬레이션에서도 컨베이어 '끝점 설정'이 calibration.json 을
    덮어쓰는데(conveyor.set_end), 그 파일에는 사용자가 실측한 값(LM 가이드 180mm)이 들어 있다.
    경로를 안 돌려 두면 QA 한 번에 실측값이 날아간다.
  * 학습 로그: <data>/logs/train. 가짜 실행 폴더(run.json·metrics.jsonl)를 만들어 '실제 학습 로그'
    모드를 확인할 때 실제 logs/train 을 어지럽히지 않는다(실기 서버가 가장 최근 실행을 따라 읽는다).
  * 자동 사이클의 픽 궤적: <data>/waypoints/pick_place.json (cycle.pick_checkpoint 는 비운다). 기본값
    lerobot_data/waypoints/pick_place.json 은 실기 팔이 따라갈 실제 경로라, 시뮬레이션 확인용 가짜 궤적을
    거기 두면 실물 팔이 그대로 움직인다. 사이클을 돌려 보려면 이 격리 경로에 짧은 궤적을 만든다.
  * 인스턴스 락은 잡지 않는다 — run.py 의 임시폴더 잠금을 쓰면 실기 서버와 동시에 못 뜬다.
"""

# run.py 와 같은 torch 선점 import(같은 이유·같은 자리). 반드시 cv2/pandas 를 끌어오는 import 보다 먼저 둔다.
# 순서 보정용일 뿐이라 실패해도(torch 미설치, 스마트 앱 컨트롤의 WinError 4551) 서버는 떠야 한다.
_torch_error = None
try:
    import torch  # noqa: F401
except (ImportError, OSError) as _exc:
    _torch_error = _exc   # 알림·진단 화면이 보여줄 근거(run.py 와 같다)

import argparse
import os
import socket
import sys
import threading
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
DEFAULT_PORT = 5100
DEFAULT_DATA = ".dev_data"
# 사용자가 실기 서버를 띄우는 자리. dev_sim 은 하드웨어를 안 열지만, 이 포트를 쥐고 있으면
# 정작 실기 서버가 못 뜨거나(혹은 화면 작업이 사용자 서버에 붙는 사고가 난다).
FORBIDDEN_PORTS = (5000, 5001)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="UI 개발·QA 전용 격리 시뮬레이션 서버(하드웨어를 열지 않는다).",
        epilog="5000·5001 은 사용자 실기 서버 자리라 거부합니다. 서버를 여럿 띄우면 포트와 --data 를 각각 다르게 주세요.",
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="화면·API 포트 (기본 %(default)s)")
    parser.add_argument("--stream-port", type=int, default=None,
                        help="MJPEG 프리뷰 포트 (기본 --port + 1)")
    parser.add_argument("--data", default=DEFAULT_DATA,
                        help="격리 데이터 폴더 (기본 %(default)s, 상대 경로는 레포 루트 기준)")
    args = parser.parse_args(argv)

    if args.stream_port is None:
        args.stream_port = args.port + 1
    for name, port in (("--port", args.port), ("--stream-port", args.stream_port)):
        if port in FORBIDDEN_PORTS:
            parser.error(f"{name} {port}: 5000·5001 은 사용자 실기 서버 자리라 dev_sim 이 쓸 수 없습니다.")
        if not 1 <= port <= 65535:
            parser.error(f"{name} {port}: 1~65535 범위의 포트여야 합니다.")
    if args.port == args.stream_port:
        # 같은 포트면 브라우저의 오리진당 연결 한도(6개)를 MJPEG 이 먹어 API 가 굶는다(run.py 참고).
        parser.error("--port 와 --stream-port 는 서로 달라야 합니다.")
    return args


def resolve_data_dir(value: str, protected: list[Path]) -> Path:
    """격리 데이터 폴더를 확정한다. 실제 데이터와 겹치면 서버를 띄우지 않는다.

    protected 의 폴더와 같거나 그 안/밖(부모)이면 거부한다 — 안이면 가짜 녹화가 진짜 데이터에 섞이고,
    밖(예: 레포 루트)이면 <data>/calibration.json 이 사용자의 실제 calibration.json 이 된다.
    """
    data = Path(value).expanduser()
    if not data.is_absolute():
        data = ROOT / data
    data = data.resolve()
    for real in (Path(p).resolve() for p in protected):
        if data == real or real in data.parents or data in real.parents:
            raise SystemExit(f"--data {data} 가 실제 데이터 폴더({real})와 겹칩니다. 격리 폴더를 따로 지정하세요.")
    if (data / "calibration.json") == (ROOT / "calibration.json").resolve():
        raise SystemExit(f"--data {data} 의 calibration.json 이 실제 보정 파일과 같습니다. 격리 폴더를 따로 지정하세요.")
    return data


def isolated_cycle(cycle, data: Path):
    """자동 사이클 설정에서 실기 경로를 걷어 낸다: 픽 궤적은 <data>/waypoints/pick_place.json, 체크포인트는 없음.

    체크포인트는 비워 둔다 — 값이 있으면 웨이포인트보다 앞서고(core/cycle.py), 실제 학습 결과를 가리킬 수 있다.
    시간·속도 손잡이는 config.yaml 값 그대로다(사이클이 몇 초 걸려도 시뮬레이션이라 괜찮다).
    """
    return replace(cycle, pick_waypoints=data / "waypoints" / "pick_place.json", pick_checkpoint=None)


def _abort_if_in_use(*ports: int) -> None:
    """이미 누가 듣고 있으면 뜨지 않는다.

    Windows 는 같은 주소·포트에 두 번 bind 해도 막지 않는다(run.py 주석 참고). 그래서
    bind 가 아니라 connect 로 확인한다 — 안 그러면 두 번째 서버가 조용히 같은 포트를 나눠 쓴다.
    """
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((HOST, port)) == 0:
                raise SystemExit(f"{port} 번 포트를 이미 누가 쓰고 있습니다. 다른 --port 를 주거나 먼저 띄운 서버를 종료하세요.")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    # 콘솔 코드페이지(cp949)로 못 찍는 글자(경로 등) 때문에 서버가 뜨다 죽으면 안 된다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    os.environ["OMX_SIMULATE"] = "1"   # load_config() 가 읽기 전에 정한다
    _abort_if_in_use(args.port, args.stream_port)

    # --help 와 인자 오류는 위에서 끝난다. cv2·pandas 를 끌어오는 backend 는 여기서부터 import 한다.
    sys.path.insert(0, str(ROOT))
    from backend.app import create_app
    from backend.config import load_config
    from backend.core import system_check

    system_check.record_torch_import(_torch_error)
    cfg = load_config()
    data = resolve_data_dir(args.data, [ROOT / "lerobot_data", cfg.recording.root])
    cfg = replace(
        cfg,
        server=replace(cfg.server, host=HOST, port=args.port, stream_port=args.stream_port,
                       simulate=True, debug=False),
        recording=replace(cfg.recording, root=data),
        cycle=isolated_cycle(cfg.cycle, data),
    )
    app = create_app(cfg)
    hub = app.extensions["omx_hub"]

    # create_app() 은 HardwareHub 를 만들며 실제 calibration.json 을 읽는다. 서버가 뜨기 전이라
    # 아직 아무것도 쓰지 않았다 — 저장 경로를 격리 폴더로 돌리고 메모리의 값도 비운다(격리 폴더에
    # 이미 있으면 그걸 읽는다). 비우지 않으면 첫 저장이 실제 값을 격리 파일에 복사한다.
    hub.calibration._path = data / "calibration.json"
    hub.calibration._data = {}
    hub.calibration._load()
    # 학습 로그도 격리한다(위 docstring).
    hub.training._log_root = data / "logs" / "train"

    # MJPEG 은 두 번째 포트에서도 서빙한다(브라우저의 오리진당 연결 한도 때문 — run.py 참고).
    threading.Thread(
        target=lambda: app.run(host=HOST, port=args.stream_port, debug=False, threaded=True, use_reloader=False),
        daemon=True,
        name="stream-server",
    ).start()
    print(f"dev_sim: http://{HOST}:{args.port}  (MJPEG {args.stream_port}, 데이터 {data}, pid {os.getpid()})", flush=True)
    print(f"dev_sim: 사이클 픽 궤적 {cfg.cycle.pick_waypoints}", flush=True)
    app.run(host=HOST, port=args.port, debug=False, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
