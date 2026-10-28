"""컨베이어(STM32F411 BlackPill + TB6600 + NEMA17) 제어 API.

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
    """목표 속도를 모터축 RPM 으로 지정한다. 예: {"rpm": 45}

    steps/sec 대신 RPM 을 받는다. 1.8도 모터의 마이크로스텝 환산은 펌웨어가
    하므로(DIP 스위치와 같은 값이 거기 박혀 있다) 호스트는 몰라도 된다.
    """
    _manual()
    body = request.get_json(silent=True) or {}
    return _ok(name, _belt(name).set_speed_rpm(body.get("rpm")))


@bp.post("/dir", defaults={"name": None})
@bp.post("/<name>/dir")
def direction(name):
    """회전 방향. 예: {"direction": "ccw"}

    구동 중에 바꾸면 펌웨어가 잠깐 멈추고 저속에서 다시 가속한다
    (부하가 걸린 상태에서 즉시 반전시키면 탈조한다).
    """
    _manual()
    body = request.get_json(silent=True) or {}
    return _ok(name, _belt(name).set_direction(body.get("direction")))


@bp.post("/enable", defaults={"name": None})
@bp.post("/<name>/enable")
def enable(name):
    """드라이버 활성/해제. 예: {"enabled": false}

    해제하면 홀딩 토크가 풀려 벨트를 손으로 움직일 수 있다. 사이클 중에도 허용하지만 해제는 정지와 같아
    사이클을 중단한다(다음 START 가 드라이버를 다시 켜므로, 사이클이 이어지면 손이 닿은 벨트가 다시 돈다).
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


@bp.post("/zero", defaults={"name": None})
@bp.post("/<name>/zero")
def zero(name):
    """현재 위치를 원점(0)으로 잡는다. 리밋 스위치가 없는 LM 가이드용.

    축을 한쪽 끝으로 옮겨둔 뒤 부른다. 이후 config 의 limit_rev 범위를 벗어나면
    펌웨어가 스스로 멈춘다. 보드 전원을 껐다 켜면 다시 잡아야 한다.
    """
    _manual()
    return _ok(name, _belt(name).set_zero())


@bp.post("/end", defaults={"name": None})
@bp.post("/<name>/end")
def set_end(name):
    """지금 자리를 가동 범위의 끝으로 삼는다(끝점 티칭).

    원점을 잡고 반대쪽 끝까지 몰고 온 뒤 부른다. 자로 재서 config 에 옮겨 적는
    대신 축을 직접 움직여 정하므로 값이 실물과 어긋나지 않는다. 끝점은 파일에
    저장돼 전원을 껐다 켜도 남는다.
    """
    _manual()
    return _ok(name, _belt(name).set_end())


@bp.post("/move", defaults={"name": None})
@bp.post("/<name>/move")
def move(name):
    """그 위치(mm)로 이동하고 스스로 멈춘다. 예: {"mm": 320}"""
    _manual()
    body = request.get_json(silent=True) or {}
    return _ok(name, _belt(name).move_to_mm(body.get("mm")))


@bp.post("/release", defaults={"name": None})
@bp.post("/<name>/release")
def release(name):
    """COM 포트를 놓는다. 펌웨어를 다시 업로드할 때 쓴다.

    포트는 한 번 열면 계속 잡고 있다(여닫이가 잦으면 명령이 유실될 틈이 생긴다).
    그래서 업로드하려면 포트를 비워야 하는데, 그것 때문에 서버를 통째로 내리면
    카메라까지 끊긴다. 이 엔드포인트는 포트만 놓아준다. 다음 START 에서 자동 재연결.
    """
    _manual()
    belt = _belt(name)
    belt.disconnect()
    return jsonify({"ok": True, "name": name, "released": True, **belt.state})
