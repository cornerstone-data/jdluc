"""Google Earth Engine client utilities.

Thin wrappers over ``ee.data`` plus the GCS + HTTP plumbing the extract
stage needs to stage a local file into a GEE asset. Centralized here so
that per-dataset extractors in ``extract/`` stay focused on dataset-
specific logic and so the client surface is uniform across callers.

"""

import hashlib
import logging
import time
from typing import Any, cast

import ee
import requests

HIGH_VOLUME_ENDPOINT = 'https://earthengine-highvolume.googleapis.com'

logger = logging.getLogger(__name__)

# Poll cadence for ingestion / export task status.
POLL_INTERVAL_S: float = 30.0
# Hard timeout for a single ingestion task — ingest should finish in
# minutes; hours means something is stuck, fail loudly.
INGESTION_TIMEOUT_S: float = 2 * 60 * 60
# Hard timeout for a single export task. Transform-stage exports for
# CONUS land_use and emissions empirically take 130-145 min wall-clock,
# so 2 hours is too tight; this budget gives multi-hour exports headroom
# without letting truly stuck tasks linger forever.
EXPORT_TIMEOUT_S: float = 6 * 60 * 60


def initialize_gee(project: str) -> None:
    """Initialize Earth Engine against the high-volume endpoint.

    Args:
        project: GCP project ID for Earth Engine authentication, billing,
            and asset ownership.
    """
    ee.Initialize(project=project, opt_url=HIGH_VOLUME_ENDPOINT)


def asset_exists(asset_id: str) -> bool:
    """Return True iff the GEE asset exists."""
    try:
        ee.data.getAsset(asset_id)
        return True
    except ee.EEException:
        return False


def asset_is_populated(asset_id: str) -> bool:
    """Return True iff the asset exists AND, if an ImageCollection, is non-empty.

    The extract orchestrator uses this — not bare ``asset_exists`` — to
    decide whether a dataset is cache-hit. An empty ImageCollection (left
    behind by ``create_image_collection_if_absent`` when a partial
    ingestion failed mid-iteration) would otherwise be mistaken for a
    valid cached asset and skip re-extraction, leaving downstream
    transforms to mosaic 0 bands.
    """
    try:
        info = ee.data.getAsset(asset_id)
    except ee.EEException:
        return False
    if info.get('type') == 'IMAGE_COLLECTION':
        children = ee.data.listAssets({'parent': asset_id}).get('assets', [])
        return len(children) > 0
    return True


def delete_asset_if_present(asset_id: str) -> None:
    """Delete the asset if it exists; no-op otherwise."""
    if asset_exists(asset_id):
        logger.info(f'Deleting existing asset: {asset_id}')
        ee.data.deleteAsset(asset_id)


def create_image_collection_if_absent(asset_id: str) -> None:
    """Create an empty ImageCollection asset at ``asset_id`` if absent."""
    if asset_exists(asset_id):
        return
    logger.info(f'Creating empty ImageCollection: {asset_id}')
    # ``ee.data.createAsset``'s first argument is the asset payload; the
    # stub's type is narrower than the real API accepts.
    ee.data.createAsset(cast(Any, {'type': 'ImageCollection'}), asset_id)


def download_with_retries(
    url: str,
    dst_path: str,
    *,
    timeout_s: float = 600.0,
    retries: int = 3,
    backoff_s: float = 2.0,
    chunk_size: int = 1 << 20,
) -> None:
    """Streaming HTTP GET with exponential backoff between attempts.

    Retries transient failures (network / 5xx); raises the last exception
    if all attempts fail.
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            logger.info(f'Download attempt {attempt}/{retries}: {url} -> {dst_path}')
            with requests.get(url, stream=True, timeout=timeout_s) as response:
                response.raise_for_status()
                with open(dst_path, 'wb') as fh:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            fh.write(chunk)
            return
        except (requests.RequestException, OSError) as exc:
            last_exc = exc
            logger.warning(f'Download attempt {attempt} failed: {exc}')
            if attempt < retries:
                time.sleep(backoff_s * attempt)
    assert last_exc is not None
    raise last_exc


def upload_to_gcs(
    gcp_project: str,
    bucket_name: str,
    blob_name: str,
    local_path: str,
) -> str:
    """Stage ``local_path`` to ``gs://{bucket}/{blob}`` and return the URI."""
    # Lazy import: google.cloud.storage is a heavy transitive dep.
    from google.cloud import storage

    gcs_uri = f'gs://{bucket_name}/{blob_name}'
    logger.info(f'Uploading to GCS: {gcs_uri}')
    client = storage.Client(project=gcp_project)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(local_path)
    return gcs_uri


def delete_gcs_blob(
    gcp_project: str,
    bucket_name: str,
    blob_name: str,
) -> None:
    """Delete a GCS staging object; logs but does not raise on NotFound."""
    from google.api_core import exceptions as gcs_exceptions
    from google.cloud import storage

    logger.info(f'Deleting GCS staging object: gs://{bucket_name}/{blob_name}')
    client = storage.Client(project=gcp_project)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    try:
        blob.delete()
    except gcs_exceptions.NotFound:
        logger.warning(f'GCS blob already absent: gs://{bucket_name}/{blob_name}')


def start_ingestion_no_wait(
    gcs_uri: str,
    asset_id: str,
    *,
    band_name: str = 'b1',
    allow_overwrite: bool = False,
    request_id: str | None = None,
) -> str:
    """Submit an image ingestion task and return its task ID without polling.

    Default ``request_id`` is a deterministic SHA-256 hash of ``asset_id``
    so that re-issuing the call after an orchestrator crash returns the
    existing in-flight task (GEE request-id idempotency) instead of
    starting a duplicate. ``force=True`` callers in the extract stage
    delete the asset before submission, breaking the idempotency hash
    domain (the new ingest is a different request semantically) — passing
    a fresh ``request_id`` from the caller is the escape hatch if that's
    ever needed.
    """
    if request_id is None:
        request_id = hashlib.sha256(asset_id.encode()).hexdigest()[:32]
    manifest: dict[str, Any] = {
        'name': asset_id,
        'tilesets': [{'sources': [{'uris': [gcs_uri]}]}],
        'bands': [{'id': band_name}],
    }
    logger.info(f'Starting ingestion: {asset_id} <- {gcs_uri}')
    task_info = ee.data.startIngestion(
        request_id=request_id,
        params=manifest,
        allow_overwrite=allow_overwrite,
    )
    return str(task_info['id'])


def wait_for_tasks(
    task_ids: list[str],
    *,
    asset_ids_for_logging: dict[str, str] | None = None,
    poll_interval_s: float = POLL_INTERVAL_S,
    timeout_s: float = INGESTION_TIMEOUT_S,
    heartbeat_interval_s: float | None = None,
    task_labels: dict[str, str] | None = None,
) -> dict[str, str]:
    """Poll task IDs in bulk until each reaches a terminal state.

    Returns ``{task_id: error_message}`` for tasks that ended FAILED or
    CANCELLED; successful completions are absent from the return dict.
    Raises ``TimeoutError`` if any task is still pending at ``timeout_s``.

    ``asset_ids_for_logging`` is an optional ``{task_id: asset_id}`` map
    used only to make log messages identify the failed asset by name
    rather than by opaque task ID.

    ``heartbeat_interval_s`` (optional) — when set, logs an INFO line at
    that cadence listing the still-pending task labels and elapsed
    minutes. Used by long-running export polls (transform stage); the
    extract stage's tile-ingest polling leaves it None for quiet polling.

    ``task_labels`` (optional ``{task_id: label}``) — caller-friendly
    kind labels (e.g. ``'transitions'``, ``'crops'``) used in heartbeat
    and per-task log lines. Falls back to ``asset_ids_for_logging`` and
    then the task ID when absent.

    ``getTaskStatus`` is called in chunks of 100 IDs — empirically the
    largest chunk size GEE accepts without rejecting the request. At
    CONUS scale (≤19 tile-based ingest tasks) the chunking is a no-op,
    but it lets a global-scale (~280 task) extract stay on a single
    polling loop without surfacing the chunk count to callers.
    """
    pending: set[str] = set(task_ids)
    failed: dict[str, str] = {}
    start = time.monotonic()
    deadline = start + timeout_s
    next_heartbeat = start + heartbeat_interval_s if heartbeat_interval_s else None

    def _label_for(tid: str) -> str:
        if task_labels and tid in task_labels:
            return task_labels[tid]
        if asset_ids_for_logging and tid in asset_ids_for_logging:
            return asset_ids_for_logging[tid]
        return tid

    while pending:
        ids = list(pending)
        rows: list[dict[str, Any]] = []
        for i in range(0, len(ids), 100):
            rows.extend(ee.data.getTaskStatus(ids[i : i + 100]))
        for row in rows:
            tid = str(row.get('id', ''))
            if tid not in pending:
                continue
            state = str(row.get('state', 'UNKNOWN'))
            if state == 'COMPLETED':
                # GEE reports per-task batch_eecu_usage_seconds in the
                # status row; log it on completion so the run log is the
                # source of truth for cost ($0.40 per EECU-hr at the list
                # rate). start/update timestamps give wall time.
                eecu_s = float(row.get('batch_eecu_usage_seconds', 0.0) or 0.0)
                eecu_hr = eecu_s / 3600.0
                cost_usd = eecu_hr * 0.40
                start_ms = row.get('start_timestamp_ms') or 0
                end_ms = row.get('update_timestamp_ms') or 0
                wall_min = (
                    (int(end_ms) - int(start_ms)) / 60000.0
                    if start_ms and end_ms
                    else 0.0
                )
                logger.info(
                    f'Task {tid} for {_label_for(tid)} COMPLETED '
                    f'wall={wall_min:.1f}min EECU-hr={eecu_hr:.3f} '
                    f'cost_usd={cost_usd:.2f}'
                )
                pending.discard(tid)
            elif state in ('FAILED', 'CANCELLED'):
                err = str(row.get('error_message', ''))
                logger.error(f'Task {tid} for {_label_for(tid)} {state}: {err}')
                failed[tid] = err
                pending.discard(tid)
        if pending and time.monotonic() > deadline:
            raise TimeoutError(
                f'wait_for_tasks: {len(pending)} task(s) pending after '
                f'{timeout_s:.0f}s'
            )
        if pending and next_heartbeat is not None:
            now = time.monotonic()
            if now >= next_heartbeat:
                elapsed_min = (now - start) / 60
                pending_labels = sorted(_label_for(tid) for tid in pending)
                logger.info(
                    f'wait_for_tasks: still waiting on {pending_labels} '
                    f'(elapsed={elapsed_min:.0f}m)'
                )
                assert heartbeat_interval_s is not None
                next_heartbeat = now + heartbeat_interval_s
        if pending:
            time.sleep(poll_interval_s)
    return failed


def start_ingestion_and_wait(
    gcs_uri: str,
    asset_id: str,
    *,
    band_name: str = 'b1',
    allow_overwrite: bool = False,
    poll_interval_s: float = POLL_INTERVAL_S,
    timeout_s: float = INGESTION_TIMEOUT_S,
) -> str:
    """Ingest ``gcs_uri`` into ``asset_id`` and block until the task lands.

    Convenience wrapper for single-task callers (``huang_bgb``,
    ``ipcc_climate_zones``); composes ``start_ingestion_no_wait`` and
    ``wait_for_tasks``. Tile-based extractors (``harris_agb``,
    ``gfw_peatlands``) call those primitives directly so the submit
    phase can fan out across all tiles before the first poll.

    Returns the GEE task ID. Raises ``RuntimeError`` on FAILED/CANCELLED,
    ``TimeoutError`` if the task hasn't completed within ``timeout_s``.
    """
    task_id = start_ingestion_no_wait(
        gcs_uri,
        asset_id,
        band_name=band_name,
        allow_overwrite=allow_overwrite,
    )
    failed = wait_for_tasks(
        [task_id],
        asset_ids_for_logging={task_id: asset_id},
        poll_interval_s=poll_interval_s,
        timeout_s=timeout_s,
    )
    if task_id in failed:
        raise RuntimeError(f'Task {task_id} for {asset_id} failed: {failed[task_id]}')
    return task_id


def start_table_ingestion_and_wait(
    gcs_uri: str,
    asset_id: str,
    *,
    allow_overwrite: bool = False,
    poll_interval_s: float = POLL_INTERVAL_S,
    timeout_s: float = INGESTION_TIMEOUT_S,
) -> str:
    """Ingest ``gcs_uri`` as a FeatureCollection at ``asset_id`` and block.

    For shapefile sources, ``gcs_uri`` must point at the .zip bundle
    (GEE reads .shp/.shx/.dbf/.prj out of it). For CSVs, point at the
    .csv. Returns the GEE task ID. Raises on FAILED/CANCELLED or
    timeout, matching ``start_ingestion_and_wait``'s error model.
    """
    request_id = ee.data.newTaskId(1)[0]
    manifest: dict[str, Any] = {
        'name': asset_id,
        'sources': [{'primaryPath': gcs_uri}],
    }
    logger.info(f'Starting table ingestion: {asset_id} <- {gcs_uri}')
    task_info = ee.data.startTableIngestion(
        request_id=request_id,
        params=manifest,
        allow_overwrite=allow_overwrite,
    )
    task_id = str(task_info['id'])
    failed = wait_for_tasks(
        [task_id],
        asset_ids_for_logging={task_id: asset_id},
        poll_interval_s=poll_interval_s,
        timeout_s=timeout_s,
    )
    if task_id in failed:
        raise RuntimeError(f'Task {task_id} for {asset_id} failed: {failed[task_id]}')
    return task_id


def wait_for_export_task(
    task: ee.batch.Task,
    asset_id: str,
    *,
    poll_interval_s: float = POLL_INTERVAL_S,
    timeout_s: float = EXPORT_TIMEOUT_S,
) -> None:
    """Block until an ``ee.batch.Task`` (Export.*.toAsset) finishes."""
    task_id = str(task.id)
    failed = wait_for_tasks(
        [task_id],
        asset_ids_for_logging={task_id: asset_id},
        poll_interval_s=poll_interval_s,
        timeout_s=timeout_s,
    )
    if task_id in failed:
        raise RuntimeError(f'Task {task_id} for {asset_id} failed: {failed[task_id]}')
