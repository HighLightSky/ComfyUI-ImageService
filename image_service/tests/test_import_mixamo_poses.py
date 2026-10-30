import numpy as np
import pytest

from image_service.tools.import_mixamo_poses import JOINTS, pose_features, select_frames


def test_selection_removes_duplicate_frames_and_keeps_different_positions():
    features = np.zeros((4, len(JOINTS), 3))
    features[1, :, 0] = 0.01
    features[2:, :, 0] = 0.3
    selected = select_frames(features, np.empty((0, len(JOINTS), 3)), 4, 0.055)
    assert len(selected) == 2
    assert any(index < 2 for index in selected)
    assert any(index >= 2 for index in selected)


def test_selection_deduplicates_against_other_clips():
    accepted = np.zeros((1, len(JOINTS), 3))
    features = np.zeros((3, len(JOINTS), 3))
    features[1, :, 1] = 0.1
    features[2, :, 1] = 0.4
    assert select_frames(features, accepted, 1, 0.055) == [2]
    assert select_frames(features[:1], accepted, 4, 0.055) == []


def test_features_accept_rest_pose_but_reject_nonfinite_rotations():
    frame = {'joints': {name: [0, 0, 0] for name in JOINTS}, 'pose': {'bones': {}}}
    assert pose_features([frame]).shape == (1, len(JOINTS), 3)
    frame['pose']['bones'] = {'head': [0, float('nan'), 0]}
    with pytest.raises(ValueError, match='骨骼旋转'):
        pose_features([frame])


def test_features_reject_nonfinite_joint_positions():
    frame = {'joints': {name: [0, 0, 0] for name in JOINTS}, 'pose': {'bones': {}}}
    frame['joints']['head'][1] = float('inf')
    with pytest.raises(ValueError, match='关节坐标'):
        pose_features([frame])
