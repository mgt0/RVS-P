"""컨베이어(STM32F411 BlackPill + HW-095(L298N) + DC 모터) 제어 API.

벨트가 여러 대다(공급 벨트 + 로봇 작업 영역 벨트). 경로의 <name> 이
config.yaml 의 conveyors 키와 일치한다. 이름을 생략한 예전 경로는
첫 번째 벨트로 위임해 기존 클라이언트가 깨지지 않게 한다.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..core.cycle import CycleBusyError
from ..utils.errors import ValidationError
from . import current_hub

bp = Blueprint("conveyor", __name__, url_prefix="/api/conveyor")


def _belt(name: str | None):
    conveyors = current_hub().conveyors
    if name is None:
        # 이름 생략 = 첫 벨트. 단일 벨트 시절의 클라이언트/스크립트 호환.
        return next(iter(conveyors.values()))
    try:
        return conveyors[name]
    except KeyError:
        raise ValidationError(
            f"'{name}' 벨트가 없습니다. 설정된 벨트: {', '.join(conveyors)}"
        ) from None


def _ok(name: str | None, state: dict):
    # 모든 응답이 같은 모양이어야 프론트가 분기 없이 상태를 갱신할 수 있다.
    return jsonify({"ok": True, "name": name, **state})


def _cycle():
    # 벨트만 붙인 최소 허브(테스트)에는 사이클이 없다.
    return getattr(current_hub(), "cycle", None)


def _manual() -> None:
    """자동 사이클이 벨트를 쥐고 있는 동안 수동 구동 명령을 거부한다. stop 은 이걸 부르지 않는다 — 정지는 늘 허용한다."""
    cycle = _cycle()
    if cycle is not None and cycle.active:
        raise CycleBusyError("벨트를 조작")


def _abort_cycle() -> None:
    """사람이 벨트를 세웠는데 사이클이 다음 단계에서 다시 돌리면 안 된다 — 사이클도 함께 중단한다.

    플래그만 세운다. 모터는 사이클 스레드가 세우고, 이 요청의 정지는 지금 바로 세운다.
    """
    cycle = _cycle()
    if cycle is not None:
        cycle.abort("벨트가 수동으로 정지되어 사이클을 중단했습니다.")


@bp.get("/status")
def status():
    """전 벨트 상태. 폴링해도 안전하다(연결이 끊겨 있으면 포트를 열지 않는다)."""
    hub = current_hub()
    return jsonify({
        "ok": True,
        "conveyors": [
            {"name": name, "label": ctrl.label, **ctrl.status()}
            for name, ctrl in hub.conveyors.items()
        ],
    })


@bp.post("/start", defaults={"name": None})
@bp.post("/<name>/start")
def start(name):
    _manual()
    return _ok(name, _belt(name).start())


@bp.post("/stop", defaults={"name": None})
@bp.post("/<name>/stop")
def stop(name):
    belt = _belt(name)
    _abort_cycle()      # 정지는 사이클 중에도 허용한다 — 대신 사이클도 멈춘다
    return _ok(name, belt.stop())


@bp.post("/speed", defaults={"name": None})
@bp.post("/<name>/speed")
def speed(name):
    """속도를 PWM 듀티(%)로 지정한다. 예: {"duty": 40}  (0~100, 0 은 정지와 같다)

    엔코더가 없는 DC 모터라 RPM 은 알 수 없다. 달리는 중이면 펌웨어가 램프로 반영한다.
    범위를 벗어나거나 duty 가 없으면(예전 {"rpm": ..} 포함) 400 이다.
    """
    _manual()
    body = request.get_json(silent=True)
    duty = body.get("duty") if isinstance(body, dict) else None
    return _ok(name, _belt(name).set_duty(duty))


@bp.post("/dir", defaults={"name": None})
@bp.post("/<name>/dir")
def direction(name):
    """회전 방향. 예: {"direction": "ccw"}

    달리는 중에는 펌웨어가 거부한다(ERR: RUNNING). 먼저 정지한 뒤 바꾼다.
    """
    _manual()
    body = request.get_json(silent=True) or {}
    return _ok(name, _belt(name).set_direction(body.get("direction")))


@bp.post("/enable", defaults={"name": None})
@bp.post("/<name>/enable")
def enable(name):
    """드라이버 활성/해제. 예: {"enabled": false}

    해제하면 모터가 브레이크로 서고(EN 이 5V 고정이라 프리런은 없다), 다시 켜기 전에는 START 를 펌웨어가 거부한다.
    사이클 중에도 허용하지만 해제는 정지와 같아 사이클을 중단한다(이어지면 다음 START 가 ERR: DISABLED 로 거부당해
    엉뚱한 오류로 끝난다).
    """
    body = request.get_json(silent=True) or {}
    enabled = bool(body.get("enabled", True))
    belt = _belt(name)
    if not enabled:
        _abort_cycle()
    return _ok(name, belt.set_enabled(enabled))


@bp.post("/servo", defaults={"name": None})
@bp.post("/<name>/servo")
def servo(name):
    """분류 게이트 서보 각도. 예: {"deg": 45}  (0~180)"""
    _manual()
    body = request.get_json(silent=True) or {}
    return _ok(name, _belt(name).set_servo_deg(body.get("deg")))


@bp.post("/release", defaults={"name": None})
@bp.post("/<name>/release")
def release(name):
    """COM 포트를 놓는다. 펌웨어를 다시 업로드할 때 쓴다.

    포트는 한 번 열면 계속 잡고 있다(여닫이가 잦으면 명령이 유실될 틈이 생긴다).
    그래서 업로드하려면 포트를 비워야 하는데, 그것 때문에 서버를 통째로 내리면
    카메라까지 끊긴다. 이 엔드포인트는 포트만 놓아준다. 다음 START 에서 자동 재연결.
    달리던 벨트는 포트를 닫는 순간(DTR 하강) 펌웨어가 세우고, 못 받아도 PING 이 끊겨 호스트 워치독(HOST_TIMEOUT_MS,
    기본 2초)이 세운다.
    """
    _manual()
    belt = _belt(name)
    belt.disconnect()
    return jsonify({"ok": True, "name": name, "released": True, **belt.state})
