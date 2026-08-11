"""수집 제어 · 로봇 상태 API."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from ..config import CAMERA_ORDER, DEFECT_TYPES

bp = Blueprint("recording", __name__, url_prefix="/api")


def _hub():
    return current_app.extensions["omx_hub"]


@bp.get("/meta")
def meta():
    """프론트가 하드코딩하지 않도록 카메라 순서/불량 유형을 서버가 내려준다."""
    hub = _hub()
    return jsonify(
        {
            "ok": True,
            "cameras": list(CAMERA_ORDER),
            "defect_types": list(DEFECT_TYPES),
            "sample_rate_hz": hub.cfg.recording.sample_rate_hz,
            "max_relative_target": hub.cfg.safety.max_relative_target,
            "next_episode_id": hub.recorder.index.next_episode_id(),
        }
    )


@bp.get("/status")
def status():
    """대시보드 메인 폴링 엔드포인트. 한 번에 모든 상태를 반환."""
    hub = _hub()
    return jsonify(
        {
            "ok": True,
            "recording": hub.recorder.status(),
            "leader": hub.leader.status(),
            "follower": hub.follower.status(),
            "teleop": hub.teleop_status,
            "autorun": hub.autorun_status,
            "cameras": hub.cameras.status_all(),
        }
    )


@bp.post("/recording/start")
def start_recording():
    body = request.json or {}
    result = _hub().recorder.start(
        episode_id=body.get("episode_id", ""),
        object_id=body.get("object_id", ""),
        label=body.get("label", "good"),
        defect_type=body.get("defect_type"),
    )
    return jsonify({"ok": True, "recording": result})


@bp.post("/recording/stop")
def stop_recording():
    result = _hub().recorder.stop()
    return jsonify({"ok": True, "recording": result})


@bp.post("/teleop/start")
def start_teleop():
    return jsonify({"ok": True, "teleop": _hub().start_teleop()})


@bp.post("/teleop/stop")
def stop_teleop():
    return jsonify({"ok": True, "teleop": _hub().stop_teleop()})


@bp.post("/rollout/start")
def start_rollout():
    """학습된 ACT 정책으로 팔로워 자율 제어 시작.

    body: {"checkpoint": "<경로 또는 생략>", "rate_hz": 10}
    checkpoint 생략 시 가장 최근 학습 완료된 체크포인트를 자동으로 쓴다.
    """
    body = request.json or {}
    result = _hub().start_autorun(
        checkpoint=body.get("checkpoint"),
        rate_hz=int(body.get("rate_hz", 10)),
    )
    return jsonify({"ok": True, "autorun": result})


@bp.post("/rollout/stop")
def stop_rollout():
    return jsonify({"ok": True, "autorun": _hub().stop_autorun()})


@bp.get("/rollout/status")
def rollout_status():
    return jsonify({"ok": True, "autorun": _hub().autorun_status})


@bp.post("/arm/home")
def go_home():
    """홈 포지션 복귀. 녹화 중에는 거부한다 — 데이터 중간에 인위적 궤적이 섞이면
    ACT 학습이 오염된다."""
    hub = _hub()
    from ..utils.errors import RecordingStateError

    if hub.recorder.state.value == "RECORDING":
        raise RecordingStateError("녹화 중에는 홈 복귀를 실행할 수 없습니다. 먼저 STOP 하세요.")

    hub.stop_teleop()
    positions = hub.follower.go_home()
    return jsonify({"ok": True, "joints": [round(p, 2) for p in positions]})
