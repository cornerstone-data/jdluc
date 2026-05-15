"""Unit tests for stage-scoped pipeline versioning (no GEE dependency)."""

import os
import re
import subprocess

from jdluc.utils.version import (
    compute_publish_version,
    compute_transform_version,
)


def _head_sha() -> str:
    return subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()


def _repo_root() -> str:
    return subprocess.check_output(
        ['git', 'rev-parse', '--show-toplevel'], text=True
    ).strip()


# ---------- format + determinism --------------------------------------------


def test_transform_version_format_clean() -> None:
    """Transform version starts with HEAD[:12] and matches the expected regex.

    The working tree may be dirty with respect to ``transform/`` + ``utils/``
    (e.g. these very tests are untracked), in which case the ``-dirty-...``
    suffix is correct.
    """
    version = compute_transform_version()
    assert version.startswith(_head_sha()[:12])
    assert re.fullmatch(r'[0-9a-f]{12}(-dirty-[0-9a-f]{8})?', version)


def test_publish_version_format_clean() -> None:
    version = compute_publish_version()
    assert version.startswith(_head_sha()[:12])
    assert re.fullmatch(r'[0-9a-f]{12}(-dirty-[0-9a-f]{8})?', version)


def test_transform_version_deterministic() -> None:
    assert compute_transform_version() == compute_transform_version()


def test_publish_version_deterministic() -> None:
    assert compute_publish_version() == compute_publish_version()


# ---------- stage scoping ---------------------------------------------------


def test_transform_version_ignores_publish_files() -> None:
    """An untracked file under ``publish/`` does not change the transform SHA."""
    tmp_path = os.path.join(_repo_root(), 'jdluc', 'publish', '_tmp_version_probe')
    before = compute_transform_version()
    try:
        with open(tmp_path, 'w') as f:
            f.write('probe\n')
        after = compute_transform_version()
        assert before == after
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_publish_version_ignores_transform_files() -> None:
    """An untracked file under ``transform/`` does not change the publish SHA."""
    tmp_path = os.path.join(_repo_root(), 'jdluc', 'transform', '_tmp_version_probe')
    before = compute_publish_version()
    try:
        with open(tmp_path, 'w') as f:
            f.write('probe\n')
        after = compute_publish_version()
        assert before == after
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def test_utils_change_affects_both_versions() -> None:
    """Touching ``utils/`` changes both transform and publish SHAs."""
    tmp_path = os.path.join(_repo_root(), 'jdluc', 'utils', '_tmp_version_probe')
    t_before = compute_transform_version()
    p_before = compute_publish_version()
    try:
        with open(tmp_path, 'w') as f:
            f.write('probe\n')
        t_after = compute_transform_version()
        p_after = compute_publish_version()
        assert t_before != t_after
        assert p_before != p_after
        assert '-dirty-' in t_after
        assert '-dirty-' in p_after
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
