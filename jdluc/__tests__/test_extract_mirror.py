"""Unit tests for extract/mirror.py.

Mocks the google.cloud.storage client and the underlying
download_with_retries / upload_to_gcs helpers so these tests run fully
offline. Covers: mirror-hit short-circuits upstream; mirror-miss falls
through to upstream + writes the mirror; callable source is only
resolved on miss; errors propagate sanely.
"""

from unittest.mock import MagicMock, patch

import pytest

from jdluc.extract import mirror
from jdluc.utils.constants import GCS_BUCKET_NAME


def test_mirror_hit_short_circuits_upstream(tmp_path: object) -> None:
    """On mirror hit, upstream is never called and the blob is streamed down."""
    dst = str(tmp_path / 'payload.bin')  # type: ignore[operator]

    def resolver() -> str:
        raise AssertionError('resolver should not be called on mirror hit')

    with (
        patch.object(mirror, '_gcs_blob_exists', return_value=True) as exists_mock,
        patch.object(mirror, '_download_from_gcs') as download_mock,
        patch.object(mirror, 'download_with_retries') as upstream_mock,
        patch.object(mirror, 'upload_to_gcs') as upload_mock,
    ):
        mirror.fetch_with_mirror(
            dst,
            dataset='nass_yields',
            filename='qs.crops_20260423.txt.gz',
            gcp_project='gcp-project-name',
            source=resolver,
        )

    exists_mock.assert_called_once_with(
        'gcp-project-name',
        GCS_BUCKET_NAME,
        'luc_high_res/extract_mirror/nass_yields/qs.crops_20260423.txt.gz',
    )
    download_mock.assert_called_once_with(
        'gcp-project-name',
        GCS_BUCKET_NAME,
        'luc_high_res/extract_mirror/nass_yields/qs.crops_20260423.txt.gz',
        dst,
    )
    upstream_mock.assert_not_called()
    upload_mock.assert_not_called()


def test_mirror_miss_fetches_upstream_and_writes_mirror(tmp_path: object) -> None:
    """On mirror miss, upstream is called and the result uploads to GCS."""
    dst = str(tmp_path / 'payload.bin')  # type: ignore[operator]

    with (
        patch.object(mirror, '_gcs_blob_exists', return_value=False),
        patch.object(mirror, '_download_from_gcs') as gcs_download_mock,
        patch.object(mirror, 'download_with_retries') as upstream_mock,
        patch.object(mirror, 'upload_to_gcs') as upload_mock,
    ):
        mirror.fetch_with_mirror(
            dst,
            dataset='huang_bgb',
            filename='data_code_to_submit.zip',
            gcp_project='gcp-project-name',
            source='https://example/huang.zip',
            timeout_s=900.0,
        )

    gcs_download_mock.assert_not_called()
    upstream_mock.assert_called_once_with(
        'https://example/huang.zip',
        dst,
        timeout_s=900.0,
        retries=3,
        backoff_s=2.0,
    )
    upload_mock.assert_called_once_with(
        'gcp-project-name',
        GCS_BUCKET_NAME,
        'luc_high_res/extract_mirror/huang_bgb/data_code_to_submit.zip',
        dst,
    )


def test_mirror_miss_resolves_callable_source(tmp_path: object) -> None:
    """Deferred-resolution sources get invoked exactly once on miss."""
    dst = str(tmp_path / 'payload.bin')  # type: ignore[operator]

    resolver = MagicMock(return_value='https://zenodo/files/record.tif')

    with (
        patch.object(mirror, '_gcs_blob_exists', return_value=False),
        patch.object(mirror, 'download_with_retries'),
        patch.object(mirror, 'upload_to_gcs'),
    ):
        mirror.fetch_with_mirror(
            dst,
            dataset='ipcc_climate_zones',
            filename='ipcc_climate_zones_v2006.tif',
            gcp_project='gcp-project-name',
            source=resolver,
        )

    resolver.assert_called_once_with()


def test_mirror_hit_does_not_resolve_callable_source(tmp_path: object) -> None:
    """On hit, a callable source is never invoked (no Zenodo probe)."""
    dst = str(tmp_path / 'payload.bin')  # type: ignore[operator]

    resolver = MagicMock(return_value='irrelevant')

    with (
        patch.object(mirror, '_gcs_blob_exists', return_value=True),
        patch.object(mirror, '_download_from_gcs'),
    ):
        mirror.fetch_with_mirror(
            dst,
            dataset='ipcc_climate_zones',
            filename='ipcc_climate_zones_v2006.tif',
            gcp_project='gcp-project-name',
            source=resolver,
        )

    resolver.assert_not_called()


def test_mirror_miss_upstream_failure_propagates(tmp_path: object) -> None:
    """If upstream download raises on mirror miss, the error bubbles up."""
    dst = str(tmp_path / 'payload.bin')  # type: ignore[operator]

    with (
        patch.object(mirror, '_gcs_blob_exists', return_value=False),
        patch.object(
            mirror,
            'download_with_retries',
            side_effect=RuntimeError('upstream 404'),
        ),
        patch.object(mirror, 'upload_to_gcs') as upload_mock,
    ):
        with pytest.raises(RuntimeError, match='upstream 404'):
            mirror.fetch_with_mirror(
                dst,
                dataset='nass_yields',
                filename='qs.crops_missing.txt.gz',
                gcp_project='gcp-project-name',
                source='https://nass/missing',
            )

    # Upload must NOT run if the upstream download failed.
    upload_mock.assert_not_called()


def test_mirror_retry_kwargs_pass_through(tmp_path: object) -> None:
    """timeout_s / retries / backoff_s forward to download_with_retries."""
    dst = str(tmp_path / 'payload.bin')  # type: ignore[operator]

    with (
        patch.object(mirror, '_gcs_blob_exists', return_value=False),
        patch.object(mirror, 'download_with_retries') as upstream_mock,
        patch.object(mirror, 'upload_to_gcs'),
    ):
        mirror.fetch_with_mirror(
            dst,
            dataset='harris_agb',
            filename='40N_080W.tif',
            gcp_project='gcp-project-name',
            source='https://gfw/tile',
            timeout_s=300.0,
            retries=5,
            backoff_s=1.5,
        )

    kwargs = upstream_mock.call_args.kwargs
    assert kwargs['timeout_s'] == 300.0
    assert kwargs['retries'] == 5
    assert kwargs['backoff_s'] == 1.5
