"""컨베이어 제어 계층 테스트(DC 모터 + HW-095).

실물 보드 없이 검증한다. FakeSerial 이 펌웨어(conveyor_blackpill.ino)의 명령/응답
규칙을 그대로 흉내내므로, 프로토콜이 어긋나면 여기서 먼저 깨진다.

지켜야 할 것:
1. 속도는 PWM 듀티(%)로만 정한다(0~100). 범위 밖은 펌웨어에 보내기 전에 거부한다.
2. DISABLE 뒤에는 START 를 펌웨어가 거부한다. 달리는 중 DIR 도 거부한다. 정지(STOP)는 어떤 상태에서도 된다.
3. 호스트 워치독: 벨트가 하나라도 달리는 동안 포트당 하나의 keepalive 가 PING 을 보낸다. 명령과 같은 락을 쓰고,
   멈추거나 포트를 놓으면 끝나며, PING 이 실패해도 경고만 남기고 죽지 않는다.
4. LM 가이드(M3)와 위치 이동(/zero·/end·/move)은 없다.
"""

from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest
from flask import Flask

from backend.api import conveyor as conveyor_api
from backend.app import _register_error_handlers
from backend.config import ConveyorConfig, load_config
from backend.core import conveyor as conveyor_mod
from backend.core.conveyor import ConveyorController, _parse_status
from backend.utils.errors import ConveyorError, ValidationError


class FakeSerial:
    """conveyor_blackpill.ino(DC 모터 + HW-095)의 시리얼 규약을 흉내내는 스텁.

    - 가장 불리한 경우(리셋형 보드/방금 부팅한 BlackPill)를 흉내내 부팅 로그와
      READY 를 먼저 뱉는다. 호스트는 이 잡음을 응답으로 오해하면 안 된다.
    - 명령마다 한 줄 응답. 모터 둘(M1/M2)을 따로 들고, 접두사가 없으면 M1 이다.
      서보는 보드 공용이라 STATUS 에는 M1 만 싣는다.
    - 펌웨어는 지난번 호스트가 정한 듀티를 기억한다(boot_duty) — BlackPill 은 호스트가 접속을 끊어도 리셋되지 않는다.
    - watchdog_sec 이 있으면 호스트 워치독을 흉내낸다: 달리는 동안 그 시간 넘게 아무 줄도 못 받으면 모든 모터가 선다.
    """

    instances: list["FakeSerial"] = []
    boot_banner = True
    boot_duty = 70.0            # 펌웨어가 기억하던 값. 화면의 기본값(40)과 일부러 다르게 둔다
    watchdog_sec: float | None = None
    ping_reply = "OK: PONG"     # "" 로 바꾸면 PING 에 대답하지 않는 보드
    ping_error = False          # True 면 PING 을 쓰는 순간 포트가 죽는다

    def __init__(self, port, baudrate, timeout=None):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.is_open = True
        self.commands: list[str] = []
        # 읽히지 않은 응답이 reset_input_buffer 로 버려진 횟수. 요청과 응답이 짝이 아니었다(줄이 섞였다)는 증거다.
        self.discarded = 0
        self.servo = 90
        self.motors = {n: {"run": False, "dir": "CW", "duty": self.boot_duty, "en": True} for n in (1, 2)}
        self._out: list[str] = []
        self._last_line = time.monotonic()
        if self.boot_banner:
            # 리셋 직후 보드는 부트 잡음을 먼저 뱉는다. 호스트가 이걸 응답으로
            # 오해하지 않는지 확인하려고 일부러 섞어둔다.
            self._out.append("ESP-ROM:esp32s3-20210327")
            self._out.append("READY: Conveyor Controller Initialized")
        FakeSerial.instances.append(self)

    # ---- pyserial 인터페이스
    @property
    def in_waiting(self) -> int:
        return len(self._out)

    def readline(self) -> bytes:
        if not self._out:
            return b""  # timeout
        return (self._out.pop(0) + "\r\n").encode()

    def write(self, payload: bytes) -> int:
        line = payload.decode().strip()
        if line == "PING" and self.ping_error:
            raise conveyor_mod.serial.SerialException("장치가 응답하지 않습니다")
        now = time.monotonic()
        if self.watchdog_sec is not None and now - self._last_line > self.watchdog_sec:
            for m in self.motors.values():
                m["run"] = False        # 호스트 워치독: 오래 조용하면 모든 모터를 세운다
        self._last_line = now
        self.commands.append(line)
        self._out.extend(self._reply(line).split("\n"))     # 응답 앞에 비동기 줄을 끼워 보낼 수 있다
        return len(payload)

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        self.discarded += len(self._out)
        self._out.clear()

    def close(self) -> None:
        self.is_open = False

    # ---- 펌웨어 흉내
    def _reply(self, cmd: str) -> str:
        # 모터 지정 프리픽스("M2 START"). 실제 펌웨어처럼 떼고 그 모터에 적용한다.
        n = 1
        if len(cmd) >= 3 and cmd[0] == "M" and cmd[1].isdigit() and cmd[2] == " ":
            n, cmd = int(cmd[1]), cmd[3:]
            if n not in self.motors:
                return f"ERR: NO MOTOR {n}"
        m = self.motors[n]
        if cmd == "START":
            if not m["en"]:
                return "ERR: DISABLED"      # DISABLE 뒤에는 ENABLE 전까지 거부한다
            m["run"] = True
            return "OK: RUN"
        if cmd == "STOP":
            m["run"] = False
            return "OK: STOP"
        if cmd.startswith("DUTY "):
            pct = float(cmd[5:])
            if not 0 <= pct <= 100:
                return "ERR: DUTY 범위는 0~100"
            if pct == 0:
                m["run"] = False            # 0 이면 STOP 과 같고 설정한 듀티는 그대로 남는다
            else:
                m["duty"] = pct
            return f"OK: DUTY={pct:g}"
        if cmd.startswith("DIR "):
            arg = cmd[4:].strip().upper()
            if arg not in ("CW", "CCW"):
                return "ERR: DIR 인자는 CW 또는 CCW"
            if m["run"]:
                return "ERR: RUNNING"
            m["dir"] = arg
            return f"OK: DIR={arg}"
        if cmd == "ENABLE":
            m["en"] = True
            return "OK: ENABLED"
        if cmd == "DISABLE":
            m.update(en=False, run=False)
            return "OK: DISABLED"
        if cmd.startswith("SERVO "):
            deg = int(cmd[6:])
            if not 0 <= deg <= 180:
                return "ERR: SERVO 각도는 0~180"
            self.servo = deg
            return f"OK: SERVO={deg}"
        if cmd == "STATUS":
            line = f"OK: STATUS RUN={int(m['run'])} DIR={m['dir']} DUTY={m['duty']:g} EN={int(m['en'])}"
            return line + (f" SERVO={self.servo}" if n == 1 else "")
        if cmd == "PING":
            return self.ping_reply
        return f"ERR: UNKNOWN COMMAND: {cmd}"


def _reset_ports() -> None:
    """포트 공유 레지스트리는 모듈 전역이다. 이전 테스트의 핸들이 남으면 새 테스트가 포트를 다시 열지 않아
    instances 계수가 어긋나고, keepalive 스레드가 남으면 새 테스트의 포트에 PING 을 쓴다."""
    for port in list(conveyor_mod._keepalives):
        conveyor_mod._stop_keepalive(port)
    conveyor_mod._ports.clear()
    conveyor_mod._port_locks.clear()
    conveyor_mod._axes.clear()


@pytest.fixture()
def fake_serial(monkeypatch):
    FakeSerial.instances.clear()
    FakeSerial.boot_banner = True
    FakeSerial.boot_duty = 70.0
    FakeSerial.watchdog_sec = None
    FakeSerial.ping_reply = "OK: PONG"
    FakeSerial.ping_error = False
    _reset_ports()
    monkeypatch.setattr(conveyor_mod.serial, "Serial", FakeSerial)
    yield FakeSerial
    _reset_ports()


@pytest.fixture()
def fast_keepalive(monkeypatch):
    """keepalive 를 촘촘하게 돌려 시험을 빨리 끝낸다(실제 간격은 0.5초). 명령 순서를 정확히 보는 시험에는 쓰지 않는다."""
    monkeypatch.setattr(conveyor_mod, "_KEEPALIVE_SEC", 0.02)


@pytest.fixture()
def ctl(fake_serial):
    return ConveyorController(ConveyorConfig(port="COM11", baudrate=115200, timeout_sec=0.2))


def pings(ser: FakeSerial) -> int:
    return ser.commands.count("PING")


def keepalive_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("conveyor-keepalive") and t.is_alive()]


def wait_until(predicate, what: str, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"{timeout}초 안에 '{what}' 이(가) 되지 않았습니다")


# --------------------------------------------------------------------- 연결
def test_start_는_부팅배너를_소비한_뒤_명령을_보낸다(ctl, fake_serial):
    """회귀 테스트: 포트를 열자마자 START 를 쓰면 부트로더가 삼켜 조용히 실패했다."""
    state = ctl.start()

    ser = fake_serial.instances[-1]
    # 첫 연결은 PING 프로브로 시작한다 — 배너가 없는 보드(BlackPill)에서
    # 타임아웃 5초를 통째로 기다리지 않기 위한 능동 확인이다(실측 6.1초→0.05초).
    # 이어서 화면에 보이던 속도를 한 번 보내고(_sync_duty) 구동한다.
    assert ser.commands == ["PING", "DUTY 40.000", "START", "STATUS"]  # 부팅 로그를 응답으로 오해하지 않았다
    assert state["running"] is True
    assert state["connected"] is True


def test_배너가_없어도_진행한다(ctl, fake_serial):
    """이미 부팅이 끝난 보드를 다시 열면 READY 가 안 온다. 그래도 동작해야 한다."""
    fake_serial.boot_banner = False
    assert ctl.start()["running"] is True


def test_PING_응답은_옛_형식_PONG_도_받는다(ctl, fake_serial):
    """지금 펌웨어는 "OK: PONG", 옛 펌웨어는 "PONG" 이다. 어느 쪽이든 부팅 확인이 즉시 끝나야 한다(2초 대기 없음)."""
    fake_serial.boot_banner = False
    fake_serial.ping_reply = "PONG"
    t0 = time.monotonic()
    ctl.connect()
    assert time.monotonic() - t0 < 1.0 and ctl.connected


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
    # 재연결도 PING 프로브부터 시작한 뒤 본 명령이 나간다. 새 연결이라 속도도 다시 보낸다
    assert fake_serial.instances[-1].commands[:3] == ["PING", "DUTY 40.000", "START"]


def test_같은_포트의_모터들은_시리얼을_공유하고_프리픽스를_붙인다(fake_serial):
    """보드 1대(BlackPill) + 모터 2개: 포트는 한 번만 열리고 명령은 M<n> 로 갈린다."""
    feed = ConveyorController(ConveyorConfig(port="COM11", motor=1, timeout_sec=0.2))
    work = ConveyorController(ConveyorConfig(port="COM11", motor=2, timeout_sec=0.2))

    feed.start()
    work.start()

    assert len(fake_serial.instances) == 1  # 같은 보드 = 포트 1개
    cmds = fake_serial.instances[0].commands
    assert "M1 START" in cmds and "M2 START" in cmds
    # 한쪽을 release 하면 보드 전체가 끊긴다(물리적으로 한 포트다) — 형제 축도 '달리는 중' 으로 굳지 않는다
    feed.disconnect()
    assert work.connected is False
    assert feed.state["running"] is False and work.state["running"] is False


def test_M3_는_없는_모터라_펌웨어가_거부한다(fake_serial):
    """LM 가이드(M3)는 사라졌다. 옛 설정이 남아 M3 로 가더라도 보드는 구동하지 않고 이유를 준다."""
    ghost = ConveyorController(ConveyorConfig(port="COM11", motor=3, timeout_sec=0.2))
    with pytest.raises(ConveyorError, match="NO MOTOR 3"):
        ghost.start()


# --------------------------------------------------------------------- 명령
def test_stop_은_상태를_정지로_되돌린다(ctl):
    ctl.start()
    state = ctl.stop()
    assert state["running"] is False


def test_duty_설정이_펌웨어_상태에_반영된다(ctl, fake_serial):
    ctl.start()
    state = ctl.set_duty(65)
    assert "DUTY 65.000" in fake_serial.instances[-1].commands
    assert state["duty"] == 65.0
    assert fake_serial.instances[-1].motors[1]["duty"] == 65.0


@pytest.mark.parametrize("ok", [0, 0.5, 100, "55"])
def test_duty_범위_경계는_받아들인다(ctl, ok):
    assert ctl.set_duty(ok)["connected"] is True


@pytest.mark.parametrize("bad", [-1, -0.001, 100.001, 101, "빠르게", None, float("nan"), float("inf")])
def test_범위_밖_duty_는_ValidationError_이고_펌웨어에_보내지_않는다(ctl, fake_serial, bad):
    ctl.connect()
    with pytest.raises(ValidationError):
        ctl.set_duty(bad)
    assert not any(c.startswith("DUTY") for c in fake_serial.instances[-1].commands)


def test_duty_0_은_세우기만_하고_설정한_속도는_남긴다(ctl, fake_serial):
    """펌웨어에서 DUTY 0 은 STOP 과 같다. 설정값(duty)은 그대로라 다음 START 가 그 속도로 돈다."""
    ctl.set_duty(55)
    ctl.start()
    state = ctl.set_duty(0)
    assert state["running"] is False
    assert state["duty"] == 55.0                      # STATUS 의 DUTY 는 '설정한 속도' 다
    ser = fake_serial.instances[-1]
    assert ser.motors[1]["run"] is False and ser.motors[1]["duty"] == 55.0
    assert ctl.start()["duty"] == 55.0 and ser.motors[1]["run"] is True


def test_방향_전환(ctl):
    assert ctl.set_direction("ccw")["direction"] == "ccw"      # STATUS 의 DIR=CCW 를 소문자로 읽는다


def test_달리는_중_방향_전환은_펌웨어가_거부하고_방향은_그대로다(ctl):
    ctl.start()
    with pytest.raises(ConveyorError, match="RUNNING"):
        ctl.set_direction("ccw")
    assert ctl.status()["direction"] == "cw"
    ctl.stop()
    assert ctl.set_direction("ccw")["direction"] == "ccw"


def test_disable_은_구동을_멈추고_ENABLE_전까지_START_를_거부한다(ctl):
    ctl.start()
    state = ctl.set_enabled(False)
    assert state["enabled"] is False
    assert state["running"] is False
    with pytest.raises(ConveyorError, match="DISABLED"):
        ctl.start()
    assert ctl.status()["running"] is False       # 거부된 START 는 아무것도 돌리지 않았다
    ctl.set_enabled(True)
    assert ctl.start()["running"] is True


def test_정지는_드라이버가_꺼져_있어도_된다(ctl):
    """정지는 항상 허용된다 — 비상 정지가 DISABLE 한 뒤에도, 서 있는 벨트에도 STOP 이 거부되지 않는다."""
    ctl.set_enabled(False)
    assert ctl.stop()["running"] is False
    ctl.set_enabled(True)
    assert ctl.stop()["running"] is False


def test_펌웨어_거부는_연결_실패가_아니라_400_이다(ctl):
    """ERR: TIMER CONFLICT 같은 거부 문구도 그대로 올라간다. 503(연결 실패)이 아니라 400 이다."""
    ctl.set_enabled(False)
    with pytest.raises(ConveyorError) as exc:
        ctl.start()
    assert exc.value.status_code == 400 and exc.value.code == "conveyor_rejected"
    assert "ERR: DISABLED" in exc.value.message and exc.value.detail == {"command": "START"}

    class Conflict(FakeSerial):
        def _reply(self, cmd: str) -> str:
            return "ERR: TIMER CONFLICT" if cmd.endswith("START") else super()._reply(cmd)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(conveyor_mod.serial, "Serial", Conflict)
        board = ConveyorController(ConveyorConfig(port="COM12", timeout_sec=0.2))
        with pytest.raises(ConveyorError, match="ERR: TIMER CONFLICT") as exc:
            board.start()
        assert exc.value.status_code == 400 and board.state["running"] is False
        assert keepalive_threads() == []              # 구동하지 못했으니 keepalive 도 없다


def test_READY_와_WARN_줄은_응답이_아니라_건너뛰고_WARN_은_경고로_남긴다(ctl, fake_serial, caplog):
    ctl.connect()
    ser = fake_serial.instances[-1]
    real = ser._reply
    # 보드가 응답 앞에 비동기 줄을 끼워 보낸다(호스트 워치독이 걸리면 WARN 을 한 번 낸다)
    ser._reply = lambda cmd: "READY: Conveyor Controller Initialized\nWARN: HOST TIMEOUT\n" + real(cmd)
    with caplog.at_level(logging.WARNING, logger=conveyor_mod.logger.name):
        state = ctl.start()
    assert state["running"] is True                   # 응답(OK: RUN)은 놓치지 않았다
    assert any("WARN: HOST TIMEOUT" in r.getMessage() for r in caplog.records)


# ------------------------------------------------------------ STATUS · 속도 동기화
def test_STATUS_한_줄을_상태로_읽는다():
    assert _parse_status("OK: STATUS RUN=1 DIR=CCW DUTY=55.5 EN=0 SERVO=120") == {
        "running": True, "direction": "ccw", "duty": 55.5, "enabled": False, "servo_deg": 120,
    }
    # SERVO 는 보드에 하나뿐이라 M1 의 STATUS 에만 실린다 — 없으면 키도 없다
    assert _parse_status("OK: STATUS RUN=0 DIR=CW DUTY=40 EN=1") == {
        "running": False, "direction": "cw", "duty": 40.0, "enabled": True,
    }
    # 키 순서에 기대지 않고, 모르는 키는 건너뛴다
    assert _parse_status("OK: STATUS EN=1 FOO=bar DUTY=10 RUN=0 DIR=CW") == {
        "enabled": True, "duty": 10.0, "running": False, "direction": "cw",
    }


@pytest.mark.parametrize("line", [
    "", "OK: DUTY=40", "ERR: DISABLED", '{"running": true}',          # STATUS 가 아니다(옛 JSON 포함)
    "OK: STATUS RUN=2 DIR=CW DUTY=40 EN=1",                          # 플래그는 0/1
    "OK: STATUS RUN=1 DIR=UP DUTY=40 EN=1",
    "OK: STATUS RUN=1 DIR=CW DUTY=abc EN=1",
    "OK: STATUS RUN=1 DIR=CW DUTY=nan EN=1",
    "OK: STATUS RUN=1 DIR=CW DUTY=inf EN=1",
])
def test_STATUS_가_아니거나_깨진_줄은_None(line):
    assert _parse_status(line) is None


def test_상태의_키는_계약과_같다(fake_serial):
    feed = ConveyorController(ConveyorConfig(port="COM11", motor=1, timeout_sec=0.2))
    work = ConveyorController(ConveyorConfig(port="COM11", motor=2, timeout_sec=0.2))
    base = {"connected", "port", "motor", "running", "direction", "duty", "enabled"}
    assert set(feed.state) == base                      # 연결 전에도 같은 모양이다(서보는 아직 모른다)
    # 서보는 보드에 하나라 M1 의 STATUS 에만 실린다. RPM·위치·행정·리밋 계열은 없다
    assert set(feed.start()) == base | {"servo_deg"}
    assert set(work.start()) == base


def test_연결_전_상태는_설정의_기본_듀티를_보여준다():
    belt = ConveyorController(ConveyorConfig(port="COM11", motor=1, default_duty=25.0))
    assert belt.state["duty"] == 25.0 and belt.state["connected"] is False
    assert (belt.state["running"], belt.state["direction"], belt.state["enabled"]) == (False, "cw", True)


def test_첫_START_전에_화면의_속도를_펌웨어에_보낸다(fake_serial):
    """BlackPill 은 호스트가 접속을 끊어도 리셋되지 않아 펌웨어가 지난번 듀티(70)를 기억한다.
    화면에는 config 의 기본값(40)이 보이는데 그대로 START 하면 70 으로 달린다."""
    belt = ConveyorController(ConveyorConfig(port="COM11", motor=1, timeout_sec=0.2))
    belt.connect()
    ser = fake_serial.instances[-1]
    # 연결만 했고 아직 속도를 보내지 않았다 — STATUS 가 펌웨어의 70 을 알려 줘도 화면 값은 첫 START 때 보낼 40 이다
    assert belt.status()["duty"] == 40.0 and ser.motors[1]["duty"] == 70.0
    state = belt.start()
    assert ser.commands.index("M1 DUTY 40.000") < ser.commands.index("M1 START")
    assert state["duty"] == 40.0 and ser.motors[1]["duty"] == 40.0


def test_같은_연결에서는_START_마다_속도를_다시_보내지_않는다(ctl, fake_serial):
    ctl.start()
    ctl.stop()
    ctl.start()
    assert fake_serial.instances[-1].commands.count("DUTY 40.000") == 1


def test_set_duty_로_속도를_정했으면_START_가_기본값으로_되돌리지_않는다(ctl, fake_serial):
    ctl.set_duty(55)
    state = ctl.start()
    assert [c for c in fake_serial.instances[-1].commands if c.startswith("DUTY")] == ["DUTY 55.000"]
    assert state["duty"] == 55.0


def test_연결을_새로_열면_속도를_다시_보낸다(ctl, fake_serial):
    ctl.set_duty(55)
    ctl.start()
    ctl.disconnect()
    ctl.start()
    assert [c for c in fake_serial.instances[-1].commands if c.startswith("DUTY")] == ["DUTY 55.000"]


# ------------------------------------------------- 호스트 워치독 keepalive
class TestKeepalive:
    """펌웨어는 달리는 동안 HOST_TIMEOUT_MS 안에 아무 줄도 못 받으면 모든 모터를 세운다(서버가 죽어도 벨트가 안 돈다).
    그래서 달리는 동안 포트당 하나의 keepalive 가 PING 을 보낸다."""

    def test_달리는_동안만_PING_을_보낸다(self, ctl, fake_serial, fast_keepalive):
        ctl.set_duty(40)                          # 연결됐지만 서 있다
        ser = fake_serial.instances[-1]
        boot = pings(ser)                         # 연결할 때의 부팅 확인 PING 한 번
        time.sleep(0.2)
        assert pings(ser) == boot and keepalive_threads() == []

        ctl.start()
        wait_until(lambda: pings(ser) >= boot + 3, "달리는 동안 PING")
        assert len(keepalive_threads()) == 1

        ctl.stop()
        wait_until(lambda: keepalive_threads() == [], "정지 뒤 keepalive 종료")
        after = pings(ser)
        time.sleep(0.2)
        assert pings(ser) == after                # 멈춘 뒤에는 PING 이 없다

    def test_드라이버를_끄거나_DUTY_0_으로_세워도_끝난다(self, ctl, fake_serial, fast_keepalive):
        ctl.start()
        wait_until(lambda: len(keepalive_threads()) == 1, "keepalive 시작")
        ctl.set_enabled(False)
        wait_until(lambda: keepalive_threads() == [], "DISABLE 뒤 종료")
        ctl.set_enabled(True)
        ctl.start()
        wait_until(lambda: len(keepalive_threads()) == 1, "다시 START 하면 다시 뜬다")
        ctl.set_duty(0)
        wait_until(lambda: keepalive_threads() == [], "DUTY 0 뒤 종료")

    def test_PING_이_워치독을_살려_벨트가_안_멈춘다(self, ctl, fake_serial, fast_keepalive):
        fake_serial.watchdog_sec = 0.5            # 실제는 2초 — 시험이 빨리 끝나게 줄였다
        ctl.start()
        time.sleep(1.2)                           # 워치독 시간의 두 배 넘게 명령 없이 둔다
        assert ctl.status()["running"] is True    # 보드의 STATUS 로 확인한다

    def test_keepalive_가_없으면_펌웨어_워치독이_벨트를_세운다(self, ctl, fake_serial, monkeypatch):
        """위 시험의 대조군: PING 이 끊기면 같은 조건에서 벨트가 선다 — 시험이 워치독을 실제로 흉내내고 있다는 증거."""
        fake_serial.watchdog_sec = 0.3
        monkeypatch.setattr(conveyor_mod, "_KEEPALIVE_SEC", 60.0)     # 사실상 PING 없음
        ctl.start()
        time.sleep(0.6)
        assert ctl.status()["running"] is False

    def test_포트당_하나만_돌고_달리는_축이_남아_있으면_계속_돈다(self, fake_serial, fast_keepalive):
        feed = ConveyorController(ConveyorConfig(port="COM11", motor=1, timeout_sec=0.2))
        work = ConveyorController(ConveyorConfig(port="COM11", motor=2, timeout_sec=0.2))
        feed.start()
        work.start()
        assert len(keepalive_threads()) == 1      # 두 축이 달려도 스레드는 포트당 하나다
        ser = fake_serial.instances[-1]
        feed.stop()
        n = pings(ser)
        wait_until(lambda: pings(ser) >= n + 3, "work 가 달리는 동안 계속 PING")
        assert len(keepalive_threads()) == 1
        work.stop()
        wait_until(lambda: keepalive_threads() == [], "모두 서면 종료")

    def test_PING_은_명령과_같은_락을_쓴다(self, ctl, fake_serial, fast_keepalive, monkeypatch):
        monkeypatch.setattr(conveyor_mod, "_KEEPALIVE_LOCK_SEC", 0.02)
        ctl.start()
        ser = fake_serial.instances[-1]
        with ctl._lock:                           # 다른 명령이 포트를 쥐고 있다
            time.sleep(0.1)                       # 이미 나가던 PING 이 있었어도 끝났다
            before = pings(ser)
            time.sleep(0.2)
            assert pings(ser) == before           # 락을 쥔 동안에는 PING 을 쓰지 않는다 — 줄이 섞이지 않는다
        wait_until(lambda: pings(ser) > before, "락이 풀리면 PING 재개")

    def test_명령과_keepalive_가_번갈아_돌아도_요청과_응답이_짝이다(self, ctl, fake_serial, fast_keepalive):
        ctl.start()
        ser = fake_serial.instances[-1]
        base = ser.discarded                      # 연결·첫 명령이 남긴 부팅 응답은 센 값에서 뺀다
        end = time.monotonic() + 0.5
        n = 0
        while time.monotonic() < end:
            ctl.set_duty(41 + n % 2)
            ctl.status()
            n += 1
            time.sleep(0.002)                     # 락이 비는 틈을 준다(파이썬 락은 공정하지 않아 꽉 물고 있으면 굶긴다)
        assert n > 10 and pings(ser) > 5          # 실제로 섞여 돌았다
        assert ser.discarded == base              # 읽히지 않은 응답이 버려지지 않았다 = 줄이 섞이지 않았다
        assert ctl.state["running"] is True

    def test_disconnect_는_keepalive_를_끝내고_포트를_다시_열지_않는다(self, ctl, fake_serial, fast_keepalive):
        ctl.start()
        wait_until(lambda: len(keepalive_threads()) == 1, "keepalive 시작")
        ser = fake_serial.instances[-1]
        ctl.disconnect()
        assert keepalive_threads() == []          # disconnect 가 끝나길 기다렸다 돌아온다
        opened, written = len(fake_serial.instances), len(ser.commands)
        time.sleep(0.2)
        assert len(fake_serial.instances) == opened and len(ser.commands) == written    # 다시 열지도 더 쓰지도 않는다
        assert ctl.connected is False and ctl.state["running"] is False

    def test_한_축을_release_하면_보드_전체가_끊겨_keepalive_도_끝난다(self, fake_serial, fast_keepalive):
        feed = ConveyorController(ConveyorConfig(port="COM11", motor=1, timeout_sec=0.2))
        work = ConveyorController(ConveyorConfig(port="COM11", motor=2, timeout_sec=0.2))
        feed.start()
        work.start()
        feed.disconnect()
        assert keepalive_threads() == []
        assert work.state["running"] is False     # 형제 축이 '달리는 중' 으로 굳으면 keepalive 가 되살아나고 사이클 점검이 막힌다

    def test_PING_이_실패해도_경고만_남기고_서버가_죽지_않는다(self, ctl, fake_serial, fast_keepalive, monkeypatch, caplog):
        monkeypatch.setattr(conveyor_mod, "_KEEPALIVE_REPLY_SEC", 0.03)
        ctl.start()
        fake_serial.ping_reply = ""               # 보드가 PING 에 대답하지 않는다
        with caplog.at_level(logging.WARNING, logger=conveyor_mod.logger.name):
            time.sleep(0.4)                       # 실패한 틱이 여러 번
        warns = [r for r in caplog.records if "keepalive PING 실패" in r.getMessage()]
        assert len(keepalive_threads()) == 1      # 스레드는 계속 돈다
        assert len(warns) == 1                    # 매 틱마다 찍지 않는다(처음 한 번만)
        assert ctl.status()["running"] is True    # 다른 명령은 그 사이에도 통한다
        fake_serial.ping_reply = "OK: PONG"
        ser = fake_serial.instances[-1]
        n = pings(ser)
        wait_until(lambda: pings(ser) >= n + 3, "응답이 돌아오면 정상 PING")
        assert len(keepalive_threads()) == 1
        assert ctl.stop()["running"] is False

    def test_PING_전송이_죽으면_포트를_버리고_keepalive_도_끝난다(self, ctl, fake_serial, fast_keepalive, caplog):
        ctl.start()
        fake_serial.ping_error = True             # PING 을 쓰는 순간 포트가 죽는다
        with caplog.at_level(logging.WARNING, logger=conveyor_mod.logger.name):
            wait_until(lambda: ctl.connected is False, "죽은 포트를 버림")
            wait_until(lambda: keepalive_threads() == [], "keepalive 종료")
        assert ctl.state["running"] is False      # 포트가 없으니 달린다고 믿지 않는다(보드가 DTR 하강·워치독으로 세운다)
        assert any("keepalive PING 실패" in r.getMessage() for r in caplog.records)
        fake_serial.ping_error = False
        assert ctl.start()["running"] is True     # 다음 START 가 포트를 다시 열고 keepalive 도 다시 띄운다
        wait_until(lambda: len(keepalive_threads()) == 1, "다시 뜬 keepalive")

    def test_시뮬레이션은_keepalive_를_띄우지_않는다(self, fast_keepalive):
        belt = ConveyorController(ConveyorConfig(port="SIM"), simulate=True)
        belt.start()
        time.sleep(0.1)
        assert keepalive_threads() == [] and belt.state["running"] is True


# ------------------------------------------------------------------ 설정
def _config(tmp_path, text: str):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return load_config(path)


def test_default_duty_기본값과_범위(tmp_path):
    cfg = _config(tmp_path, "conveyors:\n  a: {port: COM1, motor: 1}\n  b: {default_duty: 55.5}\n  c: {default_duty: 100}\n")
    assert [c.default_duty for c in cfg.conveyors.values()] == [40.0, 55.5, 100.0]


@pytest.mark.parametrize("bad", ["0", "-1", "100.5", "abc", ".nan", ".inf", "null"])
def test_범위_밖_default_duty_는_서버가_뜨기_전에_거부한다(tmp_path, bad):
    with pytest.raises(ValueError, match=r"conveyors\.feed\.default_duty"):
        _config(tmp_path, f"conveyors:\n  feed: {{port: COM1, default_duty: {bad}}}\n")


def test_옛_설정_키는_경고만_남기고_무시한다(tmp_path, caplog):
    """사용자 로컬 config.yaml 에 LM 가이드·스텝모터 시절의 키가 남아 있어도 서버는 떠야 한다."""
    text = (
        "conveyors:\n"
        "  feed: {port: COM13, motor: 1, stroke_mm: 100, mm_per_rev: 40}\n"
        "  lm:\n    port: COM13\n    motor: 3\n    stroke_mm: 180\n    mm_per_rev: 20\n"
        "cycle:\n  belt_rpm: 10\n"
    )
    with caplog.at_level(logging.WARNING, logger="backend.config"):
        cfg = _config(tmp_path, text)
    assert list(cfg.conveyors) == ["feed"]                  # lm 절은 통째로 무시한다
    assert not hasattr(cfg.conveyors["feed"], "stroke_mm") and not hasattr(cfg.conveyors["feed"], "mm_per_rev")
    assert cfg.cycle.belt_duty == 40.0                      # belt_rpm 은 무시하고 기본값
    said = " ".join(r.getMessage() for r in caplog.records)
    for key in ("conveyors.lm", "stroke_mm", "mm_per_rev", "belt_rpm"):
        assert key in said, key


def test_config_yaml_은_두_벨트만_갖는다():
    cfg = load_config()
    assert "lm" not in cfg.conveyors and {"feed", "work"} <= set(cfg.conveyors)
    assert all(0.0 < c.default_duty <= 100.0 for c in cfg.conveyors.values())
    assert not any(c.motor == 3 for c in cfg.conveyors.values())


# ------------------------------------------------- 한 축이 막혀도 나머지는 산다
def test_한_축이_포트를_붙잡아도_다른_축이_무한대기하지_않는다(fake_serial):
    """회귀: 두 축이 한 락을 공유해, 한 명령이 막히면 화면 전체가 '대기' 로 굳었다.

    실제로 그랬다 — 펌웨어 업로드가 포트를 잡고 있는 동안 모든 버튼이 응답을
    잃었다. 이제는 기다리다 끊고 이유를 준다.
    """
    feed = ConveyorController(ConveyorConfig(port="COM11", motor=1, timeout_sec=0.2))
    work = ConveyorController(ConveyorConfig(port="COM11", motor=2, timeout_sec=0.2))
    feed.start()                       # 포트를 연다(둘이 공유한다)

    held = threading.Event()
    done = threading.Event()

    def hog():
        with feed._lock:               # 다른 명령이 포트를 오래 붙잡은 상황
            held.set()
            done.wait(timeout=5)

    t = threading.Thread(target=hog, daemon=True)
    t.start()
    held.wait(timeout=2)

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(conveyor_mod, "_CMD_LOCK_TIMEOUT_SEC", 0.2)   # 테스트를 빨리 끝낸다
            start = time.monotonic()
            with pytest.raises(ConveyorError, match="붙잡혀"):
                work.start()
            waited = time.monotonic() - start
        assert waited < 2.0, f"무한 대기하면 안 된다(실제 {waited:.1f}초)"
    finally:
        done.set()
        t.join(timeout=2)


def test_명령_중_상태_폴링은_기다리지_않고_캐시를_준다(fake_serial):
    """폴링이 명령 뒤에 줄을 서면 폴링끼리 쌓여 버튼 요청이 뒤로 밀린다."""
    work = ConveyorController(ConveyorConfig(port="COM11", motor=2, timeout_sec=0.2))
    work.start()

    held = threading.Event()
    done = threading.Event()

    def hog():
        with work._lock:
            held.set()
            done.wait(timeout=5)

    t = threading.Thread(target=hog, daemon=True)
    t.start()
    held.wait(timeout=2)

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(conveyor_mod, "_STATUS_LOCK_TIMEOUT_SEC", 0.1)
            start = time.monotonic()
            state = work.status()        # 예외가 아니라 캐시를 준다
            waited = time.monotonic() - start
        assert waited < 1.0, f"폴링은 기다리면 안 된다(실제 {waited:.1f}초)"
        assert state["stale"] is True    # 캐시라고 표시된다
    finally:
        done.set()
        t.join(timeout=2)


# --------------------------------------------------------------------- 오류
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
    _register_error_handlers(app)       # 실제 앱과 같은 에러 → 상태 코드 변환(400/404/503)
    return app.test_client()


def belts(client) -> list[dict]:
    return client.get("/api/conveyor/status").get_json()["conveyors"]


def test_api_start_stop(client):
    res = client.post("/api/conveyor/start")
    assert res.status_code == 200
    assert res.get_json()["running"] is True

    res = client.post("/api/conveyor/stop")
    assert res.get_json()["running"] is False


def test_api_speed_and_dir(client):
    body = client.post("/api/conveyor/speed", json={"duty": 30}).get_json()
    assert body["ok"] is True and body["duty"] == 30.0 and "target_rpm" not in body
    assert client.post("/api/conveyor/dir", json={"direction": "ccw"}).get_json()["direction"] == "ccw"


@pytest.mark.parametrize("ok", [0, 0.5, 100])
def test_api_speed_경계값은_받는다(client, ok):
    assert client.post("/api/conveyor/speed", json={"duty": ok}).status_code == 200


@pytest.mark.parametrize("body", [
    {"duty": 101}, {"duty": -1}, {"duty": "빠르게"}, {"duty": None}, {},
    {"rpm": 30},                       # 예전 RPM 키는 더 이상 받지 않는다
    None, [30], "30",                  # JSON 객체가 아닌 본문도 500 이 아니라 400 이다
])
def test_api_speed_는_0_100_duty_만_받는다(client, body):
    res = client.post("/api/conveyor/speed", json=body)
    assert res.status_code == 400
    assert res.get_json()["error"]["code"] == "validation_failed"
    assert belts(client)[0]["duty"] == 40.0         # 거부는 아무것도 바꾸지 않는다


def test_api_status_는_전_벨트_목록(client):
    body = client.get("/api/conveyor/status").get_json()
    assert body["ok"] is True
    assert len(body["conveyors"]) == 1
    belt = body["conveyors"][0]
    assert belt["name"] == "main"
    # 계약의 키. RPM·위치·행정·리밋 계열(target_rpm·pos_rev·stroke_mm·limit_rev)은 없다
    assert set(belt) == {"name", "label", "port", "connected", "running", "direction", "duty", "enabled", "motor"}
    assert (belt["running"], belt["direction"], belt["duty"], belt["enabled"]) == (False, "cw", 40.0, True)


def test_이름_경로도_같은_벨트를_제어한다(client):
    # /api/conveyor/main/start 와 /api/conveyor/start 는 같은 벨트다
    assert client.post("/api/conveyor/main/start").get_json()["running"] is True
    assert client.post("/api/conveyor/stop").get_json()["running"] is False


@pytest.mark.parametrize("path", [
    "/api/conveyor/zero", "/api/conveyor/end", "/api/conveyor/move",
    "/api/conveyor/main/zero", "/api/conveyor/main/end", "/api/conveyor/main/move",
])
def test_LM_전용_엔드포인트는_사라졌다(client, path):
    res = client.post(path, json={"mm": 10})
    assert res.status_code == 404
    assert client.get("/api/conveyor/status").status_code == 200      # 나머지는 그대로다


def test_api_달리는_중_방향_변경은_400_이고_방향은_그대로다(client):
    client.post("/api/conveyor/start")
    res = client.post("/api/conveyor/dir", json={"direction": "ccw"})
    assert res.status_code == 400 and "RUNNING" in res.get_json()["error"]["message"]
    assert belts(client)[0]["direction"] == "cw" and belts(client)[0]["running"] is True
    client.post("/api/conveyor/stop")
    assert client.post("/api/conveyor/dir", json={"direction": "ccw"}).get_json()["direction"] == "ccw"


def test_api_드라이버를_끄면_START_는_거부되고_켜면_된다(client):
    assert client.post("/api/conveyor/enable", json={"enabled": False}).get_json()["enabled"] is False
    res = client.post("/api/conveyor/start")
    assert res.status_code == 400 and "DISABLED" in res.get_json()["error"]["message"]
    assert belts(client)[0]["running"] is False
    assert client.post("/api/conveyor/enable", json={"enabled": True}).get_json()["enabled"] is True
    assert client.post("/api/conveyor/start").get_json()["running"] is True


def test_api_정지는_어떤_상태에서도_허용된다(client):
    assert client.post("/api/conveyor/stop").status_code == 200               # 서 있어도
    client.post("/api/conveyor/start")
    assert client.post("/api/conveyor/stop").get_json()["running"] is False   # 달리는 중에도
    client.post("/api/conveyor/enable", json={"enabled": False})
    assert client.post("/api/conveyor/stop").status_code == 200               # 드라이버가 꺼져 있어도


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
