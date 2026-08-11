"""컨베이어(ESP32+TB6600+NEMA17) 제어 API.

벨트가 여러 대다(공급 벨트 + 로봇 작업 영역 벨트). 경로의 <name> 이
config.yaml 의 conveyors 키와 일치한다. 이름을 생략한 예전 경로는
첫 번째 벨트로 위임해 기존 클라이언트가 깨지지 않게 한다.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

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
    return _ok(name, _belt(name).start())


@bp.post("/stop", defaults={"name": None})
@bp.post("/<name>/stop")
def stop(name):
    return _ok(name, _belt(name).stop())


@bp.post("/speed", defaults={"name": None})
@bp.post("/<name>/speed")
def speed(name):
    """목표 속도를 모터축 RPM 으로 지정한다. 예: {"rpm": 45}

    steps/sec 대신 RPM 을 받는다. 1.8도 모터의 마이크로스텝 환산은 펌웨어가
    하므로(DIP 스위치와 같은 값이 거기 박혀 있다) 호스트는 몰라도 된다.
    """
    body = request.get_json(silent=True) or {}
    return _ok(name, _belt(name).set_speed_rpm(body.get("rpm")))


@bp.post("/dir", defaults={"name": None})
@bp.post("/<name>/dir")
def direction(name):
    """회전 방향. 예: {"direction": "ccw"}

    구동 중에 바꾸면 펌웨어가 잠깐 멈추고 저속에서 다시 가속한다
    (부하가 걸린 상태에서 즉시 반전시키면 탈조한다).
    """
    body = request.get_json(silent=True) or {}
    return _ok(name, _belt(name).set_direction(body.get("direction")))


@bp.post("/enable", defaults={"name": None})
@bp.post("/<name>/enable")
def enable(name):
    """드라이버 활성/해제. 예: {"enabled": false}

    해제하면 홀딩 토크가 풀려 벨트를 손으로 움직일 수 있다.
    """
    body = request.get_json(silent=True) or {}
    return _ok(name, _belt(name).set_enabled(bool(body.get("enabled", True))))


@bp.post("/release", defaults={"name": None})
@bp.post("/<name>/release")
def release(name):
    """COM 포트를 놓는다. 펌웨어를 다시 업로드할 때 쓴다.

    포트는 한 번 열면 계속 잡고 있다(닫으면 ESP32 가 리셋돼 모터가 멈춘다).
    그래서 업로드하려면 포트를 비워야 하는데, 그것 때문에 서버를 통째로 내리면
    카메라까지 끊긴다. 이 엔드포인트는 포트만 놓아준다. 다음 START 에서 자동 재연결.
    """
    belt = _belt(name)
    belt.disconnect()
    return jsonify({"ok": True, "name": name, "released": True, **belt.state})
