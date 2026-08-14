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
def client(tmp_path_factory):
    """테스트용 앱. 수집 루트는 반드시 임시 폴더로 바꾼다.

    실제 lerobot_data 를 그대로 쓰면 테스트가 사용자의 수집 데이터에 파일을
    쓰거나 지운다(실제로 업로드 테스트가 models/ 에 파일을 남겼다).
    """
    from dataclasses import replace

    from backend.app import create_app
    from backend.config import load_config

    cfg = load_config()
    cfg = replace(cfg, recording=replace(cfg.recording, root=tmp_path_factory.mktemp("data")))
    app = create_app(cfg)
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


# --------------------------------------------------------------- 실루엣 추출
class TestSilhouette:
    """스캔대가 실험실 한가운데 있으면 배경(선반·창문·현수막)이 물체보다 크게 잡힌다.
    Otsu 만으로는 배경에 마스크가 붙어 5뷰 교집합이 비고 3D 형상이 통째로 사라진다."""

    def _scene(self):
        rng = np.random.default_rng(0)
        bg = rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)
        bg = np.repeat(np.repeat(bg[::8, ::8], 8, 0), 8, 1)[:240, :320]
        obj = bg.copy()
        obj[90:150, 120:200] = (40, 200, 220)   # 중앙 물체
        return obj, bg

    def test_배경차분은_중앙_물체만_잡는다(self):
        from backend.core.reconstruct3d import extract_silhouette
        obj, bg = self._scene()
        mask = extract_silhouette(obj, bg)
        ys, xs = np.nonzero(mask)
        assert 0.4 < ys.mean() / 240 < 0.6      # 무게중심이 중앙
        assert 0.4 < xs.mean() / 320 < 0.6
        assert 0.02 < (mask > 0).mean() < 0.2   # 물체 크기 수준

    def test_배경이_없으면_Otsu_로_폴백한다(self):
        from backend.core.reconstruct3d import extract_silhouette
        obj, _ = self._scene()
        assert extract_silhouette(obj).shape == obj.shape[:2]

    def test_교집합이_비면_이유를_알려주고_실패한다(self):
        from backend.core.reconstruct3d import reconstruct_from_views
        from backend.utils.errors import EmptyHullError
        # 한 대가 물체를 아예 못 본 상황(가려짐/화면 밖). visual hull 은 AND 라
        # 그 한 뷰 때문에 전부 깎인다. 위치가 다른 것만으로는 비지 않는다 —
        # 실루엣을 바운딩박스 기준으로 정규화해 맞추기 때문(normalize_silhouette).
        views = {}
        for i, name in enumerate(("cam1", "cam2", "cam3")):
            img = np.zeros((120, 120, 3), np.uint8)
            if name != "cam3":
                img[40:80, 40:80] = 255
            views[name] = img
        with pytest.raises(EmptyHullError) as exc:
            reconstruct_from_views(views, resolution=24)
        assert "실루엣" in exc.value.message
        assert exc.value.status_code == 422

    def test_배경에_물체가_찍혀있으면_그것을_짚어준다(self):
        # 가장 흔한 실수: 물체를 안 치우고 배경 캡처 → 차이가 0 이라 실루엣이 사라진다
        from backend.core.reconstruct3d import reconstruct_from_views
        from backend.utils.errors import EmptyHullError
        obj, _ = self._scene()
        views = {f"cam{i}": obj for i in range(1, 6)}
        backgrounds = {f"cam{i}": obj.copy() for i in range(1, 6)}   # 배경 == 촬영본
        with pytest.raises(EmptyHullError) as exc:
            reconstruct_from_views(views, resolution=24, backgrounds=backgrounds)
        assert "배경 캡처" in exc.value.message


# --------------------------------------------------------------- 밴딩 측정
class TestBandingScore:
    """조명 플리커로 생기는 가로 줄무늬를 수치화한다. 장면 내용이 아니라
    '행 방향 주기성'만 봐야 노출값 비교에 쓸 수 있다."""

    def _metric(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        from find_cameras import banding_score
        return banding_score

    def _scene(self):
        return np.random.default_rng(0).integers(60, 200, (480, 640, 3), dtype=np.uint8)

    def test_가로_줄무늬가_있으면_점수가_크게_오른다(self):
        score = self._metric()
        scene = self._scene()
        band = (12 * np.sin(np.arange(480) * 2 * np.pi / 24))[:, None, None]
        banded = np.clip(scene + band, 0, 255).astype(np.uint8)
        assert score(banded) > score(scene) * 3

    def test_밝기_기울기는_밴딩으로_치지_않는다(self):
        # 위아래 밝기가 다른 것은 조명 배치 문제지 플리커가 아니다.
        score = self._metric()
        scene = self._scene()
        sloped = np.clip(scene + np.linspace(-40, 40, 480)[:, None, None], 0, 255).astype(np.uint8)
        assert score(sloped) < score(scene) * 1.5


# --------------------------------------------------------------- 실루엣 정규화
class TestSilhouetteNormalization:
    """카메라마다 물체까지의 거리와 각도가 달라 화면에서 차지하는 크기·위치가
    다르다(실측: 같은 물체가 뷰별로 8%~54%). 정규화 없이 복셀을 이미지 전체에
    투영하면 한 뷰에서라도 배경에 떨어져 교집합이 통째로 비어버린다."""

    def _view(self, size, cx, cy, r):
        """물체 하나를 크기·위치를 달리해 그린 뷰."""
        img = np.zeros((240, 320, 3), np.uint8)
        yy, xx = np.mgrid[0:240, 0:320]
        img[((yy - cy) ** 2 + (xx - cx) ** 2) < r ** 2] = 255
        return img

    def test_뷰마다_크기와_위치가_달라도_형상이_나온다(self):
        from backend.core.reconstruct3d import reconstruct_from_views
        # 같은 구를 뷰마다 다른 크기·위치로 촬영한 상황
        views = {
            "cam1": self._view(320, 80, 60, 25),     # 작고 왼쪽 위
            "cam2": self._view(320, 160, 120, 70),   # 크고 가운데
            "cam3": self._view(320, 240, 180, 40),
            "cam4": self._view(320, 160, 120, 90),
            "cam5": self._view(320, 100, 150, 55),
        }
        r = reconstruct_from_views(views, resolution=32)
        assert r.stats["num_vertices"] > 0
        assert r.stats["voxel_occupancy"] > 0

    def test_빈_마스크는_빈_정사각형을_돌려준다(self):
        from backend.core.reconstruct3d import normalize_silhouette
        out = normalize_silhouette(np.zeros((240, 320), np.uint8), 32)
        assert out.shape == (32, 32) and out.max() == 0

    def test_종횡비를_유지한다(self):
        # 가로로 긴 물체를 정사각으로 늘리면 뷰마다 다르게 찌그러진다
        from backend.core.reconstruct3d import normalize_silhouette
        mask = np.zeros((240, 320), np.uint8)
        mask[100:120, 60:260] = 255          # 200x20 (10:1)
        out = normalize_silhouette(mask, 64)
        ys, xs = np.nonzero(out)
        assert (xs.max() - xs.min()) > (ys.max() - ys.min()) * 4


# --------------------------------------------------------------- 임의 방향 투영
class TestArbitraryViewDirections:
    """리그는 직교 5뷰가 아니라 대각선 4대 + 하단 1대다. 예전 구현은 X·Y·Z 세 축
    중 하나로만 투영해 대각선 카메라를 가장 가까운 축으로 스냅했고, 그래서
    형상이 물체와 전혀 다르게 나왔다."""

    def _circle_views(self, names):
        S = 160
        yy, xx = np.mgrid[0:S, 0:S]
        circle = (((yy - S / 2) ** 2 + (xx - S / 2) ** 2) < (S * 0.35) ** 2)
        return {n: (circle.astype(np.uint8) * 255) for n in names}

    def test_구는_구로_복원된다(self):
        # 어느 방향에서 봐도 원 → 재구성 결과도 구여야 한다.
        # 정육면체(점유율 100%)와 확실히 구분되는지로 판정한다.
        from backend.core.reconstruct3d import _carve_visual_hull
        dirs = {"a": (0, 0, 1), "b": (0, 0, -1), "c": (-1, 0, 0),
                "d": (1, 0, 0), "e": (0, 1, 0), "f": (0, -1, 0)}
        vol = _carve_visual_hull(self._circle_views(dirs), resolution=40, directions=dirs)
        assert 0.45 < vol.mean() < 0.65      # 내접구 이론값 0.524

    def test_대각선_방향도_실제로_깎는다(self):
        # 축에 정렬되지 않은 방향이 무시되면 아무것도 안 깎여 점유율이 1.0 이 된다.
        from backend.core.reconstruct3d import _carve_visual_hull
        dirs = {"a": (-1, 1, 1), "b": (1, 1, 1), "c": (-1, 0, 1),
                "d": (1, 0, 1), "e": (0, -1, 0)}
        vol = _carve_visual_hull(self._circle_views(dirs), resolution=40, directions=dirs)
        assert vol.mean() < 0.85

    def test_시선이_월드up과_나란해도_기저가_만들어진다(self):
        # 하단/상단 뷰는 up 벡터를 그대로 쓸 수 없다(외적이 0).
        from backend.core.reconstruct3d import view_basis
        for d in ((0, 1, 0), (0, -1, 0)):
            right, up = view_basis(d)
            assert np.isfinite(right).all() and np.isfinite(up).all()
            assert abs(float(right @ up)) < 1e-9      # 직교


# --------------------------------------------------------------- 플리커 밴딩
class TestDebanding:
    """조명 플리커로 생긴 가로 줄무늬. 촬영본과 배경은 서로 다른 시각에 찍혀
    줄무늬 위상이 다르므로, 그냥 빼면 화면 전체가 '달라진 영역'으로 잡힌다.
    그 엉터리 실루엣 5장의 교집합이 형상을 잘라먹는다."""

    def _scene(self):
        import cv2
        H, W = 240, 320
        rng = np.random.default_rng(0)
        bg = np.repeat(np.repeat(rng.integers(80, 180, (H // 8, W // 8, 3), dtype=np.uint8), 8, 0), 8, 1)
        obj = bg.copy()
        cv2.circle(obj, (W // 2, H // 2), 55, (60, 200, 220), -1)
        truth = np.zeros((H, W), np.uint8)
        cv2.circle(truth, (W // 2, H // 2), 55, 1, -1)
        return obj, bg, truth > 0

    def _band(self, img, amp, phase):
        m = 1.0 + amp * np.sin(np.arange(img.shape[0]) * 2 * np.pi / 26 + phase)
        return np.clip(img.astype(np.float32) * m[:, None, None], 0, 255).astype(np.uint8)

    def _iou(self, mask, truth):
        return (mask & truth).sum() / (mask | truth).sum()

    def test_밴딩이_있어도_실루엣이_유지된다(self):
        from backend.core.reconstruct3d import extract_silhouette
        obj, bg, truth = self._scene()
        # 위상이 다른 강한 밴딩 — 디밴딩이 없으면 IoU 가 0.13 까지 떨어진다(실측)
        mask = extract_silhouette(self._band(obj, 0.30, 0.0), self._band(bg, 0.30, 1.7)) > 0
        assert self._iou(mask, truth) > 0.9

    def test_밴딩이_없을_때도_품질이_떨어지지_않는다(self):
        from backend.core.reconstruct3d import extract_silhouette
        obj, bg, truth = self._scene()
        mask = extract_silhouette(obj, bg) > 0
        assert self._iou(mask, truth) > 0.9

    def test_디밴딩은_가로_밝기변조만_없앤다(self):
        # 물체의 공간 구조는 건드리면 안 된다.
        from backend.core.reconstruct3d import _deband
        obj, _, _ = self._scene()
        out = _deband(self._band(obj, 0.3, 0.0))
        rows = out.reshape(out.shape[0], -1).mean(axis=1)
        banded = self._band(obj, 0.3, 0.0).reshape(obj.shape[0], -1).mean(axis=1)
        assert rows.std() < banded.std() * 0.6      # 행간 밝기 편차가 줄어야 한다


# --------------------------------------------------------------- 메시 후처리
class TestMeshCleanup:
    """space carving 결과를 그대로 메시로 만들면 공중에 뜬 파편 수십 개와
    계단 모양 표면이 그대로 보인다(실측: 파편 98개, 인접면 각도차 48.5도)."""

    def _volume_with_debris(self):
        vol = np.zeros((32, 32, 32), np.float32)
        zz, yy, xx = np.mgrid[0:32, 0:32, 0:32]
        vol[((zz - 16) ** 2 + (yy - 16) ** 2 + (xx - 16) ** 2) < 8 ** 2] = 1.0  # 본체
        rng = np.random.default_rng(0)
        for _ in range(20):                      # 떠다니는 파편
            z, y, x = rng.integers(1, 30, 3)
            if (z - 16) ** 2 + (y - 16) ** 2 + (x - 16) ** 2 > 12 ** 2:
                vol[z, y, x] = 1.0
        return vol

    def test_떠다니는_파편이_제거된다(self):
        from scipy import ndimage
        from backend.core.reconstruct3d import _clean_volume
        vol = self._volume_with_debris()
        _, before = ndimage.label(vol > 0.5)
        _, after = ndimage.label(_clean_volume(vol) > 0.5)
        assert before > 5
        assert after == 1

    def test_본체는_남는다(self):
        from backend.core.reconstruct3d import _clean_volume
        vol = self._volume_with_debris()
        cleaned = _clean_volume(vol)
        # 구의 중심은 반드시 살아 있어야 한다
        assert cleaned[16, 16, 16] > 0.5
        # 부피가 절반 아래로 깎이면 평활화가 과하다
        assert (cleaned > 0.5).sum() > (vol > 0.5).sum() * 0.5

    def test_빈_볼륨은_그대로_돌려준다(self):
        from backend.core.reconstruct3d import _clean_volume
        empty = np.zeros((16, 16, 16), np.float32)
        assert _clean_volume(empty).sum() == 0


# --------------------------------------------------------------- CAD 기준 모델
class TestStlLoader:
    """STL 은 삼각형마다 정점을 중복 저장하고, 바이너리 헤더에도 'solid' 를
    써넣는 CAD 툴이 있어 포맷 판별이 함정이다."""

    def _cube_triangles(self):
        # 정육면체 8정점 → 12삼각형
        v = np.array([[0, 0, 0], [1, 0, 0], [1, 1, 0], [0, 1, 0],
                      [0, 0, 1], [1, 0, 1], [1, 1, 1], [0, 1, 1]], np.float32)
        idx = [(0,1,2),(0,2,3),(4,6,5),(4,7,6),(0,4,5),(0,5,1),
               (1,5,6),(1,6,2),(2,6,7),(2,7,3),(3,7,4),(3,4,0)]
        return v[np.array(idx)]

    def _write_binary(self, path, tris, header=b"solid fake header padded"):
        import struct
        with open(path, "wb") as f:
            f.write(header.ljust(80, b"\0"))
            f.write(struct.pack("<I", len(tris)))
            for t in tris:
                f.write(struct.pack("<3f", 0, 0, 1))
                for p in t:
                    f.write(struct.pack("<3f", *p))
                f.write(b"\0\0")

    def _write_ascii(self, path, tris):
        lines = ["solid test"]
        for t in tris:
            lines += ["facet normal 0 0 1", "  outer loop"]
            lines += [f"    vertex {p[0]} {p[1]} {p[2]}" for p in t]
            lines += ["  endloop", "endfacet"]
        lines.append("endsolid test")
        path.write_text("\n".join(lines), encoding="utf-8")

    def test_바이너리_STL_을_읽는다(self, tmp_path):
        from backend.core.cad import load_stl
        p = tmp_path / "cube.stl"
        self._write_binary(p, self._cube_triangles())
        verts, faces = load_stl(p)
        assert len(faces) == 12
        assert len(verts) == 8        # 중복 정점이 합쳐져야 한다

    def test_헤더에_solid_가_있어도_바이너리로_인식한다(self, tmp_path):
        # 확장자나 첫 5바이트로 판별하면 여기서 틀린다
        from backend.core.cad import load_stl
        p = tmp_path / "trap.stl"
        self._write_binary(p, self._cube_triangles(), header=b"solid created by CAD")
        verts, faces = load_stl(p)
        assert len(faces) == 12 and len(verts) == 8

    def test_ASCII_STL_을_읽는다(self, tmp_path):
        from backend.core.cad import load_stl
        p = tmp_path / "cube_ascii.stl"
        self._write_ascii(p, self._cube_triangles())
        verts, faces = load_stl(p)
        assert len(faces) == 12 and len(verts) == 8

    def test_정규화는_중심을_원점에_두고_단위크기로_맞춘다(self, tmp_path):
        from backend.core.cad import load_stl, normalize
        p = tmp_path / "cube.stl"
        tris = self._cube_triangles() * np.array([30.0, 10.0, 5.0], np.float32)
        self._write_binary(p, tris)
        verts, _ = load_stl(p)
        out, info = normalize(verts)
        assert abs(float(out.mean())) < 1e-5           # 중심이 원점
        assert abs(float(np.abs(out).max()) - 1.0) < 1e-5
        assert info["size_mm"] == [30.0, 10.0, 5.0]    # 원본 치수는 보존해 알려준다

    def test_모델이_없으면_None(self, tmp_path):
        from backend.core.cad import load_reference
        assert load_reference(tmp_path, "nosuch") is None


# --------------------------------------------------------------- 모델 업로드
class TestModelUpload:
    """업로드는 신뢰 경계다. 확장자만 보고 받아두면 깨진 파일이 남아
    이후 조회마다 터진다."""

    def _stl_bytes(self):
        import struct
        v = np.array([[0,0,0],[10,0,0],[10,5,0],[0,5,0],[0,0,3],[10,0,3],[10,5,3],[0,5,3]], np.float32)
        idx = [(0,1,2),(0,2,3),(4,6,5),(4,7,6),(0,4,5),(0,5,1),
               (1,5,6),(1,6,2),(2,6,7),(2,7,3),(3,7,4),(3,4,0)]
        tris = v[np.array(idx)]
        out = b"solid header trap".ljust(80, b"\0") + struct.pack("<I", len(tris))
        for t in tris:
            out += struct.pack("<3f", 0, 0, 1)
            for p in t:
                out += struct.pack("<3f", *p)
            out += b"\0\0"
        return out

    def test_업로드하면_조회로_되돌아온다(self, client):
        import io
        r = client.post("/api/scan/model/uptest",
                        data={"file": (io.BytesIO(self._stl_bytes()), "whatever.stl")},
                        content_type="multipart/form-data")
        assert r.status_code == 200
        d = r.get_json()
        assert d["num_faces"] == 12 and d["size_mm"] == [10.0, 5.0, 3.0]

        got = client.get("/api/scan/model/uptest")
        assert got.status_code == 200
        assert got.get_json()["mesh"]["stats"]["source"] == "cad_stl"

    def test_STL_이_아니면_거부하고_저장하지_않는다(self, client):
        import io
        r = client.post("/api/scan/model/badfile",
                        data={"file": (io.BytesIO(b"not an stl at all"), "evil.stl")},
                        content_type="multipart/form-data")
        assert r.status_code == 400
        # 저장되지 않았으므로 조회도 실패해야 한다
        assert client.get("/api/scan/model/badfile").status_code == 400

    def test_경로_조작_object_id_는_거부된다(self, client):
        import io
        r = client.post("/api/scan/model/..%2F..%2Fevil",
                        data={"file": (io.BytesIO(self._stl_bytes()), "x.stl")},
                        content_type="multipart/form-data")
        assert r.status_code in (400, 404)

    def test_파일이_없으면_400(self, client):
        r = client.post("/api/scan/model/nofile", data={}, content_type="multipart/form-data")
        assert r.status_code == 400


# --------------------------------------------------------------- CAD 차이 검출
class TestDeviationDetection:
    """설계(CAD)와 실물 스캔을 2D 에서 비교해 다른 곳을 정점 색으로 표시한다.
    3D 재구성을 거치지 않는 것이 요점 — 형상은 CAD 를, 이미지는 증거로만 쓴다."""

    DIRS = {"cam1": (0.5, 0.7071, 0.5), "cam2": (-0.5, 0.7071, 0.5),
            "cam3": (-0.7071, 0, 0.7071), "cam4": (0.7071, 0, 0.7071), "cam5": (0, -1, 0)}

    def _shapes(self, R=40):
        design = np.zeros((R, R, R), bool)
        design[10:30, 12:28, 8:32] = True
        actual = design.copy()
        actual[10:19, 12:20, 8:16] = False       # 모서리 결손
        return design, actual

    def _render(self, vol, d, size=160):
        """카메라가 찍은 것처럼 '꽉 찬' 실루엣. 점 구름이면 축소 시 사라진다."""
        import cv2
        from backend.core.cad import _project_fitted, _voxel_points
        px, py, _ = _project_fitted(_voxel_points(vol), d, size)
        m = np.zeros((size, size), np.uint8)
        m[py, px] = 255
        return cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8), iterations=2)

    def test_같은_형상이면_정합이_잘_맞는다(self):
        from backend.core.cad import align_to_scan
        design, _ = self._shapes()
        sils = {n: self._render(design, d) for n, d in self.DIRS.items()}
        _, score = align_to_scan(design, sils, self.DIRS, steps=3)
        assert score > 0.7

    def test_결손_부위가_표시된다(self):
        from skimage import measure
        from backend.core.cad import align_to_scan, deviation_colors
        R = 40
        design, actual = self._shapes(R)
        sils = {n: self._render(actual, d) for n, d in self.DIRS.items()}
        verts, faces, _, _ = measure.marching_cubes(design.astype(np.float32), level=0.5)
        verts = verts / (R - 1) * 2 - 1

        rot, _ = align_to_scan(design, sils, self.DIRS, steps=3)
        colors, stats = deviation_colors(verts, faces, design, sils, self.DIRS, rot)

        # 표시가 되긴 해야 하고(0 이면 검출 실패), 전체가 칠해지면 안 된다.
        assert 0 < stats["flagged_ratio"] < 0.15
        # 결손이 크면 심각도가 최대치까지 올라야 한다(색 농도로 쓰인다).
        assert stats["max_severity"] > 0.7

        flagged = np.array(colors)[:, 0] > 0.9
        lo = np.array([10, 12, 8]) / (R - 1) * 2 - 1
        hi = np.array([19, 20, 16]) / (R - 1) * 2 - 1
        inside = np.all((verts[flagged] >= lo - 0.15) & (verts[flagged] <= hi + 0.15), axis=1)
        # 거리 변환으로 얇은 정합 오차를 걸러낸 뒤 실측 73~98%. 하한을 넉넉히 잡는다.
        assert inside.mean() > 0.6

    def test_결손이_없으면_거의_표시되지_않는다(self):
        from skimage import measure
        from backend.core.cad import align_to_scan, deviation_colors
        R = 40
        design, _ = self._shapes(R)
        sils = {n: self._render(design, d) for n, d in self.DIRS.items()}
        verts, faces, _, _ = measure.marching_cubes(design.astype(np.float32), level=0.5)
        verts = verts / (R - 1) * 2 - 1
        rot, _ = align_to_scan(design, sils, self.DIRS, steps=3)
        _, stats = deviation_colors(verts, faces, design, sils, self.DIRS, rot)
        # 양품을 불량으로 칠하면 안 된다(오검출). 실측 0.2%.
        assert stats["flagged_ratio"] < 0.05
        # 옅게라도 뜰 수는 있으나 심각도가 낮아야 한다.
        assert stats["max_severity"] < 0.5

    def test_정합이_나쁘면_색칠하지_않는다(self):
        """정상품이 통째로 주황색이 되는 사고를 막는 가드.

        CAD 를 실제 자세에 못 맞추면 실루엣이 거의 안 겹쳐 모델 전체가
        '빠진 부분'으로 잡힌다. 그 색은 불량 정보가 아니라 정합 실패의 표시다.
        """
        import struct

        from backend.core.cad import compare_with_scan, parse_stl, normalize
        R = 32
        design, _ = self._shapes(R)

        from skimage import measure
        v, f, _, _ = measure.marching_cubes(design.astype(np.float32), level=0.5)
        tris = (v / (R - 1) * 2 - 1)[f]
        raw = b"x".ljust(80, b"\0") + struct.pack("<I", len(tris))
        for t in tris:
            raw += struct.pack("<3f", 0, 0, 1)
            for q in t:
                raw += struct.pack("<3f", *q)
            raw += b"\0\0"
        verts, faces = parse_stl(raw)
        verts, info = normalize(verts)
        mesh = {"vertices": verts.tolist(), "faces": faces.tolist(),
                "vertex_colors": [[0.78, 0.78, 0.80]] * len(verts), "stats": info}

        # 물체와 전혀 관계없는 실루엣(가느다란 막대) → 정합이 될 리 없다
        junk = np.zeros((120, 120), np.uint8)
        junk[50:70, 10:110] = 255
        sils = {n: junk.copy() for n in self.DIRS}

        out = compare_with_scan(mesh, sils, self.DIRS, resolution=24)
        assert "comparison_skipped" in out["stats"]
        assert out["stats"]["num_defect_regions"] == 0
        # 색은 그대로 중립이어야 한다
        assert all(c[0] < 0.9 for c in out["vertex_colors"])


# --------------------------------------------------------------- 배경 무효화 대응
class TestBackgroundFallback:
    """배경 캡처 이후 조명이 바뀌거나 배경에 다른 물체가 있었으면 배경 차분이
    화면 전체를 물체로 잡는다(실측: 54~80%). 그 상태로는 정합도 비교도 무의미하다."""

    def _scene(self):
        rng = np.random.default_rng(0)
        # 저채도 배경(회색 실험실) + 고채도 물체(수지 부품)
        bg = rng.integers(90, 150, (240, 320, 3), dtype=np.uint8)
        bg[:, :, 1] = bg[:, :, 0]           # 무채색에 가깝게
        bg[:, :, 2] = bg[:, :, 0]
        obj = bg.copy()
        obj[100:150, 130:200] = (30, 200, 230)     # 노란 부품
        return obj, bg

    def test_조명이_크게_달라지면_채도_기반으로_전환한다(self):
        from backend.core.reconstruct3d import extract_silhouette
        obj, bg = self._scene()
        # 배경 참조만 크게 어둡게(자동 노출이 바뀐 상황). 밝기 매칭으로도 못 메운다.
        stale_bg = np.clip(bg.astype(np.float32) * 0.35, 0, 255).astype(np.uint8)

        mask = extract_silhouette(obj, stale_bg) > 0
        # 화면 전체가 물체가 되면 안 된다
        assert mask.mean() < 0.3
        ys, xs = np.nonzero(mask)
        assert 0.3 < ys.mean() / 240 < 0.75      # 물체 위치를 잡아야 한다
        assert 0.3 < xs.mean() / 320 < 0.8

    def test_배경이_멀쩡하면_차분_결과를_그대로_쓴다(self):
        from backend.core.reconstruct3d import extract_silhouette
        obj, bg = self._scene()
        mask = extract_silhouette(obj, bg) > 0
        assert 0.02 < mask.mean() < 0.25

    def test_채도_마스크는_저채도_배경에서_고채도_물체를_찾는다(self):
        from backend.core.reconstruct3d import _saturation_mask
        obj, _ = self._scene()
        mask = _saturation_mask(obj) > 0
        assert mask.mean() < 0.3
        ys, xs = np.nonzero(mask)
        assert 0.3 < ys.mean() / 240 < 0.75


# --------------------------------------------------------------- 비상 정지
class TestEmergencyStop:
    def test_모든_장치를_세우고_결과를_보고한다(self, client):
        r = client.post("/api/emergency_stop")
        assert r.status_code == 200
        d = r.get_json()
        assert d["ok"] is True
        assert d["teleop"]["running"] is False
        # 설정된 벨트 전부에 대해 결과가 보고돼야 한다(부분 실패도 항목으로 남는다)
        assert set(d["conveyors"]) == {"feed", "work", "lm"}
        assert d["recording"] in ("idle", "aborted")
