"""Unit tests for extract/county_fips.py.

Mocks GEE so the tests run offline. Covers the (a) cache-hit /
force-true delete, (b) server-side paint recipe shape (FC filter,
fips_int property, paint property name), and (c) export call's GLAD
pinning + region-as-bounds invariants.
"""

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jdluc.extract import county_fips
from jdluc.utils.constants import (
    CONUS_STATE_FIPS,
    GEE_COUNTY_FIPS_LABEL,
    GLAD_CRS,
    GLAD_CRS_TRANSFORM,
)


@pytest.fixture
def mock_io() -> Any:
    """Patch the GEE-side IO helpers used by the extract module."""
    with (
        patch.object(county_fips, 'asset_exists') as asset_exists_mock,
        patch.object(county_fips, 'delete_asset_if_present') as delete_mock,
        patch.object(county_fips, 'wait_for_export_task') as wait_mock,
    ):
        ns = MagicMock()
        ns.asset_exists = asset_exists_mock
        ns.delete_asset = delete_mock
        ns.wait_export = wait_mock
        # By default the extract function is invoked on a cache miss
        # (orchestrator already gated). Asset is present after export.
        asset_exists_mock.return_value = True
        yield ns


def test_extract_runs_paint_then_export_then_wait(mock_io: Any) -> None:
    """Happy path: builds FC, builds image, starts export, waits, returns id."""
    with patch.object(county_fips, '_start_fips_export') as start_export:
        export_task = MagicMock()
        start_export.return_value = export_task
        with (
            patch.object(county_fips, '_build_conus_counties_fc') as build_fc,
            patch.object(county_fips, '_build_fips_image') as build_image,
        ):
            fc = MagicMock(name='counties_fc')
            geom = MagicMock(name='geometry')
            bbox = MagicMock(name='bbox')
            fc.geometry.return_value = geom
            geom.bounds.return_value = bbox
            build_fc.return_value = fc
            build_image.return_value = 'fips_image_sentinel'

            result = county_fips.extract_county_fips(gcp_project='ws-dev')

    assert result == GEE_COUNTY_FIPS_LABEL
    # Force-false: no pre-delete on the target asset.
    mock_io.delete_asset.assert_not_called()
    # Image is built from the fc the helper produced.
    build_image.assert_called_once_with(fc)
    # Export call uses the painted image, the inventory asset id, and
    # the FC's bounding rectangle.
    start_export.assert_called_once()
    kwargs = start_export.call_args.kwargs
    assert kwargs['image'] == 'fips_image_sentinel'
    assert kwargs['asset_id'] == GEE_COUNTY_FIPS_LABEL
    assert kwargs['region'] is bbox
    assert 'description' in kwargs
    # Caller waits on the export.
    mock_io.wait_export.assert_called_once_with(export_task, GEE_COUNTY_FIPS_LABEL)


def test_force_true_deletes_existing_asset_first(mock_io: Any) -> None:
    """force=True clears the target before the new export runs."""
    with (
        patch.object(county_fips, '_start_fips_export') as start_export,
        patch.object(county_fips, '_build_conus_counties_fc'),
        patch.object(county_fips, '_build_fips_image'),
    ):
        start_export.return_value = MagicMock()
        county_fips.extract_county_fips(gcp_project='ws-dev', force=True)

    mock_io.delete_asset.assert_called_once_with(GEE_COUNTY_FIPS_LABEL)


def test_extract_raises_if_post_export_asset_missing(mock_io: Any) -> None:
    """Defensive: if the export 'completes' but no asset lands, fail loudly."""
    mock_io.asset_exists.return_value = False
    with (
        patch.object(county_fips, '_start_fips_export', return_value=MagicMock()),
        patch.object(county_fips, '_build_conus_counties_fc'),
        patch.object(county_fips, '_build_fips_image'),
    ):
        with pytest.raises(RuntimeError, match='asset is not present'):
            county_fips.extract_county_fips(gcp_project='ws-dev')


def test_build_conus_counties_fc_filters_and_maps() -> None:
    """The server-side FC builder filters STATEFP and applies the tag mapper."""
    with patch('jdluc.extract.county_fips.ee') as ee_mock:
        fc_root = MagicMock(name='fc_root')
        filtered = MagicMock(name='filtered')
        mapped = MagicMock(name='mapped')
        ee_mock.FeatureCollection.return_value = fc_root
        fc_root.filter.return_value = filtered
        filtered.map.return_value = mapped
        ee_mock.Filter.inList.return_value = 'inList_filter_sentinel'

        result = county_fips._build_conus_counties_fc()

    assert result is mapped
    ee_mock.FeatureCollection.assert_called_once_with('TIGER/2018/Counties')
    ee_mock.Filter.inList.assert_called_once_with('STATEFP', CONUS_STATE_FIPS)
    fc_root.filter.assert_called_once_with('inList_filter_sentinel')
    # The mapper passed to .map() is the tag-with-fips-int helper.
    filtered.map.assert_called_once_with(county_fips._tag_with_fips_int)


def test_tag_with_fips_int_parses_geoid_to_int_and_sets_property() -> None:
    """Per-feature tagger: GEOID string → ee.Number.parse → toInt → set."""
    with patch('jdluc.extract.county_fips.ee') as ee_mock:
        feature = MagicMock(name='feature')
        feature.get.return_value = 'geoid_value'
        # Each step in the chain returns a distinct sentinel so we can
        # verify the call sequence.
        ee_mock.String.return_value = 'string_sentinel'
        parsed = MagicMock(name='parsed_number')
        ee_mock.Number.parse.return_value = parsed
        parsed.toInt.return_value = 'int_sentinel'
        feature.set.return_value = 'feature_with_fips_int'

        result = county_fips._tag_with_fips_int(feature)

    assert result == 'feature_with_fips_int'
    feature.get.assert_called_once_with('GEOID')
    ee_mock.String.assert_called_once_with('geoid_value')
    ee_mock.Number.parse.assert_called_once_with('string_sentinel')
    parsed.toInt.assert_called_once_with()
    feature.set.assert_called_once_with('fips_int', 'int_sentinel')


def test_build_fips_image_paints_fips_int_and_self_masks() -> None:
    """The image builder paints the fips_int property into a constant=0 int image."""
    with patch('jdluc.extract.county_fips.ee') as ee_mock:
        constant = MagicMock(name='constant')
        as_int = MagicMock(name='int')
        painted = MagicMock(name='painted')
        masked = MagicMock(name='masked')
        renamed = MagicMock(name='renamed')
        ee_mock.Image.constant.return_value = constant
        constant.int.return_value = as_int
        as_int.paint.return_value = painted
        painted.selfMask.return_value = masked
        masked.rename.return_value = renamed

        fc_sentinel = 'fc_sentinel'
        result = county_fips._build_fips_image(fc_sentinel)

    assert result is renamed
    ee_mock.Image.constant.assert_called_once_with(0)
    constant.int.assert_called_once_with()
    as_int.paint.assert_called_once_with(fc_sentinel, 'fips_int')
    painted.selfMask.assert_called_once_with()
    masked.rename.assert_called_once_with('county_fips')
    # No inline .reproject() — the projection is pinned at the export
    # sink instead, since inline reproject re-evaluates the polygon
    # paint per tile (investigation doc § "Bisect probe: inline paint
    # doesn't amortize, asset-loaded mask does").
    masked.reproject.assert_not_called()


def test_start_fips_export_pins_glad_grid_and_uses_supplied_region() -> None:
    """Export targets the GLAD-pinned grid; region passes through unchanged."""
    with patch('jdluc.extract.county_fips.ee') as ee_mock:
        export_task = MagicMock()
        ee_mock.batch.Export.image.toAsset.return_value = export_task

        result = county_fips._start_fips_export(
            image='img',
            asset_id='target_asset',
            region='region_sentinel',
            description='extract_county_fips_smoke',
        )

    assert result is export_task
    kwargs = ee_mock.batch.Export.image.toAsset.call_args.kwargs
    assert kwargs['image'] == 'img'
    assert kwargs['assetId'] == 'target_asset'
    assert kwargs['region'] == 'region_sentinel'
    assert kwargs['crs'] == GLAD_CRS
    assert kwargs['crsTransform'] == GLAD_CRS_TRANSFORM
    assert kwargs['description'] == 'extract_county_fips_smoke'
    export_task.start.assert_called_once()
