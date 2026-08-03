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
from backend.config import SafetyConfig, load_config  # noqa: E402
from backend.core.arm_controller import SafetyGuard, deg_to_ticks, ticks_to_deg  # noqa: E402
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
