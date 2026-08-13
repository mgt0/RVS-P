"""컨베이어 제어 계층 테스트.

실물 ESP32 없이 검증한다. FakeSerial 이 펌웨어(conveyor_stepper.ino)의 명령/응답
규칙을 그대로 흉내내므로, 프로토콜이 어긋나면 여기서 먼저 깨진다.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from flask import Flask

from backend.api import conveyor as conveyor_api
from backend.config import ConveyorConfig
from backend.core import conveyor as conveyor_mod
from backend.core.conveyor import ConveyorController
from backend.utils.errors import ConveyorError, ValidationError


class FakeSerial:
    """conveyor_stepper.ino 의 시리얼 동작을 흉내내는 스텁.

    - 포트를 열면(=인스턴스 생성) ESP32 가 리셋되므로 부팅 로그와 READY 를 먼저 뱉는다.
    - 명령마다 한 줄 응답. STATUS 는 JSON 한 줄.
    """

    instances: list["FakeSerial"] = []
    boot_banner = True

    def __init__(self, port, baudrate, timeout=None):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True
        self.commands: list[str] = []
        self._out: list[str] = []
        self.state = {
            "running": False,
            "enabled": True,
            "dir": "cw",
            "rpm": 0.0,
            "target_rpm": 0.0,
            "sps": 0.0,
            "target_sps": 50.0,
            "microstep": 8,
            "steps_per_rev": 1600,
        }
        if self.boot_banner:
            # 실제 ESP32-S3 도 부트로더 잡음을 먼저 뱉는다. 호스트가 이걸 응답으로
            # 오해하지 않는지 확인하려고 일부러 섞어둔다.
            self._out.append("ESP-ROM:esp32s3-20210327")
            self._out.append("READY: Conveyor Controller Initialized")
        FakeSerial.instances.append(self)

    # ---- pyserial 인터페이스
    def readline(self) -> bytes:
        if not self._out:
            return b""  # timeout
        return (self._out.pop(0) + "\r\n").encode()

    def write(self, payload: bytes) -> int:
        line = payload.decode().strip()
        self.commands.append(line)
        self._out.append(self._reply(line))
        return len(payload)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        self.is_open = False

    # ---- 펌웨어 흉내
    def _reply(self, cmd: str) -> str:
        if cmd == "START":
            self.state.update(running=True, rpm=self.state["target_rpm"])
            return "OK: STARTED"
        if cmd == "STOP":
            self.state.update(running=False, rpm=0.0)
            return "OK: STOPPED"
        if cmd.startswith("RPM "):
            rpm = float(cmd[4:])
            if not 0.375 <= rpm <= 112.5:  # 10~3000sps @ 1600 step/rev
                return "ERR: RPM 범위는 0.38~112.50"
            self.state["target_rpm"] = rpm
            if self.state["running"]:
                self.state["rpm"] = rpm
            return f"OK: TARGET_RPM={rpm}"
        if cmd.startswith("DIR "):
            arg = cmd[4:].strip().lower()
            if arg not in ("cw", "ccw"):
                return "ERR: DIR 인자는 CW 또는 CCW"
            self.state["dir"] = arg
            return f"OK: DIR={arg.upper()}"
        if cmd in ("ENABLE", "DISABLE"):
            self.state["enabled"] = cmd == "ENABLE"
            if cmd == "DISABLE":
                self.state.update(running=False, rpm=0.0)
            return f"OK: {cmd}D"
        if cmd == "STATUS":
            return json.dumps(self.state)
        return f"ERR: UNKNOWN COMMAND: {cmd}"


@pytest.fixture()
def fake_serial(monkeypatch):
    FakeSerial.instances.clear()
    FakeSerial.boot_banner = True
    monkeypatch.setattr(conveyor_mod.serial, "Serial", FakeSerial)
    return FakeSerial


@pytest.fixture()
def ctl(fake_serial):
    return ConveyorController(ConveyorConfig(port="COM11", baudrate=115200, timeout_sec=0.2))


# --------------------------------------------------------------------- 연결
def test_start_는_부팅배너를_소비한_뒤_명령을_보낸다(ctl, fake_serial):
    """회귀 테스트: 포트를 열자마자 START 를 쓰면 부트로더가 삼켜 조용히 실패했다."""
    state = ctl.start()

    ser = fake_serial.instances[-1]
    assert ser.commands == ["START", "STATUS"]  # 부팅 로그를 응답으로 오해하지 않았다
    assert state["running"] is True
    assert state["connected"] is True


def test_배너가_없어도_진행한다(ctl, fake_serial):
    """이미 부팅이 끝난 보드를 다시 열면 READY 가 안 온다. 그래도 동작해야 한다."""
    fake_serial.boot_banner = False
    assert ctl.start()["running"] is True


def test_release_후_status_는_포트를_다시_열지_않는다(ctl, fake_serial):
    ctl.start()
    ctl.disconnect()
    opened = len(fake_serial.instances)

    state = ctl.status()

    # 여기서 포트를 다시 열면 ESP32 가 리셋돼 돌던 컨베이어가 멈춘다.
    assert len(fake_serial.instances) == opened
    assert state["connected"] is False
    assert state["running"] is False


def test_release_후_start_하면_자동_재연결한다(ctl, fake_serial):
    ctl.start()
    ctl.disconnect()
    ctl.start()
    assert len(fake_serial.instances) == 2
    assert fake_serial.instances[-1].commands[0] == "START"


# --------------------------------------------------------------------- 명령
def test_stop_은_상태를_정지로_되돌린다(ctl):
    ctl.start()
    state = ctl.stop()
    assert state["running"] is False
    assert state["rpm"] == 0.0


def test_rpm_설정이_펌웨어_상태에_반영된다(ctl, fake_serial):
    ctl.start()
    state = ctl.set_speed_rpm(45)
    assert "RPM 45.000" in fake_serial.instances[-1].commands
    assert state["target_rpm"] == 45.0
    assert state["rpm"] == 45.0


def test_방향_전환(ctl):
    assert ctl.set_direction("ccw")["dir"] == "ccw"


def test_disable_은_구동을_멈춘다(ctl):
    ctl.start()
    state = ctl.set_enabled(False)
    assert state["enabled"] is False
    assert state["running"] is False


# --------------------------------------------------------------------- 오류
def test_펌웨어_거부는_ConveyorError(ctl):
    with pytest.raises(ConveyorError):
        ctl.set_speed_rpm(500)  # 펌웨어 허용 범위 밖 -> "ERR: ..."


@pytest.mark.parametrize("bad", [0, -5, "빠르게", None])
def test_잘못된_rpm_은_ValidationError(ctl, bad):
    with pytest.raises(ValidationError):
        ctl.set_speed_rpm(bad)


def test_잘못된_방향은_ValidationError(ctl):
    with pytest.raises(ValidationError):
        ctl.set_direction("왼쪽")


def test_포트_미설정이면_ConveyorError(fake_serial):
    ctl = ConveyorController(ConveyorConfig(port=""))
    with pytest.raises(ConveyorError):
        ctl.start()


def test_응답이_없으면_ConveyorError(ctl, fake_serial):
    class Mute(FakeSerial):
        def _reply(self, cmd: str) -> str:  # noqa: ARG002
            return ""  # 펌웨어가 안 올라가 있거나 다른 포트를 잡은 상황

    ctl_mute = ConveyorController(ConveyorConfig(port="COM11", timeout_sec=0.05))
    Mute.boot_banner = True
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(conveyor_mod.serial, "Serial", Mute)
        with pytest.raises(ConveyorError):
            ctl_mute.start()


# --------------------------------------------------------------------- API
@pytest.fixture()
def client():
    """simulate 컨트롤러를 붙인 최소 Flask 앱. 카메라/arm 을 끌고 오지 않는다."""
    app = Flask(__name__)
    controller = ConveyorController(ConveyorConfig(port="SIM"), simulate=True)
    # 벨트 여러 대 구조. 이름 없는 예전 경로는 첫 벨트로 위임된다.
    app.extensions["omx_hub"] = SimpleNamespace(conveyors={"main": controller})
    app.register_blueprint(conveyor_api.bp)
    return app.test_client()


def test_api_start_stop(client):
    res = client.post("/api/conveyor/start")
    assert res.status_code == 200
    assert res.get_json()["running"] is True

    res = client.post("/api/conveyor/stop")
    assert res.get_json()["running"] is False


def test_api_speed_and_dir(client):
    assert client.post("/api/conveyor/speed", json={"rpm": 30}).get_json()["target_rpm"] == 30.0
    assert client.post("/api/conveyor/dir", json={"direction": "ccw"}).get_json()["dir"] == "ccw"


def test_api_status_는_전_벨트_목록(client):
    body = client.get("/api/conveyor/status").get_json()
    assert body["ok"] is True
    belts = body["conveyors"]
    assert len(belts) == 1
    assert belts[0]["name"] == "main"
    assert "running" in belts[0]


def test_이름_경로도_같은_벨트를_제어한다(client):
    # /api/conveyor/main/start 와 /api/conveyor/start 는 같은 벨트다
    assert client.post("/api/conveyor/main/start").get_json()["running"] is True
    assert client.post("/api/conveyor/stop").get_json()["running"] is False


def test_api_release(client):
    body = client.post("/api/conveyor/release").get_json()
    assert body["released"] is True
