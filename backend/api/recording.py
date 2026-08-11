"""수집 제어 · 로봇 상태 API."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..config import CAMERA_ORDER, DEFECT_TYPES
from ..utils.errors import RecordingStateError
from . import current_hub

bp = Blueprint("recording", __name__, url_prefix="/api")


@bp.get("/meta")
def meta():
    """프론트가 하드코딩하지 않도록 카메라 순서/불량 유형을 서버가 내려준다."""
    hub = current_hub()
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
    hub = current_hub()
    return jsonify(
        {
            "ok": True,
            "recording": hub.recorder.status(),
            "leader": hub.leader.status(),
            "follower": hub.follower.status(),
            "teleop": hub.teleop_status,
            "cameras": hub.cameras.status_all(),
        }
    )


@bp.post("/recording/start")
def start_recording():
    body = request.json or {}
    result = current_hub().recorder.start(
        episode_id=body.get("episode_id", ""),
        object_id=body.get("object_id", ""),
        label=body.get("label", "good"),
        defect_type=body.get("defect_type"),
    )
    return jsonify({"ok": True, "recording": result})


@bp.post("/recording/stop")
def stop_recording():
    result = current_hub().recorder.stop()
    return jsonify({"ok": True, "recording": result})


# ══════════════════════════ 에피소드 관리 ══════════════════════════
def _guard_not_recording(episode_id: str) -> None:
    """녹화 중인 에피소드를 수정/삭제하면 라이터가 사라진 디렉터리에 쓰게 된다."""
    rec = current_hub().recorder.status()
    if rec["state"] != "IDLE" and rec["episode_id"] == episode_id:
        raise RecordingStateError(
            f"에피소드 '{episode_id}' 는 지금 녹화 중입니다. 먼저 STOP 하세요."
        )


@bp.get("/episodes")
def list_episodes():
    return jsonify({"ok": True, "episodes": current_hub().recorder.index.list()})


@bp.patch("/episodes/<episode_id>")
def update_episode(episode_id: str):
    """라벨 오기입 정정. metadata.json 과 분류 이미지 폴더를 함께 옮긴다."""
    body = request.json or {}
    _guard_not_recording(episode_id)
    meta = current_hub().recorder.index.update(
        episode_id,
        object_id=body.get("object_id"),
        label=body.get("label"),
        defect_type=body.get("defect_type"),
    )
    return jsonify({"ok": True, "episode": meta})


@bp.delete("/episodes/<episode_id>")
def delete_episode(episode_id: str):
    _guard_not_recording(episode_id)
    current_hub().recorder.index.delete(episode_id)
    return jsonify({"ok": True})


@bp.post("/emergency_stop")
def emergency_stop():
    """비상 정지 — 텔레오퍼레이션·전 컨베이어·녹화를 즉시 세운다.

    확인창 없이 즉시 실행되는 전제의 엔드포인트다. 프론트도 confirm 을 띄우지
    않는다 — 비상 상황에서 망설임을 요구하는 UI 가 더 위험하다.
    """
    return jsonify({"ok": True, **current_hub().emergency_stop()})


@bp.post("/teleop/start")
def start_teleop():
    return jsonify({"ok": True, "teleop": current_hub().start_teleop()})


@bp.post("/teleop/stop")
def stop_teleop():
    return jsonify({"ok": True, "teleop": current_hub().stop_teleop()})


@bp.post("/arm/home")
def go_home():
    """홈 포지션 복귀. 녹화 중에는 거부한다 — 데이터 중간에 인위적 궤적이 섞이면
    ACT 학습이 오염된다."""
    hub = current_hub()
    if hub.recorder.state.value == "RECORDING":
        raise RecordingStateError("녹화 중에는 홈 복귀를 실행할 수 없습니다. 먼저 STOP 하세요.")

    hub.stop_teleop()
    positions = hub.follower.go_home()
    return jsonify({"ok": True, "joints": [round(p, 2) for p in positions]})
