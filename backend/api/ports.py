"""포트 모니터링 API."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..core.port_finder import scan_serial_ports
from ..utils.errors import ArmConnectionError, ArmPortNotFoundError, ValidationError

bp = Blueprint("ports", __name__, url_prefix="/api/ports")


def _hub():
    return current_app.extensions["omx_hub"]


def _arm_status(hub, role: str) -> dict:
    """포트 해석 결과 + 컨트롤러 실제 연결 상태를 합친 뷰.

    시뮬레이션 모드에는 물리 포트가 없으므로 포트 스캔만으로는 항상 OFFLINE 이
    된다. 사용자에게는 "이 arm 을 지금 쓸 수 있는가"가 유일한 관심사이므로
    컨트롤러 상태를 우선한다.
    """
    if hub.cfg.server.simulate:
        arm = hub.leader if role == "leader" else hub.follower
        return {
            "role": role,
            "connected": arm.connected,
            "device": f"simulated://{role}",
            "serial_number": "SIM",
            "error": None,
        }
    return hub.ports.status(role)


@bp.get("")
def list_ports():
    """대시보드 PORT MONITORING 섹션 전체를 한 번에 채우는 응답.

    카메라 상태까지 같이 내려주는 이유: 프론트에서 2번 폴링하면 두 응답 사이에
    상태가 갈려 '연결된 포트 수' 배지가 깜빡인다. 한 스냅샷으로 묶는다.
    """
    hub = _hub()
    arms = [_arm_status(hub, role) for role in ("leader", "follower")]
    cameras = hub.cameras.status_all()
    connected = sum(1 for a in arms if a["connected"]) + sum(1 for c in cameras if c["connected"])

    return jsonify(
        {
            "ok": True,
            "arms": arms,
            "cameras": cameras,
            "serial_ports": [p.to_dict() for p in scan_serial_ports()],
            "connected_count": connected,
            "total_count": len(arms) + len(cameras),
            "simulate": hub.cfg.server.simulate,
        }
    )


@bp.post("/test")
def test_connections():
    """실제로 열고 ping 까지 해본다. 목록에 보이는 것과 통신되는 것은 다르다."""
    hub = _hub()
    results = []

    for role in ("leader", "follower"):
        entry = {"role": role, "ok": False, "message": None}
        try:
            hub.ports.resolve(role)
            arm = hub.leader if role == "leader" else hub.follower
            arm.connect()
            arm.read()
            entry.update(ok=True, message=f"{role.upper()} arm 통신 정상")
        except (ArmPortNotFoundError, ArmConnectionError) as exc:
            entry.update(ok=False, message=exc.message)
        results.append(entry)

    for cam in hub.cameras.status_all():
        results.append(
            {
                "role": f"camera_{cam['name']}",
                "ok": cam["connected"],
                "message": f"{cam['resolution']} @ {cam['fps']}fps"
                if cam["connected"]
                else (cam["error"] or "프레임 없음"),
            }
        )

    return jsonify({"ok": all(r["ok"] for r in results), "results": results})


@bp.post("/identify/start")
def identify_start():
    role = (request.json or {}).get("role")
    if role not in {"leader", "follower"}:
        raise ValidationError("role 은 'leader' 또는 'follower' 여야 합니다.")
    result = _hub().identify.start(role)
    return jsonify({"ok": True, **result})


@bp.post("/identify/confirm")
def identify_confirm():
    role = (request.json or {}).get("role")
    if role not in {"leader", "follower"}:
        raise ValidationError("role 은 'leader' 또는 'follower' 여야 합니다.")
    result = _hub().identify.confirm(role)
    return jsonify({"ok": True, **result})


@bp.post("/refresh")
def refresh():
    """오버라이드 캐시를 비우고 다시 스캔. 케이블을 바꿔 꽂았을 때 사용."""
    _hub().ports.clear_overrides()
    return list_ports()
