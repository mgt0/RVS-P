"""시스템 점검 API(알림·진단·최근 학습 실행), /api/status 의 sync_skew_ms, 폰트 캐시 헤더.

알림은 사람이 놓치면 안 되는 목록이라 규칙마다 '켜지고 꺼지는 것'을 하나씩 확인한다. 하드웨어 없이
시뮬레이션 허브로 돌고, 규칙 조건은 허브·파일을 직접 만져 만든다. 수집 루트는 반드시 임시 폴더다.
"""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("OMX_SIMULATE", "1")

from backend.app import create_app  # noqa: E402
from backend.config import load_config  # noqa: E402
from backend.core import system_check  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
POLICY_BLOCK = OSError("[WinError 4551] 애플리케이션 제어 정책에서 이 파일을 차단했습니다 (torch_python.dll)")


@pytest.fixture(scope="module")
def sysenv(tmp_path_factory):
    root = tmp_path_factory.mktemp("system")
    cfg = load_config()
    cfg = replace(cfg, server=replace(cfg.server, simulate=True),
                  recording=replace(cfg.recording, root=root))
    app = create_app(cfg)
    hub = app.extensions["omx_hub"]
    hub.training._log_root = root / "logs"      # 실제 logs/train 을 읽지 않게 한다
    time.sleep(0.8)
    with app.test_client() as c:
        yield SimpleNamespace(app=app, c=c, hub=hub, root=root)
    hub.shutdown()


@pytest.fixture(autouse=True)
def reset(sysenv, monkeypatch):
    """테스트마다 같은 출발점: torch·lerobot 정상, 보정·배경 없음, 점검 캐시와 since 비움."""
    monkeypatch.setattr(system_check, "_torch_error", None)
    monkeypatch.setattr(system_check, "is_installed", lambda name: True)

    def clean():
        shutil.rmtree(sysenv.root / "background", ignore_errors=True)
        (sysenv.root / "rig_calibration.json").unlink(missing_ok=True)
        sysenv.hub._teleop = None
        sysenv.hub._autorun = None
        sysenv.hub.recorder._last_error = None
        for arm in (sysenv.hub.leader, sysenv.hub.follower):
            arm.connect()
        sysenv.app.extensions.pop("omx_system", None)

    clean()
    yield
    clean()


def alerts(env) -> dict:
    """캐시를 건너뛰고 지금 상태로 계산한 알림을 id 로 찾게 돌려준다."""
    payload = env.c.get("/api/alerts?refresh=1").get_json()
    return {a["id"]: a for a in payload["alerts"]}


def make_background(root: Path, **cam_age_days: float) -> None:
    """background/<cam>.jpg 를 만들고 mtime 을 N일 전으로 맞춘다(내용은 상관없다)."""
    bg = root / "background"
    bg.mkdir(exist_ok=True)
    for cam, days in cam_age_days.items():
        path = bg / f"{cam}.jpg"
        path.write_bytes(b"x")
        t = time.time() - days * 86400
        os.utime(path, (t, t))


# ------------------------------------------------------------------ 알림 형태
class TestAlertShape:
    def test_응답_형태와_합계(self, sysenv):
        d = sysenv.c.get("/api/alerts").get_json()
        assert d["ok"] is True and isinstance(d["generated_at"], float)
        assert set(d["counts"]) == {"fault", "warn", "info"}
        for sev, n in d["counts"].items():
            assert n == sum(1 for a in d["alerts"] if a["severity"] == sev)
        for a in d["alerts"]:
            assert {"id", "severity", "title", "summary", "since", "detail", "action"} <= set(a)
            assert {"problem", "impact", "cause", "steps", "note", "evidence"} == set(a["detail"])
            assert isinstance(a["detail"]["steps"], list) and a["detail"]["steps"]
            assert {"label", "route"} == set(a["action"])
            assert a["action"]["route"] in {"diagnostics", "inspect", "equipment", "collect", "overview"}   # 사이클 알림은 overview
        json.dumps(d)

    def test_심각도_순으로_정렬된다(self, sysenv):
        sysenv.hub._teleop = SimpleNamespace(is_alive=lambda: False, fault="리더 통신이 500ms 이상 끊겼습니다")
        rank = {"fault": 0, "warn": 1, "info": 2}
        sevs = [a["severity"] for a in sysenv.c.get("/api/alerts?refresh=1").get_json()["alerts"]]
        assert sevs[0] == "fault"
        assert [rank[s] for s in sevs] == sorted(rank[s] for s in sevs)

    def test_기본_상태는_배경_없음_경고와_보정_없음_정보(self, sysenv):
        found = alerts(sysenv)
        assert found["background_missing"]["severity"] == "warn"
        assert found["calib_missing"]["severity"] == "info"
        assert not {"torch_blocked", "torch_missing", "lerobot_missing"} & set(found)


# ------------------------------------------------------------------ 규칙: 환경
class TestEnvRules:
    def test_torch_4551_은_스마트_앱_컨트롤_차단으로_안내한다(self, sysenv, monkeypatch):
        monkeypatch.setattr(system_check, "_torch_error", POLICY_BLOCK)
        a = alerts(sysenv)["torch_blocked"]
        assert a["severity"] == "warn" and a["action"]["route"] == "diagnostics"
        assert "스마트 앱 컨트롤" in a["detail"]["cause"] and "4551" in a["detail"]["cause"]
        assert any("스마트 앱 컨트롤을 끕니다" in s for s in a["detail"]["steps"])
        assert any("다른 PC" in s for s in a["detail"]["steps"])
        assert a["detail"]["evidence"]["error"].startswith("OSError: ") and "4551" in a["detail"]["evidence"]["error"]
        assert "torch_missing" not in alerts(sysenv)

    def test_4551_이_아닌_OSError_는_원인을_단정하지_않는다(self, sysenv, monkeypatch):
        monkeypatch.setattr(system_check, "_torch_error", OSError("[WinError 1114] DLL 초기화 루틴을 실행하지 못했습니다"))
        a = alerts(sysenv)["torch_blocked"]
        text = json.dumps(a, ensure_ascii=False)
        assert "스마트 앱 컨트롤" not in text and "4551" not in text
        assert "1114" in a["detail"]["evidence"]["error"]

    @pytest.mark.parametrize("exc, expect", [
        (ModuleNotFoundError("No module named 'torch'", name="torch"), "설치되어 있지 않습니다"),
        (ImportError("cannot import name 'x'"), "손상"),
        # torch 가 찾던 다른 모듈이 없는 경우는 '미설치'가 아니다
        (ModuleNotFoundError("No module named 'typing_extensions'", name="typing_extensions"), "손상"),
    ])
    def test_ImportError_는_torch_missing(self, sysenv, monkeypatch, exc, expect):
        monkeypatch.setattr(system_check, "_torch_error", exc)
        found = alerts(sysenv)
        assert found["torch_missing"]["severity"] == "info"
        assert expect in found["torch_missing"]["detail"]["cause"]
        assert "torch_blocked" not in found

    def test_기록이_없으면_설치돼_있을_때만_알림을_내지_않는다(self, sysenv, monkeypatch):
        monkeypatch.setattr(system_check, "_torch_error", system_check._UNRECORDED)
        assert not {"torch_blocked", "torch_missing"} & set(alerts(sysenv))      # 설치돼 있음 → 확인 불가, 알림 없음
        monkeypatch.setattr(system_check, "is_installed", lambda name: name != "torch")
        a = alerts(sysenv)["torch_missing"]                                       # 설치 자체가 없으면 import 없이도 안다
        assert a["detail"]["evidence"] == {}

    def test_lerobot_미설치(self, sysenv, monkeypatch):
        assert "lerobot_missing" not in alerts(sysenv)
        monkeypatch.setattr(system_check, "is_installed", lambda name: name != "lerobot")
        a = alerts(sysenv)["lerobot_missing"]
        assert a["severity"] == "info" and a["action"]["route"] == "diagnostics"
        assert "requirements-lerobot.txt" in " ".join(a["detail"]["steps"])


# ------------------------------------------------------------------ 규칙: 보정·배경
class TestRigRules:
    def test_캘리브레이션이_있으면_알림이_꺼진다(self, sysenv):
        a = alerts(sysenv)["calib_missing"]
        assert a["action"]["route"] == "diagnostics"            # 보정 버튼은 진단 화면에 있다
        assert "진단 화면" in " ".join(a["detail"]["steps"]) and a["detail"]["note"]
        (sysenv.root / "rig_calibration.json").write_text(
            json.dumps({"cameras": {"cam2": {"rms": 0.5}}}), encoding="utf-8")
        assert "calib_missing" not in alerts(sysenv)

    def test_배경이_신선하면_배경_알림이_없다(self, sysenv):
        make_background(sysenv.root, cam2=0, cam3=6)
        assert not {"background_missing", "background_stale"} & set(alerts(sysenv))

    def test_7일을_넘기면_background_stale(self, sysenv):
        make_background(sysenv.root, cam2=8)
        found = alerts(sysenv)
        assert "background_missing" not in found
        a = found["background_stale"]
        assert a["severity"] == "info" and a["action"]["route"] == "inspect"
        assert a["detail"]["evidence"]["age_days"] == 8 and a["detail"]["evidence"]["camera"] == "cam2"
        assert "8일" in a["title"]

    def test_가장_오래된_배경이_기준이다(self, sysenv):
        # 한 카메라만 옛 배경이면 그 카메라의 실루엣이 오염된다
        make_background(sysenv.root, cam2=0, cam3=9, cam4=1)
        a = alerts(sysenv)["background_stale"]
        assert a["detail"]["evidence"]["camera"] == "cam3" and a["detail"]["evidence"]["age_days"] == 9


# ------------------------------------------------------------------ 규칙: 장치·실행
class TestDeviceRules:
    @pytest.mark.parametrize("role", ["leader", "follower"])
    def test_팔_연결이_끊기면_arm_offline(self, sysenv, role):
        arm = getattr(sysenv.hub, role)
        assert f"arm_offline:{role}" not in alerts(sysenv)
        arm.disconnect(home_first=False)
        a = alerts(sysenv)[f"arm_offline:{role}"]
        assert a["severity"] == "warn" and a["action"]["route"] == "diagnostics"
        assert a["detail"]["evidence"]["role"] == role
        arm.connect()
        assert f"arm_offline:{role}" not in alerts(sysenv)

    def test_카메라_신호가_없으면_camera_offline(self, sysenv, monkeypatch):
        live = [{**c, "connected": True, "error": None} for c in sysenv.hub.cameras.status_all()]   # 부하로 흔들리는 실제 값에 기대지 않는다
        down = [{**live[0], "connected": False, "error": "프레임 수신 없음"}, *live[1:]]
        monkeypatch.setattr(sysenv.hub.cameras, "status_all", lambda: down)
        found = alerts(sysenv)
        a = found[f"camera_offline:{live[0]['name']}"]
        assert a["severity"] == "warn" and a["action"]["route"] == "equipment"
        assert live[0]["label"] in a["title"] and a["detail"]["evidence"]["slot"] == live[0]["name"]
        assert not any(k.startswith("camera_offline:") and k != f"camera_offline:{live[0]['name']}" for k in found)

    def test_텔레옵_폴트(self, sysenv):
        sysenv.hub._teleop = SimpleNamespace(is_alive=lambda: False, fault="리더 통신이 500ms 이상 끊겼습니다: x")
        a = alerts(sysenv)["teleop_fault"]
        assert a["severity"] == "fault" and a["action"]["route"] == "equipment"
        assert "500ms" in a["summary"] and a["detail"]["evidence"]["fault"] == a["summary"]
        sysenv.hub._teleop = None                    # 정지하면 폴트도 사라진다(stop_teleop)
        assert "teleop_fault" not in alerts(sysenv)

    def test_autorun_폴트(self, sysenv):
        sysenv.hub._autorun = SimpleNamespace(status=lambda: {"running": False, "fault": "추론 시간 초과", "tick_count": 3})
        a = alerts(sysenv)["autorun_fault"]
        assert a["severity"] == "fault" and a["action"]["route"] == "equipment"
        sysenv.hub._autorun = None
        assert "autorun_fault" not in alerts(sysenv)

    def test_녹화가_중단되면_recording_aborted(self, sysenv):
        assert "recording_aborted" not in alerts(sysenv)
        sysenv.hub.recorder._last_error = "디스크 쓰기 실패: x"
        a = alerts(sysenv)["recording_aborted"]
        assert a["severity"] == "warn" and a["action"]["route"] == "collect"
        assert "디스크 쓰기 실패" in a["summary"]
        sysenv.hub.recorder._last_error = None       # 다음 녹화 시작이 지운다
        assert "recording_aborted" not in alerts(sysenv)

    def test_컨베이어_포트_닫힘은_알림이_아니다(self, sysenv):
        assert not any("conveyor" in k for k in alerts(sysenv))


# ------------------------------------------------------------------ since · 캐시
class TestSinceAndCache:
    def test_since_는_해소될_때까지_유지되고_다시_생기면_새로_붙는다(self, sysenv):
        first = alerts(sysenv)["background_missing"]["since"]
        time.sleep(0.02)
        assert alerts(sysenv)["background_missing"]["since"] == first

        make_background(sysenv.root, cam2=0)                       # 해소
        assert "background_missing" not in alerts(sysenv)
        time.sleep(0.02)
        shutil.rmtree(sysenv.root / "background")                  # 다시 발생
        assert alerts(sysenv)["background_missing"]["since"] > first

    def test_다른_알림의_since_는_서로_독립이다(self, sysenv):
        before = alerts(sysenv)["calib_missing"]["since"]
        time.sleep(0.02)
        sysenv.hub._teleop = SimpleNamespace(is_alive=lambda: False, fault="x")
        found = alerts(sysenv)
        assert found["calib_missing"]["since"] == before
        assert found["teleop_fault"]["since"] > before

    def test_알림은_5초_캐시이고_refresh_로_즉시_갱신된다(self, sysenv):
        first = sysenv.c.get("/api/alerts?refresh=1").get_json()
        assert "background_missing" in {a["id"] for a in first["alerts"]}
        make_background(sysenv.root, cam2=0)
        cached = sysenv.c.get("/api/alerts").get_json()
        assert cached["generated_at"] == first["generated_at"]      # 같은 계산 결과
        assert "background_missing" in {a["id"] for a in cached["alerts"]}
        assert "background_missing" not in alerts(sysenv)


# ------------------------------------------------------------------ 진단
class TestDiagnostics:
    def test_응답_형태(self, sysenv):
        cfg = sysenv.hub.cfg
        d = sysenv.c.get("/api/diagnostics?refresh=1").get_json()
        assert d["ok"] is True and set(d) == {"ok", "env", "serial", "cameras", "limits"}
        assert [e["id"] for e in d["env"]] == ["python", "torch", "lerobot", "opencv"]
        for e in d["env"]:
            assert {"id", "severity", "title", "detail", "hint"} == set(e) and e["severity"] in {"ok", "info", "warn"}

        # 기대값은 config.yaml 에서 읽는다 — 사용자가 설정을 고쳐도 테스트가 깨지지 않게
        assert [s["id"] for s in d["serial"]] == ["leader", "follower", "conveyor"]
        leader = d["serial"][0]
        assert leader["name"] == f"{cfg.arms['leader'].model} 리더" and leader["connected"] is True
        assert leader["error"] is None
        assert leader["match"] == {"by": "serial_number" if cfg.arms["leader"].serial_number else "port_hint",
                                   "hint": cfg.arms["leader"].port_hint}
        board = d["serial"][2]
        assert board["axes"] == list(cfg.conveyors) and board["match"] is None
        assert board["port"] == (next(iter(cfg.conveyors.values())).port or None)
        assert board["name"].startswith("컨베이어 보드")

        assert [c["slot"] for c in d["cameras"]] == list(cfg.cameras)
        for got in d["cameras"]:
            cam = cfg.cameras[got["slot"]]
            assert got["label"] == (cam.label or cam.name) and got["used_in_3d"] is (cam.view_dir is not None)
            assert got["settings"] == {"width": cam.width, "height": cam.height, "fps": cam.fps, "fourcc": cam.fourcc,
                                       "exposure": cam.exposure, "power_line_hz": cam.power_line_hz,
                                       "vflip": cam.vflip, "hflip": cam.hflip}
            assert got["roi"] == (list(cam.roi) if cam.roi else None)
            assert {"device", "connected", "error"} <= set(got)

        assert d["limits"] == {
            "joint_limits": [list(p) for p in cfg.safety.joint_limits],
            "home": list(cfg.arms["follower"].home_position),
            "max_relative_target": cfg.safety.max_relative_target,
            "homing_speed_deg_per_s": cfg.safety.homing_speed_deg_per_s,
            "max_sync_skew_ms": cfg.recording.max_sync_skew_ms,
            "sample_rate_hz": cfg.recording.sample_rate_hz,
        }
        json.dumps(d)

    def test_30초_캐시와_refresh(self, sysenv):
        assert sysenv.c.get("/api/diagnostics?refresh=1").get_json()["serial"][0]["connected"] is True
        sysenv.hub.leader.disconnect(home_first=False)
        assert sysenv.c.get("/api/diagnostics").get_json()["serial"][0]["connected"] is True       # 캐시
        assert sysenv.c.get("/api/diagnostics?refresh=1").get_json()["serial"][0]["connected"] is False

    def test_torch_행은_진입점이_남긴_기록을_따른다(self, sysenv, monkeypatch):
        def torch_row():
            rows = sysenv.c.get("/api/diagnostics?refresh=1").get_json()["env"]
            return next(r for r in rows if r["id"] == "torch")

        assert torch_row()["severity"] == "ok"
        monkeypatch.setattr(system_check, "_torch_error", POLICY_BLOCK)
        row = torch_row()
        assert row["severity"] == "warn" and "WinError 4551" in row["detail"] and "스마트 앱 컨트롤" in row["hint"]
        monkeypatch.setattr(system_check, "_torch_error", ModuleNotFoundError("x", name="torch"))
        assert torch_row()["severity"] == "info" and "미설치" in torch_row()["title"]
        monkeypatch.setattr(system_check, "_torch_error", system_check._UNRECORDED)
        assert "확인 불가" in torch_row()["title"]

    def test_python_3_13_이상은_경고(self):
        assert system_check._python_row(3, 11, 0, True) == {
            "id": "python", "severity": "ok", "title": "Python 3.11.0 가상환경(.venv)", "detail": "", "hint": ""}
        assert system_check._python_row(3, 12, 4, True)["severity"] == "ok"
        assert system_check._python_row(3, 13, 0, True)["severity"] == "warn"
        assert system_check._python_row(3, 14, 1, True)["severity"] == "warn"
        assert "가상환경 아님" in system_check._python_row(3, 12, 0, False)["title"]

    def test_opencv_는_두_패키지가_함께_있을_때만_경고(self):
        assert system_check._opencv_row("4.10", "4.10")["severity"] == "warn"
        assert system_check._opencv_row(None, "4.10")["severity"] == "ok"
        assert system_check._opencv_row("4.10", None)["severity"] == "info"
        assert system_check._opencv_row(None, None)["severity"] == "info"


# ------------------------------------------------------------------ /api/status · 학습 실행
class TestStatusAndRuns:
    def test_sync_skew_ms_는_숫자(self, sysenv):
        skew = sysenv.c.get("/api/status").get_json()["sync_skew_ms"]
        assert isinstance(skew, float) and skew >= 0

    def test_카메라가_2대_미만이면_None_이고_소수_한_자리로_반올림한다(self, sysenv, monkeypatch):
        frame = SimpleNamespace()
        monkeypatch.setattr(sysenv.hub.cameras, "snapshot", lambda: ({"cam2": frame}, 5.0))
        assert sysenv.c.get("/api/status").get_json()["sync_skew_ms"] is None
        monkeypatch.setattr(sysenv.hub.cameras, "snapshot", lambda: ({}, 0.0))
        assert sysenv.c.get("/api/status").get_json()["sync_skew_ms"] is None
        monkeypatch.setattr(sysenv.hub.cameras, "snapshot", lambda: ({"cam2": frame, "cam3": frame}, 12.345))
        assert sysenv.c.get("/api/status").get_json()["sync_skew_ms"] == 12.3

    def test_기존_status_키는_그대로(self, sysenv):
        d = sysenv.c.get("/api/status").get_json()
        assert {"ok", "recording", "leader", "follower", "teleop", "autorun", "cameras"} <= set(d)

    def test_학습_실행이_없으면_run_은_null(self, sysenv):
        assert sysenv.c.get("/api/train/runs/latest").get_json() == {"ok": True, "run": None}

    def test_가장_최근_실행을_요약한다_부작용_없이(self, sysenv):
        logs = sysenv.root / "logs"
        for name, status in (("20261027_090000", "DONE"), ("20261028_100000", "RUNNING")):
            (logs / name).mkdir(parents=True)
            (logs / name / "run.json").write_text(json.dumps({
                "status": status, "log_freq": 50, "steps": 20000, "started_at": "2026-10-28T10:00:00",
                "args": {"batch_size": 8}}), encoding="utf-8")
        (logs / "20261029_110000").mkdir()                           # run.json 없는 폴더는 건너뛴다
        try:
            assert sysenv.c.get("/api/train/runs/latest").get_json() == {"ok": True, "run": {
                "run_id": "20261028_100000", "status": "RUNNING", "steps": 20000, "log_freq": 50,
                "started_at": "2026-10-28T10:00:00"}}
            # 따라 읽기를 시작하지 않는다
            assert sysenv.c.get("/api/train/status").get_json()["training"]["state"] == "IDLE"
        finally:
            shutil.rmtree(logs)

    def test_run_json_이_깨져_있으면_null(self, sysenv):
        logs = sysenv.root / "logs"
        (logs / "20261028_100000").mkdir(parents=True)
        (logs / "20261028_100000" / "run.json").write_text('{"status": "RUN', encoding="utf-8")   # 래퍼가 쓰는 중
        try:
            assert sysenv.c.get("/api/train/runs/latest").get_json()["run"] is None
        finally:
            shutil.rmtree(logs)


# ------------------------------------------------------------------ 정적 파일 헤더
class TestStaticHeaders:
    @pytest.fixture()
    def static_root(self, tmp_path, monkeypatch):
        import backend.app as app_mod

        (tmp_path / "static" / "ui" / "fonts").mkdir(parents=True)
        (tmp_path / "static" / "ui" / "fonts" / "Test.woff2").write_bytes(b"wOF2" + b"\0" * 16)
        (tmp_path / "static" / "ui" / "x.js").write_text("1", encoding="utf-8")
        (tmp_path / "secret.txt").write_text("secret", encoding="utf-8")
        monkeypatch.setattr(app_mod, "FRONTEND_DIR", tmp_path)

    def test_폰트는_1년_캐시와_font_woff2(self, sysenv, static_root):
        r = sysenv.c.get("/static/ui/fonts/Test.woff2")
        assert r.status_code == 200 and r.mimetype == "font/woff2"
        assert r.headers["Cache-Control"] == "public, max-age=31536000, immutable"
        assert r.data[:4] == b"wOF2"

    def test_다른_정적_파일은_계속_no_store(self, sysenv, static_root):
        r = sysenv.c.get("/static/ui/x.js")
        assert r.status_code == 200 and "no-store" in r.headers["Cache-Control"]

    def test_폰트_경로로_바깥_파일을_읽을_수_없다(self, sysenv, static_root):
        assert sysenv.c.get("/static/ui/fonts/../../../secret.txt").status_code == 404


# ------------------------------------------------------------------ 진입점 규칙(CLAUDE.md 의 torch 선점)
class TestImportRules:
    def test_system_check_는_cv2_pandas_numpy_를_끌어오지_않는다(self):
        # 진입점이 torch 를 먼저 import 한 뒤에야 이것들이 올라와야 한다 — 이 모듈이 먼저 끌어오면 선점이 깨진다.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import backend.core.system_check; "
                "bad = [m for m in ('cv2', 'pandas', 'numpy', 'torch') if m in sys.modules]; "
                "sys.exit('끌려온 모듈: ' + ','.join(bad) if bad else 0)")
        done = subprocess.run([sys.executable, "-I", "-c", code, str(REPO)], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr

    @pytest.mark.parametrize("entry", ["run.py", "scripts/dev_sim.py"])
    def test_진입점은_torch_선점을_다른_import_보다_먼저_하고_OSError_도_잡는다(self, entry):
        tree = ast.parse((REPO / entry).read_text(encoding="utf-8"))
        torch_at = next(i for i, node in enumerate(tree.body)
                        if isinstance(node, ast.Try) and any(
                            isinstance(n, ast.Import) and n.names[0].name == "torch" for n in node.body))
        assert not any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in tree.body[:torch_at]), \
            "torch 선점 앞에 다른 import 가 있다"
        handler = tree.body[torch_at].handlers[0]
        caught = {e.id for e in handler.type.elts}
        assert caught == {"ImportError", "OSError"}
        # 예외를 버리지 않고 system_check 에 넘긴다
        assert "record_torch_import" in (REPO / entry).read_text(encoding="utf-8")
