"""검사 샘플·물체·데이터 준비도 API.

지켜야 할 것 세 가지:
1. 라벨은 inspections/<id>/label.json 에만 쓴다. classification/ 을 만들거나 만지면 에피소드 수정·삭제가
   그 폴더를 옮기고 지우면서 검사 라벨이 CNN 학습셋을 오염시킨다.
2. URL 로 들어온 capture_id·cam 이 파일 경로가 되므로 경로 탈출이 새면 안 된다.
3. 라벨된 촬영은 판정 근거라 삭제를 막는다.
수집 루트는 임시 폴더다 — 실제 lerobot_data 는 읽지도 쓰지도 않는다.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("OMX_SIMULATE", "1")

from backend.app import create_app  # noqa: E402
from backend.config import load_config  # noqa: E402

JPEG = b"\xff\xd8not-really-a-jpeg\xff\xd9"
OLD, MID, NEW = "20261012_100000", "20261012_100100", "20261012_100200"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    root = tmp_path_factory.mktemp("datasets")
    cfg = load_config()
    cfg = replace(cfg, server=replace(cfg.server, simulate=True), recording=replace(cfg.recording, root=root))
    app = create_app(cfg)
    hub = app.extensions["omx_hub"]
    with app.test_client() as client:
        yield SimpleNamespace(c=client, hub=hub, root=root, slots=list(hub.cfg.cameras))
    hub.shutdown()


@pytest.fixture(autouse=True)
def clean(env):
    def wipe():
        for name in ("inspections", "episodes", "models", "classification"):
            shutil.rmtree(env.root / name, ignore_errors=True)

    wipe()
    yield
    wipe()


def make_capture(env, capture_id, cams=None):
    directory = env.root / "inspections" / capture_id
    directory.mkdir(parents=True)
    for cam in cams or env.slots:
        (directory / f"{cam}.jpg").write_bytes(JPEG)
    return directory


def make_episode(env, episode_id, object_id, label, **extra):
    directory = env.root / "episodes" / f"episode_{episode_id}"
    directory.mkdir(parents=True)
    meta = {"episode_id": episode_id, "object_id": object_id, "label": label, "defect_type": None, **extra}
    (directory / "metadata.json").write_text(json.dumps(meta), encoding="utf-8")


def label(env, capture_id, **body):
    return env.c.post(f"/api/inspections/{capture_id}/label", json=body)


def stored(env, capture_id):
    return json.loads((env.root / "inspections" / capture_id / "label.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ 목록
class TestList:
    def test_최신순이고_카메라는_파일이_있는_슬롯만_나온다(self, env):
        make_capture(env, OLD)
        make_capture(env, NEW, cams=env.slots[:2])
        body = env.c.get("/api/inspections").get_json()
        assert [i["capture_id"] for i in body["inspections"]] == [NEW, OLD]
        newest = body["inspections"][0]
        assert newest["cameras"] == env.slots[:2]
        assert newest["captured_at"] == "2026-10-12T10:02:00"
        assert newest["label"] is None

    def test_같은_초의_촬영은_접미사가_붙고_원본보다_최신으로_나온다(self, env):
        # run_scan_capture 가 같은 초의 두 번째 촬영에 _02 를 붙인다(덮어쓰지 않으려고). 목록·이미지·라벨이 그 ID 를 받아야 한다.
        suffixed = f"{OLD}_02"
        make_capture(env, OLD)
        make_capture(env, suffixed)
        body = env.c.get("/api/inspections").get_json()
        assert [i["capture_id"] for i in body["inspections"]] == [suffixed, OLD]
        assert body["inspections"][0]["captured_at"] == "2026-10-12T10:00:00"       # 접미사를 떼고 시각을 읽는다
        assert env.c.get(f"/api/inspections/{suffixed}/{env.slots[0]}.jpg").status_code == 200
        assert label(env, suffixed, object_id="cube", label="good").status_code == 200
        assert env.c.delete(f"/api/inspections/{OLD}").status_code == 200

    def test_limit_은_목록만_자르고_summary_는_전체_기준이다(self, env):
        for cid in (OLD, MID, NEW):
            make_capture(env, cid)
        body = env.c.get("/api/inspections?limit=1").get_json()
        assert len(body["inspections"]) == 1
        assert body["summary"] == {"total": 3, "unlabeled": 3, "objects": {"good": 0, "defect": 0}}

    def test_잘못된_limit_은_400(self, env):
        assert env.c.get("/api/inspections?limit=abc").status_code == 400

    def test_폴더가_없으면_빈_목록(self, env):
        body = env.c.get("/api/inspections").get_json()
        assert body["inspections"] == [] and body["summary"]["total"] == 0

    def test_촬영_폴더가_아닌_이름은_무시한다(self, env):
        make_capture(env, OLD)
        (env.root / "inspections" / "notes").mkdir()
        (env.root / "inspections" / "99999999_999999").mkdir()      # 모양만 맞는 날짜
        ids = [i["capture_id"] for i in env.c.get("/api/inspections").get_json()["inspections"]]
        assert ids == [OLD]


# ------------------------------------------------------------------ 이미지 · 경로 탈출
BAD_IDS = ["..", "x", "2026101_195221", "20261012-195221", "99999999_999999", "..%5C..%5Cepisodes", "20261012_100000%0A",
           "20261012_100000_2", "20261012_100000_", "20261012_100000_ab", "20261012_100000_02_03"]    # 접미사는 숫자 두 자리 이상 하나


class TestImage:
    def test_이미지를_jpeg_로_돌려준다(self, env):
        make_capture(env, OLD)
        res = env.c.get(f"/api/inspections/{OLD}/{env.slots[0]}.jpg")
        assert res.status_code == 200 and res.mimetype == "image/jpeg" and res.data == JPEG
        assert res.headers["Cache-Control"].startswith("private")

    def test_상대_경로_root_에서도_이미지를_돌려준다(self, env, monkeypatch):
        # 실기 config.yaml 의 recording.root 는 './lerobot_data'(상대)다. send_file 이 상대 경로를
        # app.root_path(backend/) 에 붙여서 썸네일이 전부 실패했다. 다른 테스트는 절대 tmp 경로라 못 잡았다.
        make_capture(env, OLD)
        monkeypatch.chdir(env.root.parent)
        cfg = env.hub.cfg
        monkeypatch.setattr(env.hub, "cfg", replace(cfg, recording=replace(cfg.recording, root=Path(env.root.name))))
        res = env.c.get(f"/api/inspections/{OLD}/{env.slots[0]}.jpg")
        assert res.status_code == 200 and res.data == JPEG

    def test_설정에_없는_cam_은_404(self, env):
        make_capture(env, OLD)
        res = env.c.get(f"/api/inspections/{OLD}/cam99.jpg")
        assert res.status_code == 404 and res.get_json()["error"]["code"] == "unknown_camera"

    def test_슬롯은_맞지만_파일이_없으면_404(self, env):
        make_capture(env, OLD, cams=env.slots[:1])
        assert env.c.get(f"/api/inspections/{OLD}/{env.slots[1]}.jpg").status_code == 404

    def test_없는_촬영은_404(self, env):
        assert env.c.get(f"/api/inspections/{OLD}/{env.slots[0]}.jpg").status_code == 404

    @pytest.mark.parametrize("bad", BAD_IDS)
    def test_잘못된_capture_id_는_모든_라우트에서_400(self, env, bad):
        make_capture(env, OLD)
        assert env.c.get(f"/api/inspections/{bad}/{env.slots[0]}.jpg").status_code == 400
        assert label(env, bad, object_id="cube", label="good").status_code == 400
        assert env.c.delete(f"/api/inspections/{bad}").status_code == 400
        assert (env.root / "inspections" / OLD).is_dir()


# ------------------------------------------------------------------ 라벨
class TestLabel:
    def test_label_json_에만_쓰고_classification_은_만들지_않는다(self, env):
        make_capture(env, OLD)
        res = label(env, OLD, object_id="cube", label="defect", defect_type="crack", orange_eval="partial",
                    anomaly={"flagged_ratio": 0.031, "num_regions": 2, "max_severity": 0.82, "junk": 1})
        assert res.status_code == 200
        inspection = res.get_json()["inspection"]
        assert inspection["capture_id"] == OLD and inspection["cameras"] == env.slots
        saved = inspection["label"]
        assert saved["object_id"] == "cube" and saved["label"] == "defect" and saved["defect_type"] == "crack"
        assert saved["pose_index"] == 1 and saved["orange_eval"] == "partial" and saved["labeled_at"]
        assert saved["anomaly"] == {"flagged_ratio": 0.031, "num_regions": 2, "max_severity": 0.82}   # 모르는 키는 버린다
        assert stored(env, OLD) == saved
        assert not (env.root / "classification").exists()
        assert not (env.root / "inspections" / OLD / "label.json.tmp").exists()
        # 목록에도 같은 라벨이 보인다
        assert env.c.get("/api/inspections").get_json()["inspections"][0]["label"] == saved

    def test_양품이면_defect_type_을_지운다(self, env):
        make_capture(env, OLD)
        saved = label(env, OLD, object_id="cube", label="good", defect_type="crack").get_json()["inspection"]["label"]
        assert saved["defect_type"] is None and saved["orange_eval"] is None and saved["anomaly"] is None

    def test_자세_번호는_물체별로_올라가고_다시_저장해도_유지된다(self, env):
        for cid in (OLD, MID, NEW):
            make_capture(env, cid)
        pose = lambda cid, **b: label(env, cid, **b).get_json()["inspection"]["label"]["pose_index"]  # noqa: E731
        assert pose(OLD, object_id="cube", label="good") == 1
        assert pose(MID, object_id="cube", label="defect", defect_type="dent") == 2
        assert pose(NEW, object_id="other", label="good") == 1
        assert pose(OLD, object_id="cube", label="defect", defect_type="crack") == 1     # 재저장 — 유지

    def test_다른_물체로_옮겨도_남은_촬영과_번호가_겹치지_않는다(self, env):
        ids = ["20261012_100000", "20261012_100100", "20261012_100200", "20261012_100300"]
        for cid in ids:
            make_capture(env, cid)
        for cid in ids[:3]:
            label(env, cid, object_id="cube", label="good")              # 1, 2, 3
        label(env, ids[1], object_id="other", label="good")              # 2번을 다른 물체로 — cube 에는 1, 3 이 남는다
        assert label(env, ids[3], object_id="cube", label="good").get_json()["inspection"]["label"]["pose_index"] == 4

    @pytest.mark.parametrize("body", [
        {"object_id": "cube", "label": "maybe"},
        {"object_id": "cube", "label": "defect"},                                     # 불량 유형 없음
        {"object_id": "cube", "label": "defect", "defect_type": "burnt"},
        {"object_id": "../escape", "label": "good"},
        {"object_id": "", "label": "good"},
        {"label": "good"},
        {"object_id": "cube", "label": "good", "orange_eval": "great"},
        {"object_id": "cube", "label": "good", "anomaly": [1]},
        {"object_id": "cube", "label": "good", "anomaly": {"flagged_ratio": "high"}},
        {"object_id": "cube", "label": "good", "anomaly": {"num_regions": True}},
    ])
    def test_검증에_실패하면_400이고_라벨이_남지_않는다(self, env, body):
        make_capture(env, OLD)
        assert label(env, OLD, **body).status_code == 400
        assert not (env.root / "inspections" / OLD / "label.json").exists()

    def test_본문이_JSON_객체가_아니면_400(self, env):
        make_capture(env, OLD)
        assert env.c.post(f"/api/inspections/{OLD}/label", data="nope").status_code == 400

    def test_없는_촬영은_404(self, env):
        assert label(env, OLD, object_id="cube", label="good").status_code == 404


# ------------------------------------------------------------------ 삭제
class TestDelete:
    def test_라벨_없는_촬영은_지운다(self, env):
        make_capture(env, OLD)
        assert env.c.delete(f"/api/inspections/{OLD}").get_json() == {"ok": True}
        assert not (env.root / "inspections" / OLD).exists()

    def test_라벨된_촬영은_400이고_그대로_남는다(self, env):
        make_capture(env, OLD)
        label(env, OLD, object_id="cube", label="good")
        res = env.c.delete(f"/api/inspections/{OLD}")
        assert res.status_code == 400 and "라벨" in res.get_json()["error"]["message"]
        assert (env.root / "inspections" / OLD / "label.json").exists()

    def test_없는_촬영은_404(self, env):
        assert env.c.delete(f"/api/inspections/{OLD}").status_code == 404


# ------------------------------------------------------------------ 물체 · 준비도
@pytest.fixture
def populated(env):
    """기존 에피소드 3개(양품 cube 2, 불량 2911), 시연 에피소드 1개, 촬영 3개(라벨 2 + 없음 1), CAD 모델."""
    make_episode(env, "000001", "cube", "good")
    make_episode(env, "000002", "2911", "defect", defect_type="crack")
    make_episode(env, "000003", "cube", "good")
    make_episode(env, "000004", "widget", "good", kind="demo", task="옮긴다")    # label 은 자리표시
    for cid in (OLD, MID, NEW):
        make_capture(env, cid)
    label(env, OLD, object_id="cube", label="good")
    label(env, MID, object_id="bolt", label="defect", defect_type="dent")
    models = env.root / "models"
    models.mkdir()
    (models / "cube.stl").write_bytes(b"x")
    (models / "Gear.STEP").write_bytes(b"x")
    (models / "not valid.stl").write_bytes(b"x")
    (models / "readme.txt").write_bytes(b"x")
    return env


class TestObjectsAndReadiness:
    def test_준비도는_시연을_ACT_에만_세고_분류_물체는_기존_에피소드와_라벨된_촬영만_센다(self, populated):
        body = populated.c.get("/api/readiness").get_json()
        assert body == {
            "ok": True,
            "goals": {"act_episodes": 50, "objects_per_class": 50},
            "act_episodes": 4,          # 시연(kind=demo)도 ACT 에는 쓴다
            "good_objects": 1,          # cube — 에피소드 라벨과 촬영 라벨의 합집합(중복 제외). widget(시연)은 아니다
            "defect_objects": 2,        # 2911(에피소드) + bolt(촬영)
        }

    def test_검사_목록의_summary_는_준비도와_같은_물체_수를_쓴다(self, populated):
        summary = populated.c.get("/api/inspections").get_json()["summary"]
        assert summary == {"total": 3, "unlabeled": 1, "objects": {"good": 1, "defect": 2}}

    def test_물체_목록은_에피소드_촬영_CAD_어디든_나온_물체를_합친다(self, populated):
        body = populated.c.get("/api/objects").get_json()
        assert body["recommended_poses"] == 6
        by_id = {o["object_id"]: o for o in body["objects"]}
        assert list(by_id) == ["2911", "bolt", "cube", "Gear", "widget"]       # 대소문자 무시 정렬
        assert by_id["cube"] == {"object_id": "cube", "has_cad": True, "poses": 1, "labels": {"good": 1, "defect": 0}}
        assert by_id["bolt"]["poses"] == 1 and by_id["bolt"]["labels"] == {"good": 0, "defect": 1}
        assert by_id["Gear"]["has_cad"] is True and by_id["Gear"]["poses"] == 0     # CAD 만 있는 물체
        assert by_id["widget"]["has_cad"] is False                                    # 시연으로만 나온 물체
        assert "not valid" not in by_id                                               # 화면에서 못 쓰는 이름

    def test_데이터가_없으면_전부_0(self, env):
        assert env.c.get("/api/objects").get_json()["objects"] == []
        body = env.c.get("/api/readiness").get_json()
        assert (body["act_episodes"], body["good_objects"], body["defect_objects"]) == (0, 0, 0)
