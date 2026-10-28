"""3D 검사 정확도 회귀 테스트 — ROI·뷰 제외·표면 이상·리그 캘리브레이션·원근 carving.

하드웨어 없이 합성 장면으로 검증한다. 원근 장면은 알려진 K/R/t 로 큐브 면 텍스처를
warpPerspective 해서 만든다(마커 큐브 · 흰 큐브 · 자국 있는 큐브).
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

os.environ["OMX_SIMULATE"] = "1"

from backend.core import cad  # noqa: E402
from backend.core import reconstruct3d as R  # noqa: E402
from backend.core import rig_calibration as RC  # noqa: E402

# 실제 리그와 같은 배치(config.yaml 의 view_dir). C1 하단 · C2/C4 수평 ±45° · C3/C5 위 대각.
DIRS = {"cam1": (0, -1, 0), "cam2": (-0.7071, 0, 0.7071), "cam3": (-0.5, 0.7071, 0.5),
        "cam4": (0.7071, 0, 0.7071), "cam5": (0.5, 0.7071, 0.5)}
TRUE_F = 480.0
SHADES = {0: 245, 1: 200, 2: 230, 3: 215, 4: 220, 5: 225}   # 면마다 조명이 다르다
BG = 110


# --------------------------------------------------------------- 합성 원근 장면
def _camera(name: str, dist_mm: float = 250.0) -> RC.CameraModel:
    c = np.asarray(DIRS[name], float)
    c = c / np.linalg.norm(c) * dist_mm
    z = -c / np.linalg.norm(c)
    up = np.array([0.0, 1.0, 0.0]) if abs(z[1]) < 0.99 else np.array([0.0, 0.0, 1.0])
    x = np.cross(z, up)
    x /= np.linalg.norm(x)
    rot = np.stack([x, np.cross(z, x), z])
    K = np.array([[TRUE_F, 0, 319.5], [0, TRUE_F, 239.5], [0, 0, 1]])
    return RC.CameraModel(K, np.zeros(5), cv2.Rodrigues(rot)[0].ravel(), -rot @ c, (640, 480))


def _textures(markers: bool, line_face: int | None = None, size: int = 500) -> dict:
    d = cv2.aruco.getPredefinedDictionary(RC.ARUCO_DICT)
    out = {}
    for fid in RC.FACES:
        tex = np.full((size, size, 3), SHADES[fid], np.uint8)
        if markers:
            m = int(size * RC.MARKER_MM / RC.CUBE_MM)
            o = (size - m) // 2
            tex[o:o + m, o:o + m] = cv2.cvtColor(
                cv2.aruco.generateImageMarker(d, fid, m, borderBits=1), cv2.COLOR_GRAY2BGR)
        if line_face == fid:     # 펜으로 그은 자국 같은 굵은 사선
            cv2.line(tex, (int(size * .25), int(size * .3)), (int(size * .7), int(size * .75)),
                     (40, 40, 40), 14)
        out[fid] = tex
    return out


def _render(cam: RC.CameraModel, tex: dict, rot=np.eye(3), shift=np.zeros(3)) -> np.ndarray:
    """큐브(중심 원점, 한 변 CUBE_MM)를 cam 으로 찍은 그림. 면 텍스처의 네 꼭짓점 = FACES 의 TL,TR,BR,BL."""
    h = RC.CUBE_MM / 2
    img = np.full((480, 640, 3), BG, np.uint8)
    size = next(iter(tex.values())).shape[0]
    src = np.float32([[0, 0], [size - 1, 0], [size - 1, size - 1], [0, size - 1]])
    faces = []
    for fid, (n, u, v) in RC.FACES.items():
        n, u, v = (np.asarray(a, float) for a in (n, u, v))
        c = n * h
        corners = np.array([c + (-u + v) * h, c + (u + v) * h, c + (u - v) * h, c + (-u - v) * h])
        corners = corners @ rot.T + shift
        cw = rot @ c + shift
        if (rot @ n) @ (cam.center - cw) > 0:                  # 카메라를 향한 면만
            faces.append((float(np.linalg.norm(cam.center - cw)), fid, corners))
    for _, fid, corners in sorted(faces, reverse=True):         # 먼 면부터 칠한다
        uv, _ = cam.project(corners)
        H = cv2.getPerspectiveTransform(src, uv.astype(np.float32))
        warped = cv2.warpPerspective(tex[fid], H, (640, 480))
        mask = cv2.warpPerspective(np.full((size, size), 255, np.uint8), H, (640, 480),
                                   flags=cv2.INTER_NEAREST)
        img[mask > 0] = warped[mask > 0]
    return cv2.GaussianBlur(img, (3, 3), 0)


def _rot(axis, deg):
    return cv2.Rodrigues(np.asarray(axis, float) * math.radians(deg))[0]


@pytest.fixture(scope="module")
def rig():
    return RC.Rig({n: _camera(n) for n in DIRS})


@pytest.fixture(scope="module")
def backgrounds():
    return {n: np.full((480, 640, 3), BG, np.uint8) for n in DIRS}


# --------------------------------------------------------------- 실루엣 · 정사영
class TestRoiSilhouette:
    def test_ROI_밖의_변화는_실루엣에_들어오지_않는다(self):
        """배경 캡처 뒤에 화면 가장자리로 사람이 지나가도 물체 자리만 본다."""
        bg = np.full((240, 320, 3), 120, np.uint8)
        img = bg.copy()
        img[100:160, 140:200] = 235          # 물체
        img[20:220, 10:70] = 30              # ROI 밖: 지나간 사람
        mask = R.extract_silhouette(img, bg, (0.35, 0.3, 0.75, 0.8)) > 0
        assert not mask[:, :90].any()
        assert mask[110:150, 150:190].all()

    def test_정사영_정육면체가_찌그러지지_않는다(self):
        """정사각 픽셀로 찍은 실루엣(실제 카메라)과 복셀 투영의 배율이 같아야 한다.

        예전에는 복셀 투영을 가로·세로 따로 늘려(su, sv) 대각선 뷰마다 형상이 한 축으로
        눌렸다(_ortho_scale). 축 정렬 뷰 3개만으로도 정육면체는 정확히 깎이므로, 대각선
        뷰가 더 깎아 내면 그만큼이 배율 오차다.
        """
        dirs = {"x": (1, 0, 0), "y": (0, 1, 0), "z": (0, 0, 1),
                "d1": (-0.5, 0.7071, 0.5), "d2": (0.7071, 0, 0.7071)}
        g = np.linspace(-0.5, 0.5, 60)
        pts = np.stack(np.meshgrid(g, g, g, indexing="ij"), -1).reshape(-1, 3)
        sils = {}
        for name, d in dirs.items():
            right, up = R.view_basis(d)
            img = np.zeros((400, 400), np.uint8)          # 대각선 투영 폭은 최대 √3/2 ≈ 0.87
            img[(200 - (pts @ up) * 200).astype(int), ((pts @ right) * 200 + 200).astype(int)] = 255
            sils[name] = cv2.morphologyEx(img, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        occupancy = (R._carve_visual_hull(sils, 40, dirs) > 0.5).mean()   # 참값 = 격자 전체
        assert occupancy > 0.9


# --------------------------------------------------------------- 마커 큐브 인쇄물
def _make_calib_cube():
    path = Path(__file__).resolve().parents[1] / "scripts" / "make_calib_cube.py"
    spec = importlib.util.spec_from_file_location("make_calib_cube", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestCalibCubePrint:
    def test_인쇄물에서_마커_6개가_다시_검출된다(self):
        mod = _make_calib_cube()
        assert sorted(RC.detect_markers(mod.render())) == sorted(RC.FACES)

    def test_전개도를_접으면_마커_코너가_FACES_관례와_일치한다(self):
        """십자형 전개도를 실제로 접는 변환으로 인쇄된 코너를 3D 로 옮겨 marker_corners_3d 와 비교.

        종이 좌표(a=오른쪽, b=위, 면 중심 기준)를 앞면(ID 2)에 붙은 경첩을 따라 접는다.
        """
        mod = _make_calib_cube()
        h, m = RC.CUBE_MM / 2, RC.MARKER_MM / 2
        fold = {   # 면 → (a, b) ↦ 3D
            2: lambda a, b: (a, b, h),
            0: lambda a, b: (a, h, -b),     # 앞면 위쪽 경첩으로 뒤로 접힌다
            1: lambda a, b: (a, -h, b),     # 앞면 아래쪽 경첩
            3: lambda a, b: (a, -b, -h),    # 아래 면을 지나 한 번 더 접힌다
            5: lambda a, b: (-h, b, a),     # 앞면 왼쪽 경첩
            4: lambda a, b: (h, b, -a),     # 앞면 오른쪽 경첩
        }
        for fid, (_, _, turns) in mod.NET.items():
            th = math.radians(90 * turns)   # np.rot90: 종이 위에서 반시계
            printed = []
            for a, b in ((-m, m), (m, m), (m, -m), (-m, -m)):   # 똑바로 인쇄했을 때 TL,TR,BR,BL
                printed.append(fold[fid](a * math.cos(th) - b * math.sin(th),
                                         a * math.sin(th) + b * math.cos(th)))
            np.testing.assert_allclose(np.array(printed), RC.marker_corners_3d(fid), atol=1e-4,
                                       err_msg=f"ID{fid} 면의 인쇄 방향이 FACES 관례와 다르다")


# --------------------------------------------------------------- 리그 캘리브레이션
class TestRigCalibration:
    POSES = [(np.eye(3), np.zeros(3)),
             (_rot((0, 1, 0), 20), np.array([6.0, 0, -4])),
             (_rot((0, 1, 0), -25), np.array([-5.0, 0, 5])),
             (_rot((1, 0, 0), 15), np.array([0.0, 3, 0]))]   # 마지막은 기울인 샷

    def _shots(self, rig, n):
        tex = _textures(markers=True)
        return [{c: _render(cam, tex, r, t) for c, cam in rig.cameras.items()} for r, t in self.POSES[:n]]

    def test_기울인_샷을_포함하면_다섯_대_모두_복원된다(self, rig):
        est, failed = RC.calibrate(self._shots(rig, 4), nominal_dirs=DIRS)
        assert not failed
        for name, cam in rig.cameras.items():
            got = est.cameras[name]
            assert np.linalg.norm(got.center - cam.center) < 10.0, name     # 실측 0.7~6.3mm / 250mm
            assert abs(got.K[0, 0] - TRUE_F) / TRUE_F < 0.05, name
            assert not got.focal_fixed

    def test_돌리기만_하면_하단_C1_의_초점거리는_가정값으로_고정한다(self, rig):
        """밑면만 보는 C1 은 큐브를 벨트 위에서 돌려도 늘 같은 각도라 초점거리를 못 푼다.
        풀게 두면 위치가 수백 mm 틀어졌다(실측 768mm). 고정하면 가정 화각 오차만큼만 틀린다."""
        est, _ = RC.calibrate(self._shots(rig, 3), nominal_dirs=DIRS)
        c1 = est.cameras["cam1"]
        assert c1.focal_fixed
        assert np.linalg.norm(c1.center - rig.cameras["cam1"].center) < 30.0

    def test_저장했다_불러오면_같은_모델이다(self, rig, tmp_path):
        RC.save_rig(tmp_path, rig, {}, marker_mm=RC.MARKER_MM, shots=1)
        back = RC.load_rig(tmp_path)
        for name, cam in rig.cameras.items():
            np.testing.assert_allclose(back.cameras[name].center, cam.center, atol=1e-2)

    @pytest.mark.parametrize("damage", [
        lambda d: {**d, "cameras": None},
        lambda d: {**d, "cameras": {**d["cameras"], "cam1": None}},
        lambda d: {**d, "cameras": {**d["cameras"], "cam1": {**d["cameras"]["cam1"], "size": None}}},
        lambda d: {**d, "cube_mm": None},
        lambda d: [],
    ], ids=["cameras_null", "camera_null", "size_null", "cube_mm_null", "not_an_object"])
    def test_손상된_json_은_None_이라_정사영_경로로_돈다(self, rig, tmp_path, damage):
        """null 필드가 TypeError·AttributeError 로 새면 load_rig 를 부르는 모든 재구성이 500 이 된다."""
        data = RC.save_rig(tmp_path, rig, {}, marker_mm=RC.MARKER_MM, shots=1)
        (tmp_path / RC.RIG_FILE).write_text(json.dumps(damage(data)), encoding="utf-8")
        assert RC.load_rig(tmp_path) is None

    @pytest.mark.parametrize("payload", [[], None, "x", {"cameras": [1]}, {"cameras": {"cam1": None}}],
                             ids=["list", "null", "string", "cameras_list", "camera_null"])
    def test_rig_summary_도_손상된_json_은_보정_없음(self, tmp_path, payload):
        (tmp_path / RC.RIG_FILE).write_text(json.dumps(payload), encoding="utf-8")
        summary = RC.rig_summary(tmp_path)
        assert summary["calibrated"] is False and summary["cameras"] == {}


# --------------------------------------------------------------- 원근 carving · 표면 이상
class TestMetricReconstruction:
    def test_원근_경로는_정육면체를_깎고_외접_치수를_낸다(self, rig, backgrounds):
        views = {n: _render(c, _textures(markers=False)) for n, c in rig.cameras.items()}
        sils = {n: R.extract_silhouette(views[n], backgrounds[n], rig.auto_roi(n)) for n in views}
        vol, keep, dropped, *_ = R._carve_metric(sils, rig.cameras, rig, 64)
        pts, _, _ = rig.grid(64)
        h = RC.CUBE_MM / 2
        truth = (np.abs(pts[:, 0]) <= h) & (np.abs(pts[:, 2]) <= h) & (np.abs(pts[:, 1]) <= h)
        carved = vol.ravel() > 0.5
        assert not dropped
        assert (carved & truth).sum() / (carved | truth).sum() > 0.8      # 실측 0.845

        result = R.reconstruct_from_views(views, resolution=64, backgrounds=backgrounds, rig=rig)
        size = np.array(result.stats["size_mm"])
        assert result.stats["mesh_method"] == "visual_hull_metric" and "calibration_note" not in result.stats
        # visual hull 은 물체를 감싸는 외접 형상이다. 이 배치(대각 4 + 하단 1)로는 모서리 바깥을
        # 다 깎지 못해 실측 X·Z +22%, Y +9% 크게 나온다 — 참값보다 작으면 깎기가 틀린 것.
        assert np.all(size >= RC.CUBE_MM - 2) and np.all(size <= RC.CUBE_MM * 1.3)

    def test_보정된_카메라가_MIN_VIEWS_미만이면_모든_뷰로_정사영_폴백한다(self, rig, backgrounds):
        """2대만 보정된 리그: 원근 경로는 보정 안 된 뷰를 버려 쓸 뷰가 2대가 되고 품질이 늘 low 였다
        (이유도 '카메라 연결 확인' 이라 원인을 잘못 짚었다)."""
        views = {n: _render(c, _textures(markers=False)) for n, c in rig.cameras.items()}
        two = RC.Rig({n: rig.cameras[n] for n in ("cam2", "cam4")})
        stats = R.reconstruct_from_views(views, resolution=48, backgrounds=backgrounds, rig=two, directions=DIRS).stats
        assert stats["mesh_method"] == "visual_hull" and "size_mm" not in stats
        assert stats["num_views"] == len(DIRS)                     # 보정 안 된 뷰도 버리지 않는다
        assert stats["quality"]["views_used"] >= R.MIN_VIEWS
        assert not any("쓸 수 있는 카메라" in why for why in stats["quality"]["reasons"])
        # 폴백했다는 사실은 quality.reasons 가 아니라 따로 남긴다 — reasons 가 있으면 level 이 low 가 된다
        assert "2대" in stats["calibration_note"] and stats["calibration_note"] not in stats["quality"]["reasons"]

    def test_표면_영역은_뷰마다_한_번만_구한다(self, rig, backgrounds, monkeypatch):
        """색 샘플링과 anomaly_map 이 같은 _part_surface 를 각자 구하던 것을 한 번으로 줄였다."""
        calls = []
        real = R._part_surface
        monkeypatch.setattr(R, "_part_surface", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
        views = {n: _render(c, _textures(markers=False, line_face=2)) for n, c in rig.cameras.items()}
        for geometry in ({"rig": rig}, {"directions": DIRS}):          # 원근 경로, 정사영 경로
            calls.clear()
            stats = R.reconstruct_from_views(views, resolution=32, backgrounds=backgrounds, **geometry).stats
            assert len(calls) == len(stats["views_used"])

        img, mask = views["cam3"], np.zeros((480, 640), np.uint8)
        mask[120:360, 160:480] = 255
        surface = real(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), mask > 0)
        assert np.array_equal(R.anomaly_map(img, mask), R.anomaly_map(img, mask, surface))   # 넘겨도 같은 결과

    def test_깨끗한_큐브에는_주황이_없다(self, rig, backgrounds):
        views = {n: _render(c, _textures(markers=False)) for n, c in rig.cameras.items()}
        result = R.reconstruct_from_views(views, resolution=64, backgrounds=backgrounds, rig=rig)
        assert result.stats["anomaly"]["flagged_ratio"] < 0.005    # 면마다 밝기가 달라도

    def test_자국은_그_면에만_주황으로_표시된다(self, rig, backgrounds):
        views = {n: _render(c, _textures(markers=False, line_face=2)) for n, c in rig.cameras.items()}
        result = R.reconstruct_from_views(views, resolution=64, backgrounds=backgrounds, rig=rig)
        assert result.stats["anomaly"]["flagged_ratio"] > 0.005
        v, col = np.array(result.vertices), np.array(result.vertex_colors)
        orange = (col[:, 0] - col[:, 2]) > 0.3          # 회색·흰색 텍스처는 R≈B
        assert orange.any()
        # 자국은 앞면(ID 2, +Z)에 그었다. 표시된 정점은 앞면 근처여야 한다.
        assert np.median(v[orange, 2]) > 0.6 * v[:, 2].max()

    def test_어긋난_뷰는_깎기에서_빠진다(self, rig, backgrounds):
        views = {n: _render(c, _textures(markers=False)) for n, c in rig.cameras.items()}
        shift = np.float32([[1, 0, 70], [0, 1, 30]])
        views["cam2"] = cv2.warpAffine(views["cam2"], shift, (640, 480), borderValue=(BG, BG, BG))
        result = R.reconstruct_from_views(views, resolution=48, backgrounds=backgrounds, rig=rig)
        assert "cam2" in result.stats["dropped_views"]
        assert "cam2" not in result.stats["views_used"]

    def test_형상이_안_맞으면_주황을_칠하지_않는다(self, rig, backgrounds, monkeypatch):
        """뷰끼리 안 맞는 상태의 주황은 결함 위치가 아니다(QUALITY_MIN_CONSISTENCY).
        기준을 어떤 장면도 못 넘게 올리면, 자국이 있어도 칠하지 않고 이유·할 일을 남겨야 한다."""
        monkeypatch.setattr(R, "QUALITY_MIN_CONSISTENCY", 1.01)
        views = {n: _render(c, _textures(markers=False, line_face=2)) for n, c in rig.cameras.items()}
        result = R.reconstruct_from_views(views, resolution=48, backgrounds=backgrounds, rig=rig)
        anomaly, quality = result.stats["anomaly"], result.stats["quality"]
        assert anomaly["flagged_ratio"] == 0.0 and "skipped" in anomaly
        assert quality["level"] == "low" and quality["reasons"] and quality["actions"]
        col = np.array(result.vertex_colors)
        assert np.allclose(col, R.NEUTRAL_GRAY)          # 깨진 형상에 색을 입혀 보여주지 않는다


# --------------------------------------------------------------- 강건성(2차)
class TestRobustness:
    def test_GrabCut_실루엣은_같은_사진이면_매번_같다(self):
        """GrabCut 초기화가 OpenCV 전역 난수를 써서, 고정하지 않으면 같은 사진도 뷰 선택이
        돌릴 때마다 바뀌었다."""
        rng = np.random.default_rng(5)
        bg = rng.integers(60, 200, (240, 320, 3), dtype=np.uint8)
        img = bg.copy()
        cv2.rectangle(img, (130, 90), (200, 160), (235, 235, 235), -1)
        img[100:150, 60:110] = rng.integers(0, 255, (50, 50, 3), dtype=np.uint8)   # 뒤에서 바뀐 잡동사니
        roi = (0.15, 0.25, 0.75, 0.8)
        masks = [cv2.cvtColor(cv2.cvtColor(img, cv2.COLOR_BGR2RGB), cv2.COLOR_RGB2BGR) for _ in range(2)]
        a, b = (R.extract_silhouette(m, bg, roi) for m in masks)
        assert np.array_equal(a, b)

    def test_config_에_없는_뷰는_깎기와_색칠이_같은_기본_방향을_쓴다(self):
        """view_dir 이 없는 카메라: 깎기는 VIEW_DIRECTIONS 를 쓰는데 색·이상 샘플링은 +Z 를 써서 갈렸다."""
        directions = {"cam1": DIRS["cam1"]}                       # cam5 는 config 에 없다
        d5 = np.asarray(R.VIEW_DIRECTIONS["cam5"], float)
        d5 /= np.linalg.norm(d5)
        img, mask = np.full((64, 64, 3), 200, np.uint8), np.zeros((64, 64), np.uint8)
        mask[16:48, 16:48] = 255
        normals = np.eye(3, dtype=np.float32)
        _, _, dot, _ = R._ortho_view_samples(normals * 0.3, normals, {"cam5": img}, {"cam5": mask},
                                             directions, 16)["cam5"]
        np.testing.assert_allclose(dot, normals @ d5.astype(np.float32), atol=1e-6)
        for got, want in zip(R._ortho_projections(["cam5"], 16, directions)["cam5"][:2],
                             R._ortho_projections(["cam5"], 16, None)["cam5"][:2]):
            assert np.array_equal(got, want)                      # 깎기도 같은 방향
        assert R._view_direction({}, "nope") == (0.0, 0.0, 1.0)   # 어디에도 없으면 +Z

    def test_나쁜_뷰가_둘이어도_둘_다_뺀다(self, rig, backgrounds):
        """예전 규칙(나머지 중앙값의 0.6배 미만)은 나쁜 뷰가 둘이면 서로 중앙값을 끌어내려
        하나도 못 뺐다 — 형상이 무너지던 세트가 그랬다."""
        views = {n: _render(c, _textures(markers=False)) for n, c in rig.cameras.items()}
        for name, (dx, dy) in (("cam2", (70, 30)), ("cam5", (-60, 40))):
            views[name] = cv2.warpAffine(views[name], np.float32([[1, 0, dx], [0, 1, dy]]), (640, 480),
                                         borderValue=(BG, BG, BG))
        result = R.reconstruct_from_views(views, resolution=48, backgrounds=backgrounds, rig=rig)
        assert set(result.stats["dropped_views"]) == {"cam2", "cam5"}
        assert result.stats["quality"]["level"] == "ok"

    def test_부품_테두리_바깥_틈은_자국으로_치지_않는다(self):
        """실루엣이 부품보다 넓게 잡히면 부품 테두리와 뒤쪽 밝은 물체 사이 틈이 블랙햇에 걸린다.
        깨끗한 큐브 오탐의 실제 원인이었다. 부품 면에 둘러싸인 자국만 잡아야 한다."""
        img = np.full((200, 260, 3), 90, np.uint8)
        img[40:160, 40:150] = 235                    # 부품 면
        img[40:160, 154:200] = 225                   # 뒤쪽 밝은 물체(4px 어두운 틈을 두고)
        cv2.line(img, (60, 70), (120, 130), (40, 40, 40), 3)   # 면 안의 자국
        mask = np.zeros(img.shape[:2], np.uint8)
        mask[36:164, 36:204] = 255                   # 틈·뒤쪽 물체까지 덮은 실루엣
        amap = R.anomaly_map(img, mask)
        assert amap[95:105, 85:95].max() > 0         # 자국은 잡는다
        assert amap[:, 149:156].max() == 0           # 틈은 안 잡는다

    def test_실루엣에_붙은_배경은_텍스처로_칠하지_않는다(self, rig, backgrounds):
        """실루엣이 부품 옆 배경(초록 매트)까지 덮어도 그 색을 큐브 옆면에 입히지 않는다."""
        views = {n: _render(c, _textures(markers=False)) for n, c in rig.cameras.items()}
        for name in views:
            img = views[name]
            ys, xs = np.nonzero(np.abs(img.astype(int) - BG).sum(axis=2) > 30)
            x1 = xs.max()
            img[ys.min():ys.max(), x1:min(x1 + 25, 639)] = (60, 170, 60)   # 붙어 있는 초록 띠
        result = R.reconstruct_from_views(views, resolution=48, backgrounds=backgrounds, rig=rig)
        col = np.array(result.vertex_colors)
        green = (col[:, 1] - np.maximum(col[:, 0], col[:, 2])) > 0.15
        assert green.mean() < 0.005

    def test_볼록도와_품질_판정(self):
        g = np.zeros((20, 20, 20), np.float32)
        g[4:16, 4:16, 4:16] = 1
        assert R._solidity(g) > 0.95
        g[4:16, 4:12, 4:12] = 0                      # 한 귀퉁이가 크게 파인 형상(정확한 값 80/112)
        assert abs(R._solidity(g) - 80 / 112) < 0.01
        assert R._solidity(g) < R.QUALITY_MIN_SOLIDITY
        ok = R._quality(0.7, 0.95, 4)
        low = R._quality(0.7, 0.6, 4)
        assert ok["level"] == "ok" and not ok["reasons"]
        assert low["level"] == "low" and "볼록도" in low["reasons"][0] and low["actions"]
        assert R._quality(0.3, 0.95, 4)["level"] == "low"
        assert R._quality(0.9, 0.95, 2)["level"] == "low"


# --------------------------------------------------------------- CAD 복셀화
class TestCadVoxelize:
    def test_격자_끝에_닿는_모델도_속이_찬다(self):
        """normalize() 가 모델을 ±1 에 꽉 맞추므로 껍질은 늘 격자 끝에 닿는다. 닫힘 연산이
        그 끝 면을 깎으면 fill 이 새어 속이 빈다(실측: 50mm 큐브 STL 점유 0.005)."""
        from scipy.spatial import ConvexHull

        v = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], float)
        vol = cad.voxelize(v, ConvexHull(v).simplices, 24)
        assert vol.mean() > 0.9

    def test_가늘고_긴_삼각형이_많아도_껍질_점을_모아두지_않는다(self, monkeypatch):
        """삼각형마다 (k+1)(k+2)/2 점이라 다 모으면 수억 개(수 GB)가 된다. 덩어리마다 찍고 버리면
        껍질은 똑같고 메모리가 점 수와 무관해진다."""
        import tracemalloc

        rng = np.random.default_rng(0)
        n = 1200                                              # 평면 면 분할에서 흔한 가늘고 긴 삼각형
        p0 = rng.uniform(-1, -0.6, (n, 3))
        d = rng.normal(size=(n, 3))
        d /= np.linalg.norm(d, axis=1, keepdims=True)
        p1 = np.clip(p0 + 3.0 * d, -1, 1)
        side = np.cross(d, rng.normal(size=(n, 3)))
        p2 = np.clip(p1 + 0.002 * side / np.linalg.norm(side, axis=1, keepdims=True), -1, 1)
        verts, faces = np.concatenate([p0, p1, p2]), np.stack([np.arange(n), n + np.arange(n), 2 * n + np.arange(n)], 1)

        tracemalloc.start()
        try:
            whole = cad.voxelize(verts, faces, 48)            # 점 4M 개 — 한꺼번에 모으면 400MB 였다
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        assert whole.any() and peak < 100e6
        monkeypatch.setattr(cad, "SHELL_CHUNK_POINTS", 700)   # 삼각형 하나씩 처리하게 잘게 쪼개도
        assert np.array_equal(cad.voxelize(verts, faces, 48), whole)


class TestCadReferenceCache:
    def test_같은_모델은_한_번만_읽고_파일이_바뀌면_다시_읽는다(self, tmp_path, monkeypatch):
        """models/ 에 직접 둔 STEP 은 조회할 때마다 gmsh 로 다시 삼각형화했다. (경로, 수정 시각) 키로 한 번만 읽는다."""
        from scipy.spatial import ConvexHull

        v = np.array([[x, y, z] for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], np.float32)
        (tmp_path / "models").mkdir()
        model = tmp_path / "models" / "cube.stl"
        model.write_bytes(cad.to_binary_stl(v, ConvexHull(v).simplices))
        reads = []
        real = cad.load_mesh
        monkeypatch.setattr(cad, "load_mesh", lambda path: (reads.append(path), real(path))[1])

        first = cad.load_reference(tmp_path, "cube")
        assert cad.load_reference(tmp_path, "cube") == first and len(reads) == 1
        assert not cad._cached_mesh(model, model.stat().st_mtime_ns)[0].flags.writeable   # 공유 배열은 고치지 못한다

        stamp = model.stat().st_mtime_ns + 1_000_000_000
        os.utime(model, ns=(stamp, stamp))                    # 파일이 바뀌었다
        cad.load_reference(tmp_path, "cube")
        assert len(reads) == 2


# --------------------------------------------------------------- API
@pytest.fixture(scope="module")
def client(tmp_path_factory):
    from dataclasses import replace

    from backend.app import create_app
    from backend.config import load_config

    cfg = load_config()
    cfg = replace(cfg, recording=replace(cfg.recording, root=tmp_path_factory.mktemp("scan3d")))
    app = create_app(cfg)
    time.sleep(0.8)
    with app.test_client() as c:
        yield c, cfg.recording.root
    app.extensions["omx_hub"].shutdown()


class TestScanApi:
    def test_캘리브레이션_샷_계산_초기화(self, client):
        c, _ = client
        assert c.get("/api/scan/calibration").get_json()["calibrated"] is False
        r = c.post("/api/scan/calibrate/shot", json={"settle_sec": 0})
        assert r.status_code == 200
        d = r.get_json()
        # 시뮬레이션 프레임에는 마커가 없다 — 어느 카메라에서 못 찾았는지 바로 알려준다.
        assert d["pending_shots"] == 1 and set(d["missing"]) == set(d["detected"])
        assert c.post("/api/scan/calibrate/solve").status_code == 400
        assert c.delete("/api/scan/calibrate/shots").get_json()["pending_shots"] == 0

    def test_저장된_촬영을_capture_id_로_다시_형상화한다(self, client):
        # 개요의 '최근 검사 촬영'과 사이클 위젯의 '검사 결과 보기'(#/inspect?capture=<id>)가 부르는 경로
        from backend.core.storage import imwrite

        c, root = client
        hub = c.application.extensions["omx_hub"]
        names = [cam.name for cam in hub.cfg.cameras.values() if cam.view_dir]
        capture_id = "20261012_100000_02"           # 같은 초의 두 번째 촬영(접미사)도 받는다
        folder = root / "inspections" / capture_id
        folder.mkdir(parents=True)
        for name in names:
            img = np.full((128, 128, 3), 240, np.uint8)
            cv2.circle(img, (64, 64), 40, (60, 60, 60), -1)
            imwrite(folder / f"{name}.jpg", img, 95)

        r = c.post("/api/scan/reconstruct", json={"capture_id": capture_id, "resolution": 32})
        assert r.status_code == 200
        d = r.get_json()
        assert d["capture_id"] == capture_id and d["cameras"] == sorted(names)
        stats = d["mesh"]["stats"]
        assert stats["num_vertices"] > 0 and stats["classification"] == {"label": "unknown", "confidence": 0.0}

        # 경로 탈출·잘못된 형식은 400, 없는 촬영은 404, 읽을 이미지가 모자라면 400
        for bad in ("../escape", "..\\..\\x", "20261012_100000/../..", "2026-10-12", "", "20261012_100000_2", "99999999_999999"):
            assert c.post("/api/scan/reconstruct", json={"capture_id": bad}).status_code == 400, bad
        assert c.post("/api/scan/reconstruct", json={"capture_id": 123}).status_code == 400
        assert c.post("/api/scan/reconstruct", json={"capture_id": "20991231_235959"}).status_code == 404
        (root / "inspections" / "20261012_100001").mkdir()
        imwrite(root / "inspections" / "20261012_100001" / f"{names[0]}.jpg", img, 95)
        assert c.post("/api/scan/reconstruct", json={"capture_id": "20261012_100001"}).status_code == 400
        res = c.post("/api/scan/reconstruct", json={})
        assert res.status_code == 400 and "object_id" in res.get_json()["error"]["message"]    # 둘 다 없다

    def test_같은_초의_촬영은_덮어쓰지_않고_접미사를_붙인다(self, client, tmp_path, monkeypatch):
        from datetime import datetime

        from backend.api import analysis
        from backend.core import inspections

        class SameSecond(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 28, 15, 30, 0)

        monkeypatch.setattr(analysis, "datetime", SameSecond)
        ids = [analysis._new_capture_dir(tmp_path)[0] for _ in range(3)]
        assert ids == ["20261028_153000", "20261028_153000_02", "20261028_153000_03"]
        assert all((tmp_path / "inspections" / i).is_dir() for i in ids)
        assert all(inspections.CAPTURE_ID_RE.fullmatch(i) for i in ids)     # 목록·이미지·라벨 라우트의 검증을 통과한다

        # 실제 촬영 경로도 같다: 같은 초에 두 번 찍어도 둘 다 남는다
        c, root = client
        hub = c.application.extensions["omx_hub"]
        first, second = (analysis.run_scan_capture(hub, 0.0, 32) for _ in range(2))
        assert second["capture_id"] == first["capture_id"] + "_02"
        for shot in (first, second):
            assert (root / "inspections" / shot["capture_id"] / f"{shot['cameras'][0]}.jpg").is_file()

    @pytest.mark.parametrize("path, body", [
        ("/api/scan/capture", {"settle_sec": "abc"}), ("/api/scan/capture", {"settle_sec": "nan"}),
        ("/api/scan/capture", {"resolution": None}), ("/api/scan/capture", {"resolution": "x"}),
        ("/api/scan/calibrate/shot", {"settle_sec": "abc"}),
        ("/api/scan/reconstruct", {"object_id": "x", "resolution": "abc"}),
        ("/api/scan/reconstruct_demo", {"resolution": [1]}),
    ])
    def test_숫자가_아닌_값은_500_이_아니라_400(self, client, path, body):
        c, _ = client
        res = c.post(path, json=body)
        message = res.get_json()["error"]["message"]
        assert res.status_code == 400 and ("settle_sec" in message or "resolution" in message), message

    def test_손상된_리그_파일이어도_캘리브레이션_상태는_200(self, client):
        c, root = client
        path = root / RC.RIG_FILE
        path.write_text("[]", encoding="utf-8")
        try:
            res = c.get("/api/scan/calibration")
            assert res.status_code == 200 and res.get_json()["calibrated"] is False
        finally:
            path.unlink()

    def test_벨트가_돌면_캘리브레이션_촬영도_거부(self, client):
        c, _ = client
        c.post("/api/conveyor/start")
        try:
            assert c.post("/api/scan/calibrate/shot", json={"settle_sec": 0}).status_code == 400
        finally:
            c.post("/api/conveyor/stop")

    def test_불량_라벨이어도_가짜_주황_영역을_만들지_않는다(self, client):
        """예전에는 defect 라벨이면 고정 좌표 하나를 칠했다(실제 결함 위치와 무관)."""
        c, root = client
        obj = root / "classification" / "defect" / "synth"
        obj.mkdir(parents=True)
        for name in DIRS:
            img = np.full((128, 128, 3), 240, np.uint8)
            cv2.circle(img, (64, 64), 40, (60, 60, 60), -1)
            cv2.imwrite(str(obj / f"{name}.jpg"), img)
        r = c.post("/api/scan/reconstruct", json={"object_id": "synth", "label": "defect", "resolution": 32})
        assert r.status_code == 200
        stats = r.get_json()["mesh"]["stats"]
        assert stats["num_defect_regions"] == 0
        assert stats["anomaly"]["flagged_ratio"] == 0.0
