"""Pipeline versioning from git state.

Exposes compute_transform_version() and compute_publish_version(),
Each version string is either {HEAD SHA[:12]} (working tree matches HEAD
for the hashed files) or {HEAD SHA[:12]}-dirty-{sha256(diff)[:8]}
(working tree differs).

See specs/pipeline_tech_design.md § version.py for details.
"""

import hashlib
import subprocess

# Path prefixes (relative to repo root) whose git state feeds each stage's
# version. Both stages include utils/ because any utility change can
# affect either stage's output.
TRANSFORM_PATHS = ['jdluc/transform/', 'jdluc/utils/']
PUBLISH_PATHS = ['jdluc/publish/', 'jdluc/utils/']


def _compute_version_for_paths(paths: list[str]) -> str:
    """Compute a version string derived from git state of the given paths.

    Returns the first 12 hex chars of HEAD, optionally suffixed with
    -dirty-{sha256(diff)[:8]} when the working tree has staged, unstaged,
    or untracked changes under any of paths.
    """
    repo_root = subprocess.check_output(
        ['git', 'rev-parse', '--show-toplevel'], text=True
    ).strip()
    head_sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()

    # Staged + unstaged changes limited to the requested path prefixes.
    tracked_diff = subprocess.check_output(
        ['git', 'diff', 'HEAD', '--'] + paths,
        cwd=repo_root,
        text=True,
    )

    # Untracked files under any of the requested path prefixes.
    untracked_files = subprocess.check_output(
        ['git', 'ls-files', '--others', '--exclude-standard', '--'] + paths,
        cwd=repo_root,
        text=True,
    ).strip()

    untracked_content = ''
    if untracked_files:
        for fpath in untracked_files.splitlines():
            try:
                with open(f'{repo_root}/{fpath}') as f:
                    untracked_content += f'--- untracked: {fpath}\n{f.read()}\n'
            except OSError:
                pass

    combined_diff = tracked_diff + untracked_content

    if not combined_diff.strip():
        return head_sha[:12]

    diff_hash = hashlib.sha256(combined_diff.encode()).hexdigest()[:8]
    return f'{head_sha[:12]}-dirty-{diff_hash}'


def compute_transform_version() -> str:
    """Version string for transform-stage assets: hashes transform/ + utils/."""
    return _compute_version_for_paths(TRANSFORM_PATHS)


def compute_publish_version() -> str:
    """Version string for publish-stage assets: hashes publish/ + utils/."""
    return _compute_version_for_paths(PUBLISH_PATHS)
