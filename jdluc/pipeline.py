"""Pipeline orchestration for jdLUC land use change emissions.

Provides ``run_pipeline()`` which drives the Extract -> Transform -> Publish
stages in sequence, returning a ``PipelineResult`` with per-stage asset IDs.

See specs/pipeline_tech_design.md § pipeline.py for details.
"""

import dataclasses
import logging

from jdluc.extract.extract import (
    ExtractError,
    ExtractResult,
    extract_all,
)
from jdluc.publish.publish import PublishResult, run_publish, target_entries
from jdluc.transform.transform import run_transform
from jdluc.utils.gee import initialize_gee

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class PipelineResult:
    """Result of a full pipeline run."""

    version: str
    region_name: str
    states: list[str]
    extract_result: ExtractResult | None = None
    land_use_asset_id: str | None = None
    emissions_asset_id: str | None = None
    transitions_table_id: str | None = None
    crops_table_id: str | None = None
    publish_result: PublishResult | None = None
    from_cache: bool = False


def run_pipeline(
    gcp_project: str,
    states: list[str],
    region_name: str,
    force: bool = False,
) -> PipelineResult:
    """Run the full jdLUC pipeline: extract -> transform -> publish.

    Args:
        gcp_project: GCP project ID for Earth Engine.
        states: List of state FIPS codes.
        region_name: Region label for asset naming (e.g. 'delaware').
        force: If True, re-export even if cached assets exist.

    Returns:
        PipelineResult with version, region, asset IDs, and per-stage
        results. ``from_cache`` is True iff every stage was a cache hit
        (transform `from_cache` AND every BQ target's `from_cache`).

    Raises:
        ExtractError: If any dataset in ``extract_all`` failed — transform
            is not attempted so a broken upstream dataset doesn't produce
            silently-wrong downstream outputs.
        PublishError: If any publish target failed.
    """
    logger.info(f'Initializing Earth Engine with project: {gcp_project}')
    initialize_gee(gcp_project)

    # Extract
    extract_result = extract_all(gcp_project=gcp_project, force=force)
    if extract_result.failed:
        raise ExtractError(extract_result)

    # Transform
    transform_result = run_transform(
        gcp_project=gcp_project,
        states=states,
        region_name=region_name,
        force=force,
    )

    # Publish — only valid when transform produced both tables (single-state
    # and multi-state both populate transitions_table_id + crops_table_id;
    # cached-only re-runs do too).
    if (
        transform_result.transitions_table_id is None
        or transform_result.crops_table_id is None
        or transform_result.land_use_asset_id is None
        or transform_result.emissions_asset_id is None
    ):
        raise RuntimeError(
            'Transform completed without all required asset IDs '
            '(transitions_table_id, crops_table_id, land_use_asset_id, '
            'emissions_asset_id) — publish stage cannot proceed.'
        )

    publish_result = run_publish(
        region_name=region_name,
        transitions_asset_id=transform_result.transitions_table_id,
        crops_asset_id=transform_result.crops_table_id,
        land_use_asset_id=transform_result.land_use_asset_id,
        emissions_asset_id=transform_result.emissions_asset_id,
        transform_version=transform_result.version,
        force=force,
    )

    publish_from_cache = all(
        target.from_cache for _, target in target_entries(publish_result)
    )

    return PipelineResult(
        version=transform_result.version,
        region_name=region_name,
        states=states,
        extract_result=extract_result,
        land_use_asset_id=transform_result.land_use_asset_id,
        emissions_asset_id=transform_result.emissions_asset_id,
        transitions_table_id=transform_result.transitions_table_id,
        crops_table_id=transform_result.crops_table_id,
        publish_result=publish_result,
        from_cache=transform_result.from_cache and publish_from_cache,
    )
