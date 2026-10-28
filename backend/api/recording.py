"""수집 제어 · 로봇 상태 API."""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from ..config import CAMERA_ORDER, DEFECT_TYPES
from ..core.cycle import CycleBusyError
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
            # 프리뷰를 어느 포트에서 받을지. 브라우저의 오리진당 연결 한도(6개)를
            # 스트림이 다 먹지 않도록 분리한다 — 자세한 이유는 ServerConfig 주석.
            "stream_port": hub.cfg.server.stream_port,
        }
    )


@bp.get("/status")
def status():
    """대시보드 메인 폴링 엔드포인트. 한 번에 모든 상태를 반환."""
    hub = current_hub()
    # 지금 녹화를 시작하면 기록될 카메라 간 동기 오차(ms). 2대 미만이면 오차라는 개념이 없어 None.
    frames, skew_ms = hub.cameras.snapshot()
    return jsonify(
        {
            "ok": True,
            "recording": hub.recorder.status(),
            "leader": hub.leader.status(),
            "follower": hub.follower.status(),
            "teleop": hub.teleop_status,
            "autorun": hub.autorun_status,
            "demo": hub.demo.status(),
            "cycle": hub.cycle.status(),
            "cameras": hub.cameras.status_all(),
            "sync_skew_ms": round(skew_ms, 1) if len(frames) >= 2 else None,
        }
    )


@bp.post("/recording/start")
def start_recording():
    if current_hub().cycle.active:    # 사이클이 도는 동안의 움직임은 시연 데이터가 아니다
        raise CycleBusyError("녹화를 시작")
    if current_hub().demo.active:    # 세션이 녹화를 쥐고 있다 — 수동 시작이 끼면 세션의 에피소드와 엉킨다
        raise RecordingStateError("시연 세션이 진행 중입니다. 세션을 끝낸 뒤 수동으로 시작하세요.")
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
    if current_hub().demo.active:    # 세션 스레드가 녹화를 쥐고 있다 — 수동 정지가 끼면 그 스레드의 stop 과 겹쳐 에피소드를 잃는다
        raise RecordingStateError(
            "시연 세션이 진행 중이라 녹화를 수동으로 정지할 수 없습니다. 세션의 '다음'(저장) 또는 '끝내기'를 쓰세요."
        )
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


@bp.post("/emergency_reset")
def emergency_reset():
    """비상 정지 해제 — 비상 정지가 꺼둔(DISABLE) 벨트 드라이버를 다시 켠다.

    펌웨어는 DISABLE 상태의 START 를 거부하므로 비상 정지 뒤에는 이걸 먼저 해야 다시 구동할 수 있다.
    ENABLE 만 한다. 벨트를 돌리거나 축을 옮기지 않는다 — 다시 구동하는 것은 사람이
    각 START 로 한다. 한 축이 실패해도 나머지는 계속 시도한다(emergency_stop 과 같은 원칙).

    연결된 벨트만 대상으로 한다(status() 처럼). 끊겼거나 release 한 벨트에 ENABLE 을 보내면 포트를 다시 연다 —
    펌웨어를 업로드하는 중이면 방해가 된다. 건너뛴 벨트는 "not_connected" 로 알린다.
    """
    result: dict = {}
    for name, ctrl in current_hub().conveyors.items():
        if not ctrl.connected:
            result[name] = "not_connected"
            continue
        try:
            ctrl.set_enabled(True)
            result[name] = "enabled"
        except Exception as exc:  # noqa: BLE001
            result[name] = {"error": str(exc)}
    return jsonify({"ok": True, "conveyors": result})


@bp.post("/teleop/start")
def start_teleop():
    return jsonify({"ok": True, "teleop": current_hub().start_teleop()})


@bp.post("/teleop/stop")
def stop_teleop():
    return jsonify({"ok": True, "teleop": current_hub().stop_teleop()})


@bp.post("/rollout/start")
def start_rollout():
    """학습된 ACT 정책으로 팔로워 자율 제어 시작.

    body: {"checkpoint": "<pretrained_model 폴더 경로>", "rate_hz": 10}
       또는 {"waypoints": "<to_waypoints.py 가 만든 JSON 경로>", "speed": 1.0}

    waypoints 는 학습 없이 시연 궤적을 그대로 되돌리는 경로다(신경망 미사용).
    checkpoint는 lerobot-train 학습 로그 마지막 줄에 출력된 경로를 그대로 쓴다
    (예: outputs/train/<job_name>/checkpoints/<step>/pretrained_model). 이 서버는
    학습을 별도 프로세스로 돌리므로(act_trainer.LeRobotTrainer 참고) 어떤 체크포인트가
    최신인지 스스로 알 수 없어 자동 선택을 지원하지 않는다.
    """
    body = request.json or {}
    result = current_hub().start_autorun(
        checkpoint=body.get("checkpoint"),
        rate_hz=int(body.get("rate_hz", 10)),
        waypoints=body.get("waypoints"),
        speed=float(body.get("speed", 1.0)),
    )
    return jsonify({"ok": True, "autorun": result})


@bp.post("/rollout/stop")
def stop_rollout():
    return jsonify({"ok": True, "autorun": current_hub().stop_autorun()})


@bp.get("/rollout/status")
def rollout_status():
    return jsonify({"ok": True, "autorun": current_hub().autorun_status})


@bp.post("/arm/home")
def go_home():
    """홈 포지션 복귀. 녹화 중에는 거부한다 — 데이터 중간에 인위적 궤적이 섞이면
    ACT 학습이 오염된다."""
    hub = current_hub()
    if hub.cycle.active:    # 사이클의 autorun 이 팔로워를 쥐고 있다 — 홈 복귀가 끼면 두 쪽이 같은 팔에 명령을 쓴다
        raise CycleBusyError("홈 복귀를 실행")
    if hub.recorder.state.value == "RECORDING":
        raise RecordingStateError("녹화 중에는 홈 복귀를 실행할 수 없습니다. 먼저 STOP 하세요.")

    hub.stop_teleop()
    positions = hub.follower.go_home()
    return jsonify({"ok": True, "joints": [round(p, 2) for p in positions]})
