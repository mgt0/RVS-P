"""컨베이어(STM32F411 BlackPill + HW-095(L298N) + DC 모터) 제어. USB CDC 로 한 줄 텍스트 명령을 주고받는다.

프로토콜은 firmware/conveyor_blackpill/conveyor_blackpill.ino 와 1:1로 맞춰져 있다.
보내는 것: START / STOP / DUTY <pct> / DIR CW|CCW / ENABLE / DISABLE / SERVO <deg> / STATUS / PING
받는 것: "OK: ..." 또는 "ERR: ..." 한 줄. STATUS 는 "OK: STATUS RUN=1 DIR=CW DUTY=40.0 EN=1 SERVO=90".
"READY: ..."·"WARN: HOST TIMEOUT" 은 응답이 아니라 보드가 아무 때나 내는 줄이다 — 응답을 기다리는 동안 건너뛴다.
엔코더가 없어 RPM·위치는 모른다. 속도는 PWM 듀티(%)로만 정한다. 상태의 duty 는 '설정한 속도'(DUTY 0 은 STOP 과
같고 설정값은 그대로 남는다)이고, 실제로 도는지는 running(STATUS 의 RUN)으로 본다.

보드 하나가 DC 모터 2개(M1=공급 벨트 A 채널, M2=작업 벨트 B 채널)를 물고 있다.
config 의 motor 가 1 이상이면 모든 명령 앞에 "M<n> " 를 붙여 모터를 지정하고,
같은 port 를 쓰는 컨트롤러들은 시리얼 핸들과 락을 공유한다(포트는 한 번만 연다).

세 가지가 이 클래스의 존재 이유다.

1) 연결을 명령마다 새로 열지 않는다. 열고 닫기가 잦으면 명령이 유실될 틈이
   생기고, CH340 계열 보드(구 ESP32 리그)는 열 때마다 리셋까지 된다.
2) 처음 열 때 _wait_boot() 로 READY 배너를 잠깐 기다린다. BlackPill 은 네이티브
   USB 라 포트를 열어도 리셋되지 않으므로 배너는 '보드가 방금 부팅했고 호스트가
   이미 듣고 있던' 경우에만 온다 — 그래서 배너가 없어도 그대로 진행한다.
3) 호스트 워치독 keepalive. 펌웨어는 모터가 달리는 중에 HOST_TIMEOUT_MS(기본 2000ms)
   동안 아무 줄도 못 받으면 모든 모터를 세운다 — 서버가 사이클 도중에 죽어도 벨트가
   계속 돌지 않게 하려는 안전장치다. 그래서 벨트가 하나라도 달리는 동안 포트당 스레드
   하나(_Keepalive)가 0.5초마다 PING 을 보낸다. 명령과 같은 락을 쓰므로 줄이 섞이지 않는다.
"""

from __future__ import annotations

import logging
import math
import threading
import time
import weakref
from contextlib import contextmanager

import serial

from ..config import ConveyorConfig
from ..utils.errors import ConveyorError, ConveyorRejectedError, ValidationError

logger = logging.getLogger(__name__)

# 펌웨어 부팅 배너. 이 접두사를 바꾸면 펌웨어의 setup() 도 같이 고쳐야 한다.
_READY_PREFIX = "READY"
# 보드가 리셋된 직후라면 READY 까지 1초 남짓(ESP32 실측). BlackPill 은 보통
# 배너가 안 오고 그게 정상이다 — 타임아웃 후 그대로 진행한다.
# PONG 이 오면 즉시 끝나므로(실측 0.05초) 이 값은 '보드가 대답을 안 할 때' 만
# 쓰인다. 그 상황에서 5초를 통째로 붙잡으면 두 축의 요청이 다 밀리므로 짧게 잡는다.
_BOOT_TIMEOUT_SEC = 2.0
# 속도(PWM 듀티 %)의 범위. 0 은 STOP 과 같다(펌웨어 규약).
_DUTY_MIN, _DUTY_MAX = 0.0, 100.0
_STATUS_PREFIX = "OK: STATUS"

# 한 보드(포트)에 모터가 여럿이라 컨트롤러들이 같은 시리얼을 공유한다.
# 포트당 핸들/락을 하나만 두고, 끊기면 dict 에서 지워 다음 명령이 다시 열게 한다.
_ports: dict[str, serial.Serial] = {}
_port_locks: dict[str, threading.RLock] = {}

# 락을 기다리는 한도. 두 축이 한 포트(=한 락)를 공유하므로, 한 명령이 오래 걸리면
# 나머지 축의 요청이 전부 그 뒤에 줄을 선다. 무한정 기다리면 화면 전체가 '대기'로
# 굳으므로(실측: 포트를 다른 프로세스가 잡았을 때 그랬다) 여기서 끊고 이유를 준다.
_CMD_LOCK_TIMEOUT_SEC = 3.0
# 상태 폴링은 더 짧다. 5초마다 도는 폴링이 명령을 기다리며 밀리면 폴링끼리 쌓여
# 서버 스레드를 다 먹는다. 못 잡으면 캐시를 주는 편이 낫다 — 다음 폴링에 갱신된다.
_STATUS_LOCK_TIMEOUT_SEC = 0.5

# 호스트 워치독 대응(모듈 docstring 3). 펌웨어 HOST_TIMEOUT_MS(기본 2000ms)의 1/4 간격이라,
# 한두 번 놓쳐도(락이 바빠 건너뛴 틱 등) 타임아웃에 닿지 않는다.
_KEEPALIVE_SEC = 0.5
# keepalive 는 명령에 길을 비킨다. 락을 이만큼만 기다리고 못 잡으면 이번 틱은 건너뛴다 — 진행 중인 명령도
# 펌웨어에 줄을 주므로 워치독은 갱신된다. 정지 명령이 keepalive 때문에 밀리면 안 된다.
_KEEPALIVE_LOCK_SEC = 0.4
# PING 응답을 기다리는 상한. 락을 쥔 채 기다리므로 짧게 잡는다(정상이면 수 ms 안에 온다).
_KEEPALIVE_REPLY_SEC = 0.25

# 같은 포트를 쓰는 컨트롤러들. keepalive 가 '누구라도 달리는 중인가' 를 볼 때 쓴다.
# 약한 참조라 컨트롤러가 사라지면 저절로 빠진다. 시뮬레이션 컨트롤러는 포트가 없어 넣지 않는다.
_axes: dict[str, weakref.WeakSet] = {}
# 포트당 keepalive 스레드 하나.
_keepalives: dict[str, _Keepalive] = {}
# _axes·_keepalives 를 지키는 짧은 락. 포트 락(명령)과 별개이며, 이 락을 쥔 채 포트 락을 기다리지 않는다.
_registry_lock = threading.Lock()


def _parse_status(reply: str) -> dict | None:
    """"OK: STATUS RUN=1 DIR=CW DUTY=40.0 EN=1 [SERVO=90]" 를 상태 dict 로. 형식이 다르면 None.

    키 순서에 기대지 않고 KEY=VALUE 쌍으로 읽는다. 모르는 키는 건너뛰고 있는 키만 돌려준다
    (SERVO 는 보드에 하나뿐이라 M1 의 STATUS 에만 실릴 수 있다).
    """
    if not reply.startswith(_STATUS_PREFIX):
        return None
    fields = dict(tok.split("=", 1) for tok in reply[len(_STATUS_PREFIX):].split() if "=" in tok)
    out: dict = {}
    try:
        if "RUN" in fields:
            out["running"] = _flag(fields["RUN"])
        if "EN" in fields:
            out["enabled"] = _flag(fields["EN"])
        if "DIR" in fields:
            direction = fields["DIR"].lower()
            if direction not in ("cw", "ccw"):
                return None
            out["direction"] = direction
        if "DUTY" in fields:
            out["duty"] = float(fields["DUTY"])
            if not math.isfinite(out["duty"]):
                return None
        if "SERVO" in fields:
            out["servo_deg"] = int(float(fields["SERVO"]))
    except ValueError:
        return None
    return out


def _flag(value: str) -> bool:
    if value not in ("0", "1"):
        raise ValueError(value)
    return value == "1"


def _mark_stopped(port: str) -> None:
    """보드와의 연결이 끊겼다 — 같은 포트의 모든 축을 '멈춤'으로 본다(호스트 워치독이 모터를 세운다).

    끊은 축 하나만 지우면 형제 축이 '달리는 중' 으로 굳어 keepalive 가 안 멈추고 사이클 점검이 막힌다.
    """
    with _registry_lock:
        for axis in _axes.get(port, ()):
            axis._state["running"] = False


def _ensure_keepalive(port: str) -> None:
    """포트의 keepalive 를 돌게 한다. 이미 이 연결에 묶여 돌고 있으면 그대로 둔다."""
    with _registry_lock:
        ser = _ports.get(port)
        ka = _keepalives.get(port)
        if ser is None or (ka is not None and ka.is_alive() and ka.ser is ser):
            return
        if ka is not None:
            ka.halt.set()       # 낡은 연결에 묶인 스레드는 새 연결에 자리를 내준다
        ka = _keepalives[port] = _Keepalive(port, ser)
        ka.start()


def _stop_keepalive(port: str) -> None:
    """포트의 keepalive 를 세우고 끝나길 기다린다. 포트 락을 쥔 채 부르지 않는다(스레드가 그 락을 기다릴 수 있다)."""
    with _registry_lock:
        ka = _keepalives.pop(port, None)
    if ka is not None:
        ka.halt.set()
        if ka is not threading.current_thread():
            ka.join(timeout=1.0)


class _Keepalive(threading.Thread):
    """포트 하나당 하나. 벨트가 하나라도 달리는 동안 PING 으로 펌웨어의 호스트 워치독을 갱신한다.

    이 연결(시리얼 핸들)에 묶여 있다 — 포트가 놓이거나 다시 열리면 스스로 끝난다. 아무도 안 달려도 끝난다
    (다음 START 가 다시 띄운다). PING 이 실패해도 경고만 남기고 계속 시도한다 — 스레드가 죽으면
    보호가 조용히 사라지고, 모터는 펌웨어 워치독이 세울 때까지 아무도 모르게 돈다.
    """

    def __init__(self, port: str, ser: serial.Serial) -> None:
        super().__init__(name=f"conveyor-keepalive-{port}", daemon=True)
        self.port, self.ser = port, ser
        self.halt = threading.Event()

    def run(self) -> None:
        warned = False
        while not self.halt.wait(_KEEPALIVE_SEC):
            with _registry_lock:
                runner = next((a for a in _axes.get(self.port, ()) if a._state.get("running")), None)
                if runner is None or _ports.get(self.port) is not self.ser:
                    if _keepalives.get(self.port) is self:
                        del _keepalives[self.port]
                    return
            try:
                runner._ping(self.ser)
                warned = False
            except Exception as exc:  # noqa: BLE001
                if not warned:      # 매 틱마다 찍으면 초당 두 줄이 쌓인다 — 처음 한 번만
                    logger.warning(
                        "컨베이어 keepalive PING 실패(%s): %s. 계속 시도합니다 — 응답이 계속 없으면 펌웨어 워치독이 모터를 세웁니다.",
                        self.port, exc,
                    )
                warned = True


class ConveyorController:
    def __init__(self, cfg: ConveyorConfig, *, simulate: bool = False) -> None:
        self._cfg = cfg
        self._simulate = simulate
        # 중첩 획득이 있다(_command → _refresh → _command). RLock 이어야 한다.
        # 같은 포트의 다른 모터 컨트롤러와도 한 락을 나눠 쓴다(한 선로 = 한 락).
        self._lock = (
            _port_locks.setdefault(cfg.port, threading.RLock())
            if cfg.port else threading.RLock()
        )
        self._state: dict = {
            "running": False,
            "enabled": True,
            "direction": "cw",
            "duty": cfg.default_duty,
        }
        # DUTY 를 마지막으로 보낸 연결(시리얼 핸들). 연결을 새로 열면 달라지고, 그러면 첫 START 전에 다시 보낸다(_sync_duty).
        self._duty_ser: serial.Serial | None = None
        if cfg.port and not simulate:
            with _registry_lock:
                _axes.setdefault(cfg.port, weakref.WeakSet()).add(self)

    @property
    def label(self) -> str:
        """대시보드 표시용 이름. config 에 없으면 포트라도 보여준다."""
        return self._cfg.label or self._cfg.port or "conveyor"

    # ------------------------------------------------------------------ 연결
    @property
    def connected(self) -> bool:
        if self._simulate:
            return True
        ser = _ports.get(self._cfg.port)
        return ser is not None and ser.is_open

    def _ensure_open(self) -> serial.Serial | None:
        if self._simulate:
            return None
        if not self._cfg.port:
            raise ConveyorError("config.yaml 의 conveyor.port 가 설정되지 않았습니다.")
        ser = _ports.get(self._cfg.port)
        if ser is None or not ser.is_open:
            try:
                ser = serial.Serial(self._cfg.port, self._cfg.baudrate, timeout=self._cfg.timeout_sec)
            except (serial.SerialException, OSError) as exc:
                raise ConveyorError(f"{self._cfg.port} 포트를 열 수 없습니다: {exc}") from exc
            _ports[self._cfg.port] = ser
            # 방금 열었으므로 캐시된 구동 상태는 믿을 수 없다. 나머지는 첫 STATUS 가 맞춘다.
            self._state.update(running=False, enabled=True)
            self._wait_boot(ser)
        return ser

    def connect(self) -> None:
        """포트를 연다(이미 열려 있으면 그대로). 첫 명령이 하던 lazy 연결을 명령 없이 미리 한다.

        자동 사이클이 팔을 움직이기 전에 벨트 포트부터 확인하려고 쓴다. 열지 못하면 ConveyorError.
        """
        with self._hold(_CMD_LOCK_TIMEOUT_SEC, "connect"):
            self._ensure_open()

    def _wait_boot(self, ser: serial.Serial) -> None:
        # 수동 대기 대신 PING 을 먼저 쏜다. BlackPill(네이티브 USB)은 리셋이 없어
        # READY 배너가 영영 안 오는데, 그때 타임아웃까지 통째로 기다리면 첫 명령이
        # 항상 5초 늦는다(실측 6.1초). PONG 이 오면 즉시 준비 완료로 판정한다.
        # 리셋형 보드라면 부팅 중이라 PING 이 버려지고 READY 배너가 대신 온다 —
        # 어느 쪽이든 한 경로로 처리된다.
        try:
            ser.write(b"PING\n")
            ser.flush()
        except (serial.SerialException, OSError) as exc:
            raise ConveyorError(f"부팅 확인 PING 전송 실패: {exc}") from exc

        deadline = time.monotonic() + _BOOT_TIMEOUT_SEC
        while time.monotonic() < deadline:
            try:
                raw = ser.readline()
            except (serial.SerialException, OSError) as exc:
                raise ConveyorError(f"부팅 대기 중 포트가 끊겼습니다: {exc}") from exc
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                # timeout_sec 을 0 으로 둔 설정에서 readline() 이 즉시 돌아와
                # CPU 를 태우는 것만 막는다(정상 설정이면 여기 거의 안 온다).
                time.sleep(0.02)
                continue
            logger.debug("컨베이어 부팅 로그: %s", line)
            if line.startswith(_READY_PREFIX) or line.endswith("PONG"):
                logger.info("컨베이어 펌웨어 준비됨 (%s)", self._cfg.port)
                return
        # 배너를 못 봤어도 계속 간다. 이미 부팅이 끝난 뒤 포트를 다시 연 경우
        # (리셋이 안 걸리는 보드/USB 허브) 배너가 안 오는 게 정상이다.
        logger.warning(
            "컨베이어 READY 배너를 %.0f초 안에 받지 못했습니다. 그대로 진행합니다.", _BOOT_TIMEOUT_SEC
        )
        ser.reset_input_buffer()

    def disconnect(self) -> None:
        # 포트는 보드 단위다 — 같은 보드에 물린 다른 모터들도 함께 끊긴다
        # (펌웨어 업로드용이므로 그게 맞다). 각자 다음 명령에서 자동 재연결한다.
        #
        # 락을 못 잡아도 포트는 닫는다. 이 함수는 '포트를 놓아달라'는 요청이고
        # (펌웨어 업로드 직전에 쓴다), 뭔가에 붙잡혀 있을 때야말로 놓아야 한다.
        #
        # 달리던 벨트는 포트를 닫으면(DTR 하강) 펌웨어가 즉시 세우고, 그 신호를 못 받아도 PING 이 끊겨
        # HOST_TIMEOUT_MS 뒤에 워치독이 세운다. 서버 종료도 이 길이다.
        got = self._lock.acquire(timeout=_CMD_LOCK_TIMEOUT_SEC)
        try:
            ser = _ports.pop(self._cfg.port, None)
            if ser is not None:
                try:
                    ser.close()
                except (serial.SerialException, OSError):
                    logger.exception("컨베이어 포트 닫기 실패")
            self._state["running"] = False
            _mark_stopped(self._cfg.port)
        finally:
            if got:
                self._lock.release()
        # 락 밖에서 — keepalive 가 그 락을 기다리는 중일 수 있어 안에서 기다리면 서로 기다린다.
        _stop_keepalive(self._cfg.port)

    # ------------------------------------------------------------------ 통신
    @contextmanager
    def _hold(self, timeout: float, what: str):
        """락을 기한 안에만 기다린다. 못 잡으면 즉시 예외 — 무한 대기가 금지다.

        with self._lock 로 두면 앞선 명령이 끝날 때까지 무한정 기다린다. 포트가
        다른 프로세스에 잡혔거나 보드가 응답을 멈추면 그 한 건이 몇 초를 붙잡고,
        같은 보드를 쓰는 두 축의 요청이 전부 그 뒤에 쌓여 화면이 통째로 굳는다.
        """
        if not self._lock.acquire(timeout=timeout):
            raise ConveyorError(
                f"{self._cfg.port} 가 다른 작업에 붙잡혀 있습니다({what} 대기 {timeout:.1f}초 초과). "
                "펌웨어 업로드나 다른 프로그램이 포트를 쓰고 있지 않은지 확인하세요."
            )
        try:
            yield
        finally:
            self._lock.release()

    def _command(self, command: str) -> str:
        """한 줄 보내고 한 줄 받는다. "ERR: " 응답은 예외로 올린다."""
        with self._hold(_CMD_LOCK_TIMEOUT_SEC, command):
            if self._simulate:
                return self._checked(self._simulated(command), command)

            ser = self._ensure_open()
            assert ser is not None
            # 다축 펌웨어는 "M<n> " 접두사로 모터를 고른다. motor=0 이면 접두사
            # 없이 보낸다(단일 모터 펌웨어 호환; 펌웨어 쪽 기본값은 M1).
            wire = f"M{self._cfg.motor} {command}" if self._cfg.motor else command
            try:
                # 이전 명령의 늦은 응답이 남아 있으면 이번 응답으로 오해한다.
                ser.reset_input_buffer()
                ser.write(f"{wire}\n".encode("ascii"))
                ser.flush()
            except (serial.SerialException, OSError) as exc:
                self._invalidate()
                raise ConveyorError(f"명령 전송 실패: {exc}") from exc

            return self._checked(self._read_reply(ser, command), command)

    @staticmethod
    def _checked(reply: str, command: str) -> str:
        """"ERR: " 응답은 예외(400)로 올린다 — 문구는 펌웨어가 준 그대로다(예: ERR: TIMER CONFLICT).
        시뮬레이션도 같은 길을 지나므로 펌웨어의 거부 동작을 시험할 수 있다."""
        if reply.startswith("ERR"):
            raise ConveyorRejectedError(f"펌웨어가 명령을 거부했습니다: {reply}", detail={"command": command})
        return reply

    def _read_reply(self, ser: serial.Serial, command: str) -> str:
        """의미 있는 첫 줄을 돌려준다. READY·WARN 같은 비동기 줄과 부팅 로그는 건너뛴다."""
        deadline = time.monotonic() + max(self._cfg.timeout_sec, 0.5)
        while time.monotonic() < deadline:
            try:
                line = ser.readline().decode("utf-8", "replace").strip()
            except (serial.SerialException, OSError) as exc:
                self._invalidate()
                raise ConveyorError(f"응답 수신 실패: {exc}") from exc
            if not line:
                continue
            if line.startswith(("OK", "ERR")):
                return line
            self._async_line(line)
        raise ConveyorError(
            f"'{command}' 에 대한 응답이 없습니다. 포트({self._cfg.port})와 펌웨어를 확인하세요.",
            detail={"command": command},
        )

    def _ping(self, ser: serial.Serial) -> None:
        """keepalive 의 PING 한 번. 명령과 같은 락 안에서 한 줄 보내고 한 줄 받는다 — 줄이 섞이지 않는다.

        _command 와 다른 점 셋. 락을 짧게만 기다려 못 잡으면 건너뛰고(정지 명령이 밀리면 안 된다),
        응답도 짧게만 기다리며(락을 쥔 채다), 포트가 놓였으면 _ensure_open 처럼 다시 열지 않는다.
        """
        if not self._lock.acquire(timeout=_KEEPALIVE_LOCK_SEC):
            return      # 명령이 진행 중이다 — 그 명령도 펌웨어에 줄을 주므로 워치독은 갱신된다
        try:
            if _ports.get(self._cfg.port) is not ser or not ser.is_open:
                return  # 그 사이 포트가 놓였다. keepalive 는 다음 틱에 스스로 끝난다
            try:
                ser.reset_input_buffer()
                ser.write(b"PING\n")
                ser.flush()
            except (serial.SerialException, OSError) as exc:
                self._invalidate()
                raise ConveyorError(f"PING 전송 실패: {exc}") from exc
            deadline = time.monotonic() + _KEEPALIVE_REPLY_SEC
            while time.monotonic() < deadline:
                try:
                    # in_waiting 으로 확인하고 읽는다 — readline() 은 timeout_sec(기본 2초)까지 락을 쥔 채 막힌다.
                    if not ser.in_waiting:
                        time.sleep(0.005)
                        continue
                    line = ser.readline().decode("utf-8", "replace").strip()
                except (serial.SerialException, OSError) as exc:
                    self._invalidate()
                    raise ConveyorError(f"PING 응답 수신 실패: {exc}") from exc
                if line.endswith("PONG"):       # "OK: PONG"(지금 펌웨어)·"PONG"(옛 펌웨어)
                    return
                if line:
                    self._async_line(line)
            raise ConveyorError(f"PING 에 {_KEEPALIVE_REPLY_SEC:g}초 안에 응답이 없습니다.")
        finally:
            self._lock.release()

    def _async_line(self, line: str) -> None:
        """응답이 아닌 줄. WARN(예: WARN: HOST TIMEOUT)은 경고로 남기고, 나머지는 잡음이라 디버그로만 남긴다."""
        if line.startswith("WARN"):
            logger.warning("컨베이어 펌웨어 경고(%s): %s", self._cfg.port, line)
        else:
            logger.debug("컨베이어 기타 출력: %s", line)

    def _invalidate(self) -> None:
        """포트가 죽었다. 핸들을 버려 다음 명령에서 재연결하게 한다."""
        ser = _ports.pop(self._cfg.port, None)
        if ser is not None:
            try:
                ser.close()
            except (serial.SerialException, OSError):
                pass
        self._state["running"] = False
        _mark_stopped(self._cfg.port)

    def _refresh(self) -> dict:
        """펌웨어의 STATUS 로 캐시를 갱신한다. 조회가 실패해도 구동 명령 자체는 끝났으므로 캐시를 유지한다."""
        try:
            reply = self._command("STATUS")
        except ConveyorError:
            logger.debug("STATUS 조회 실패, 캐시 상태를 유지합니다.", exc_info=True)
            return self.state
        parsed = _parse_status(reply)
        if parsed is None:
            logger.warning("STATUS 응답을 파싱할 수 없습니다: %s", reply)
        else:
            if self._duty_ser is not _ports.get(self._cfg.port):
                # 이 연결에서 속도를 보낸 적이 없다(_sync_duty). 펌웨어가 기억하던 값이 아니라 첫 START 때 보낼 값을 보여준다.
                parsed.pop("duty", None)
            self._state.update(parsed)
        return self.state

    # ------------------------------------------------------------------ 명령
    def start(self) -> dict:
        self._sync_duty()
        # 드라이버가 DISABLE(비상 정지 뒤 등)이면 펌웨어가 "ERR: DISABLED" 로 거부한다 → ConveyorError.
        # 다시 켜려면 set_enabled(True) 부터 해야 한다.
        self._command("START")
        self._state["running"] = True
        if not self._simulate:
            _ensure_keepalive(self._cfg.port)   # running 을 세운 뒤에 부른다 — 스레드가 막 끝나는 순간과 겹쳐도 새로 뜬다
        return self._refresh()

    def _sync_duty(self) -> None:
        """연결을 새로 연 뒤 첫 START 전에, 호스트가 아는 듀티를 한 번 펌웨어에 보낸다.

        BlackPill 은 호스트가 접속을 끊어도 리셋되지 않아 펌웨어가 지난번 듀티를 기억한다. 그대로 START 하면
        화면에 보이는 속도(config default_duty)와 실제 속도가 다른 채로 벨트가 돈다.
        이 연결에서 이미 DUTY 를 보냈으면(set_duty·이전 START) 건너뛴다.
        """
        if self._simulate:
            return
        with self._hold(_CMD_LOCK_TIMEOUT_SEC, "DUTY"):
            ser = self._ensure_open()
            if ser is not self._duty_ser:
                self._command(f"DUTY {self._state['duty']:.3f}")
                self._duty_ser = ser

    def stop(self) -> dict:
        self._command("STOP")
        self._state["running"] = False
        return self._refresh()

    def set_duty(self, pct) -> dict:
        """속도를 PWM 듀티(%, 0~100)로 정한다. 달리는 중이면 펌웨어가 램프로 반영한다.

        0 은 STOP 과 같다 — 세우기만 하고 설정한 속도는 그대로 남는다(다음 START 가 그 속도로 돈다).
        """
        try:
            value = float(pct)
        except (TypeError, ValueError):
            raise ValidationError(f"duty 는 숫자여야 합니다: {pct!r}") from None
        if not _DUTY_MIN <= value <= _DUTY_MAX:       # NaN 도 여기서 걸린다
            raise ValidationError(f"duty 는 {_DUTY_MIN:.0f}~{_DUTY_MAX:.0f} 이어야 합니다: {value}")
        self._command(f"DUTY {value:.3f}")
        if value > 0:
            self._state["duty"] = value
            self._duty_ser = _ports.get(self._cfg.port)     # 이 연결에는 속도를 보냈다 — 첫 START 가 되돌리지 않는다
        else:
            self._state["running"] = False
        return self._refresh()

    def set_direction(self, direction: str) -> dict:
        """회전 방향. 달리는 중이면 펌웨어가 "ERR: RUNNING" 으로 거부한다 — 먼저 정지해야 한다."""
        key = str(direction).strip().lower()
        if key not in ("cw", "ccw"):
            raise ValidationError(f"direction 은 'cw' 또는 'ccw' 여야 합니다: {direction!r}")
        self._command(f"DIR {key.upper()}")
        self._state["direction"] = key
        return self._refresh()

    def set_enabled(self, enabled: bool) -> dict:
        """드라이버 허용/차단. 차단(DISABLE)은 브레이크로 세우고, 다시 허용하기 전까지 START 를 거부한다."""
        self._command("ENABLE" if enabled else "DISABLE")
        self._state["enabled"] = bool(enabled)
        if not enabled:
            self._state["running"] = False
        return self._refresh()

    def set_servo_deg(self, deg) -> dict:
        """분류 게이트 서보 각도(0~180). 벨트 위 물체를 양품/불량 라인으로 가른다."""
        try:
            value = int(deg)
        except (TypeError, ValueError):
            raise ValidationError(f"deg 는 정수여야 합니다: {deg!r}") from None
        if not 0 <= value <= 180:
            raise ValidationError(f"deg 는 0~180 이어야 합니다: {value}")
        self._command(f"SERVO {value}")
        self._state["servo_deg"] = value
        return self._refresh()

    # ------------------------------------------------------------------ 상태
    @property
    def state(self) -> dict:
        return {
            "connected": self.connected,
            "port": self._cfg.port,
            "motor": self._cfg.motor,
            **self._state,
        }

    def status(self) -> dict:
        """대시보드 폴링용. 연결돼 있지 않으면 포트를 열지 않는다.

        여기서 _ensure_open() 을 부르면 /release 로 놓아준 포트를 상태 폴링이
        다시 붙잡는다. 리셋형 보드(CH340 계열)라면 돌던 모터까지 멈추고,
        BlackPill 이라도 DFU 업로드 중이면 방해가 된다. 열지 않는 게 맞다.

        폴링은 절대 기다리지 않는다. 명령이 진행 중이면 캐시를 주고 물러난다 —
        5초마다 도는 폴링이 명령 뒤에 줄을 서면 폴링끼리 쌓여 서버가 그것만
        처리하게 되고, 정작 버튼 요청이 뒤로 밀린다(화면 전체가 '대기'로 굳는다).
        """
        if not self.connected:
            return self.state
        if not self._lock.acquire(timeout=_STATUS_LOCK_TIMEOUT_SEC):
            # 지금 명령이 오가는 중이다. 다음 폴링이 갱신하므로 캐시로 충분하다.
            return {**self.state, "stale": True}
        try:
            return self._refresh()
        finally:
            self._lock.release()

    # ------------------------------------------------------------- 시뮬레이션
    def _simulated(self, command: str) -> str:
        """하드웨어 없이 UI/파이프라인만 확인할 때의 응답(simulate=True). 펌웨어의 명령/응답 규약을 따른다.

        캐시(_state)가 곧 '펌웨어 상태'다. 램프·호스트 워치독은 흉내내지 않는다(keepalive 도 시뮬레이션에서는 돌지 않는다).
        """
        s = self._state
        if command == "START":
            if not s["enabled"]:
                return "ERR: DISABLED"      # 비상 정지 뒤에는 '드라이버 켜기' 전까지 구동할 수 없다
            s["running"] = True
            return "OK: RUN"
        if command == "STOP":
            s["running"] = False
            return "OK: STOP"
        if command.startswith("DUTY "):
            try:
                pct = float(command[5:])
            except ValueError:
                pct = math.nan
            if not _DUTY_MIN <= pct <= _DUTY_MAX:
                return "ERR: DUTY 범위는 0~100"
            if pct == 0.0:
                s["running"] = False        # 펌웨어 규약: 0 이면 STOP 과 같고 설정한 듀티는 그대로 남는다
            else:
                s["duty"] = pct
            return f"OK: DUTY={pct:g}"
        if command.startswith("DIR "):
            arg = command[4:].strip().upper()
            if arg not in ("CW", "CCW"):
                return "ERR: DIR 인자는 CW 또는 CCW"
            if s["running"]:
                return "ERR: RUNNING"
            s["direction"] = arg.lower()
            return f"OK: DIR={arg}"
        if command == "ENABLE":
            s["enabled"] = True
            return "OK: ENABLED"
        if command == "DISABLE":
            s.update(enabled=False, running=False)
            return "OK: DISABLED"
        if command.startswith("SERVO "):
            try:
                deg = int(command[6:])
            except ValueError:
                deg = -1
            if not 0 <= deg <= 180:
                return "ERR: SERVO 각도는 0~180"
            s["servo_deg"] = deg
            return f"OK: SERVO={deg}"
        if command == "STATUS":
            servo = f" SERVO={s['servo_deg']}" if "servo_deg" in s else ""
            return (f"{_STATUS_PREFIX} RUN={int(s['running'])} DIR={s['direction'].upper()} "
                    f"DUTY={s['duty']:g} EN={int(s['enabled'])}{servo}")
        if command == "PING":
            return "OK: PONG"
        return f"ERR: UNKNOWN COMMAND: {command}"
