"""시연 녹화 세션(녹화 → 리셋 → 다음 에피소드)과 시연 에피소드의 분류 폴더 격리.

지켜야 할 것:
1. 세션 규칙은 서버가 강제한다 — 텔레옵 꺼짐·팔 미연결·녹화 중·rollout 이면 시작 거부, 시간 범위 검증.
2. 시연 에피소드는 kind=demo + task 로 저장되고 classification/ 을 만들지 않는다.
3. 비상 정지·텔레옵 정지·예외는 세션을 끝내고 진행 중이던 에피소드를 남기지 않는다.
   비상 정지는 허브가 녹화기를 중단해 'recording: aborted' 로 보고한다(세션이 먼저 중단하면 안 된다).
4. kind=demo 에피소드의 자리표시 라벨이 같은 object_id 의 기존 분류 이미지를 옮기거나 지우거나 살려 두면 안 된다.
하드웨어 없이 시뮬레이션 허브로 돌고, 수집 루트는 임시 폴더다. 에피소드는 1초짜리로 짧게 끝낸다.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest

os.environ.setdefault("OMX_SIMULATE", "1")

from backend.app import create_app  # noqa: E402
from backend.config import load_config  # noqa: E402
from backend.core import demo_session  # noqa: E402
from backend.core.demo_session import DemoSession  # noqa: E402
from backend.utils.errors import RecordingStateError  # noqa: E402

TASK = "공급 벨트의 물체를 집어 작업 벨트로 옮긴다"
STATUS_KEYS = {
    "state", "episode_id", "episode_sec", "reset_sec", "phase_elapsed_sec", "phase_total_sec",
    "saved", "task", "object_id", "last_saved", "fault",
}


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("demo")
    cfg = load_config()
    cfg = replace(cfg, server=replace(cfg.server, simulate=True), recording=replace(cfg.recording, root=root))
    app = create_app(cfg)
    hub = app.extensions["omx_hub"]
    time.sleep(1.0)     # 카메라 워커 워밍업 — 프레임이 있어야 녹화를 시작할 수 있다
    with app.test_client() as client:
        yield SimpleNamespace(c=client, hub=hub, root=root)
    hub.shutdown()


def wait_until(predicate, what: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"{timeout}초 안에 '{what}' 이(가) 되지 않았습니다")


@pytest.fixture(autouse=True)
def reset(env):
    """테스트마다 같은 출발점: 세션·녹화·텔레옵 없음, 팔 연결됨, 저장 폴더 비움."""
    def clean():
        env.hub.demo.abort("테스트 정리")
        wait_until(lambda: not env.hub.demo.active, "세션 종료")
        if env.hub.recorder.state.value != "IDLE":      # 허브가 건 중단은 녹화기를 허브가 정리한다 — 여기서 대신
            env.hub.recorder.abort("테스트 정리")
        env.hub.stop_teleop()
        env.hub._autorun = None
        env.hub.recorder._last_error = None
        for arm in (env.hub.leader, env.hub.follower):
            arm.connect()
        for name in ("episodes", "classification"):
            shutil.rmtree(env.root / name, ignore_errors=True)

    clean()
    yield
    clean()


def start(env, **overrides):
    body = {"object_id": "cube", "task": TASK, "episode_sec": 30, "reset_sec": 30, **overrides}
    return env.c.post("/api/demo/start", json=body)


def demo(env) -> dict:
    return env.c.get("/api/status").get_json()["demo"]


def episode_dir(env, episode_id):
    return env.root / "episodes" / f"episode_{episode_id}"


def meta_of(env, episode_id) -> dict:
    return json.loads((episode_dir(env, episode_id) / "metadata.json").read_text(encoding="utf-8"))


def is_saved(env, episode_id) -> bool:
    # abort 는 라이터 스레드를 기다리지 않아 쓰던 폴더가 비워지다 남을 수 있다. 저장됐는지는 metadata.json 으로 본다.
    return (episode_dir(env, episode_id) / "metadata.json").exists()


def record_a_bit(env) -> None:
    wait_until(lambda: env.hub.recorder.status()["frame_count"] >= 2, "프레임 기록")


def message(res) -> str:
    return res.get_json()["error"]["message"]


# ------------------------------------------------------------------ 시작 가드 · 입력 검증
class TestStartGuards:
    def test_텔레옵이_꺼져_있으면_거부한다(self, env):
        res = start(env)
        assert res.status_code == 409 and "텔레옵" in message(res)
        assert demo(env)["state"] == "IDLE" and env.hub.recorder.state.value == "IDLE"

    def test_팔이_연결되지_않았으면_거부한다(self, env):
        env.hub.start_teleop()
        env.hub.leader.disconnect()
        res = start(env)
        assert res.status_code == 409 and "연결" in message(res)

    def test_녹화_중이면_거부한다(self, env):
        env.hub.start_teleop()
        manual = env.c.post("/api/recording/start", json={"episode_id": "900001", "object_id": "x", "label": "good"})
        assert manual.status_code == 200
        res = start(env)
        assert res.status_code == 409 and "녹화" in message(res)
        env.c.post("/api/recording/stop")

    def test_rollout_이_실행_중이면_거부한다(self, env):
        env.hub.start_teleop()
        env.hub._autorun = SimpleNamespace(status=lambda: {"running": True, "fault": None, "tick_count": 0})
        res = start(env)
        assert res.status_code == 409 and "rollout" in message(res)

    @pytest.mark.parametrize("overrides", [
        {"episode_sec": 0}, {"episode_sec": 601}, {"episode_sec": "20"}, {"episode_sec": True}, {"episode_sec": None},
        {"reset_sec": -1}, {"reset_sec": 121}, {"reset_sec": "10"},
        {"task": ""}, {"task": "   "}, {"task": None}, {"task": "가" * 201},
        {"object_id": ""}, {"object_id": "../escape"}, {"object_id": "a b"},
    ])
    def test_범위_밖_입력은_세션을_만들기_전에_400(self, env, overrides):
        env.hub.start_teleop()
        assert start(env, **overrides).status_code == 400
        assert demo(env)["state"] == "IDLE" and env.hub.recorder.state.value == "IDLE"

    def test_경계값은_받아들인다(self, env):
        env.hub.start_teleop()
        res = start(env, episode_sec=600, reset_sec=0)
        assert res.status_code == 200
        record_a_bit(env)
        assert env.c.post("/api/demo/end").get_json()["demo"]["saved"] == 1

    def test_이미_세션이_있으면_두_번째_시작은_409(self, env):
        env.hub.start_teleop()
        assert start(env).status_code == 200
        assert start(env).status_code == 409
        env.c.post("/api/demo/end")

    def test_세션이_없으면_next_redo_end_는_409(self, env):
        for action in ("next", "redo", "end"):
            assert env.c.post(f"/api/demo/{action}").status_code == 409


# ------------------------------------------------------------------ 흐름
class TestFlow:
    def test_IDLE_상태의_모양(self, env):
        assert set(demo(env)) == STATUS_KEYS and demo(env)["state"] == "IDLE"
        # 세션이 끝난 뒤에는 마지막 세션의 요약(saved·last_saved·fault)이 남는다. 처음 상태는 새 객체로 본다.
        assert DemoSession(env.hub).status() == {
            "state": "IDLE", "episode_id": None, "episode_sec": None, "reset_sec": None,
            "phase_elapsed_sec": 0.0, "phase_total_sec": 0, "saved": 0, "task": None, "object_id": None,
            "last_saved": None, "fault": None,
        }

    def test_시간이_다_되면_저장하고_리셋_없이_다음_에피소드를_이어서_녹화한다(self, env):
        env.hub.start_teleop()
        res = start(env, episode_sec=1, reset_sec=0)
        assert res.status_code == 200
        first = res.get_json()["demo"]
        assert first["state"] == "RECORDING" and first["saved"] == 0 and first["phase_total_sec"] == 1
        assert first["task"] == TASK and first["object_id"] == "cube" and first["fault"] is None

        # 저장(saved 증가)과 다음 에피소드 진입(episode_id 변경) 사이는 한 틈이 있다 — 둘 다 기다린다.
        wait_until(lambda: demo(env)["saved"] >= 1 and demo(env)["episode_id"] != first["episode_id"], "자동 저장 뒤 다음 에피소드")
        mid = demo(env)
        assert mid["state"] == "RECORDING" and mid["last_saved"] == first["episode_id"]
        assert int(mid["episode_id"]) == int(first["episode_id"]) + 1       # 자동 번호

        ended = env.c.post("/api/demo/end").get_json()["demo"]
        assert ended["state"] == "IDLE" and ended["episode_id"] is None and ended["fault"] is None
        assert ended["saved"] >= 2 and env.hub.recorder.state.value == "IDLE"

        saved = meta_of(env, first["episode_id"])
        assert saved["kind"] == "demo" and saved["task"] == TASK and saved["object_id"] == "cube"
        assert saved["num_frames"] > 0
        assert not (env.root / "classification").exists()           # 분류 이미지를 만들지 않는다
        listed = {m["episode_id"] for m in env.c.get("/api/episodes").get_json()["episodes"]}
        assert {first["episode_id"], ended["last_saved"]} <= listed

    def test_next_는_저장하고_리셋으로_가고_리셋_중_next_는_리셋을_건너뛴다(self, env):
        env.hub.start_teleop()
        first = start(env).get_json()["demo"]
        episode_id = first["episode_id"]
        record_a_bit(env)

        reset = env.c.post("/api/demo/next").get_json()["demo"]
        assert reset["state"] == "RESET" and reset["saved"] == 1 and reset["last_saved"] == episode_id
        assert reset["phase_total_sec"] == 30 and int(reset["episode_id"]) == int(episode_id) + 1
        assert meta_of(env, episode_id)["kind"] == "demo"

        again = env.c.post("/api/demo/next").get_json()["demo"]
        assert again["state"] == "RECORDING" and again["episode_id"] == reset["episode_id"]
        assert again["phase_elapsed_sec"] < 5
        record_a_bit(env)

        ended = env.c.post("/api/demo/end").get_json()["demo"]
        assert ended["state"] == "IDLE" and ended["saved"] == 2
        assert is_saved(env, again["episode_id"])

    def test_end_는_녹화_중이면_저장하고_리셋_중이면_그냥_끝낸다(self, env):
        env.hub.start_teleop()
        start(env)
        record_a_bit(env)
        env.c.post("/api/demo/next")                                    # 1 저장, 리셋 중
        ended = env.c.post("/api/demo/end").get_json()["demo"]
        assert ended["state"] == "IDLE" and ended["saved"] == 1
        assert len(list((env.root / "episodes").glob("episode_*"))) == 1

    def test_redo_는_버리고_리셋_뒤_같은_ID_로_다시_녹화한다(self, env):
        env.hub.start_teleop()
        episode_id = start(env).get_json()["demo"]["episode_id"]
        record_a_bit(env)

        reset = env.c.post("/api/demo/redo").get_json()["demo"]
        assert reset["state"] == "RESET" and reset["saved"] == 0 and reset["episode_id"] == episode_id
        assert not episode_dir(env, episode_id).exists()        # 스레드를 정리한 뒤 지우므로 폴더째 사라진다
        # 의도한 폐기는 오류가 아니다 — 'recording_aborted' 알림이 뜨면 안 된다.
        assert env.c.get("/api/status").get_json()["recording"]["last_error"] is None
        alert_ids = {a["id"] for a in env.c.get("/api/alerts?refresh=1").get_json()["alerts"]}
        assert "recording_aborted" not in alert_ids

        again = env.c.post("/api/demo/next").get_json()["demo"]         # 리셋 건너뛰기
        assert again["state"] == "RECORDING" and again["episode_id"] == episode_id
        record_a_bit(env)

        ended = env.c.post("/api/demo/end").get_json()["demo"]
        assert ended["saved"] == 1 and ended["last_saved"] == episode_id
        assert meta_of(env, episode_id)["kind"] == "demo"

    def test_리셋_중에는_버릴_에피소드가_없어_redo_는_409(self, env):
        env.hub.start_teleop()
        start(env)
        record_a_bit(env)
        env.c.post("/api/demo/next")
        res = env.c.post("/api/demo/redo")
        assert res.status_code == 409 and "리셋" in message(res)
        assert demo(env)["state"] == "RESET" and demo(env)["saved"] == 1

    def test_세션_중_수동_녹화_시작은_거부된다(self, env):
        env.hub.start_teleop()
        start(env)
        body = {"episode_id": "900001", "object_id": "x", "label": "good"}
        res = env.c.post("/api/recording/start", json=body)
        assert res.status_code == 409 and "시연 세션" in message(res)
        record_a_bit(env)
        env.c.post("/api/demo/next")                                    # 리셋 중에도 거부
        assert env.c.post("/api/recording/start", json=body).status_code == 409
        env.c.post("/api/demo/end")
        assert env.c.post("/api/recording/start", json=body).status_code == 200      # 세션이 끝나면 다시 된다
        env.c.post("/api/recording/stop")

    def test_세션_중_수동_녹화_정지는_거부되고_비상_정지는_그대로_된다(self, env):
        env.hub.start_teleop()
        start(env)
        record_a_bit(env)
        res = env.c.post("/api/recording/stop")         # 세션 스레드의 stop 과 겹치면 에피소드를 잃는다
        assert res.status_code == 409 and "시연 세션" in message(res)
        assert env.hub.recorder.state.value == "RECORDING" and demo(env)["state"] == "RECORDING"     # 세션도 녹화도 그대로
        env.c.post("/api/demo/next")                    # 리셋 중에도 거부(세션이 녹화를 쥐고 있다)
        assert env.c.post("/api/recording/stop").status_code == 409
        env.c.post("/api/demo/next")                    # 다음 에피소드 녹화 중
        record_a_bit(env)
        result = env.c.post("/api/emergency_stop").get_json()      # 비상 정지는 이 가드를 지나지 않는다
        assert result["ok"] is True and result["recording"] == "aborted"
        wait_until(lambda: demo(env)["state"] == "IDLE", "세션 종료")

    def test_세션이_끝나면_다시_시작할_수_있고_지난_fault_는_지워진다(self, env):
        env.hub.start_teleop()
        start(env)
        env.hub.recorder.abort("디스크 쓰기 실패: x")
        wait_until(lambda: demo(env)["fault"] is not None, "fault 표시")
        env.hub.start_teleop()
        res = start(env)
        assert res.status_code == 200 and res.get_json()["demo"]["fault"] is None
        env.c.post("/api/demo/end")


# ------------------------------------------------------------------ 중단
class TestAbort:
    def test_비상_정지는_세션을_끝내고_녹화는_허브가_aborted_로_중단한다(self, env):
        env.hub.start_teleop()
        episode_id = start(env).get_json()["demo"]["episode_id"]
        record_a_bit(env)

        result = env.c.post("/api/emergency_stop").get_json()
        assert result["ok"] is True
        assert result["recording"] == "aborted"        # 세션이 먼저 녹화기를 중단했다면 'idle' 이 된다
        assert result["teleop"]["running"] is False

        wait_until(lambda: demo(env)["state"] == "IDLE", "세션 종료")
        status = demo(env)
        assert "비상 정지" in status["fault"] and status["saved"] == 0
        assert env.hub.recorder.state.value == "IDLE"
        assert not is_saved(env, episode_id)
        assert "비상 정지" in env.hub.recorder.status()["last_error"]
        time.sleep(0.4)                                # 중단 뒤에 새 녹화가 시작되지 않는다
        assert env.hub.recorder.state.value == "IDLE" and demo(env)["state"] == "IDLE"

    def test_비상_정지는_리셋_중에도_세션을_끝내고_저장된_에피소드는_남긴다(self, env):
        env.hub.start_teleop()
        episode_id = start(env).get_json()["demo"]["episode_id"]
        record_a_bit(env)
        env.c.post("/api/demo/next")                   # 저장 1, 리셋 중

        result = env.c.post("/api/emergency_stop").get_json()
        assert result["recording"] == "idle"
        wait_until(lambda: demo(env)["state"] == "IDLE", "세션 종료")
        assert "비상 정지" in demo(env)["fault"]
        assert meta_of(env, episode_id)["kind"] == "demo"

    def test_비상_정지에_세션_중단이_실패해도_기존_정지는_그대로_진행된다(self, env, monkeypatch):
        env.hub.start_teleop()
        start(env)

        def broken(reason):
            raise RuntimeError("세션 중단 실패")

        monkeypatch.setattr(env.hub.demo, "abort", broken)
        result = env.c.post("/api/emergency_stop").get_json()
        assert result["ok"] is True and result["teleop"]["running"] is False
        assert result["recording"] in {"aborted", "idle"}      # 세션이 텔레옵 정지를 보고 먼저 녹화를 중단할 수도 있다
        assert result["conveyors"] and all("stopped" in o and "disabled" in o for o in result["conveyors"].values())
        # 세션은 중단 신호를 못 받았어도 텔레옵이 멈춘 것을 보고 스스로 끝난다.
        wait_until(lambda: demo(env)["state"] == "IDLE", "세션 종료")
        assert env.hub.recorder.state.value == "IDLE"

    def test_텔레옵을_멈추면_세션이_끝나고_녹화를_버린다(self, env):
        env.hub.start_teleop()
        episode_id = start(env).get_json()["demo"]["episode_id"]
        record_a_bit(env)
        env.c.post("/api/teleop/stop")

        wait_until(lambda: demo(env)["state"] == "IDLE", "세션 종료")
        assert "텔레옵" in demo(env)["fault"]
        assert env.hub.recorder.state.value == "IDLE"
        assert not is_saved(env, episode_id)
        assert "텔레옵" in env.hub.recorder.status()["last_error"]     # 허브가 모르는 사유라 세션이 직접 중단했다

    def test_녹화기가_세션_밖에서_중단되면_세션은_fault_로_끝난다(self, env):
        env.hub.start_teleop()
        start(env)
        env.hub.recorder.abort("디스크 쓰기 실패: x")
        wait_until(lambda: demo(env)["state"] == "IDLE", "세션 종료")
        assert "디스크 쓰기 실패" in demo(env)["fault"]

    def test_예외가_나면_fault_로_끝나고_앞서_저장한_에피소드는_남는다(self, env, monkeypatch):
        env.hub.start_teleop()
        real_start, calls = env.hub.recorder.start, []

        def flaky(*args, **kwargs):
            calls.append(1)
            if len(calls) > 1:
                raise OSError("disk full")
            return real_start(*args, **kwargs)

        monkeypatch.setattr(env.hub.recorder, "start", flaky)
        episode_id = start(env, episode_sec=1, reset_sec=0).get_json()["demo"]["episode_id"]

        wait_until(lambda: demo(env)["state"] == "IDLE", "세션 종료")
        status = demo(env)
        assert "disk full" in status["fault"] and status["saved"] == 1 and status["last_saved"] == episode_id
        assert meta_of(env, episode_id)["kind"] == "demo"
        assert env.hub.recorder.state.value == "IDLE"


# ------------------------------------------------------------------ next/redo 요청과 전환의 경합
class TestRequestRace:
    """요청(next/redo/end)이 세션 스레드의 전환과 엇갈리는 두 경우. 저장하는 몇 초(_finish)를 이벤트로 붙잡아 순서를 강제한다."""

    @staticmethod
    def hold_save(env, monkeypatch):
        """recorder.stop 을 붙잡는다 — 세션 스레드가 '저장하는 중'(state 는 아직 RECORDING)에 머문다."""
        entered, release = threading.Event(), threading.Event()
        real = env.hub.recorder.stop

        def stop(*args, **kwargs):
            entered.set()
            assert release.wait(10)
            return real(*args, **kwargs)

        monkeypatch.setattr(env.hub.recorder, "stop", stop)
        return entered, release

    @staticmethod
    def in_thread(fn):
        out: dict = {}

        def run():
            try:
                out["ok"] = fn()
            except Exception as exc:  # noqa: BLE001
                out["err"] = exc

        thread = threading.Thread(target=run)
        thread.start()
        return thread, out

    def test_시간이_다_되어_저장하는_동안_온_요청은_이미_넘어갔다고_거절하고_다음_구간이_집지_않는다(self, env, monkeypatch):
        env.hub.start_teleop()
        entered, release = self.hold_save(env, monkeypatch)
        assert start(env, episode_sec=1, reset_sec=30).status_code == 200
        assert entered.wait(10)                         # 1초가 지나 저장 중 — state 는 아직 RECORDING 이라 요청이 받아들여진다
        assert demo(env)["state"] == "RECORDING"
        thread, out = self.in_thread(env.hub.demo.next)
        wait_until(lambda: env.hub.demo._request == "next", "요청 대기")
        release.set()                                   # 저장이 끝나고 리셋으로 넘어간다
        thread.join(10)
        assert isinstance(out.get("err"), RecordingStateError) and "이미 다음 구간으로 넘어갔습니다" in out["err"].message
        status = demo(env)
        assert status["state"] == "RESET" and status["saved"] == 1      # 그 요청이 리셋을 통째로 건너뛰게 하지 않았다
        assert env.hub.demo.next()["state"] == "RECORDING"              # 리셋 구간에서 새로 한 요청은 그대로 먹는다

    def test_응답이_늦어_시간_초과된_요청은_거두어_나중에_실행되지_않고_새_요청을_막지_않는다(self, env, monkeypatch):
        monkeypatch.setattr(demo_session, "REQUEST_TIMEOUT_SEC", 0.3)
        env.hub.start_teleop()
        entered, release = self.hold_save(env, monkeypatch)
        assert start(env, episode_sec=1, reset_sec=30).status_code == 200
        assert entered.wait(10)                         # 저장 중 — 세션 스레드는 요청을 집지 못한다
        with pytest.raises(RecordingStateError, match="응답하지 않습니다"):
            env.hub.demo.end()
        assert env.hub.demo._request is None            # 거뒀다 — 안 거두면 나중에 그 end 가 실행되어 세션이 끝난다
        release.set()
        wait_until(lambda: demo(env)["state"] == "RESET", "리셋 진입")
        time.sleep(0.4)
        assert demo(env)["state"] == "RESET"            # 오래된 end 가 실행되지 않았다
        assert env.hub.demo.next()["state"] == "RECORDING"      # 새 요청은 '이전 요청 처리 중' 으로 거부되지 않는다

    def test_세션_스레드가_이미_집어_간_요청은_시간_초과로_거두지_않는다(self, env, monkeypatch):
        monkeypatch.setattr(demo_session, "REQUEST_TIMEOUT_SEC", 0.3)
        env.hub.start_teleop()
        assert start(env).status_code == 200
        record_a_bit(env)
        entered, release = self.hold_save(env, monkeypatch)
        thread, out = self.in_thread(env.hub.demo.next)      # next 를 집어 저장(_finish)에 들어간다
        assert entered.wait(10)
        thread.join(5)                                  # 응답이 늦어 시간 초과된다
        assert isinstance(out.get("err"), RecordingStateError) and "응답하지 않습니다" in out["err"].message
        assert env.hub.demo._request == "next"          # 이미 전환을 하는 중이라 그대로 둔다
        with pytest.raises(RecordingStateError, match="이전 요청"):
            env.hub.demo.end()                          # 전환이 끝나기 전의 새 요청은 거부된다
        release.set()
        wait_until(lambda: demo(env)["state"] == "RESET" and env.hub.demo._request is None, "전환 완료")
        assert demo(env)["saved"] == 1


# ------------------------------------------------------------------ 시연 에피소드와 분류 폴더
def make_episode(env, episode_id, object_id, label, **extra):
    directory = env.root / "episodes" / f"episode_{episode_id}"
    directory.mkdir(parents=True)
    meta = {"episode_id": episode_id, "object_id": object_id, "label": label, "defect_type": None, **extra}
    (directory / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")


def make_stills(env, label, object_id):
    directory = env.root / "classification" / label / object_id
    directory.mkdir(parents=True)
    (directory / "cam1.jpg").write_bytes(b"x")
    return directory


class TestDemoEpisodeKeepsClassificationAlone:
    """시연 에피소드의 label 은 Recorder 를 통과시키려 채운 자리표시('good')다. 같은 object_id 의 분류 이미지는
    다른(기존 방식) 에피소드가 만든 것이라, 시연 에피소드를 고치거나 지울 때 그 폴더를 건드리면 안 된다."""

    def test_수정은_분류_폴더를_옮기지_않는다(self, env):
        stills = make_stills(env, "good", "cube")
        make_episode(env, "000001", "cube", "good", kind="demo", task="옮긴다")
        meta = env.hub.recorder.index.update("000001", object_id="other")
        assert meta["object_id"] == "other"
        assert stills.is_dir() and not (env.root / "classification" / "good" / "other").exists()

    def test_삭제는_분류_이미지를_지우지_않는다(self, env):
        stills = make_stills(env, "good", "cube")
        make_episode(env, "000001", "cube", "good", kind="demo", task="옮긴다")
        env.hub.recorder.index.delete("000001")
        assert stills.is_dir()

    def test_시연_에피소드는_분류_이미지의_참조로_세지_않는다(self, env):
        stills = make_stills(env, "good", "cube")
        make_episode(env, "000001", "cube", "good")                          # 기존 방식 에피소드
        make_episode(env, "000002", "cube", "good", kind="demo", task="옮긴다")
        env.hub.recorder.index.delete("000001")                              # 진짜 마지막 참조가 사라졌다
        assert not stills.exists()

    def test_scan_objects_는_시연_에피소드만으로_분류_폴더를_되살리지_않는다(self, env):
        make_stills(env, "good", "ghost")
        make_episode(env, "000001", "ghost", "good", kind="demo", task="옮긴다")
        ids = lambda: {o["object_id"] for o in env.c.get("/api/scan/objects").get_json()["objects"]}  # noqa: E731
        assert "ghost" not in ids()
        make_episode(env, "000002", "ghost", "good")                         # 기존 방식 에피소드가 있으면 보인다
        assert "ghost" in ids()
