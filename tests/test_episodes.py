"""에피소드 사후 편집 + LeRobot 변환 매핑 테스트.

여기서 깨지면 안 되는 두 가지:
1. 라벨을 고쳤는데 분류 이미지가 옛 폴더에 남는 것 (CNN 학습셋 조용한 오염)
2. leader/follower 가 뒤바뀐 채 변환되는 것 (ACT 가 자기 자세를 예측하게 됨)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from backend.core.storage import DatasetIndex, imwrite, validate_label  # noqa: E402
from backend.utils.errors import ValidationError  # noqa: E402
from to_lerobot import cameras_in, iter_frames  # noqa: E402


def make_episode(root: Path, ep_id: str, object_id: str, label: str, defect_type=None, frames=2):
    ep = root / "episodes" / f"episode_{ep_id}"
    (ep / "camera_front").mkdir(parents=True)
    for i in range(frames):
        imwrite(ep / "camera_front" / f"frame_{i:06d}.jpg", np.zeros((8, 8, 3), np.uint8), 90)

    pd.DataFrame(
        [
            {
                "frame_index": i,
                "timestamp": i / 30,
                **{f"leader_joint_{j}": 10.0 + j for j in range(5)},
                **{f"follower_joint_{j}": 90.0 + j for j in range(5)},
            }
            for i in range(frames)
        ]
    ).to_parquet(ep / "joint_states.parquet", index=False)

    (ep / "metadata.json").write_text(
        json.dumps(
            {
                "episode_id": ep_id, "object_id": object_id, "label": label,
                "defect_type": defect_type, "fps": 30, "cameras": ["front"],
                "joint_names": [f"joint_{i}" for i in range(5)], "num_frames": frames,
            }
        ),
        encoding="utf-8",
    )
    still = root / "classification" / label / object_id
    still.mkdir(parents=True, exist_ok=True)
    (still / "front.jpg").write_bytes(b"x")
    return ep


class TestValidateLabel:
    def test_good_이면_defect_type_을_지운다(self):
        assert validate_label("good", "scratch") == ("good", None)

    def test_defect_인데_유형이_없으면_거부(self):
        with pytest.raises(ValidationError):
            validate_label("defect", None)

    def test_알_수_없는_라벨은_거부(self):
        with pytest.raises(ValidationError):
            validate_label("maybe", None)


class TestEpisodeEdit:
    def test_라벨을_고치면_분류_이미지도_따라_옮겨진다(self, tmp_path):
        make_episode(tmp_path, "000001", "bracket_a", "good")
        index = DatasetIndex(tmp_path)

        meta = index.update("000001", label="defect", defect_type="dent")

        assert meta["label"] == "defect" and meta["defect_type"] == "dent"
        assert not (tmp_path / "classification/good/bracket_a").exists()
        assert (tmp_path / "classification/defect/bracket_a/front.jpg").exists()

    def test_object_id_를_바꾸면_폴더도_이름이_바뀐다(self, tmp_path):
        make_episode(tmp_path, "000001", "bracket_a", "good")
        DatasetIndex(tmp_path).update("000001", object_id="bracket_b")
        assert (tmp_path / "classification/good/bracket_b/front.jpg").exists()

    def test_대상_폴더가_이미_있으면_덮어쓰지_않고_거부(self, tmp_path):
        make_episode(tmp_path, "000001", "bracket_a", "good")
        make_episode(tmp_path, "000002", "bracket_b", "good")
        with pytest.raises(ValidationError):
            DatasetIndex(tmp_path).update("000001", object_id="bracket_b")

    def test_없는_에피소드는_404_대신_검증오류(self, tmp_path):
        (tmp_path / "episodes").mkdir()
        with pytest.raises(ValidationError):
            DatasetIndex(tmp_path).update("999999", label="good")

    def test_경로_조작_id_는_거부된다(self, tmp_path):
        (tmp_path / "episodes").mkdir()
        with pytest.raises(ValidationError):
            DatasetIndex(tmp_path).delete("../../etc")

    def test_마지막_에피소드를_지우면_분류_이미지도_함께_지운다(self, tmp_path):
        # 남겨두면 3D 검사 드롭다운에 유령 물체로 계속 뜬다.
        make_episode(tmp_path, "000001", "bracket_a", "good")
        index = DatasetIndex(tmp_path)
        index.delete("000001")
        assert index.list() == []
        assert not (tmp_path / "classification/good/bracket_a").exists()

    def test_형제_에피소드가_남아있으면_분류_이미지는_보존한다(self, tmp_path):
        # 분류 이미지는 같은 물체의 에피소드들이 공유하는 자산이다.
        make_episode(tmp_path, "000001", "bracket_a", "good")
        make_episode(tmp_path, "000002", "bracket_a", "good", frames=1)
        index = DatasetIndex(tmp_path)
        index.delete("000001")
        assert (tmp_path / "classification/good/bracket_a/front.jpg").exists()


class TestLeRobotMapping:
    def test_action_은_리더_state_는_팔로워(self, tmp_path):
        ep = make_episode(tmp_path, "000001", "bracket_a", "good", frames=2)
        frames = list(iter_frames(ep, cameras_in(ep), 5))

        assert len(frames) == 2
        frame, _ = frames[0]
        assert frame["action"][0] == pytest.approx(10.0)            # leader = 사람이 시킨 값
        assert frame["observation.state"][0] == pytest.approx(90.0)  # follower = 현재 자세
        assert frame["observation.images.front"].shape == (8, 8, 3)

    def test_이미지가_빠진_타임스텝은_통째로_버린다(self, tmp_path):
        ep = make_episode(tmp_path, "000001", "bracket_a", "good", frames=3)
        (ep / "camera_front" / "frame_000001.jpg").unlink()
        assert len(list(iter_frames(ep, ["front"], 5))) == 2
