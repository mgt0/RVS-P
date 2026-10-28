"""scripts/dev_sim.py — 인자 처리와 실제 데이터 보호.

서버는 띄우지 않는다. 이 스크립트의 존재 이유가 '사용자의 실기 서버·데이터를 건드리지 않는 것'이라
그 보호 장치(5000/5001 거부, 실제 데이터 폴더와 겹치면 거부)만 확인한다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import dev_sim  # noqa: E402


class TestParseArgs:
    def test_기본값(self):
        args = dev_sim.parse_args([])
        assert (args.port, args.stream_port, args.data) == (5100, 5101, ".dev_data")

    def test_stream_port_기본은_port_더하기_1(self):
        args = dev_sim.parse_args(["--port", "5120", "--data", "x"])
        assert (args.port, args.stream_port, args.data) == (5120, 5121, "x")
        assert dev_sim.parse_args(["--port", "5120", "--stream-port", "5300"]).stream_port == 5300

    @pytest.mark.parametrize("argv", [
        ["--port", "5000"],
        ["--port", "5001"],
        ["--port", "5100", "--stream-port", "5000"],
        ["--port", "5100", "--stream-port", "5001"],
        ["--port", "4999"],                          # 기본 stream-port 가 5000 이 된다
        ["--port", "5000", "--stream-port", "5100"],
    ])
    def test_5000_5001_은_거부한다(self, argv, capsys):
        with pytest.raises(SystemExit) as exc:
            dev_sim.parse_args(argv)
        assert exc.value.code == 2
        assert "5000" in capsys.readouterr().err

    @pytest.mark.parametrize("argv", [
        ["--port", "5100", "--stream-port", "5100"],     # 같은 포트면 MJPEG 이 API 연결 예산을 먹는다
        ["--port", "0"],
        ["--port", "70000"],
        ["--port", "65535"],                              # 기본 stream-port 가 65536
    ])
    def test_잘못된_포트는_거부한다(self, argv):
        with pytest.raises(SystemExit) as exc:
            dev_sim.parse_args(argv)
        assert exc.value.code == 2

    def test_help_가_동작한다(self, capsys):
        with pytest.raises(SystemExit) as exc:
            dev_sim.parse_args(["--help"])
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "--port" in out and "--stream-port" in out and "--data" in out


class TestResolveDataDir:
    real = [REPO / "lerobot_data"]

    def test_상대_경로는_레포_루트_기준(self):
        assert dev_sim.resolve_data_dir(".dev_data", self.real) == (REPO / ".dev_data").resolve()
        assert dev_sim.resolve_data_dir(".dev_data/overview", self.real) == (REPO / ".dev_data" / "overview").resolve()

    def test_절대_경로는_그대로(self, tmp_path):
        assert dev_sim.resolve_data_dir(str(tmp_path / "d"), self.real) == (tmp_path / "d").resolve()

    @pytest.mark.parametrize("value", [
        "lerobot_data",              # 같다
        "lerobot_data/dev",          # 안 — 가짜 녹화가 진짜 데이터에 섞인다
        ".",                         # 밖(부모) — <data>/logs/train 같은 격리 경로가 실제 경로와 같아진다
        "..",
    ])
    def test_실제_데이터와_겹치면_거부한다(self, value):
        with pytest.raises(SystemExit) as exc:
            dev_sim.resolve_data_dir(value, self.real)
        assert "겹칩니다" in str(exc.value) or "같습니다" in str(exc.value)

    def test_설정에_적힌_실제_수집_루트도_보호한다(self, tmp_path):
        real = tmp_path / "real_data"
        with pytest.raises(SystemExit):
            dev_sim.resolve_data_dir(str(real / "x"), [real])


class TestCycleIsolation:
    def test_main_이_사이클_픽_궤적을_격리_폴더로_돌린다(self, tmp_path, monkeypatch):
        """사이클 픽 궤적의 기본값은 실기용 lerobot_data/waypoints/pick_place.json 이다. 시뮬레이션 확인용 가짜 궤적을
        거기 두면 실물 팔이 그대로 따라가므로, create_app 에 넘기는 설정이 격리 폴더를 가리켜야 한다.
        서버는 띄우지 않는다 — create_app 에 도착한 설정만 가로챈다."""
        from dataclasses import replace

        import backend.app as app_mod
        import backend.config as config_mod
        from backend.core import system_check

        base = config_mod.load_config()
        # 체크포인트가 설정된 PC 를 흉내 낸다(웨이포인트보다 앞서므로 비워야 한다). 시간 손잡이는 그대로 남아야 한다.
        cfg = replace(base, cycle=replace(base.cycle, pick_checkpoint=Path("D:/real/pretrained_model"), to_scan_sec=7.0))
        monkeypatch.setattr(config_mod, "load_config", lambda *a, **k: cfg)
        monkeypatch.setattr(system_check, "record_torch_import", lambda exc: None)   # 전역 상태를 남기지 않는다
        monkeypatch.setenv("OMX_SIMULATE", "1")                                        # main() 이 바꾸는 값을 되돌린다
        seen = {}

        class Stop(Exception):
            pass

        def fake_create_app(c):
            seen["cfg"] = c
            raise Stop

        monkeypatch.setattr(app_mod, "create_app", fake_create_app)
        data = tmp_path / "d"
        with pytest.raises(Stop):
            dev_sim.main(["--port", "49321", "--data", str(data)])

        cycle = seen["cfg"].cycle
        assert cycle.pick_waypoints == data.resolve() / "waypoints" / "pick_place.json"
        assert cycle.pick_checkpoint is None
        assert (REPO / "lerobot_data") not in cycle.pick_waypoints.parents
        assert cycle.to_scan_sec == 7.0
