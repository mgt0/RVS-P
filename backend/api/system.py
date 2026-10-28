"""시스템 점검 API — 알림 목록, 진단 정보, 최근 학습 실행.

알림은 '지금 사람이 알아야 할 것'의 목록이다. 상태 폴링(/api/status)이 값만 주는 것과 달리
원인·영향·해결 방법을 서버가 문장으로 쥔다 — 프론트의 알림 창은 이 구조를 범용으로 그리기만 한다.
문구는 확인된 사실만 쓴다. 원인을 모르는 경우(예: WinError 4551 이 아닌 OSError)는 단정하지 않고
오류 내용을 증거로 싣는다.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Blueprint, current_app, jsonify, request

from ..core import system_check
from ..core.cycle import BASKET_KO
from ..core.rig_calibration import rig_summary
from . import current_hub
from .ports import _arm_status   # 포트 화면과 같은 판정(시뮬레이션에서는 컨트롤러 상태)을 쓴다

bp = Blueprint("system", __name__, url_prefix="/api")

ALERTS_TTL_SEC = 5
DIAGNOSTICS_TTL_SEC = 30
# 가장 오래된 배경 캡처가 이보다 묵으면 조명·배치가 바뀌었을 수 있다고 알린다.
# ponytail: 고정 7일. 현장 조명이 자주 바뀌면 config 로 뺀다.
BACKGROUND_STALE_SEC = 7 * 24 * 3600
_SEVERITY_ORDER = {"fault": 0, "warn": 1, "info": 2}
_ARM_NAMES = {"leader": "리더", "follower": "팔로워"}

_BG_STEPS = ["스캔대에서 물체를 모두 치웁니다.", "검사 화면에서 '배경 캡처'를 누릅니다.", "바로 이어서 촬영합니다."]
_BG_NOTE = "가능하면 검사 자리 뒤(C2·C4 시야)에 단색 배경판을 둡니다."
_TORCH_ELSEWHERE = "torch가 동작하는 다른 PC에서 학습합니다. lerobot_data 폴더를 옮긴 뒤 scripts/train_act.py를 실행합니다."


class _State:
    """앱 하나의 점검 캐시와 알림 최초 감지 시각."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.since: dict[str, float] = {}
        self.alerts: tuple[float, dict] | None = None
        self.diagnostics: tuple[float, dict] | None = None


def _state() -> _State:
    return current_app.extensions.setdefault("omx_system", _State())


def _alert(id_: str, severity: str, route: str, title: str, summary: str, *, problem: str, impact: str,
           cause: str, steps: list[str], note: str | None = None, evidence: dict | None = None) -> dict:
    return {
        "id": id_, "severity": severity, "title": title, "summary": summary,
        "detail": {"problem": problem, "impact": impact, "cause": cause, "steps": steps,
                   "note": note, "evidence": evidence or {}},
        "action": {"label": "관련 화면 열기", "route": route},
    }


def _when(ts: float) -> str:
    dt = datetime.fromtimestamp(ts)
    return f"{dt.month}/{dt.day} {dt:%H:%M}"


# ---------------------------------------------------------------- 알림 규칙
def _env_alerts() -> list[dict]:
    out: list[dict] = []
    torch = system_check.torch_status()
    if torch["state"] == "blocked":
        policy = torch["policy_block"]
        out.append(_alert(
            "torch_blocked", "warn", "diagnostics", "CNN 분류 · rollout 사용 불가",
            ("애플리케이션 제어 정책(스마트 앱 컨트롤 등)이 torch DLL을 차단했습니다. " if policy
             else "torch DLL을 불러오지 못했습니다. ") + "수집·검사 형상·학습 로그는 정상입니다.",
            problem="서버 기동 때 import torch 가 OSError 로 실패했습니다.",
            impact="이 PC에서 ACT 학습, 자율 실행(rollout), CNN 판정을 쓸 수 없습니다. 텔레오퍼레이션, 녹화, 3D 검사, 대시보드는 정상입니다.",
            cause=("애플리케이션 제어 정책(Windows 스마트 앱 컨트롤 등)이 torch DLL을 차단했습니다(WinError 4551)." if policy
                   else "OS가 torch DLL 로드를 거부했습니다. 원인은 증거의 오류 내용으로 확인하세요."),
            steps=([_TORCH_ELSEWHERE,
                    "또는 이 PC에서 스마트 앱 컨트롤을 끕니다(Windows 보안 → 앱 및 브라우저 컨트롤 → 스마트 앱 컨트롤 설정). "
                    "끈 뒤 다시 켤 수 있는지는 Windows 버전마다 달라 확인이 필요합니다."] if policy
                   else ["증거의 오류 내용으로 원인을 확인합니다(누락된 런타임 DLL, 보안 정책 등).", _TORCH_ELSEWHERE]),
            evidence={"error": torch["error"]},
        ))
    elif torch["state"] == "missing":
        out.append(_alert(
            "torch_missing", "info", "diagnostics", "torch 를 불러오지 못함",
            "CNN 분류·rollout·ACT 학습 래퍼를 쓸 수 없습니다. 수집·검사 형상은 정상입니다.",
            problem="서버 기동 때 import torch 가 ImportError 로 실패했습니다.",
            impact="CNN 분류, 자율 실행(rollout), ACT 학습 래퍼를 쓸 수 없습니다. 텔레오퍼레이션, 녹화, 3D 검사, 대시보드는 정상입니다.",
            cause=("torch 패키지가 설치되어 있지 않습니다." if torch["not_installed"]
                   else "torch 설치가 손상됐거나 torch 가 필요로 하는 모듈이 빠져 있습니다."),
            steps=["가상환경(.venv)에서 pip install -r requirements.txt 로 torch 를 설치합니다.",
                   "설치한 뒤 서버를 다시 시작합니다. import 결과는 기동 때 한 번만 기록됩니다."],
            evidence={"error": torch["error"]} if torch["error"] else {},
        ))
    if not system_check.is_installed("lerobot"):
        out.append(_alert(
            "lerobot_missing", "info", "diagnostics", "lerobot 미설치",
            "학습 래퍼와 rollout 에 필요한 패키지입니다.",
            problem="lerobot 패키지를 찾을 수 없습니다.",
            impact="LeRobot 데이터셋 변환(scripts/to_lerobot.py), ACT 학습(scripts/train_act.py), 자율 실행(rollout)을 쓸 수 없습니다. "
                   "텔레오퍼레이션, 녹화, 3D 검사, 대시보드는 정상입니다.",
            cause="lerobot 은 선택 설치라 기본 환경(requirements.txt)에 들어 있지 않습니다.",
            steps=["pip install -r requirements-lerobot.txt 를 실행합니다. 설치하기 전에 README 의 'Windows에서 실제로 겪은 문제' 절을 읽어 두세요."],
        ))
    return out


def _rig_alerts(root: Path, now: float) -> list[dict]:
    out: list[dict] = []
    if not rig_summary(root)["calibrated"]:
        out.append(_alert(
            "calib_missing", "info", "diagnostics", "리그 캘리브레이션 없음",
            "형상은 근사치이고 치수(mm)는 표시되지 않습니다.",
            problem="카메라 캘리브레이션 파일(rig_calibration.json)이 없습니다.",
            impact="3D 검사가 공칭 카메라 각도와 정사영 근사로 동작합니다. 치수(mm)를 표시할 수 없고, 형상과 주황(이상) 표시의 정확도가 떨어집니다.",
            cause="아직 캘리브레이션을 하지 않았습니다.",
            steps=["python scripts/make_calib_cube.py 로 마커 큐브 전개도(A4)를 '실제 크기 100%'로 인쇄해 50mm 큐브에 붙입니다.",
                   "컨베이어를 멈추고 큐브를 검사 위치에 놓습니다.",
                   "진단 화면에서 '캘리브레이션 촬영'을 3~5번 합니다. 그중 1~2장은 큐브를 기울여서 찍습니다.",
                   "'계산'을 누릅니다."],
            note="촬영만 하며 모터는 움직이지 않습니다. 벨트가 돌고 있으면 촬영을 거부합니다.",
        ))

    files = sorted((root / "background").glob("*.jpg"))
    if not files:
        out.append(_alert(
            "background_missing", "warn", "inspect", "배경 캡처 없음",
            "빈 스캔대 배경이 없어 3D 형상이 부풀 수 있습니다.",
            problem="빈 스캔대 배경 캡처(background/*.jpg)가 없습니다.",
            impact="배경 차분을 못 쓰고 흑백 Otsu 로 실루엣을 뽑습니다. 이 리그에서는 배경의 선반·창문 등이 물체보다 크게 잡혀 3D 형상이 부풀 수 있습니다.",
            cause="배경 캡처를 한 적이 없습니다.",
            steps=_BG_STEPS, note=_BG_NOTE,
        ))
        return out

    oldest = min(files, key=lambda p: p.stat().st_mtime)
    captured = oldest.stat().st_mtime
    if now - captured > BACKGROUND_STALE_SEC:
        days = int((now - captured) // 86400)
        out.append(_alert(
            "background_stale", "info", "inspect", f"배경 캡처 {days}일 경과",
            f"{_when(captured)} 캡처. 조명이나 배치가 바뀌었으면 다시 찍으세요.",
            problem=f"가장 오래된 빈 배경 캡처가 {_when(captured)}입니다.",
            impact="그 뒤 조명이나 주변 물건이 바뀌었다면 실루엣이 오염됩니다. 그러면 3D 형상이 부풀고 주황 오탐이 늘어납니다.",
            cause="배경 캡처를 촬영 직전에 다시 하지 않았습니다.",
            steps=_BG_STEPS, note=_BG_NOTE,
            evidence={"camera": oldest.stem, "captured_at": datetime.fromtimestamp(captured).isoformat(timespec="seconds"),
                      "age_days": days},
        ))
    return out


def _device_alerts(hub) -> list[dict]:
    out: list[dict] = []
    for role in ("leader", "follower"):
        st = _arm_status(hub, role)
        if st["connected"]:
            continue
        name = f"{hub.cfg.arms[role].model} {_ARM_NAMES[role]}"
        out.append(_alert(
            f"arm_offline:{role}", "warn", "diagnostics", f"{name} 연결 안 됨",
            st["error"] or "시리얼 포트에서 이 팔을 찾지 못했습니다.",
            problem=f"{name} 의 USB 포트를 찾지 못했습니다.",
            impact="텔레오퍼레이션을 시작할 수 없습니다. 연결되지 않은 채 녹화하면 이 팔의 관절 값이 마지막 자세로 굳은 데이터가 저장됩니다.",
            cause="서버가 연결된 시리얼 포트에서 이 팔로 지정된 장치를 찾지 못했습니다.",
            steps=["USB 케이블과 전원을 확인합니다.", "진단 화면에서 '포트 다시 찾기'를 누릅니다.",
                   "그래도 안 잡히면 '포트 식별'로 포트를 다시 확정합니다."],
            evidence={"role": role, "error": st["error"]},
        ))
    for cam in hub.cameras.status_all():
        if cam["connected"]:
            continue
        label = cam["label"]
        out.append(_alert(
            f"camera_offline:{cam['name']}", "warn", "equipment", f"{label} 신호 없음",
            cam["error"] or "프레임 수신 없음",
            problem=f"{label} 에서 최근 2초 안에 받은 프레임이 없습니다.",
            impact="녹화와 3D 검사에 이 카메라의 영상을 쓸 수 없습니다.",
            cause=cam["error"] or "프레임 수신 없음",
            steps=["장비 제어 화면에서 해당 카메라 카드의 신호를 확인합니다.",
                   "USB 케이블과 허브 연결을 확인합니다. 케이블을 다시 꽂으면 서버가 2초 간격으로 다시 연결을 시도해 자동으로 복구됩니다.",
                   "진단 화면의 카메라 표에서 장치 이름이 맞는지 확인합니다."],
            evidence={"slot": cam["name"], "device": cam["device"], "error": cam["error"]},
        ))
    return out


def _run_alerts(hub) -> list[dict]:
    out: list[dict] = []
    fault = hub.teleop_status["fault"]
    if fault:
        out.append(_alert(
            "teleop_fault", "fault", "equipment", "텔레오퍼레이션이 안전 정지했습니다", str(fault),
            problem="텔레오퍼레이션 루프가 오류로 멈췄습니다.",
            impact="팔로워가 리더를 따라가지 않습니다. 이 상태로 녹화하면 팔로워 관절 값이 굳은 데이터가 저장됩니다.",
            cause=str(fault),
            steps=["리더 팔의 USB 케이블과 전원을 확인합니다.",
                   "장비 제어 화면에서 텔레오퍼레이션을 정지한 뒤 다시 시작합니다.",
                   "반복되면 진단 화면에서 '연결 시험'을 눌러 통신을 확인합니다."],
            evidence={"fault": fault},
        ))
    fault = hub.autorun_status.get("fault")
    if fault:
        out.append(_alert(
            "autorun_fault", "fault", "equipment", "자율 실행(rollout)이 안전 정지했습니다", str(fault),
            problem="rollout 루프가 오류로 멈췄습니다.",
            impact="팔로워가 학습된 정책(또는 웨이포인트)을 따라 움직이지 않습니다.",
            cause=str(fault),
            steps=["비상 정지 또는 POST /api/rollout/stop 으로 rollout 을 정리합니다.",
                   "오류 내용을 확인해 원인을 고친 뒤 다시 시작합니다."],
            evidence={"fault": fault},
        ))
    rec = hub.recorder.status()
    # last_error 는 Recorder.abort 가 남기고 다음 start 가 지운다.
    if rec["last_error"] and rec["state"] == "IDLE":
        out.append(_alert(
            "recording_aborted", "warn", "collect", "녹화가 중단되어 저장되지 않음", f"사유: {rec['last_error']}",
            problem="진행 중이던 녹화가 중단됐습니다.",
            impact="중단된 에피소드는 저장되지 않고 폴더째 삭제됩니다.",
            cause=rec["last_error"],
            steps=["원인을 해결한 뒤 수집 화면에서 다시 녹화합니다.", "이 알림은 다음 녹화를 시작하면 사라집니다."],
            evidence={"last_error": rec["last_error"], "episode_id": rec["episode_id"]},
        ))
    return out


def _cycle_alerts(hub) -> list[dict]:
    out: list[dict] = []
    cycle = hub.cycle.status(include_ready=False)       # 상태·바스켓만 본다 — 시작 전 점검(장치 훑기)은 필요 없다
    # 사용자가 정지한 사이클(aborted)은 알림이 아니다. 오류로 끝난 것만 다음 사이클을 시작할 때까지 남는다.
    if cycle["state"] == "failed":
        failed = next((s["label"] for s in cycle["steps"] if s["state"] == "failed"), None)
        out.append(_alert(
            "cycle_fault", "fault", "overview", "자동 사이클이 오류로 멈췄습니다", str(cycle["error"]),
            problem="자동 사이클이 오류로 끝났습니다.",
            impact="벨트와 자율 실행은 정지했습니다. 게이트와 로봇팔은 멈춘 자리 그대로라 물체가 벨트 위나 그리퍼에 남아 있을 수 있습니다.",
            cause=str(cycle["error"]),
            steps=["오류 내용을 확인해 원인을 고칩니다(팔로워·벨트 연결, 카메라 신호, config.yaml 의 cycle 설정).",
                   "벨트 위나 그리퍼에 남은 물체를 치웁니다.",
                   "상단 '사이클 시작'으로 다시 시작합니다. 시작할 때 게이트를 닫힘 각도로 되돌립니다."],
            evidence={"error": cycle["error"], "step": failed},
        ))
    for key, n in cycle["baskets"].items():
        if n["count"] < n["capacity"]:
            continue
        ko = BASKET_KO[key]
        out.append(_alert(
            f"basket_full:{key}", "warn", "overview", f"{ko} 바스켓을 비워 주세요",
            f"{ko} {n['count']}/{n['capacity']}개 — 가득 차서 새 사이클을 시작할 수 없습니다.",
            problem=f"{ko} 바스켓의 개수가 용량({n['capacity']}개)에 닿았습니다.",
            impact="바스켓을 비우고 '비웠습니다'를 누르기 전에는 자동 사이클을 시작할 수 없습니다.",
            cause=f"사이클이 {ko} 물체를 {n['count']}개 분류했습니다. 바스켓 개수는 서버가 세며 서버를 다시 켜면 0 으로 돌아갑니다.",
            steps=[f"{ko} 바스켓의 물체를 꺼냅니다.", "상단 사이클 영역의 '비웠습니다'를 누릅니다. 개수가 0 으로 돌아갑니다."],
            evidence={"basket": key, "count": n["count"], "capacity": n["capacity"]},
        ))
    return out


def _collect_alerts(hub, now: float) -> list[dict]:
    alerts = (_env_alerts() + _rig_alerts(hub.cfg.recording.root, now)
              + _device_alerts(hub) + _run_alerts(hub) + _cycle_alerts(hub))
    alerts.sort(key=lambda a: _SEVERITY_ORDER[a["severity"]])   # 안정 정렬 — 같은 등급은 규칙 순서 그대로
    return alerts


def _alerts_payload(hub, refresh: bool) -> dict:
    st = _state()
    with st.lock:
        now = time.time()
        if not refresh and st.alerts and now - st.alerts[0] < ALERTS_TTL_SEC:
            return st.alerts[1]
        alerts = _collect_alerts(hub, now)
        # since = 이 API 가 그 알림을 처음 본 시각(서버가 따로 폴링하지 않는다). 해소되면 지우고, 다시 생기면
        # 새 시각이 붙는다 — 프론트의 '확인함'은 {id: since} 로 저장해 since 가 바뀌면 다시 알린다.
        live = {a["id"] for a in alerts}
        for gone in [k for k in st.since if k not in live]:
            del st.since[gone]
        for a in alerts:
            a["since"] = st.since.setdefault(a["id"], now)
        payload = {
            "ok": True,
            "generated_at": round(now, 1),
            "counts": {sev: sum(1 for a in alerts if a["severity"] == sev) for sev in _SEVERITY_ORDER},
            "alerts": alerts,
        }
        st.alerts = (now, payload)
        return payload


@bp.get("/alerts")
def alerts():
    return jsonify(_alerts_payload(current_hub(), request.args.get("refresh") == "1"))


# ---------------------------------------------------------------- 진단
def _diagnostics_payload(hub) -> dict:
    cfg = hub.cfg
    serial = []
    for role in ("leader", "follower"):
        arm = cfg.arms[role]
        st = _arm_status(hub, role)
        serial.append({
            "id": role,
            "name": f"{arm.model} {_ARM_NAMES[role]}",
            "match": {"by": "serial_number" if arm.serial_number else ("port_hint" if arm.port_hint else None),
                      "hint": arm.port_hint},
            "port": st["device"],
            "connected": st["connected"],
            "error": st["error"],
            "serial_number": st["serial_number"],
        })
    # 보드 하나에 모터 여러 개가 같은 포트를 나눠 쓴다(config.yaml conveyors). 포트별로 한 줄.
    by_port: dict[str, list[str]] = {}
    for name, conv in cfg.conveyors.items():
        by_port.setdefault(conv.port, []).append(name)
    for port, names in by_port.items():
        motors = [f"M{cfg.conveyors[n].motor}" for n in names if cfg.conveyors[n].motor]
        serial.append({
            "id": "conveyor" if len(by_port) == 1 else f"conveyor:{port or '-'}",
            "name": f"컨베이어 보드({'·'.join(motors)})" if motors else "컨베이어 보드",
            "match": None,
            "port": port or None,
            "axes": names,
            "connected": all(hub.conveyors[n].connected for n in names),
            "error": None,
        })

    live = {c["name"]: c for c in hub.cameras.status_all()}
    cameras = []
    for slot, cam in cfg.cameras.items():
        st = live.get(slot, {})
        cameras.append({
            "slot": slot,
            "label": cam.label or slot,
            "device": st.get("device"),
            "settings": {"width": cam.width, "height": cam.height, "fps": cam.fps, "fourcc": cam.fourcc,
                         "exposure": cam.exposure, "power_line_hz": cam.power_line_hz,
                         "vflip": cam.vflip, "hflip": cam.hflip},
            "roi": list(cam.roi) if cam.roi else None,
            "used_in_3d": cam.view_dir is not None,
            "connected": st.get("connected", False),
            "error": st.get("error"),
        })

    return {
        "ok": True,
        "env": system_check.env_checks(),
        "serial": serial,
        "cameras": cameras,
        "limits": {
            "joint_limits": [list(pair) for pair in cfg.safety.joint_limits],
            "home": list(cfg.arms["follower"].home_position),
            "max_relative_target": cfg.safety.max_relative_target,
            # 홈 복귀 속도. 장비 제어 화면의 확인 창과 요청 타임아웃(이동 시간 = 각도 ÷ 속도)이 쓴다.
            "homing_speed_deg_per_s": cfg.safety.homing_speed_deg_per_s,
            "max_sync_skew_ms": cfg.recording.max_sync_skew_ms,
            "sample_rate_hz": cfg.recording.sample_rate_hz,
        },
    }


@bp.get("/diagnostics")
def diagnostics():
    """진단 화면. 30초 캐시 — 환경·설정은 거의 안 변하고 포트 열거(OS 호출)가 가볍지 않다.

    실시간 연결 상태는 /api/ports 를 쓴다. 이 응답의 connected 는 최대 30초 낡을 수 있다.
    """
    st = _state()
    with st.lock:
        now = time.time()
        if request.args.get("refresh") != "1" and st.diagnostics and now - st.diagnostics[0] < DIAGNOSTICS_TTL_SEC:
            return jsonify(st.diagnostics[1])
        payload = _diagnostics_payload(current_hub())
        st.diagnostics = (now, payload)
        return jsonify(payload)


# ---------------------------------------------------------------- 학습 실행
@bp.get("/train/runs/latest")
def latest_train_run():
    """logs/train 의 가장 최근 실행 요약. 없으면 run 이 null. 따라 읽기를 시작하지 않는다(부작용 없음)."""
    return jsonify({"ok": True, "run": current_hub().training.latest_run()})
