"""Shared tile-collection orchestrator for tile-based extractors.

`harris_agb.py` and `gfw_peatlands.py` both ingest a 10°×10° grid of
GeoTIFF tiles from an HTTP source into a GEE ImageCollection. The two
modules used to share ~85% of their structure verbatim — Phase A
concurrent staging via ``ThreadPoolExecutor``, Phase B sequential
ingestion submission, Phase C bulk poll, Phase D finally-block cleanup.
This module owns the orchestrator; per-dataset modules supply the
varying bits (asset paths, band name, URL resolver) and a one-line call.

The orchestrator's failure model is unchanged from the prior copies:

- A failed Phase A staging future is logged and the tile is dropped from
  Phase B (sibling tiles still go).
- A Phase B submit exception lands the staged blob in ``orphan_blobs``
  so Phase D still cleans GCS.
- A FAILED Phase C task is reported in the warning log; the asset ID is
  returned regardless. Re-run with ``force=True`` to retry.
"""

import logging
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed

from jdluc.extract.mirror import fetch_with_mirror
from jdluc.utils.gee import (
    asset_exists,
    create_image_collection_if_absent,
    delete_asset_if_present,
    delete_gcs_blob,
    start_ingestion_no_wait,
    upload_to_gcs,
    wait_for_tasks,
)

logger = logging.getLogger(__name__)


def _tile_image_asset_id(asset_root: str, tile_id: str) -> str:
    return f'{asset_root}/tile_{tile_id}'


def _stage_tile(
    *,
    tile_id: str,
    work_dir: str,
    gcp_project: str,
    force: bool,
    mirror_dataset: str,
    gcs_bucket: str,
    gcs_staging_prefix: str,
    asset_root: str,
    resolve_tile_url: Callable[[str], str],
) -> tuple[str, str, str, str]:
    """Phase A worker: download via mirror + upload to GCS.

    Returns ``(tile_id, gcs_uri, local_path, blob_name)``. ``force=True``
    additionally pre-deletes the per-tile asset so the subsequent ingest
    starts from a clean slate (paired with ``allow_overwrite=True`` on
    submission for resilience against eventual-consistency reads).
    """
    local_path = os.path.join(work_dir, f'{tile_id}.tif')

    # Mirror is keyed on tile_id (stable), so rotated source URLs never
    # invalidate a cached tile. The closure defers URL resolution to
    # mirror-miss.
    def _resolve(tile: str = tile_id) -> str:
        return resolve_tile_url(tile)

    fetch_with_mirror(
        local_path,
        dataset=mirror_dataset,
        filename=f'{tile_id}.tif',
        gcp_project=gcp_project,
        source=_resolve,
    )

    blob_name = f'{gcs_staging_prefix}/{tile_id}.tif'
    if force:
        delete_asset_if_present(_tile_image_asset_id(asset_root, tile_id))
    gcs_uri = upload_to_gcs(gcp_project, gcs_bucket, blob_name, local_path)
    return tile_id, gcs_uri, local_path, blob_name


def _delete_collection_if_present(
    *,
    dataset_label: str,
    asset_root: str,
    default_tile_ids: tuple[str, ...] | list[str],
) -> None:
    """Delete the ImageCollection and all member images.

    Collections must be empty before deletion, so member images are
    dropped first. Iterates ``default_tile_ids`` (the canonical CONUS
    tile list) — additional tiles outside this list would survive, but
    a future re-extract under ``force=True`` would re-ingest cleanly
    anyway.
    """
    if not asset_exists(asset_root):
        return
    logger.info(f'{dataset_label}: force=True, clearing {asset_root}')
    for tile_id in default_tile_ids:
        delete_asset_if_present(_tile_image_asset_id(asset_root, tile_id))
    delete_asset_if_present(asset_root)


def run_tile_collection_extract(
    *,
    dataset_label: str,
    mirror_dataset: str,
    gcs_bucket: str,
    gcs_staging_prefix: str,
    asset_root: str,
    band_name: str,
    gcp_project: str,
    force: bool,
    default_tile_ids: tuple[str, ...] | list[str],
    tile_ids: list[str] | tuple[str, ...] | None,
    resolve_tile_url: Callable[[str], str],
    staging_max_workers: int = 16,
) -> str:
    """Run the canonical Phase A/B/C/D tile-collection extract.

    Per-tile ingestion produces an Image asset under
    ``{asset_root}/tile_{tile_id}``; the collection itself is created
    via ``create_image_collection_if_absent`` if absent. The transform
    consumer lazy-mosaics the collection on read.

    Args:
        dataset_label: Human-readable log prefix (e.g. ``'Harris AGB'``,
            ``'GFW Peatlands'``).
        mirror_dataset: Mirror-bucket key passed to
            ``fetch_with_mirror`` (e.g. ``'harris_agb'``).
        gcs_bucket: GCS bucket for tile staging.
        gcs_staging_prefix: GCS path prefix (e.g.
            ``'luc_high_res/staging/harris_agb'``).
        asset_root: GEE ImageCollection asset path.
        band_name: Band name to assign on ingestion.
        gcp_project: GCP project for GCS + EE.
        force: If True, re-ingest existing tiles.
        default_tile_ids: Canonical CONUS tile list, used when
            ``tile_ids`` is None (and to enumerate the collection for
            the full-rebuild force path).
        tile_ids: Override tile list (tests / partial rebuilds).
        resolve_tile_url: Callable producing the per-tile download URL.
        staging_max_workers: Phase A pool size (default 16).

    Returns:
        ``asset_root``. Returned even on partial failure — callers
        should re-run with ``force=True`` to retry.
    """
    tiles: tuple[str, ...] | list[str] = (
        tile_ids if tile_ids is not None else default_tile_ids
    )
    run_id = uuid.uuid4().hex[:8]
    full_rebuild = tile_ids is None

    if force and full_rebuild:
        # Default-tile-list rebuild: drop the whole collection so the
        # run starts from a clean slate (also handles the case where
        # the collection got into a weird partial state from prior
        # crashes).
        _delete_collection_if_present(
            dataset_label=dataset_label,
            asset_root=asset_root,
            default_tile_ids=default_tile_ids,
        )
    elif force:
        # Subset rebuild: only delete the tiles the caller actually
        # asked to rebuild. Nuking the parent collection (or sibling
        # tiles) would silently destroy unrelated data — historically
        # caused an integration test to wipe 18 of 19 production tiles.
        for tid in tiles:
            delete_asset_if_present(_tile_image_asset_id(asset_root, tid))
    create_image_collection_if_absent(asset_root)

    tiles_to_extract = [
        tid
        for tid in tiles
        if force or not asset_exists(_tile_image_asset_id(asset_root, tid))
    ]
    if not tiles_to_extract:
        logger.info(f'{dataset_label}: all {len(tiles)} tile(s) cached, nothing to do')
        return asset_root
    logger.info(
        f'{dataset_label}: {len(tiles_to_extract)}/{len(tiles)} tile(s) need '
        f'extraction; run_id={run_id}'
    )

    work_dir = tempfile.mkdtemp(prefix=f'{mirror_dataset}_{run_id}_')
    staged: list[tuple[str, str, str, str]] = []
    task_to_asset: dict[str, str] = {}
    task_to_blob: dict[str, str] = {}
    # Blobs that made it to GCS but never got a successful ingest task
    # ID (Phase B exception), so Phase D still cleans them up.
    orphan_blobs: list[str] = []

    try:
        # Phase A: concurrent staging.
        with ThreadPoolExecutor(max_workers=staging_max_workers) as ex:
            futures = {
                ex.submit(
                    _stage_tile,
                    tile_id=tid,
                    work_dir=work_dir,
                    gcp_project=gcp_project,
                    force=force,
                    mirror_dataset=mirror_dataset,
                    gcs_bucket=gcs_bucket,
                    gcs_staging_prefix=gcs_staging_prefix,
                    asset_root=asset_root,
                    resolve_tile_url=resolve_tile_url,
                ): tid
                for tid in tiles_to_extract
            }
            for fut in as_completed(futures):
                tid = futures[fut]
                try:
                    staged.append(fut.result())
                except Exception as exc:
                    logger.error(f'{dataset_label}: staging failed for {tid}: {exc}')

        # Phase B: synchronous batch submission. Each staged tile
        # becomes one ingestion task; failures are isolated — siblings
        # still go.
        for tile_id, gcs_uri, _local, blob_name in staged:
            asset_id = _tile_image_asset_id(asset_root, tile_id)
            try:
                task_id = start_ingestion_no_wait(
                    gcs_uri,
                    asset_id,
                    band_name=band_name,
                    allow_overwrite=force,
                )
            except Exception as exc:
                logger.error(f'{dataset_label}: submit failed for {tile_id}: {exc}')
                orphan_blobs.append(blob_name)
                continue
            task_to_asset[task_id] = asset_id
            task_to_blob[task_id] = blob_name

        # Phase C: bulk poll.
        if task_to_asset:
            failed = wait_for_tasks(
                list(task_to_asset),
                asset_ids_for_logging=task_to_asset,
            )
            if failed:
                logger.warning(
                    f'{dataset_label}: {len(failed)}/{len(task_to_asset)} '
                    f'tile(s) failed to ingest; collection at {asset_root} '
                    f'is partial — re-run with force=True to retry'
                )
    finally:
        # Phase D: cleanup — GCS blobs first, then local files, then
        # work dir.
        for blob_name in list(task_to_blob.values()) + orphan_blobs:
            try:
                delete_gcs_blob(gcp_project, gcs_bucket, blob_name)
            except Exception as exc:
                logger.warning(
                    f'{dataset_label}: blob cleanup failed for {blob_name}: ' f'{exc}'
                )
        shutil.rmtree(work_dir, ignore_errors=True)

    logger.info(f'{dataset_label}: extract complete → {asset_root}')
    return asset_root
