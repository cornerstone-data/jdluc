"""Unit tests for extract/extract.py orchestrator.

Covers the cache-hit / cache-miss / failure-capture paths using the
injectable ``extractor_factory`` and ``asset_exists`` hooks — no GEE,
no HTTP, no per-dataset modules required.
"""

from collections.abc import Callable

import pytest

from jdluc.extract.extract import (
    ExtractError,
    ExtractorCallable,
    ExtractResult,
    extract_all,
)
from jdluc.utils.constants import (
    DATASET_INVENTORY,
    NON_NATIVE_DATASETS,
)


def _build_factory(
    produced: dict[str, str] | None = None,
    raising: dict[str, Exception] | None = None,
    calls: list[tuple[str, str, bool]] | None = None,
) -> Callable[[str], ExtractorCallable]:
    """Build a fake ``extractor_factory`` for a given dispatch plan.

    - ``produced[family]`` → the asset ID the extractor should return.
    - ``raising[family]`` → an exception to raise instead.
    - ``calls`` — if provided, each invocation appends ``(family, gcp, force)``.
    """
    produced = produced or {}
    raising = raising or {}

    def factory(family: str) -> ExtractorCallable:
        def extractor(gcp_project: str, force: bool = False) -> str:
            if calls is not None:
                calls.append((family, gcp_project, force))
            if family in raising:
                raise raising[family]
            return produced[family]

        return extractor

    return factory


def _all_present(_: str) -> bool:
    return True


def _none_present(_: str) -> bool:
    return False


def test_every_asset_cached_skips_all_extractors() -> None:
    calls: list[tuple[str, str, bool]] = []
    factory = _build_factory(calls=calls)

    result = extract_all(
        gcp_project='ws-dev',
        extractor_factory=factory,
        asset_exists=_all_present,
    )

    assert result.cached == NON_NATIVE_DATASETS
    assert result.extracted == []
    assert result.failed == {}
    assert calls == []


def test_missing_assets_trigger_extractor_dispatch() -> None:
    produced = {
        family: DATASET_INVENTORY[family]['gee_asset_id']
        for family in NON_NATIVE_DATASETS
    }
    calls: list[tuple[str, str, bool]] = []
    factory = _build_factory(produced=produced, calls=calls)

    result = extract_all(
        gcp_project='ws-dev',
        extractor_factory=factory,
        asset_exists=_none_present,
    )

    assert result.cached == []
    assert result.extracted == NON_NATIVE_DATASETS
    assert result.failed == {}
    # Order of `calls` is non-deterministic under the parallel orchestrator;
    # only assert membership and per-call args.
    assert sorted(c[0] for c in calls) == sorted(NON_NATIVE_DATASETS)
    # force propagates as False by default.
    assert all(c[2] is False for c in calls)


def test_force_true_bypasses_cache_for_all_datasets() -> None:
    produced = {
        family: DATASET_INVENTORY[family]['gee_asset_id']
        for family in NON_NATIVE_DATASETS
    }
    calls: list[tuple[str, str, bool]] = []
    factory = _build_factory(produced=produced, calls=calls)

    result = extract_all(
        gcp_project='ws-dev',
        force=True,
        extractor_factory=factory,
        asset_exists=_all_present,  # even if cached, force=True still re-runs
    )

    assert result.cached == []
    assert result.extracted == NON_NATIVE_DATASETS
    assert all(c[2] is True for c in calls)


def test_failed_extractor_is_recorded_and_other_datasets_continue() -> None:
    produced = {
        family: DATASET_INVENTORY[family]['gee_asset_id']
        for family in NON_NATIVE_DATASETS
        if family != 'huang_bgb'
    }
    raising: dict[str, Exception] = {'huang_bgb': RuntimeError('Figshare 503')}
    factory = _build_factory(produced=produced, raising=raising)

    result = extract_all(
        gcp_project='ws-dev',
        extractor_factory=factory,
        asset_exists=_none_present,
    )

    assert 'huang_bgb' in result.failed
    assert 'Figshare 503' in result.failed['huang_bgb']
    expected_extracted = [f for f in NON_NATIVE_DATASETS if f != 'huang_bgb']
    assert result.extracted == expected_extracted
    assert result.cached == []


def test_extract_error_summarizes_failures() -> None:
    partial = ExtractResult(
        cached=['ipcc_climate_zones'],
        extracted=['huang_bgb'],
        failed={'nass_yields': 'HTTP 500', 'harris_agb': 'GCS permission denied'},
    )
    err = ExtractError(partial)
    msg = str(err)
    assert '2 dataset' in msg
    assert 'nass_yields' in msg
    assert 'harris_agb' in msg
    assert err.result is partial


def test_extractor_returning_wrong_asset_id_still_counts_as_extracted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Extractor returns a path that doesn't match DATASET_INVENTORY — the
    # orchestrator warns but doesn't fail. Keeps the contract liberal for
    # now; a stricter check would be a separate enforcement step.
    produced = {
        family: DATASET_INVENTORY[family]['gee_asset_id']
        for family in NON_NATIVE_DATASETS
    }
    produced['ipcc_climate_zones'] = 'gee/asset/path'
    factory = _build_factory(produced=produced)

    with caplog.at_level('WARNING', logger='jdluc.extract.extract'):
        result = extract_all(
            gcp_project='ws-dev',
            extractor_factory=factory,
            asset_exists=_none_present,
        )

    assert 'ipcc_climate_zones' in result.extracted
    assert any('inventory expects' in r.message for r in caplog.records)


def test_extractors_run_in_parallel() -> None:
    """Cold-cache wall-clock should be closer to max(per-extractor) than sum.

    Each fake extractor sleeps 0.1s. Sequential execution would take
    ~0.6s for 6 extractors; the parallel orchestrator should finish in
    well under 0.3s (5x headroom over the per-extractor sleep).
    """
    import time

    produced = {
        family: DATASET_INVENTORY[family]['gee_asset_id']
        for family in NON_NATIVE_DATASETS
    }

    def factory(family: str) -> ExtractorCallable:
        def extractor(gcp_project: str, force: bool = False) -> str:
            time.sleep(0.1)
            return produced[family]

        return extractor

    start = time.monotonic()
    result = extract_all(
        gcp_project='ws-dev',
        extractor_factory=factory,
        asset_exists=_none_present,
    )
    elapsed = time.monotonic() - start

    assert result.extracted == NON_NATIVE_DATASETS
    assert result.failed == {}
    # Sequential floor is 0.6s; parallel ceiling is closer to 0.1s. Use
    # 0.3s as a generous bound that still proves concurrency.
    assert elapsed < 0.3, f'extract_all took {elapsed:.3f}s; expected parallel run'
