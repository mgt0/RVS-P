"""자동 사이클(집기 → 이송 → 검사 → 분류) — 순서·판정·중단·수동 조작 거부.

지켜야 할 것:
1. 단계 순서가 고정이고 벨트 명령이 그 순서대로 나간다. 양품은 벨트를 돌려 보내고, 불량은 분기점에서 세운 뒤 게이트를 열었다 닫는다.
2. 신뢰도 낮음(판정 불가)은 불량으로 간다. 촬영이 실패하면 사이클이 실패로 끝나고 벨트가 선다.
3. 중단(사용자 정지·비상 정지·벨트 수동 정지)은 대기 도중에도 즉시 먹고, 벨트와 autorun 만 세우며 다음 단계에 들어가지 않는다.
4. 비상 정지는 사이클을 가장 먼저 끊는다. 그 호출이 실패해도 기존 정지는 그대로 진행한다.
5. 사이클이 도는 동안 서버가 수동 조작을 거부한다(벨트 stop 은 예외). 시작 전 점검에 걸리면 시작하지 않고 이유를 준다.
6. 바스켓이 차면 시작을 거부하고, 비운 뒤에 허용한다.
7. 점검을 통과한 사이클 스레드가 락·포트 앞에서 기다리는 사이 비상 정지·서버 종료가 끝나는 경합(이벤트로 순서를 강제한다):
   정지 뒤에는 팔을 움직이지 않고, DISABLE 뒤에는 START 가 끼어들지 않으며, 닫은 벨트 포트를 다시 열지 않는다.
8. 벨트 포트는 시작할 때 팔이 움직이기 전에 연다(점검은 열지 않고 막지도 않는다). 비상 정지로 꺼진 드라이버는 리셋해야 시작한다.
하드웨어 없이 시뮬레이션 허브로 돌고, 수집 루트는 임시 폴더다. 시간 손잡이는 0.01초로 줄이고, 3D 촬영은 가짜로 바꿔 판정을 고정한다
(실제 촬영 함수와 이어지는지는 따로 한 번만 확인한다).
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("OMX_SIMULATE", "1")

from backend.api import analysis as analysis_api  # noqa: E402
from backend.app import create_app  # noqa: E402
from backend.config import PROJECT_ROOT, ConveyorConfig, CycleConfig, _cycle, load_config  # noqa: E402
from backend.core import cycle as cycle_mod  # noqa: E402
from backend.core.conveyor import ConveyorController  # noqa: E402
from backend.core.cycle import CycleRunner, judge  # noqa: E402
from backend.utils.errors import ConveyorError  # noqa: E402

REAL_CAPTURE = analysis_api.run_scan_capture
CYCLE_KEYS = {"state", "step", "steps", "started_at", "ended_at", "message", "error", "result",
              "baskets", "ready", "completed"}
STEP_IDS = ["pick", "to_scan", "inspect", "sort"]
NOISE = ("STATUS", "LIMIT")     # 상태 조회·리밋 설정은 구동 순서와 무관하다


def stats(flagged: float = 0.002, level: str = "ok", reasons: list[str] | None = None) -> dict:
    return {"quality": {"level": level, "reasons": reasons or []}, "anomaly": {"flagged_ratio": flagged}}


class FakeCapture:
    """/api/scan/capture 의 본문(run_scan_capture)을 대신한다. 부른 시점의 벨트 상태를 적어 둔다."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.stats = stats()
        self.error: Exception | None = None

    def __call__(self, hub, settle, resolution):
        self.calls.append({"settle": settle, "resolution": resolution,
                           "moving": [n for n, c in hub.conveyors.items() if c.state.get("running")]})
        if self.error:
            raise self.error
        return {"ok": True, "capture_id": "20261028_153000", "cameras": ["cam1", "cam2"],
                "mesh": {"stats": self.stats}}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("cycle")
    cfg = load_config()
    cfg = replace(cfg, server=replace(cfg.server, simulate=True), recording=replace(cfg.recording, root=root))
    app = create_app(cfg)
    hub = app.extensions["omx_hub"]
    time.sleep(1.0)     # 카메라 워커 워밍업 — 신호가 있어야 '재구성용 카메라 2대' 점검을 통과한다
    pose = hub.follower.read()
    waypoints = root / "waypoints" / "pick.json"
    waypoints.parent.mkdir()
    # 지금 자세를 그대로 되돌리는 0.05초짜리 궤적 — 팔이 움직이지 않아 도착 대기 없이 곧바로 끝난다.
    waypoints.write_text(json.dumps({"waypoints": [{"t": 0, "joints": pose}, {"t": 0.05, "joints": pose}]}), encoding="utf-8")
    fast = replace(hub.cfg.cycle, pick_waypoints=waypoints, pick_timeout_sec=10.0, pick_settle_sec=0.0,
                   to_scan_sec=0.01, settle_sec=0.0, resolution=32, good_run_sec=0.01, defect_to_branch_sec=0.01,
                   gate_hold_sec=0.01, basket_capacity=2)
    with app.test_client() as client:
        yield SimpleNamespace(app=app, c=client, hub=hub, root=root, pose=pose, waypoints=waypoints, fast=fast)
    hub.shutdown()


def wait_until(predicate, what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError(f"{timeout}초 안에 '{what}' 이(가) 되지 않았습니다")


@pytest.fixture(autouse=True)
def reset(env, monkeypatch):
    """테스트마다 같은 출발점: 사이클·바스켓 새로 시작, 텔레옵·autorun·녹화 없음, 벨트 정지, 짧은 시간 손잡이, 가짜 촬영."""
    def clean():
        hub = env.hub
        hub.cycle.stop()
        hub.cycle = CycleRunner(hub)
        for attr in ("_teleop", "_autorun"):        # 테스트가 끼운 가짜(stop 이 없다)는 그냥 치운다
            loop = getattr(hub, attr)
            if loop is not None and not hasattr(loop, "stop"):
                setattr(hub, attr, None)
        hub.stop_teleop()
        hub.stop_autorun()
        if hub.recorder.state.value != "IDLE":
            hub.recorder.abort("테스트 정리")
        hub.recorder._last_error = None
        for arm in (hub.leader, hub.follower):
            arm.connect()
        for belt in hub.conveyors.values():
            belt.stop()
            belt.set_enabled(True)
        hub.cfg = replace(hub.cfg, cycle=env.fast)

    clean()
    env.capture = FakeCapture()
    monkeypatch.setattr(analysis_api, "run_scan_capture", env.capture)
    # '재구성용 카메라 2대' 점검이 부하로 흔들리는 실제 신호 시각에 기대지 않게 한다(system 시험과 같은 방식).
    live = [{**c, "connected": True, "error": None} for c in env.hub.cameras.status_all()]
    monkeypatch.setattr(env.hub.cameras, "status_all", lambda: live)
    yield
    clean()


def knobs(env, **overrides) -> None:
    env.hub.cfg = replace(env.hub.cfg, cycle=replace(env.fast, **overrides))


def belt(env) -> ConveyorController:
    return env.hub.conveyors[env.fast.belt]     # 설정을 일부러 틀리게 만드는 시험에서도 같은 벨트를 본다


def spy(env, monkeypatch) -> list[str]:
    """사이클 벨트로 나가는 펌웨어 명령을 순서대로 적는다(상태 조회·리밋 제외는 commands() 가 한다)."""
    log: list[str] = []
    real = belt(env)._command

    def record(command):
        log.append(command)
        return real(command)

    monkeypatch.setattr(belt(env), "_command", record)
    return log


def commands(log: list[str]) -> list[str]:
    return [c for c in log if not c.startswith(NOISE)]


def cycle_of(env) -> dict:
    return env.c.get("/api/cycle/status").get_json()["cycle"]


def start(env):
    return env.c.post("/api/cycle/start")


def wait_cycle(env, timeout: float = 15.0) -> dict:
    wait_until(lambda: cycle_of(env)["state"] != "running", "사이클 종료", timeout)
    return cycle_of(env)


def step_states(cycle: dict) -> dict:
    return {s["id"]: s["state"] for s in cycle["steps"]}


def message(res) -> str:
    return res.get_json()["error"]["message"]


def run_in_to_scan(env) -> None:
    """이송 단계에서 벨트가 도는 채로 멈춰 있게 한다(중단·수동 조작 시험용)."""
    knobs(env, to_scan_sec=30.0)
    assert start(env).status_code == 200
    wait_until(lambda: cycle_of(env)["step"] == "to_scan" and belt(env).state["running"], "이송 중 벨트 구동")


# ------------------------------------------------------------------ 모양 · 판정
class TestShape:
    def test_대기_상태의_모양(self, env):
        res = env.c.get("/api/cycle/status")
        assert res.status_code == 200 and res.get_json()["ok"] is True
        c = res.get_json()["cycle"]
        assert set(c) == CYCLE_KEYS
        assert c["state"] == "idle" and c["step"] is None and c["result"] is None and c["error"] is None
        assert c["started_at"] is None and c["ended_at"] is None and c["completed"] == 0
        assert [s["id"] for s in c["steps"]] == STEP_IDS
        assert [s["label"] for s in c["steps"]] == ["집기", "이송", "검사", "분류"]
        assert set(step_states(c).values()) == {"pending"}
        cap = env.hub.cfg.cycle.basket_capacity
        assert c["baskets"] == {"good": {"count": 0, "capacity": cap}, "defect": {"count": 0, "capacity": cap}}
        assert c["ready"] == {"ok": True, "reasons": [], "notes": []}
        json.dumps(c)

    def test_api_status_에_cycle_키가_있다(self, env):
        d = env.c.get("/api/status").get_json()
        assert set(d["cycle"]) == CYCLE_KEYS and d["cycle"]["state"] == "idle"
        assert {"recording", "teleop", "autorun", "demo", "cameras"} <= set(d)

    def test_판정_규칙(self):
        # 신뢰도가 낮으면 이상 비율이 0 이어도 불량 — 판정 불가 물체를 양품 라인으로 보내지 않는다
        low = judge(stats(0.0, "low", ["카메라 간 형상 일치도 0.30 < 0.5"]), 0.01)
        assert low["verdict"] == "defect" and low["quality"] == "low"
        assert low["reason"].startswith("신뢰도 낮음: ") and "일치도" in low["reason"]
        assert judge(stats(0.0, "low"), 0.01)["reason"].startswith("신뢰도 낮음: ")     # 이유가 비어도 문장이 된다
        # 기준 이상이면 불량(경계 포함), 미만이면 양품
        assert judge(stats(0.01), 0.01)["verdict"] == "defect"
        assert judge(stats(0.0099), 0.01)["verdict"] == "good"
        ok = judge(stats(0.0021), 0.01)
        assert ok == {"verdict": "good", "reason": "표면 이상 0.21% (기준 1.00% 미만)", "flagged_ratio": 0.0021, "quality": "ok"}
        assert "1.50%" in judge(stats(0.015), 0.01)["reason"]


# ------------------------------------------------------------------ 흐름
class TestFlow:
    def test_양품은_벨트를_돌려_보내고_양품_개수가_는다(self, env, monkeypatch):
        log = spy(env, monkeypatch)
        fast = env.fast
        res = start(env)
        assert res.status_code == 200 and res.get_json()["cycle"]["state"] == "running"
        assert res.get_json()["cycle"]["ready"]["reasons"] == ["사이클이 진행 중입니다."]

        c = wait_cycle(env)
        assert c["state"] == "done" and c["error"] is None and c["step"] is None
        assert step_states(c) == {i: "done" for i in STEP_IDS}
        assert c["result"] == {"verdict": "good", "reason": "표면 이상 0.20% (기준 1.00% 미만)",
                               "capture_id": "20261028_153000", "flagged_ratio": 0.002, "quality": "ok"}
        assert c["baskets"]["good"]["count"] == 1 and c["baskets"]["defect"]["count"] == 0
        assert c["completed"] == 1 and c["message"] == "완료: 양품"
        assert isinstance(c["started_at"], float) and c["ended_at"] >= c["started_at"]
        # 방향·속도·게이트는 사이클이 명시적으로 정하고, 벨트는 이송 → 양품 이송 순으로 두 번 돈다
        assert commands(log) == [
            f"DIR {fast.belt_dir.upper()}", f"RPM {fast.belt_rpm:.3f}", f"SERVO {fast.gate_close_deg}",
            "START", "STOP",          # 이송
            "START", "STOP",          # 양품 쪽으로
        ]
        # 촬영은 벨트가 선 뒤에, 설정한 해상도로 한 번. settle 대기는 사이클이 끊을 수 있게 직접 하고 촬영 함수에는 0 을 넘긴다
        assert env.capture.calls == [{"settle": 0.0, "resolution": fast.resolution, "moving": []}]
        assert not env.hub.autorun_status["running"] and not belt(env).state["running"]

    def test_불량은_분기점에서_세운_뒤_게이트를_열었다_닫는다(self, env, monkeypatch):
        env.capture.stats = stats(0.015)
        log = spy(env, monkeypatch)
        fast = env.fast
        assert start(env).status_code == 200
        c = wait_cycle(env)
        assert c["state"] == "done" and c["result"]["verdict"] == "defect" and "1.50%" in c["result"]["reason"]
        assert c["baskets"]["defect"]["count"] == 1 and c["baskets"]["good"]["count"] == 0
        assert c["message"] == "완료: 불량"
        assert commands(log) == [
            f"DIR {fast.belt_dir.upper()}", f"RPM {fast.belt_rpm:.3f}", f"SERVO {fast.gate_close_deg}",
            "START", "STOP",                                    # 이송
            "START", "STOP",                                    # 분기점까지 — 세운 다음에야
            f"SERVO {fast.gate_open_deg}", f"SERVO {fast.gate_close_deg}",     # 게이트 열림 → 닫힘
        ]

    def test_신뢰도가_낮으면_불량_라인으로_간다(self, env):
        env.capture.stats = stats(0.0, "low", ["쓸 수 있는 카메라가 2대뿐"])
        assert start(env).status_code == 200
        c = wait_cycle(env)
        assert c["state"] == "done"
        assert c["result"]["verdict"] == "defect" and c["result"]["quality"] == "low"
        assert c["result"]["reason"] == "신뢰도 낮음: 쓸 수 있는 카메라가 2대뿐"
        assert c["baskets"]["defect"]["count"] == 1 and c["baskets"]["good"]["count"] == 0

    def test_촬영이_실패하면_사이클이_실패로_끝나고_벨트가_선다(self, env, monkeypatch):
        env.capture.error = RuntimeError("카메라 프레임을 받지 못했습니다")
        log = spy(env, monkeypatch)
        assert start(env).status_code == 200
        c = wait_cycle(env)
        assert c["state"] == "failed" and c["result"] is None
        assert "검사 단계 실패" in c["error"] and "카메라 프레임" in c["error"] and c["message"] == c["error"]
        assert step_states(c) == {"pick": "done", "to_scan": "done", "inspect": "failed", "sort": "skipped"}
        assert not belt(env).state["running"] and not env.hub.autorun_status["running"]
        fast = env.fast
        assert commands(log) == [
            f"DIR {fast.belt_dir.upper()}", f"RPM {fast.belt_rpm:.3f}", f"SERVO {fast.gate_close_deg}",
            "START", "STOP",          # 이송
            "STOP",                   # 실패 정리의 정지 — 게이트·팔 명령은 없다
        ]
        assert c["baskets"]["good"]["count"] == c["baskets"]["defect"]["count"] == 0 and c["completed"] == 0
        # 벨트가 도는 중이라 촬영을 못 하는 경우도 같은 길이다(도메인 오류의 메시지를 그대로 싣는다)
        from backend.utils.errors import ValidationError
        env.capture.error = ValidationError("컨베이어가 움직이는 중입니다 — 멈춘 뒤 촬영합니다")
        assert start(env).status_code == 200
        assert "컨베이어가 움직이는 중" in wait_cycle(env)["error"]

    def test_집기가_시간_안에_안_끝나면_실패하고_autorun_을_세운다(self, env, monkeypatch):
        env.waypoints.write_text(json.dumps(
            {"waypoints": [{"t": 0, "joints": env.pose}, {"t": 600, "joints": env.pose}]}), encoding="utf-8")
        try:
            knobs(env, pick_timeout_sec=0.4)
            log = spy(env, monkeypatch)
            assert start(env).status_code == 200
            c = wait_cycle(env)
            assert c["state"] == "failed" and "집기 단계 실패" in c["error"] and "0.4초" in c["error"]
            assert step_states(c) == {"pick": "failed", "to_scan": "skipped", "inspect": "skipped", "sort": "skipped"}
            assert not env.hub.autorun_status["running"]
            assert "START" not in log and env.capture.calls == []       # 집기가 끝나지 않았으니 벨트도 촬영도 없다
        finally:
            env.waypoints.write_text(json.dumps(
                {"waypoints": [{"t": 0, "joints": env.pose}, {"t": 0.05, "joints": env.pose}]}), encoding="utf-8")

    def test_실제_촬영_함수와_이어져_끝까지_돈다(self, env, monkeypatch):
        # run_scan_capture 를 바꾸지 않은 시뮬레이션 전체 경로. 판정은 합성 프레임이 정하므로 값은 가리지 않는다.
        monkeypatch.setattr(analysis_api, "run_scan_capture", REAL_CAPTURE)
        assert start(env).status_code == 200
        c = wait_cycle(env, timeout=60.0)
        assert c["state"] == "done", c["error"]
        assert c["result"]["verdict"] in ("good", "defect") and c["result"]["quality"] in ("ok", "low")
        shots = list((env.root / "inspections" / c["result"]["capture_id"]).glob("*.jpg"))
        assert len(shots) >= 2


# ------------------------------------------------------------------ 중단
class TestAbort:
    def test_이송_중_사용자_정지는_즉시_먹고_다음_단계에_들어가지_않는다(self, env, monkeypatch):
        run_in_to_scan(env)
        log = spy(env, monkeypatch)
        t0 = time.monotonic()
        res = env.c.post("/api/cycle/stop")
        assert time.monotonic() - t0 < 3.0          # 30초 대기를 끝까지 기다리지 않는다
        c = res.get_json()["cycle"]
        assert res.status_code == 200 and c["state"] == "aborted" and c["error"] == "사용자가 사이클을 정지했습니다."
        assert c["message"] == c["error"] and c["step"] is None and c["completed"] == 0
        assert step_states(c) == {"pick": "done", "to_scan": "failed", "inspect": "skipped", "sort": "skipped"}
        assert not belt(env).state["running"] and not env.hub.autorun_status["running"]
        assert "STOP" in log and "START" not in log
        time.sleep(0.2)
        assert env.capture.calls == []              # 검사 단계에 들어가지 않았다
        assert cycle_of(env)["state"] == "aborted"  # 뒤늦게 상태가 바뀌지 않는다
        assert env.c.post("/api/cycle/stop").get_json()["cycle"]["state"] == "aborted"     # 이미 끝났으면 그대로

    def test_진동_대기_중_정지도_즉시_먹고_촬영하지_않는다(self, env):
        knobs(env, settle_sec=30.0)
        assert start(env).status_code == 200
        wait_until(lambda: cycle_of(env)["step"] == "inspect", "검사 단계 진입")
        t0 = time.monotonic()
        c = env.c.post("/api/cycle/stop").get_json()["cycle"]
        assert time.monotonic() - t0 < 3.0
        assert c["state"] == "aborted"
        assert step_states(c) == {"pick": "done", "to_scan": "done", "inspect": "failed", "sort": "skipped"}
        assert env.capture.calls == [] and not belt(env).state["running"]

    def test_중단이_걸린_뒤에_난_오류는_실패가_아니라_중단으로_끝난다(self, env, monkeypatch):
        # 정지가 autorun 을 치우는 사이에 상태를 읽다 AttributeError 가 나는 식의 부수 오류를 흉내낸다
        def side_effect(hub, settle, resolution):
            hub.cycle.abort("비상 정지")
            raise RuntimeError("중단이 낳은 부수 오류")

        monkeypatch.setattr(analysis_api, "run_scan_capture", side_effect)
        assert start(env).status_code == 200
        c = wait_cycle(env)
        assert c["state"] == "aborted" and c["error"] == "비상 정지" and "부수 오류" not in c["error"]
        assert not belt(env).state["running"]

    def test_집기_중_정지는_autorun_을_세우고_벨트를_돌리지_않는다(self, env, monkeypatch):
        env.waypoints.write_text(json.dumps(
            {"waypoints": [{"t": 0, "joints": env.pose}, {"t": 600, "joints": env.pose}]}), encoding="utf-8")
        try:
            log = spy(env, monkeypatch)
            assert start(env).status_code == 200
            wait_until(lambda: env.hub.autorun_status["running"], "집기 autorun 시작")
            c = env.c.post("/api/cycle/stop").get_json()["cycle"]
            assert c["state"] == "aborted"
            assert step_states(c) == {"pick": "failed", "to_scan": "skipped", "inspect": "skipped", "sort": "skipped"}
            assert not env.hub.autorun_status["running"]
            assert "START" not in log and env.capture.calls == []
        finally:
            env.waypoints.write_text(json.dumps(
                {"waypoints": [{"t": 0, "joints": env.pose}, {"t": 0.05, "joints": env.pose}]}), encoding="utf-8")

    def test_분류_게이트를_연_채_멈추면_게이트를_더_움직이지_않는다(self, env, monkeypatch):
        env.capture.stats = stats(0.015)
        knobs(env, gate_hold_sec=30.0)
        log = spy(env, monkeypatch)
        assert start(env).status_code == 200
        open_cmd = f"SERVO {env.fast.gate_open_deg}"
        wait_until(lambda: open_cmd in log, "게이트 열림")
        c = env.c.post("/api/cycle/stop").get_json()["cycle"]
        assert c["state"] == "aborted" and step_states(c)["sort"] == "failed"
        cmds = commands(log)
        after_open = cmds[cmds.index(open_cmd) + 1:]
        assert after_open and set(after_open) == {"STOP"}       # 정지만 — 게이트를 닫는 명령은 없다
        assert c["baskets"]["defect"]["count"] == 0         # 분류가 끝까지 가지 않았다

    def test_비상_정지는_사이클을_먼저_끊고_사이클은_aborted_로_끝난다(self, env):
        run_in_to_scan(env)
        res = env.c.post("/api/emergency_stop")
        assert res.status_code == 200 and res.get_json()["ok"] is True
        assert all("stopped" in o and "disabled" in o for o in res.get_json()["conveyors"].values())
        c = wait_cycle(env)
        assert c["state"] == "aborted" and "비상 정지" in c["error"]
        assert step_states(c)["inspect"] == "skipped" and env.capture.calls == []
        assert not belt(env).state["running"]

    def test_비상_정지는_사이클_abort_를_시연_세션·autorun·텔레옵_정지보다_앞에_부른다(self, env, monkeypatch):
        calls: list[str] = []
        for owner, name, label in ((env.hub.cycle, "abort", "cycle"), (env.hub.demo, "abort", "demo"),
                                   (env.hub, "stop_autorun", "autorun"), (env.hub, "stop_teleop", "teleop")):
            real = getattr(owner, name)
            monkeypatch.setattr(owner, name, lambda *a, _real=real, _label=label, **k: (calls.append(_label), _real(*a, **k))[1])
        assert env.c.post("/api/emergency_stop").status_code == 200
        assert calls[:4] == ["cycle", "demo", "autorun", "teleop"]

    def test_비상_정지에_사이클_중단이_실패해도_기존_정지는_그대로_진행된다(self, env, monkeypatch):
        def broken(reason):
            raise RuntimeError("사이클 중단 실패")

        monkeypatch.setattr(env.hub.cycle, "abort", broken)
        belt(env).start()
        res = env.c.post("/api/emergency_stop")
        assert res.status_code == 200 and res.get_json()["ok"] is True
        assert res.get_json()["conveyors"][env.hub.cfg.cycle.belt] == "stopped + disabled"
        assert not belt(env).state["running"]

    def test_벨트를_손으로_정지하면_사이클도_중단된다(self, env, monkeypatch):
        # 사람이 세운 벨트를 사이클이 다음 단계에서 다시 돌리면 안 된다
        run_in_to_scan(env)
        log = spy(env, monkeypatch)
        res = env.c.post(f"/api/conveyor/{env.hub.cfg.cycle.belt}/stop")
        assert res.status_code == 200 and res.get_json()["running"] is False
        c = wait_cycle(env)
        assert c["state"] == "aborted" and "벨트가 수동으로 정지" in c["error"]
        assert "START" not in log and env.capture.calls == []

    def test_드라이버를_끄면_사이클도_중단되고_켜는_것은_영향이_없다(self, env):
        # 다음 START 가 드라이버를 다시 켜므로, 사이클이 이어지면 사람이 손댄 벨트가 다시 돈다
        run_in_to_scan(env)
        name = env.hub.cfg.cycle.belt
        assert env.c.post(f"/api/conveyor/{name}/enable", json={"enabled": True}).status_code == 200
        assert cycle_of(env)["state"] == "running" and belt(env).state["running"]
        assert env.c.post(f"/api/conveyor/{name}/enable", json={"enabled": False}).status_code == 200
        c = wait_cycle(env)
        assert c["state"] == "aborted" and not belt(env).state["running"]


def test_서버_종료는_사이클을_먼저_끊고_벨트를_세운다(tmp_path, monkeypatch):
    # shutdown() 은 카메라·팔 연결을 끊어 공용 허브를 못 쓰게 되므로 이 시험만 자기 앱을 쓴다.
    cfg = load_config()
    cfg = replace(cfg, server=replace(cfg.server, simulate=True), recording=replace(cfg.recording, root=tmp_path))
    app = create_app(cfg)
    hub = app.extensions["omx_hub"]
    time.sleep(1.0)
    pose = hub.follower.read()
    waypoints = tmp_path / "pick.json"
    waypoints.write_text(json.dumps({"waypoints": [{"t": 0, "joints": pose}, {"t": 0.05, "joints": pose}]}), encoding="utf-8")
    hub.cfg = replace(cfg, cycle=replace(cfg.cycle, pick_waypoints=waypoints, pick_settle_sec=0.0, to_scan_sec=30.0))
    monkeypatch.setattr(analysis_api, "run_scan_capture", FakeCapture())
    with app.test_client() as client:
        assert client.post("/api/cycle/start").status_code == 200
        wait_until(lambda: hub.conveyors[cfg.cycle.belt].state["running"], "이송 중 벨트 구동")
        hub.shutdown()
        wait_until(lambda: hub.cycle.status()["state"] != "running", "사이클 종료")
        c = hub.cycle.status()
        assert c["state"] == "aborted" and c["error"] == "서버 종료"
        assert not hub.conveyors[cfg.cycle.belt].state["running"]


def own_hub(tmp_path, monkeypatch, **cycle_overrides):
    """shutdown() 을 시험하는 쪽이 쓰는 자기 앱(공용 허브를 못 쓰게 되므로). (app, hub, cfg)"""
    cfg = load_config()
    cfg = replace(cfg, server=replace(cfg.server, simulate=True), recording=replace(cfg.recording, root=tmp_path))
    app = create_app(cfg)
    hub = app.extensions["omx_hub"]
    time.sleep(1.0)
    pose = hub.follower.read()
    waypoints = tmp_path / "pick.json"
    waypoints.write_text(json.dumps({"waypoints": [{"t": 0, "joints": pose}, {"t": 0.05, "joints": pose}]}), encoding="utf-8")
    hub.cfg = replace(cfg, cycle=replace(cfg.cycle, pick_waypoints=waypoints, pick_settle_sec=0.0, **cycle_overrides))
    monkeypatch.setattr(analysis_api, "run_scan_capture", FakeCapture())
    return app, hub, cfg


def test_서버_종료_중에도_벨트는_허브_락을_기다리지_않고_바로_선다(tmp_path, monkeypatch):
    # 서버 종료는 허브 락을 쥔 채 팔을 홈으로 보낸다. 사이클의 정지가 autorun 정지(허브 락 대기)를 먼저 하면 그동안 벨트가 돈다.
    app, hub, cfg = own_hub(tmp_path, monkeypatch, to_scan_sec=30.0)
    conveyor = hub.conveyors[cfg.cycle.belt]
    stopped, seen = threading.Event(), {}
    real_stop, real_disconnect = conveyor.stop, hub.follower.disconnect
    monkeypatch.setattr(conveyor, "stop", lambda: (stopped.set(), real_stop())[1])

    def slow_disconnect(*args, **kwargs):
        seen["belt_stopped_during_shutdown"] = stopped.wait(5)      # 종료가 끝나기를 기다리지 않고 섰는가
        return real_disconnect(*args, **kwargs)

    monkeypatch.setattr(hub.follower, "disconnect", slow_disconnect)
    with app.test_client() as client:
        assert client.post("/api/cycle/start").status_code == 200
        wait_until(lambda: conveyor.state["running"], "이송 중 벨트 구동")
        stopped.clear()
        hub.shutdown()
        wait_until(lambda: hub.cycle.status()["state"] != "running", "사이클 종료")
    assert seen["belt_stopped_during_shutdown"] is True


def test_서버_종료가_끝난_뒤에는_사이클이_팔도_끊긴_벨트_포트도_건드리지_않는다(tmp_path, monkeypatch):
    app, hub, cfg = own_hub(tmp_path, monkeypatch)
    reached, go = threading.Event(), threading.Event()
    real_start, conveyor = hub.start_autorun, hub.conveyors[cfg.cycle.belt]

    def gated(*args, **kwargs):
        reached.set()               # 사이클 스레드: 점검을 통과해 start_autorun 앞에 섰다
        assert go.wait(10)
        return real_start(*args, **kwargs)

    torque, stops = [], []
    real_torque, real_stop = hub.follower.set_torque, conveyor.stop
    monkeypatch.setattr(hub, "start_autorun", gated)
    monkeypatch.setattr(hub.follower, "set_torque", lambda on: (torque.append(on), real_torque(on))[1])
    monkeypatch.setattr(conveyor, "stop", lambda: (stops.append(1), real_stop())[1])
    # 중단 신호가 사이클에 닿지 못한 최악의 경우 — 종료 뒤의 차단이 abort 플래그가 아니라 허브의 종료 표시여야 한다
    monkeypatch.setattr(hub.cycle, "abort", lambda reason: None)
    with app.test_client() as client:
        assert client.post("/api/cycle/start").status_code == 200
        assert reached.wait(10)
        hub.shutdown()
        torque.clear()
        stops.clear()
        go.set()
        wait_until(lambda: hub.cycle.status()["state"] != "running", "사이클 종료")
        c = hub.cycle.status()
        assert c["state"] == "failed" and "사이클이 중단되어 자율 실행을 시작하지 않았습니다" in c["error"]
        assert torque == [] and stops == []     # 종료 뒤에는 팔도, 포트를 놓은 벨트도 건드리지 않는다


# ------------------------------------------------------------------ 경합(이벤트로 순서를 강제한다)
class TestRace:
    """점검(_check)을 통과한 사이클 스레드가 락·포트 앞에서 기다리는 사이 비상 정지가 끝나는 경합.
    스레드를 실제로 세워 두고 순서를 이벤트로 강제하므로 시간에 기대지 않는다."""

    def test_비상_정지가_끝난_뒤에는_집기가_autorun_을_시작하지_않는다(self, env, monkeypatch):
        reached, go = threading.Event(), threading.Event()
        real_start, real_torque = env.hub.start_autorun, env.hub.follower.set_torque

        def gated(*args, **kwargs):
            reached.set()           # 사이클 스레드: _check() 를 통과해 start_autorun 의 허브 락 앞에 섰다
            assert go.wait(10)
            return real_start(*args, **kwargs)

        torque = []
        monkeypatch.setattr(env.hub, "start_autorun", gated)
        monkeypatch.setattr(env.hub.follower, "set_torque", lambda on: (torque.append(on), real_torque(on))[1])
        assert start(env).status_code == 200
        assert reached.wait(10)
        assert env.c.post("/api/emergency_stop").status_code == 200         # 정지가 '그 사이에' 끝난다
        torque.clear()
        go.set()
        c = wait_cycle(env)
        assert c["state"] == "aborted" and "비상 정지" in c["error"]
        assert torque == []         # 정지 뒤에 팔로워 토크를 걸지 않았다
        assert env.hub._autorun is None and not env.hub.autorun_status["running"]

    def test_비상_정지의_DISABLE_뒤에는_점검을_통과한_START_가_끼어들지_않는다(self, env, monkeypatch):
        order: list[str] = []
        real_cmd, real_start = belt(env)._command, belt(env).start
        entered, release = threading.Event(), threading.Event()

        def gated_start():
            entered.set()           # 사이클 스레드: _do 의 점검을 통과해 START 를 내기 직전
            assert release.wait(10)
            return real_start()

        monkeypatch.setattr(belt(env), "_command", lambda c: (order.append(c), real_cmd(c))[1])
        monkeypatch.setattr(belt(env), "start", gated_start)
        assert start(env).status_code == 200
        assert entered.wait(10)
        stopper = threading.Thread(target=env.hub.emergency_stop)
        stopper.start()
        time.sleep(0.3)
        assert "DISABLE" not in order       # abort() 가 이미 나간 START 를 기다리는 중 — 아직 벨트를 건드리지 않았다
        release.set()
        stopper.join(10)
        assert not stopper.is_alive()
        wait_cycle(env)
        starts = [i for i, c in enumerate(order) if c == "START"]
        disables = [i for i, c in enumerate(order) if c == "DISABLE"]
        assert starts and disables and max(starts) < min(disables)      # DISABLE 뒤에 START 가 없다 — 드라이버가 다시 켜지지 않는다
        assert belt(env).state["enabled"] is False and not belt(env).state["running"]

    def test_이미_나간_명령이_끝나지_않아도_abort_는_상한_안에_돌아온다(self, env, monkeypatch):
        monkeypatch.setattr(cycle_mod, "ABORT_WAIT_SEC", 0.3)
        entered, release = threading.Event(), threading.Event()
        real = belt(env).start
        monkeypatch.setattr(belt(env), "start", lambda: (entered.set(), release.wait(10), real())[2])
        assert start(env).status_code == 200
        assert entered.wait(10)
        t0 = time.monotonic()
        env.hub.cycle.abort("시험")
        waited = time.monotonic() - t0
        release.set()
        assert 0.25 < waited < 2.0          # 상한에서 포기하고 돌아온다 — 비상 정지를 무한정 붙잡지 않는다
        assert wait_cycle(env)["state"] == "aborted"

    def test_멈춘_사이클에_abort_는_기다리지_않는다(self, env, monkeypatch):
        monkeypatch.setattr(cycle_mod, "ABORT_WAIT_SEC", 5.0)
        t0 = time.monotonic()
        env.hub.cycle.abort("시험")         # 돌고 있지 않다 — 나간 명령이 없으니 기다릴 것도 없다
        assert time.monotonic() - t0 < 1.0


# ------------------------------------------------------------------ 집기 뒤 대기
class TestPickSettle:
    def test_집기가_끝난_뒤_이송_전에_기다리고_중단이_즉시_먹는다(self, env, monkeypatch):
        knobs(env, pick_settle_sec=30.0)
        log = spy(env, monkeypatch)
        assert start(env).status_code == 200
        wait_until(lambda: f"SERVO {env.fast.gate_close_deg}" in log, "집기 준비 명령")
        time.sleep(0.6)                     # 궤적(0.05초)이 끝나고도 한참
        c = cycle_of(env)
        assert c["state"] == "running" and c["step"] == "pick" and not env.hub.autorun_status["running"]
        assert "START" not in log           # 그리퍼가 놓는 동안 벨트는 아직 서 있다
        t0 = time.monotonic()
        c = env.c.post("/api/cycle/stop").get_json()["cycle"]
        assert time.monotonic() - t0 < 3.0 and c["state"] == "aborted"      # 30초를 끝까지 기다리지 않는다
        assert "START" not in log and env.capture.calls == []

    def test_설정한_시간만큼_벨트_구동을_늦춘다(self, env, monkeypatch):
        knobs(env, pick_settle_sec=0.5)
        stamps: dict[str, float] = {}
        real = belt(env)._command

        def record(command):
            stamps.setdefault(command.split()[0], time.monotonic())
            return real(command)

        monkeypatch.setattr(belt(env), "_command", record)
        assert start(env).status_code == 200
        assert wait_cycle(env)["state"] == "done"
        assert stamps["START"] - stamps["SERVO"] >= 0.5     # 집기(0.05초 궤적)가 끝나고도 설정한 시간을 채운 뒤에야 벨트가 돈다


# ------------------------------------------------------------------ 수동 조작 거부
class TestManualLock:
    MANUAL_BELT = [
        ("/api/conveyor/start", None), ("/api/conveyor/work/start", None),
        ("/api/conveyor/speed", {"rpm": 5}), ("/api/conveyor/dir", {"direction": "ccw"}),
        ("/api/conveyor/servo", {"deg": 90}), ("/api/conveyor/zero", None), ("/api/conveyor/end", None),
        ("/api/conveyor/move", {"mm": 10}), ("/api/conveyor/release", None),
    ]

    def test_사이클_중에는_수동_조작이_409_이고_정지만_된다(self, env):
        run_in_to_scan(env)
        body = {"episode_id": "900001", "object_id": "x", "label": "good"}
        others = [
            ("/api/teleop/start", None),
            ("/api/rollout/start", {"waypoints": str(env.waypoints), "by_cycle": True}),   # by_cycle 은 라우트가 받지 않는다
            ("/api/recording/start", body),
            ("/api/demo/start", {"object_id": "cube", "task": "공급 벨트의 물체를 집어 작업 벨트로 옮긴다"}),
            ("/api/arm/home", None),
            # 수동 촬영: 사이클 도중에 배경을 찍으면 벨트 위 물체가 배경에 박혀 이후 판정이 모두 틀어진다
            ("/api/scan/background", None), ("/api/scan/calibrate/shot", None), ("/api/scan/capture", None),
        ]
        for path, payload in self.MANUAL_BELT + others:
            res = env.c.post(path, json=payload)
            assert res.status_code == 409, path
            assert res.get_json()["error"]["code"] == "cycle_busy" and "사이클" in message(res), path
        # 거부는 아무것도 바꾸지 않는다: 사이클은 계속 돌고 벨트도 그대로다
        assert cycle_of(env)["state"] == "running" and belt(env).state["running"] and belt(env).connected
        assert not env.hub.teleop_status["running"] and env.hub.recorder.state.value == "IDLE"
        assert not (env.root / "background").exists()           # 배경은 저장되지 않았다
        # 계산만 하는 엔드포인트는 막지 않는다(409 가 아니다)
        assert env.c.post("/api/scan/calibrate/solve", json={}).status_code == 400      # 샷이 없다는 이유
        assert env.c.post("/api/scan/reconstruct_demo", json={"resolution": 16}).status_code == 200
        assert cycle_of(env)["state"] == "running"

        # 정지는 허용한다(사이클도 함께 멈춘다)
        res = env.c.post("/api/conveyor/stop")
        assert res.status_code == 200
        assert wait_cycle(env)["state"] == "aborted"
        # 사이클이 끝나면 수동 조작이 다시 된다
        assert env.c.post("/api/conveyor/work/start").status_code == 200
        assert env.c.post("/api/conveyor/work/stop").status_code == 200

    def test_두_번째_시작은_400_이다(self, env):
        run_in_to_scan(env)
        res = start(env)
        assert res.status_code == 400 and "이미 사이클이 진행 중" in message(res)


# ------------------------------------------------------------------ 시작 전 점검
class TestReady:
    def reasons(self, env) -> list[str]:
        return cycle_of(env)["ready"]["reasons"]

    def refuses(self, env, text: str, monkeypatch) -> None:
        """ready 가 이유를 주고, 시작은 400 으로 같은 이유를 말하며, 벨트에는 아무 명령도 나가지 않는다."""
        log = spy(env, monkeypatch)
        ready = cycle_of(env)["ready"]
        assert ready["ok"] is False and any(text in r for r in ready["reasons"]), ready
        res = start(env)
        assert res.status_code == 400 and text in message(res)
        assert cycle_of(env)["state"] == "idle" and log == []

    def test_모두_갖추면_통과한다(self, env):
        assert cycle_of(env)["ready"] == {"ok": True, "reasons": [], "notes": []}

    def test_픽_동작_파일이_없다(self, env, monkeypatch):
        knobs(env, pick_waypoints=env.root / "없는.json")
        assert self.reasons(env)[0] == f"픽 동작 파일이 없습니다: {env.root / '없는.json'}"     # 레포 밖 경로는 그대로
        self.refuses(env, "픽 동작 파일이 없습니다", monkeypatch)

    def test_레포_안의_경로는_상대_경로로_줄여_알린다(self, env):
        # 절대 경로는 상단바에서 잘린다. 레포 안이면 레포 기준 상대 경로로, 밖이면 그대로
        knobs(env, pick_waypoints=PROJECT_ROOT / "lerobot_data" / "waypoints" / "없는_궤적.json")
        assert self.reasons(env)[0] == f"픽 동작 파일이 없습니다: {Path('lerobot_data') / 'waypoints' / '없는_궤적.json'}"

    def test_ACT_체크포인트_집기는_완료_신호가_없어_시작_전에_막는다(self, env, monkeypatch):
        # 웨이포인트가 멀쩡해도, 체크포인트가 실재해도 막는다 — 집기가 늘 시간 초과로 실패하기 때문이다
        knobs(env, pick_checkpoint=env.root)
        self.refuses(env, "ACT 체크포인트 집기는 아직 지원하지 않습니다(완료 신호 없음). pick_waypoints 를 쓰세요", monkeypatch)
        assert "없는" not in self.reasons(env)[0]           # 파일이 있나 없나가 아니라 미지원이 이유다

    def test_팔로워가_연결되지_않았다(self, env, monkeypatch):
        env.hub.follower.disconnect(home_first=False)
        self.refuses(env, "팔로워 팔이 연결되어 있지 않습니다", monkeypatch)

    def test_설정에_포트가_없는_벨트는_막는다(self, env, monkeypatch):
        # 열 포트가 없으면 시작할 때 연다는 약속을 지킬 수 없다
        no_port = ConveyorController(ConveyorConfig(label="포트 없는 벨트"), simulate=False, name=env.fast.belt)
        with monkeypatch.context() as m:        # 시험이 끝나기 전에 되돌린다(정리 단계가 이 벨트에 명령을 내지 않게)
            m.setitem(env.hub.conveyors, env.fast.belt, no_port)
            self.refuses(env, f"사이클 벨트 '{env.fast.belt}'(포트 없는 벨트)의 포트가 config.yaml 에 없습니다", m)

    def test_벨트_드라이버가_꺼져_있으면_막고_드라이버_켜기로_풀린다(self, env, monkeypatch):
        # 비상 정지는 드라이버를 DISABLE 한다. 펌웨어 START 가 다시 켜 버리므로, 사람이 명시적으로 리셋해야 재개한다
        assert env.c.post("/api/emergency_stop").status_code == 200
        self.refuses(env, "비상 정지 뒤 벨트 드라이버가 꺼져 있습니다. 장비 제어에서 '드라이버 켜기'를 누르세요.", monkeypatch)
        assert env.c.post("/api/emergency_reset").status_code == 200
        assert cycle_of(env)["ready"] == {"ok": True, "reasons": [], "notes": []}
        assert start(env).status_code == 200
        assert wait_cycle(env)["state"] == "done"

    def test_사이클_벨트가_설정에_없다(self, env, monkeypatch):
        knobs(env, belt="없는벨트")
        self.refuses(env, "'없는벨트' 가 config.yaml 의 conveyors 에 없습니다", monkeypatch)

    def test_벨트가_이미_돌고_있다(self, env, monkeypatch):
        env.hub.conveyors["feed"].start()
        self.refuses(env, "벨트가 돌고 있습니다", monkeypatch)

    def test_재구성용_카메라가_2대_미만이다(self, env, monkeypatch):
        live = [c for c in env.hub.cameras.status_all() if c["connected"] and env.hub.cfg.cameras[c["name"]].view_dir]
        monkeypatch.setattr(env.hub.cameras, "status_all", lambda: live[:1])
        self.refuses(env, "재구성용 카메라가 2대 미만입니다(신호가 있는 카메라 1대)", monkeypatch)

    def test_다른_작업이_돌고_있다(self, env, monkeypatch):
        monkeypatch.setattr(env.hub, "_teleop", SimpleNamespace(is_alive=lambda: True, fault=None))
        self.refuses(env, "텔레오퍼레이션이 실행 중입니다", monkeypatch)
        monkeypatch.setattr(env.hub, "_teleop", None)
        monkeypatch.setattr(env.hub, "_autorun", SimpleNamespace(status=lambda: {"running": True, "fault": None, "tick_count": 0}))
        self.refuses(env, "자율 실행(rollout)이 실행 중입니다", monkeypatch)
        monkeypatch.setattr(env.hub, "_autorun", None)
        monkeypatch.setattr(env.hub.demo, "_starting", True)
        self.refuses(env, "시연 세션이 진행 중입니다", monkeypatch)
        monkeypatch.setattr(env.hub.demo, "_starting", False)
        env.hub.recorder.start("900001", "x", "good", None)
        try:
            self.refuses(env, "녹화가 진행 중입니다", monkeypatch)
        finally:
            env.hub.recorder.stop()

    def test_여러_이유는_전부_알려_준다(self, env):
        knobs(env, pick_waypoints=env.root / "없는.json")
        env.hub.follower.disconnect(home_first=False)
        reasons = self.reasons(env)
        assert len(reasons) == 2 and "픽 동작" in reasons[0] and "팔로워" in reasons[1]       # 계약서의 점검 순서
        text = message(start(env))
        assert "픽 동작 파일이 없습니다" in text and "팔로워 팔이 연결되어 있지 않습니다" in text

    def test_점검이_실패해도_상태_폴링은_죽지_않는다(self, env, monkeypatch):
        monkeypatch.setattr(env.hub.cycle, "ready", lambda: 1 / 0)
        res = env.c.get("/api/status")
        assert res.status_code == 200
        ready = res.get_json()["cycle"]["ready"]
        assert ready["ok"] is False and "준비 상태를 확인하지 못했습니다" in ready["reasons"][0]


# ------------------------------------------------------------------ 벨트 포트(시작할 때 연다)
class TestBeltPort:
    """서버를 켠 직후에는 벨트 포트가 닫혀 있다(첫 명령 때 연다). 점검(ready)은 포트를 열지 않고 막지도 않으며,
    시작이 점검을 통과한 뒤 팔이 움직이기 전에 연다. 열지 못하면 아무것도 움직이지 않고 거부한다."""

    @pytest.fixture
    def closed(self, env, monkeypatch):
        """사이클 벨트의 포트가 닫혀 있는 상태. connect() 는 순서를 기록하고 열린 것으로 만든다."""
        opened, order = [], []
        monkeypatch.setattr(ConveyorController, "connected", property(lambda self: self is not belt(env) or bool(opened)))
        monkeypatch.setattr(belt(env), "connect", lambda: (order.append("connect"), opened.append(1)))
        return SimpleNamespace(order=order, opened=opened)

    def test_점검은_포트를_열지_않고_막지도_않는다(self, env, closed):
        ready = cycle_of(env)["ready"]
        assert ready["ok"] is True and ready["reasons"] == []
        assert len(ready["notes"]) == 1 and "시작하면 벨트 포트를 엽니다" in ready["notes"][0]
        env.c.get("/api/status")
        env.c.get("/api/cycle/status")      # 폴링을 거듭해도
        assert closed.order == []           # 열지 않는다

    def test_시작은_팔과_벨트가_움직이기_전에_포트를_연다(self, env, closed, monkeypatch):
        real_cmd, real_auto = belt(env)._command, env.hub.start_autorun
        monkeypatch.setattr(belt(env), "_command", lambda c: (closed.order.append(c), real_cmd(c))[1])
        monkeypatch.setattr(env.hub, "start_autorun", lambda *a, **k: (closed.order.append("autorun"), real_auto(*a, **k))[1])
        assert start(env).status_code == 200
        assert wait_cycle(env)["state"] == "done"
        order = [c for c in closed.order if not c.startswith(NOISE)]
        assert order[0] == "connect"        # 첫 일이 포트 열기다
        assert order.index("connect") < order.index(f"DIR {env.fast.belt_dir.upper()}") < order.index("autorun")

    def test_포트를_열지_못하면_팔을_움직이지_않고_400_으로_거부한다(self, env, monkeypatch):
        monkeypatch.setattr(ConveyorController, "connected", property(lambda self: self is not belt(env)))

        def refuse():
            raise ConveyorError("COM13 포트를 열 수 없습니다: 접근이 거부되었습니다")

        monkeypatch.setattr(belt(env), "connect", refuse)
        autorun, real = [], env.hub.start_autorun
        monkeypatch.setattr(env.hub, "start_autorun", lambda *a, **k: (autorun.append(1), real(*a, **k))[1])
        log = spy(env, monkeypatch)
        res = start(env)
        assert res.status_code == 400
        assert "COM13 포트를 열 수 없습니다" in message(res) and "팔은 움직이지 않았습니다" in message(res)
        assert cycle_of(env)["state"] == "idle" and autorun == [] and log == []

    def test_장치가_없는_실제_포트는_이유를_말하고_거부한다(self, env, monkeypatch):
        # 시뮬레이션이 아닌 컨트롤러가 존재하지 않는 포트를 열어 본다(pyserial 이 거부한다 — 장치는 건드리지 않는다)
        absent = ConveyorController(ConveyorConfig(port="NO_SUCH_PORT_FOR_TEST", label="시험 벨트"),
                                    simulate=False, name=env.fast.belt)
        with monkeypatch.context() as m:        # 시험이 끝나기 전에 되돌린다(정리 단계가 이 벨트에 명령을 내지 않게)
            m.setitem(env.hub.conveyors, env.fast.belt, absent)
            ready = cycle_of(env)["ready"]
            assert ready["ok"] is True and "시작하면 벨트 포트를 엽니다" in ready["notes"][0]
            res = start(env)
            assert res.status_code == 400
            assert "시험 벨트 포트를 열지 못해" in message(res) and "NO_SUCH_PORT_FOR_TEST" in message(res)
            assert cycle_of(env)["state"] == "idle"

    def test_닫혀_있던_사이_꺼진_드라이버는_연_뒤에_알고_팔을_움직이기_전에_거부한다(self, env, closed, monkeypatch):
        belt(env)._state["enabled"] = False         # 서버를 다시 켠 사이 꺼진 드라이버는 포트를 열기 전에는 알 수 없다
        assert cycle_of(env)["ready"]["ok"] is True
        autorun, real = [], env.hub.start_autorun
        monkeypatch.setattr(env.hub, "start_autorun", lambda *a, **k: (autorun.append(1), real(*a, **k))[1])
        log = spy(env, monkeypatch)
        res = start(env)
        assert res.status_code == 400 and "드라이버 켜기" in message(res)
        assert closed.order == ["connect"]          # 열어 보고서야 안다
        assert cycle_of(env)["state"] == "idle" and autorun == [] and commands(log) == []


# ------------------------------------------------------------------ 바스켓
class TestBaskets:
    def test_가득_차면_시작을_거부하고_비운_뒤에_허용한다(self, env):
        knobs(env, basket_capacity=1)
        assert start(env).status_code == 200
        c = wait_cycle(env)
        assert c["state"] == "done" and c["baskets"]["good"] == {"count": 1, "capacity": 1}
        assert c["ready"]["ok"] is False and "양품 바스켓이 가득 찼습니다(1/1)" in c["ready"]["reasons"][0]

        res = start(env)
        assert res.status_code == 400 and "양품 바스켓" in message(res)
        assert cycle_of(env)["state"] == "done"                 # 거부는 상태를 건드리지 않는다

        # 다른 바스켓을 비워도 소용없다
        assert env.c.post("/api/cycle/baskets/empty", json={"basket": "defect"}).get_json()["cycle"]["baskets"]["good"]["count"] == 1
        assert start(env).status_code == 400

        res = env.c.post("/api/cycle/baskets/empty", json={"basket": "good"})
        assert res.status_code == 200 and res.get_json()["cycle"]["baskets"]["good"]["count"] == 0
        assert res.get_json()["cycle"]["ready"]["ok"] is True
        assert start(env).status_code == 200
        assert wait_cycle(env)["completed"] == 2

    def test_all_은_둘_다_비운다(self, env):
        env.capture.stats = stats(0.015)
        assert start(env).status_code == 200
        wait_cycle(env)
        env.capture.stats = stats(0.0)
        assert start(env).status_code == 200
        c = wait_cycle(env)
        assert c["baskets"]["good"]["count"] == 1 and c["baskets"]["defect"]["count"] == 1
        c = env.c.post("/api/cycle/baskets/empty", json={"basket": "all"}).get_json()["cycle"]
        assert c["baskets"]["good"]["count"] == 0 and c["baskets"]["defect"]["count"] == 0
        assert c["completed"] == 2          # 완료 수는 비우기와 무관하다

    @pytest.mark.parametrize("body", [None, {}, {"basket": "bad"}, {"basket": None}, {"basket": 1}, ["good"]])
    def test_잘못된_basket_값은_400(self, env, body):
        res = env.c.post("/api/cycle/baskets/empty", json=body)
        assert res.status_code == 400 and "basket" in message(res)


# ------------------------------------------------------------------ 알림
class TestAlerts:
    def alerts(self, env) -> dict:
        return {a["id"]: a for a in env.c.get("/api/alerts?refresh=1").get_json()["alerts"]}

    def test_사이클_오류는_다음_시작까지_fault_알림으로_남는다(self, env):
        assert "cycle_fault" not in self.alerts(env)
        env.capture.error = RuntimeError("촬영 실패 시험")
        assert start(env).status_code == 200
        wait_cycle(env)
        a = self.alerts(env)["cycle_fault"]
        assert a["severity"] == "fault" and a["action"]["route"] == "overview"
        assert "촬영 실패 시험" in a["summary"] and a["detail"]["evidence"]["step"] == "검사"
        assert a["detail"]["steps"]

        env.capture.error = None
        assert start(env).status_code == 200
        assert wait_cycle(env)["state"] == "done"
        assert "cycle_fault" not in self.alerts(env)

    def test_사용자가_정지한_사이클은_알림이_아니다(self, env):
        run_in_to_scan(env)
        env.c.post("/api/cycle/stop")
        assert "cycle_fault" not in self.alerts(env)

    def test_알림_계산은_시작_전_점검을_돌리지_않는다(self, env, monkeypatch):
        calls, real = [], env.hub.cycle.ready
        monkeypatch.setattr(env.hub.cycle, "ready", lambda: (calls.append(1), real())[1])
        assert env.c.get("/api/alerts?refresh=1").status_code == 200
        assert calls == []                  # 알림은 상태·바스켓만 읽는다 — 장치를 훑지 않는다
        cycle_of(env)
        assert calls == [1]                 # 사이클 상태 조회는 그대로 점검한다

    def test_바스켓이_차면_warn_알림이_뜨고_비우면_꺼진다(self, env):
        knobs(env, basket_capacity=1)
        assert not any(k.startswith("basket_full") for k in self.alerts(env))
        assert start(env).status_code == 200
        wait_cycle(env)
        a = self.alerts(env)["basket_full:good"]
        assert a["severity"] == "warn" and a["action"]["route"] == "overview"
        assert a["title"] == "양품 바스켓을 비워 주세요" and a["detail"]["evidence"] == {"basket": "good", "count": 1, "capacity": 1}
        assert "basket_full:defect" not in self.alerts(env)
        env.c.post("/api/cycle/baskets/empty", json={"basket": "good"})
        assert "basket_full:good" not in self.alerts(env)


# ------------------------------------------------------------------ 설정
class TestConfig:
    def test_코드의_기본값은_계약서의_출발점이다(self):
        c = CycleConfig()
        assert c.pick_waypoints.is_absolute() and c.pick_waypoints.name == "pick_place.json" and c.pick_checkpoint is None
        assert (c.pick_speed, c.pick_timeout_sec, c.belt, c.to_scan_sec, c.settle_sec, c.resolution) == (1.0, 90.0, "work", 3.0, 0.5, 96)
        assert c.pick_settle_sec == 1.0         # 집기가 끝난 뒤 그리퍼가 놓는 동작을 기다리는 시간
        assert (c.defect_ratio, c.good_run_sec, c.defect_to_branch_sec) == (0.01, 4.0, 1.5)
        assert (c.gate_open_deg, c.gate_close_deg, c.gate_hold_sec, c.basket_capacity) == (15, 130, 2.0, 2)

    def test_config_yaml_은_검증을_통과하고_벨트가_conveyors_에_있다(self):
        # 값은 현장에서 계속 고치는 것이라 숫자를 못 박지 않는다 — 읽히고, 범위를 지키고, 벨트가 실재하는지만 본다
        cfg = load_config()
        assert cfg.cycle.belt in cfg.conveyors and cfg.cycle.belt_dir in ("cw", "ccw")
        assert cfg.cycle.pick_waypoints.is_absolute()

    def test_빠진_절은_기본값이고_값은_정규화한다(self):
        assert _cycle({}) == CycleConfig()
        assert _cycle({"basket_capacity": 5, "belt_dir": "CCW"}).basket_capacity == 5
        assert _cycle({"belt_dir": "CCW"}).belt_dir == "ccw"
        assert _cycle({"pick_checkpoint": "outputs/x"}).pick_checkpoint.is_absolute()      # 상대 경로는 레포 루트 기준
        assert _cycle({"pick_settle_sec": 0}).pick_settle_sec == 0 and _cycle({"pick_settle_sec": 2.5}).pick_settle_sec == 2.5

    @pytest.mark.parametrize("node", [
        {"settle_sec": 5}, {"settle_sec": -1}, {"resolution": 200}, {"resolution": 8}, {"resolution": 50.5},
        {"belt_dir": "up"}, {"belt": " "}, {"defect_ratio": 0}, {"defect_ratio": 1.5}, {"basket_capacity": 0},
        {"to_scan_sec": "abc"}, {"to_scan_sec": 0}, {"gate_open_deg": 181}, {"gate_close_deg": -1},
        {"belt_rpm": float("inf")}, {"pick_speed": float("nan")}, {"pick_timeout_sec": 0},
        {"to_scan_sec": 3000}, {"good_run_sec": 3000}, {"defect_to_branch_sec": 3000},     # 단위를 잘못 적은 값(ms)
        {"gate_hold_sec": 100}, {"pick_speed": 50}, {"pick_timeout_sec": 6000},
        {"pick_settle_sec": -1}, {"pick_settle_sec": 100}, {"pick_settle_sec": "abc"}, {"pick_settle_sec": float("nan")},
    ])
    def test_잘못된_값은_시작할_때_거부한다(self, node):
        with pytest.raises(ValueError, match="cycle\\."):
            _cycle(node)
