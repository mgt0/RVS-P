"""ACT 학습 메트릭 · 3D 재구성 테스트.

하드웨어·GPU 없이 데모 백엔드와 합성 뷰로 검증한다.
"""

from __future__ import annotations

import os
import time

import numpy as np
import pytest

os.environ["OMX_SIMULATE"] = "1"

from backend.core.act_metrics import compute_metrics  # noqa: E402
from backend.core.reconstruct3d import reconstruct_from_views  # noqa: E402


# --------------------------------------------------------------- 회귀 지표
class TestActMetrics:
    def test_완벽한_예측은_손실0_성공률1(self):
        target = np.random.default_rng(0).uniform(-90, 90, (16, 8, 5))
        m = compute_metrics(target, target, threshold_deg=5.0)
        assert m.loss == pytest.approx(0.0)
        assert m.success_rate == pytest.approx(1.0)
        assert m.mae_overall_deg == pytest.approx(0.0)

    def test_임계값_경계에서_성공률이_갈린다(self):
        target = np.zeros((10, 5))
        pred = np.full((10, 5), 4.0)   # 모든 관절 4도 오차
        # 임계값 5도 → 전부 성공
        assert compute_metrics(pred, target, threshold_deg=5.0).success_rate == pytest.approx(1.0)
        # 임계값 3도 → 전부 실패
        assert compute_metrics(pred, target, threshold_deg=3.0).success_rate == pytest.approx(0.0)

    def test_관절_하나만_틀려도_그_타임스텝은_실패(self):
        target = np.zeros((1, 5))
        pred = np.array([[0.0, 0.0, 0.0, 0.0, 100.0]])  # 마지막 관절만 큰 오차
        m = compute_metrics(pred, target, threshold_deg=5.0)
        assert m.success_rate == pytest.approx(0.0)

    def test_관절별_MAE가_관절수만큼_나온다(self):
        target = np.random.default_rng(1).uniform(-90, 90, (16, 8, 5))
        pred = target + 2.0
        m = compute_metrics(pred, target, threshold_deg=5.0)
        assert len(m.mae_per_joint_deg) == 5
        assert all(v == pytest.approx(2.0, abs=0.01) for v in m.mae_per_joint_deg)

    def test_shape_불일치는_에러(self):
        with pytest.raises(ValueError):
            compute_metrics(np.zeros((4, 5)), np.zeros((4, 3)))


# --------------------------------------------------------------- 3D 재구성
class TestReconstruction:
    def _synthetic_views(self, size=128):
        import cv2
        views = {}
        for name in ("front", "back", "left", "right", "top"):
            img = np.full((size, size, 3), 240, np.uint8)
            cv2.circle(img, (size // 2, size // 2), size // 3, (50, 50, 50), -1)
            views[name] = img
        return views

    def test_5뷰로_메시가_생성된다(self):
        result = reconstruct_from_views(
            self._synthetic_views(), resolution=32,
            classification={"label": "good"},
        )
        assert result.stats["num_vertices"] > 0
        assert result.stats["num_faces"] > 0
        assert len(result.vertices) == len(result.vertex_colors)

    def test_양품은_오렌지_하이라이트가_없다(self):
        result = reconstruct_from_views(
            self._synthetic_views(), resolution=32,
            defect_regions=[{"center": [0.3, 0.4, 0.6], "radius": 0.6, "severity": 1.0}],
            classification={"label": "good"},  # good 이면 regions 무시
        )
        colors = np.array(result.vertex_colors)
        orange = ((colors[:, 0] > 0.9) & (colors[:, 1] < 0.6)).sum()
        assert orange == 0

    def test_불량은_오렌지_하이라이트가_생긴다(self):
        result = reconstruct_from_views(
            self._synthetic_views(), resolution=32,
            defect_regions=[{"center": [0.3, 0.4, 0.6], "radius": 0.8, "severity": 1.0}],
            classification={"label": "defect"},
        )
        colors = np.array(result.vertex_colors)
        orange = ((colors[:, 0] > 0.85) & (colors[:, 1] < 0.65)).sum()
        assert orange > 0, "불량 영역이 오렌지로 표시되지 않았습니다"

    def test_뷰가_부족하면_에러(self):
        with pytest.raises(ValueError):
            reconstruct_from_views({"front": np.zeros((64, 64, 3), np.uint8)})


# --------------------------------------------------------------- 학습 API
@pytest.fixture(scope="module")
def client():
    from backend.app import create_app
    from backend.config import load_config
    app = create_app(load_config())
    time.sleep(0.8)
    with app.test_client() as c:
        yield c
    app.extensions["omx_hub"].shutdown()


class TestTrainingApi:
    def test_학습이_수렴한다(self, client):
        r = client.post("/api/train/start",
                        json={"epochs": 6, "epoch_delay_sec": 0.02, "demo": True})
        assert r.status_code == 200

        for _ in range(30):
            s = client.get("/api/train/status").get_json()["training"]
            if s["state"] == "DONE":
                break
            time.sleep(0.2)

        assert s["state"] == "DONE"
        recs = s["records"]
        assert len(recs) == 6
        # loss 는 내려가고 success 는 올라가야 한다
        assert recs[-1]["train_loss"] < recs[0]["train_loss"]
        assert recs[-1]["val_success_rate"] >= recs[0]["val_success_rate"]

    def test_중복_시작은_거부(self, client):
        client.post("/api/train/start", json={"epochs": 20, "epoch_delay_sec": 0.3, "demo": True})
        time.sleep(0.1)
        r = client.post("/api/train/start", json={"epochs": 5, "demo": True})
        assert r.status_code == 400
        client.post("/api/train/stop")

    def test_데모_재구성_엔드포인트(self, client):
        r = client.post("/api/scan/reconstruct_demo", json={"label": "defect", "resolution": 32})
        assert r.status_code == 200
        assert r.get_json()["mesh"]["stats"]["num_defect_regions"] == 1
