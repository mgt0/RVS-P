"""핵심 로직 테스트.

하드웨어 없이 돌아가는 것만 다룬다(SimulatedBackend 덕분에 가능).
CI 에서 `pytest` 한 줄로 회귀를 잡는 것이 목적.
"""

from __future__ import annotations

import os
import time

import pytest

os.environ["OMX_SIMULATE"] = "1"

from backend.app import create_app  # noqa: E402
from backend.config import ArmConfig, SafetyConfig, load_config  # noqa: E402
from backend.core.arm_controller import (  # noqa: E402
    ArmController,
    SafetyGuard,
    deg_to_ticks,
    s32,
    ticks_to_deg,
)
from backend.core.storage import validate_id  # noqa: E402
from backend.utils.errors import ValidationError  # noqa: E402


# --------------------------------------------------------------- 안전 클램프
class TestSafetyGuard:
    def setup_method(self):
        self.guard = SafetyGuard(SafetyConfig(max_relative_target=5.0))

    def test_스텝당_변화량을_초과하면_잘린다(self):
        # 리더가 갑자기 100도 튀어도 팔로워는 5도만 움직여야 한다.
        out, touched = self.guard.clamp([100.0], [0.0])
        assert out[0] == pytest.approx(5.0)
        assert touched is True

    def test_음의_방향도_동일하게_잘린다(self):
        out, _ = self.guard.clamp([-100.0], [0.0])
        assert out[0] == pytest.approx(-5.0)

    def test_작은_변화는_그대로_통과한다(self):
        out, touched = self.guard.clamp([2.0], [0.0])
        assert out[0] == pytest.approx(2.0)
        assert touched is False

    def test_절대_소프트리밋이_상대제한보다_먼저_적용된다(self):
        # J1 의 리밋은 ±120. 119 에서 +5 를 요청해도 120 을 넘으면 안 된다.
        guard = SafetyGuard(SafetyConfig(max_relative_target=5.0))
        out, touched = guard.clamp([0.0, 130.0], [0.0, 119.0])
        assert out[1] <= 120.0
        assert touched is True


# --------------------------------------------------------------- 좌표계 정렬
class _FakeBackend:
    """정해진 raw 각도를 돌려주고 마지막 쓰기를 기록하는 대역."""

    def __init__(self, positions):
        self.positions = list(positions)
        self.written = None

    def connect(self): ...
    def disconnect(self): ...

    def read_positions(self):
        return list(self.positions)

    def write_positions(self, degrees):
        self.written = list(degrees)
        self.positions = list(degrees)

    def set_torque(self, enabled): ...


class TestArmAlignment:
    """실기의 팔로워는 extended position mode 멀티턴 누적으로 리더와
    절대각이 어긋난다(실측: 리더 6.7° 자세에서 팔로워 raw 187.6°).
    align_to 로 리더 좌표계에 정렬한 뒤에는 읽기/쓰기가 모두 논리각이어야 한다."""

    def _arm(self, raw):
        cfg = ArmConfig(role="follower", joint_ids=(11,), home_position=(0.0,))
        backend = _FakeBackend(raw)
        arm = ArmController(cfg, backend, SafetyGuard(SafetyConfig()))
        arm.connect()
        return arm, backend

    def test_align_후_읽기는_리더_좌표계_각도를_반환한다(self):
        arm, _ = self._arm([187.6])
        arm.align_to([6.7])
        assert arm.read()[0] == pytest.approx(6.7)

    def test_align_후_쓰기는_raw_좌표계로_변환되어_나간다(self):
        arm, backend = self._arm([187.6])
        arm.align_to([6.7])
        arm.write([8.0])
        # 논리 8.0° = raw 8.0 + (187.6 - 6.7)
        assert backend.written[0] == pytest.approx(188.9)

    def test_align_전에는_offset_이_없다(self):
        arm, backend = self._arm([50.0])
        assert arm.read()[0] == pytest.approx(50.0)


def test_unsigned32_음수_변환():
    # extended position mode 에서 0 아래로 내려간 모터는 unsigned 로 큰 값을 반환한다
    assert s32(4294967246) == -50
    assert s32(2124) == 2124


# --------------------------------------------------------------- 단위 변환
class TestUnitConversion:
    def test_중앙값은_0도(self):
        assert ticks_to_deg(2048) == pytest.approx(0.0)

    def test_왕복_변환이_안정적이다(self):
        for deg in (-90.0, -12.5, 0.0, 45.0, 179.0):
            assert ticks_to_deg(deg_to_ticks(deg)) == pytest.approx(deg, abs=0.1)


# --------------------------------------------------------------- ID 검증
class TestValidateId:
    @pytest.mark.parametrize("value", ["000042", "bracket_a01", "obj-1"])
    def test_정상_ID를_통과시킨다(self, value):
        assert validate_id(value, "test") == value

    @pytest.mark.parametrize(
        "value",
        ["../../etc/passwd", "a/b", "with space", "", "-leading", "x" * 65, "탈출"],
    )
    def test_위험하거나_잘못된_ID를_막는다(self, value):
        # 사용자 입력이 그대로 디렉터리명이 되므로 경로 조작 방어는 필수다.
        with pytest.raises(ValidationError):
            validate_id(value, "test")


# --------------------------------------------------------------- API 통합
@pytest.fixture(scope="module")
def client():
    app = create_app(load_config())
    time.sleep(1.0)  # 카메라 워커 워밍업
    with app.test_client() as c:
        yield c
    app.extensions["omx_hub"].shutdown()


class TestRecordingApi:
    def test_상태_조회가_동작한다(self, client):
        data = client.get("/api/status").get_json()
        assert data["ok"] is True
        assert data["recording"]["state"] == "IDLE"

    def test_IDLE에서_stop하면_409(self, client):
        assert client.post("/api/recording/stop").status_code == 409

    def test_defect인데_유형이_없으면_400(self, client):
        res = client.post(
            "/api/recording/start",
            json={"episode_id": "900001", "object_id": "x", "label": "defect"},
        )
        assert res.status_code == 400

    def test_잘못된_라벨은_400(self, client):
        res = client.post(
            "/api/recording/start",
            json={"episode_id": "900002", "object_id": "x", "label": "maybe"},
        )
        assert res.status_code == 400

    def test_없는_카메라는_404(self, client):
        assert client.get("/api/snapshot/nonexistent").status_code == 404

    def test_에러_응답_형식이_일관적이다(self, client):
        body = client.post("/api/recording/stop").get_json()
        assert body["ok"] is False
        assert "code" in body["error"] and "message" in body["error"]


class TestCameraSync:
    def test_스냅샷의_skew가_임계값_근처에_머문다(self, client):
        hub = client.application.extensions["omx_hub"]
        skews = []
        for _ in range(30):
            _, skew = hub.cameras.snapshot()
            skews.append(skew)
            time.sleep(1 / 30)

        skews.sort()
        median_skew = skews[len(skews) // 2]
        # 최근접 이웃 정렬이 동작하면 중앙값은 한 프레임 주기(33ms)보다 훨씬 작다.
        assert median_skew < 20.0, f"동기화 회귀 의심: median skew {median_skew:.1f}ms"


# --------------------------------------------------------------- 잘린 프레임 판정
class TestTruncatedFrame:
    """대역폭이 모자라면 MJPEG 가 중간에 끊긴 채 디코딩돼 아래쪽이 단색이 된다.
    이 판정이 헐거우면 검은 줄이 학습 데이터에 섞이고, 빡빡하면 정상 프레임을 버린다."""

    def _worker(self):
        from backend.config import CameraConfig
        from backend.core.camera_manager import _CameraWorker
        return _CameraWorker(CameraConfig(name="cam1", device=0))

    def test_아래쪽이_단색이면_잘린_프레임(self):
        import numpy as np
        img = np.random.default_rng(0).integers(0, 255, (480, 640, 3), dtype=np.uint8)
        img[-8:] = 0                      # 디코더가 검정으로 채운 상태
        assert self._worker()._is_truncated(img) is True

    def test_실제_장면은_노이즈가_있어_버리지_않는다(self):
        import numpy as np
        img = np.random.default_rng(1).integers(0, 255, (480, 640, 3), dtype=np.uint8)
        assert self._worker()._is_truncated(img) is False

    def test_거의_균일하지만_완전하지_않으면_통과시킨다(self):
        import numpy as np
        img = np.full((480, 640, 3), 30, np.uint8)   # 어두운 배경
        img[-1, -1, 0] = 31                          # 센서 노이즈 1픽셀
        assert self._worker()._is_truncated(img) is False


# --------------------------------------------------------------- 포트 힌트 매칭
class TestPortHint:
    """Windows 는 COM 번호 대소문자를 가리지 않는다. config 에 'com6' 라고 적었다고
    실제 'COM6' 를 못 찾으면 팔이 통째로 인식 안 된 것처럼 보인다."""

    def _registry(self, hint):
        from backend.config import ArmConfig
        from backend.core.port_finder import PortRegistry
        return PortRegistry({"leader": ArmConfig(role="leader", port_hint=hint)})

    def test_소문자_힌트도_매칭된다(self, monkeypatch):
        import backend.core.port_finder as pf
        fake = pf.SerialPortInfo("COM6", "", "", 0x2F5D, 0x2202, None, None)
        monkeypatch.setattr(pf, "scan_serial_ports", lambda: [fake])
        assert self._registry("com6").resolve("leader") == "COM6"

    def test_없는_포트는_여전히_실패한다(self, monkeypatch):
        import backend.core.port_finder as pf
        from backend.utils.errors import ArmPortNotFoundError
        monkeypatch.setattr(pf, "scan_serial_ports", lambda: [])
        with pytest.raises(ArmPortNotFoundError):
            self._registry("com7").resolve("leader")
