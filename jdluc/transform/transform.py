"""Transform stage entry point.

Orchestrates four GEE export tasks per pipeline run, all scoped to the
region's union geometry: one `land_use` raster, one `emissions` raster, then
`transitions` + `crops` tables in parallel. No per-state subdivision; no
consolidation step. The unified raster export *is* the cross-state mosaic;
the unified `reduceRegions` *is* the cross-state table merge.

See specs/pipeline_tech_design.md § Transform for details.
"""

import dataclasses
import enum
import logging
from typing import Any

import ee

from jdluc.transform.emissions import (
    build_emissions_image,
    export_emissions_asset,
)
from jdluc.transform.land_use import (
    build_land_use_image,
    export_land_use_asset,
)
from jdluc.transform.summary_tables import compute_region_tables
from jdluc.utils.constants import (
    GEE_COUNTY_FIPS_LABEL,
    STATE_FIPS_TO_NAME,
    output_asset_id,
)
from jdluc.utils.gee import EXPORT_TIMEOUT_S, wait_for_tasks
from jdluc.utils.states import get_multi_state_boundary
from jdluc.utils.version import compute_transform_version

logger = logging.getLogger(__name__)

# Heartbeat interval for "still waiting" INFO logs while polling (seconds).
_HEARTBEAT_INTERVAL_S: int = 600


class TransformStage(enum.Enum):
    """Public-facing stage label for `TransformError.failed_stage`."""

    LAND_USE = 'LAND_USE'
    EMISSIONS = 'EMISSIONS'
    TABLES = 'TABLES'


class TransformError(Exception):
    """Raised when one of the four region-scope exports fails.

    Carries the failing stage name and the underlying error message so
    callers can inspect what went wrong without parsing the exception
    string.
    """

    def __init__(self, failed_stage: str, error_message: str) -> None:
        self.failed_stage = failed_stage
        self.error_message = error_message
        super().__init__(f'Transform failed at stage {failed_stage}: {error_message}')


@dataclasses.dataclass
class TransformResult:
    """Result of a transform-stage run, four region-scope asset IDs."""

    version: str
    region_name: str
    land_use_asset_id: str
    emissions_asset_id: str
    transitions_table_id: str
    crops_table_id: str
    from_cache: bool


def run_transform(
    gcp_project: str,
    states: list[str],
    region_name: str,
    force: bool = False,
) -> TransformResult:
    """Run the transform stage as four region-scope GEE exports.

    1. Resolves the region geometry (`get_multi_state_boundary(states)`)
       and the counties FeatureCollection (filtered to the region's
       STATEFP set, with `county_fips` set to STATEFP+COUNTYFP) once.
    2. Submits and awaits the `land_use` export.
    3. Submits and awaits the `emissions` export (reads the just-
       materialized land_use asset).
    4. Submits `transitions` and `crops` table exports in parallel; awaits
       both.

    Args:
        gcp_project: GCP project ID. Accepted for API parity with
          ``extract_all`` / ``run_publish``; the transform stage only
          uses the already-initialized Earth Engine session.
        states: List of state FIPS codes. Must be non-empty and all in
          STATE_FIPS_TO_NAME.
        region_name: Region label (e.g. `'delaware'`, `'great_plains_test'`,
          `'conus'`). Used in asset names: `{kind}_{region}_{version}`.
        force: If True, re-export even if the target asset already exists.

    Returns:
        TransformResult with version, region_name, the four asset IDs, and
        `from_cache` flag (True iff every export was a cache hit).

    Raises:
        ValueError: If `states` is empty or any FIPS is unknown.
        TransformError: If any of the four exports fails.
    """
    del gcp_project
    _validate_inputs(states, region_name)

    version = compute_transform_version()
    logger.info(f'Transform version: {version}')

    region_geometry = get_multi_state_boundary(states)
    region_bbox = region_geometry.bounds()
    fips_band = _load_fips_band_for_states(states)
    fips_mask = fips_band.mask()

    any_work = False

    # Stage 1: land_use
    land_use_asset_id = output_asset_id('land_use', region_name, version)
    try:
        land_use_image = build_land_use_image(region_geometry)
        task = export_land_use_asset(
            image=land_use_image,
            region=region_name,
            version=version,
            geometry=region_geometry,
            fips_mask=fips_mask,
            force=force,
        )
    except Exception as exc:
        raise TransformError(
            TransformStage.LAND_USE.value, f'{type(exc).__name__}: {exc}'
        ) from exc
    if task is not None:
        any_work = True
        try:
            _wait_for_export_task(
                task, TransformStage.LAND_USE.value, land_use_asset_id
            )
        except RuntimeError as exc:
            raise TransformError(TransformStage.LAND_USE.value, str(exc)) from exc

    # Stage 2: emissions (reads the materialized land_use asset)
    emissions_asset_id = output_asset_id('emissions', region_name, version)
    try:
        emissions_image = build_emissions_image(land_use_asset_id, region_geometry)
        task = export_emissions_asset(
            image=emissions_image,
            region=region_name,
            version=version,
            geometry=region_geometry,
            fips_mask=fips_mask,
            force=force,
        )
    except Exception as exc:
        raise TransformError(
            TransformStage.EMISSIONS.value, f'{type(exc).__name__}: {exc}'
        ) from exc
    if task is not None:
        any_work = True
        try:
            _wait_for_export_task(
                task, TransformStage.EMISSIONS.value, emissions_asset_id
            )
        except RuntimeError as exc:
            raise TransformError(TransformStage.EMISSIONS.value, str(exc)) from exc

    # Stage 3: transitions + crops in parallel
    try:
        tables_result = compute_region_tables(
            land_use_asset_id=land_use_asset_id,
            emissions_asset_id=emissions_asset_id,
            region=region_name,
            version=version,
            fips_band=fips_band,
            region_bbox=region_bbox,
            force=force,
        )
    except Exception as exc:
        raise TransformError(
            TransformStage.TABLES.value, f'{type(exc).__name__}: {exc}'
        ) from exc

    transitions_task = tables_result['transitions_task']
    crops_task = tables_result['crops_task']
    pending_table_tasks: dict[str, Any] = {}
    if transitions_task is not None:
        any_work = True
        pending_table_tasks['transitions'] = transitions_task
    if crops_task is not None:
        any_work = True
        pending_table_tasks['crops'] = crops_task
    if pending_table_tasks:
        try:
            _wait_for_table_tasks(
                tasks=pending_table_tasks,
                stage_label=TransformStage.TABLES.value,
            )
        except RuntimeError as exc:
            raise TransformError(TransformStage.TABLES.value, str(exc)) from exc

    return TransformResult(
        version=version,
        region_name=region_name,
        land_use_asset_id=land_use_asset_id,
        emissions_asset_id=emissions_asset_id,
        transitions_table_id=tables_result['transitions_asset_id'],
        crops_table_id=tables_result['crops_asset_id'],
        from_cache=not any_work,
    )


def _load_fips_band_for_states(state_fips_list: list[str]) -> Any:
    """Run-scoped FIPS-valued image: county FIPS in the run's states, masked elsewhere.

    Reads the canonical ``GEE_COUNTY_FIPS_LABEL`` asset (built by
    ``extract/county_fips.py``), divides by 1000 to recover the state-FIPS
    integer, and ``updateMask``-s to keep only pixels whose state-FIPS is
    in ``state_fips_list``. The returned image carries the FIPS values
    (1001..56045) on selected pixels and is masked elsewhere. Two
    consumers downstream:

    * Raster export sinks call ``image.mask()`` on the result to get a
      0/1 mask suitable for ``updateMask`` against the rasters.
    * Summary-table reducers consume the FIPS values directly as one
      half of the composite grouping key.

    The FIPS asset is painted at GLAD 30m using paint's pixel-center
    rule.
    """
    fips_label = ee.Image(GEE_COUNTY_FIPS_LABEL)
    state_codes = [int(s) for s in state_fips_list]
    state_band = fips_label.divide(1000).toInt()
    in_set = state_band.eq(state_codes[0])
    for code in state_codes[1:]:
        in_set = in_set.Or(state_band.eq(code))
    return fips_label.updateMask(in_set).rename('county_fips')


def _validate_inputs(states: list[str], region_name: str) -> None:
    """Reject bad input before submitting any GEE work."""
    if not states:
        raise ValueError('states must not be empty')
    if not region_name:
        raise ValueError('region_name must not be empty')
    unknown = [fips for fips in states if fips not in STATE_FIPS_TO_NAME]
    if unknown:
        raise ValueError(
            f'Unknown state FIPS codes (not in STATE_FIPS_TO_NAME): {unknown}'
        )


def _wait_for_export_task(task: Any, stage_label: str, asset_id: str) -> None:
    """Poll one GEE export task until COMPLETED, raising on FAILED.

    Thin wrapper over ``utils.gee.wait_for_tasks`` that turns the failure
    map into a ``RuntimeError`` (which the caller wraps as
    ``TransformError(stage, ...)``).
    """
    task_id = str(task.id)
    failed = wait_for_tasks(
        [task_id],
        asset_ids_for_logging={task_id: asset_id},
        task_labels={task_id: stage_label},
        heartbeat_interval_s=_HEARTBEAT_INTERVAL_S,
        timeout_s=EXPORT_TIMEOUT_S,
    )
    if task_id in failed:
        raise RuntimeError(f'{stage_label} export failed: {failed[task_id]}')


def _wait_for_table_tasks(tasks: dict[str, Any], stage_label: str) -> None:
    """Poll multiple GEE export tasks concurrently until all COMPLETED.

    Used for the TABLES stage where transitions and crops can run in
    parallel (different table assets, no inter-dependency). Raises
    RuntimeError if any task ends FAILED/CANCELLED, naming the first
    failed kind. Unlike the prior fail-fast loop, the canonical helper
    waits for all tasks to reach a terminal state before returning —
    the task itself keeps running on GEE either way, so the only
    difference is wall-clock-to-error.
    """
    task_id_to_kind = {str(t.id): kind for kind, t in tasks.items()}
    failed = wait_for_tasks(
        list(task_id_to_kind),
        task_labels={
            tid: f'{stage_label}({kind})' for tid, kind in task_id_to_kind.items()
        },
        heartbeat_interval_s=_HEARTBEAT_INTERVAL_S,
        timeout_s=EXPORT_TIMEOUT_S,
    )
    if failed:
        first_tid = next(iter(failed))
        kind = task_id_to_kind[first_tid]
        raise RuntimeError(f'{stage_label}({kind}) export failed: {failed[first_tid]}')


__all__ = [
    'TransformError',
    'TransformResult',
    'TransformStage',
    'run_transform',
]
