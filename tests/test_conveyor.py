"""컨베이어 제어 계층 테스트.

실물 보드 없이 검증한다. FakeSerial 이 펌웨어(conveyor_blackpill.ino)의 명령/응답
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

    - 가장 불리한 경우(리셋형 보드/방금 부팅한 BlackPill)를 흉내내 부팅 로그와
      READY 를 먼저 뱉는다. 호스트는 이 잡음을 응답으로 오해하면 안 된다.
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
            # 리셋 직후 보드는 부트 잡음을 먼저 뱉는다. 호스트가 이걸 응답으로
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
        # 3축 펌웨어의 모터 지정 프리픽스("M2 START"). 실제 펌웨어처럼 떼고 처리한다
        # (여기서는 모터별 상태를 나누지 않는다 — 프로토콜 형식 검증이 목적이다).
        if len(cmd) >= 3 and cmd[0] == "M" and cmd[1] in "123" and cmd[2] == " ":
            cmd = cmd[3:]
        if cmd == "START":
            # 펌웨어 beginRun() 은 꺼져 있던 드라이버를 다시 켠다(DISABLE 후 START).
            self.state.update(running=True, enabled=True, rpm=self.state["target_rpm"])
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
        if cmd == "ZERO":
            self.state["pos_rev"] = 0.0
            return "OK: ZERO"
        if cmd.startswith("LIMIT "):
            self.state["limit_rev"] = float(cmd[6:])
            return f"OK: LIMIT={self.state['limit_rev']:.2f}"
        if cmd.startswith("GOTO "):
            self.state["pos_rev"] = float(cmd[5:])
            self.state.update(running=False, rpm=0.0)
            return f"OK: GOTO {self.state['pos_rev']:.3f}"
        if cmd.startswith("SERVO "):
            deg = int(cmd[6:])
            if not 0 <= deg <= 180:
                return "ERR: SERVO 각도는 0~180"
            self.state["servo_deg"] = deg
            return f"OK: SERVO={deg}"
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
    # 포트 공유 레지스트리는 모듈 전역이다. 이전 테스트의 핸들이 남으면
    # 새 테스트가 포트를 다시 열지 않아 instances 계수가 어긋난다.
    conveyor_mod._ports.clear()
    conveyor_mod._port_locks.clear()
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
    # 첫 연결은 PING 프로브로 시작한다 — 배너가 없는 보드(BlackPill)에서
    # 타임아웃 5초를 통째로 기다리지 않기 위한 능동 확인이다(실측 6.1초→0.05초).
    assert ser.commands == ["PING", "START", "STATUS"]  # 부팅 로그를 응답으로 오해하지 않았다
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

    # 여기서 포트를 다시 열면 펌웨어 업로드를 방해한다(리셋형 보드면 모터도 멈춘다).
    assert len(fake_serial.instances) == opened
    assert state["connected"] is False
    assert state["running"] is False


def test_release_후_start_하면_자동_재연결한다(ctl, fake_serial):
    ctl.start()
    ctl.disconnect()
    ctl.start()
    assert len(fake_serial.instances) == 2
    # 재연결도 PING 프로브부터 시작한 뒤 본 명령이 나간다
    assert fake_serial.instances[-1].commands[:2] == ["PING", "START"]


def test_같은_포트의_모터들은_시리얼을_공유하고_프리픽스를_붙인다(fake_serial):
    """보드 1대(BlackPill) + 모터 3개: 포트는 한 번만 열리고 명령은 M<n> 로 갈린다."""
    feed = ConveyorController(ConveyorConfig(port="COM11", motor=1, timeout_sec=0.2))
    lm = ConveyorController(ConveyorConfig(port="COM11", motor=3, timeout_sec=0.2))

    feed.start()
    lm.start()

    assert len(fake_serial.instances) == 1  # 같은 보드 = 포트 1개
    cmds = fake_serial.instances[0].commands
    assert "M1 START" in cmds and "M3 START" in cmds
    # 한쪽을 release 하면 보드 전체가 끊긴다(물리적으로 한 포트다)
    feed.disconnect()
    assert lm.connected is False


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


def test_리밋이_설정된_축은_start_때마다_리밋을_다시_보낸다(fake_serial):
    """보드가 재부팅되면 리밋이 사라진다. 매 START 에 실어 보내야 항상 적용돼 있다."""
    lm = ConveyorController(
        ConveyorConfig(port="COM11", motor=3, stroke_mm=200.0, mm_per_rev=20.0, timeout_sec=0.2)
    )
    lm.start()
    assert "M3 LIMIT 10.000" in fake_serial.instances[-1].commands
    # 리밋이 없는 축(벨트)은 보내지 않는다 — 무한 회전이라 제한이 없다
    belt = ConveyorController(ConveyorConfig(port="COM11", motor=1, timeout_sec=0.2))
    belt.start()
    assert not any(c.startswith("M1 LIMIT") for c in fake_serial.instances[-1].commands)


def test_zero_는_원점을_잡고_진행_방향을_리밋_안쪽으로_돌린다(fake_serial):
    """회귀: ccw 인 채로 원점을 잡으면 곧바로 하한이라 START 가 거부됐다.

    사용자에겐 '시작을 눌러도 아무 일이 없는' 것으로 보인다. 원점을 잡으면
    갈 수 있는 방향은 +(cw) 뿐이므로 방향까지 함께 맞춘다.
    """
    lm = ConveyorController(ConveyorConfig(port="COM11", motor=3, stroke_mm=200.0, mm_per_rev=20.0))
    lm.set_direction("ccw")
    state = lm.set_zero()

    cmds = fake_serial.instances[-1].commands
    assert "M3 ZERO" in cmds
    assert cmds.index("M3 ZERO") < cmds.index("M3 DIR CW")  # 원점을 먼저, 방향은 그 뒤
    assert state["dir"] == "cw"


def test_리밋이_없는_벨트의_zero_는_방향을_건드리지_않는다(fake_serial):
    """벨트는 무한 회전이라 진행 방향이 사용자의 선택이다. 멋대로 되돌리면 안 된다."""
    belt = ConveyorController(ConveyorConfig(port="COM11", motor=1))
    belt.set_direction("ccw")
    belt.set_zero()
    assert not any(c == "M1 DIR CW" for c in fake_serial.instances[-1].commands)


def test_리밋_설정값은_연결_전에도_상태에_실린다():
    """대시보드가 연결 전에도 '원점 버튼을 붙일 축'을 알아야 한다."""
    lm = ConveyorController(ConveyorConfig(port="COM11", motor=3, stroke_mm=200.0, mm_per_rev=20.0))
    assert lm.state["limit_rev"] == 10.0
    belt = ConveyorController(ConveyorConfig(port="COM11", motor=1))
    assert belt.state["limit_rev"] == 0.0


# ------------------------------------------------------- 끝점 티칭 · 위치 이동
class FakeCalibration:
    """calibration.Calibration 과 같은 모양의 인메모리 스텁."""

    def __init__(self) -> None:
        self.data: dict[str, dict] = {}

    def get(self, axis, key, default=None):
        return self.data.get(axis, {}).get(key, default)

    def set(self, axis, key, value):
        self.data.setdefault(axis, {})[key] = value


def _lm(fake_serial, cal=None, stroke=0.0):
    return ConveyorController(
        ConveyorConfig(port="COM11", motor=3, stroke_mm=stroke, mm_per_rev=40.0,
                       timeout_sec=0.2),
        calibration=cal, name="lm",
    )


def test_끝점_설정은_현재_위치를_저장하고_리밋으로_보낸다(fake_serial):
    """자로 재서 config 에 옮겨 적는 대신 축을 직접 움직여 정한다."""
    cal = FakeCalibration()
    lm = _lm(fake_serial, cal)
    lm.set_zero()
    lm.move_to_mm(0)                      # 펌웨어 상태를 0 으로
    fake_serial.instances[-1].state["pos_rev"] = 16.0   # 16 회전 = 640mm 지점까지 이동했다

    state = lm.set_end()

    assert cal.get("lm", "stroke_mm") == 640.0
    assert state["stroke_mm"] == 640.0
    assert "M3 LIMIT 16.000" in fake_serial.instances[-1].commands


def test_저장된_끝점이_config_기본값을_이긴다(fake_serial):
    cal = FakeCalibration()
    cal.set("lm", "stroke_mm", 500.0)
    lm = _lm(fake_serial, cal, stroke=640.0)
    assert lm.stroke_mm == 500.0
    assert lm.limit_rev == 12.5           # 500 / 40


def test_끝점을_안_찍었으면_config_값을_쓴다(fake_serial):
    lm = _lm(fake_serial, FakeCalibration(), stroke=640.0)
    assert lm.stroke_mm == 640.0


def test_원점에서_바로_끝점을_찍으면_거부한다(fake_serial):
    """움직이지 않고 누르면 가동 범위가 0 이 되어 축이 아예 못 움직인다."""
    lm = _lm(fake_serial, FakeCalibration())
    lm.set_zero()
    with pytest.raises(ValidationError):
        lm.set_end()


def test_위치_이동은_mm를_회전수로_바꿔_보낸다(fake_serial):
    lm = _lm(fake_serial, FakeCalibration(), stroke=640.0)
    lm.move_to_mm(320)
    assert "M3 GOTO 8.0000" in fake_serial.instances[-1].commands


@pytest.mark.parametrize("bad", [-1, 700, "가운데"])
def test_가동_범위_밖_위치는_ValidationError(fake_serial, bad):
    lm = _lm(fake_serial, FakeCalibration(), stroke=640.0)
    with pytest.raises(ValidationError):
        lm.move_to_mm(bad)


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


def test_api_zero(client):
    body = client.post("/api/conveyor/main/zero").get_json()
    assert body["ok"] is True
    assert body["pos_rev"] == 0.0


def test_api_release(client):
    body = client.post("/api/conveyor/release").get_json()
    assert body["released"] is True


def test_servo_각도를_지정하고_범위를_검증한다(ctl):
    state = ctl.set_servo_deg(45)
    assert state["servo_deg"] == 45
    import pytest as _pytest
    from backend.utils.errors import ValidationError
    with _pytest.raises(ValidationError):
        ctl.set_servo_deg(181)
    with _pytest.raises(ValidationError):
        ctl.set_servo_deg("abc")
