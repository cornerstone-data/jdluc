"""GCS mirror for non-native source-file fetches.

Every upstream download in the extract modules flows through
``fetch_with_mirror``. The mirror lives at
``gs://{GCS_MIRROR_BUCKET}/{GCS_MIRROR_PREFIX}/{dataset}/{filename}``
using the bucket's default storage class. Subsequent fetches read from
the mirror; upstream is only hit on mirror miss.

Motivation: upstream publishers rotate files (NASS QuickStats supersedes
older releases when a new one ships; tile-server signed URLs rotate;
Figshare/Zenodo records occasionally move). With the mirror in place
the pipeline is insulated from upstream availability after a first
successful extract.

Licensing: all four mirrored datasets permit redistribution with
attribution.

- Harris AGB: CC-BY 4.0 (Harris et al. 2021, *Nature Climate Change*).
- Huang BGB: CC-BY 4.0 (Huang et al. 2021, Figshare deposit).
- NASS QuickStats: US-government public domain (no restrictions).
- IPCC climate zones: open Zenodo deposit (Ogle et al. 2006).

Attributions are preserved in each per-dataset extract module's
docstring; this mirror helper does not embed license metadata on the
GCS blobs themselves.
"""

import logging
from collections.abc import Callable
from typing import Union

from google.cloud import storage

from jdluc.utils.constants import GCS_BUCKET_NAME
from jdluc.utils.gee import download_with_retries, upload_to_gcs

logger = logging.getLogger(__name__)

# GCS mirror location for the 4 non-native extract inputs.
# ``GCS_MIRROR_BUCKET`` is sourced from ``utils.constants`` (edit it there to
# repoint at a different bucket); the prefix is bucket-internal and fixed.
# The legacy ``luc_high_res/...`` prefix is preserved so existing mirrored
# fetches continue to be cache hits across the cliq → jdluc extraction.
GCS_MIRROR_PREFIX: str = 'luc_high_res/extract_mirror'

# Type alias for the source: either a literal URL or a zero-arg callable
# that resolves to one. Deferred resolution is the path IPCC takes —
# Zenodo's download URL requires a record-api probe to discover, and we
# want to skip that probe entirely on a mirror hit.
SourceUrl = Union[str, Callable[[], str]]


def _gcs_blob_exists(gcp_project: str, bucket_name: str, blob_name: str) -> bool:
    """Return True if the blob is present in GCS, False otherwise."""
    client = storage.Client(project=gcp_project)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    return bool(blob.exists())


def _download_from_gcs(
    gcp_project: str, bucket_name: str, blob_name: str, dst_path: str
) -> None:
    """Stream a GCS blob to ``dst_path``."""

    client = storage.Client(project=gcp_project)
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.download_to_filename(dst_path)


def fetch_with_mirror(
    dst_path: str,
    *,
    dataset: str,
    filename: str,
    gcp_project: str,
    source: SourceUrl,
    timeout_s: float = 600.0,
    retries: int = 3,
    backoff_s: float = 2.0,
) -> None:
    """Fetch a source file, preferring the GCS mirror over upstream.

    Semantics:
      1. Probe ``gs://{GCS_MIRROR_BUCKET}/{blob_name}`` where ``blob_name``
         follows the ``{GCS_MIRROR_PREFIX}/{dataset}/{filename}`` layout.
      2. On mirror hit, stream the blob to ``dst_path`` (fast intra-Google
         read; no egress cost when GEE and GCS sit in the same region).
         Done — upstream is never contacted.
      3. On mirror miss, resolve ``source`` to a URL (calling it if it's
         a callable so deferred-discovery sources like Zenodo only probe
         on miss), run ``download_with_retries`` with the usual retry
         policy, then upload ``dst_path`` back to the mirror at Archive
         storage class.

    Args:
        dst_path: Local filesystem path to materialize.
        dataset: Dataset key (stable, short — e.g. ``'nass_yields'``,
            ``'harris_agb'``). Used as the mirror-layout directory.
        filename: Stable file basename for the mirror key. Does NOT have
            to match the upstream filename — if upstream renames the
            source file (e.g. Zenodo hash-suffixed URLs), the mirror
            filename is what we probe on subsequent runs.
        gcp_project: GCP project used for GCS reads/writes.
        source: Either a literal URL or a zero-arg callable that returns
            one. Deferred resolution is intended for cases where the
            upstream URL requires a discovery probe we want to skip on
            mirror hit.
        timeout_s, retries, backoff_s: Passed through to
            ``download_with_retries``.

    Raises:
        Whatever ``download_with_retries`` raises on exhausted retries.
        ``google.cloud.storage`` exceptions if the mirror is unreachable.
    """
    blob_name = f'{GCS_MIRROR_PREFIX}/{dataset:s}/{filename:s}'
    gcs_uri = f'gs://{GCS_BUCKET_NAME}/{blob_name}'

    if _gcs_blob_exists(gcp_project, GCS_BUCKET_NAME, blob_name):
        logger.info(f'mirror[{dataset}]: cache hit, reading from {gcs_uri}')
        _download_from_gcs(gcp_project, GCS_BUCKET_NAME, blob_name, dst_path)
        return

    logger.info(f'mirror[{dataset}]: cache miss, fetching upstream')
    source_url = source() if callable(source) else source
    download_with_retries(
        source_url,
        dst_path,
        timeout_s=timeout_s,
        retries=retries,
        backoff_s=backoff_s,
    )

    logger.info(f'mirror[{dataset}]: writing upstream copy to {gcs_uri}')
    upload_to_gcs(gcp_project, GCS_BUCKET_NAME, blob_name, dst_path)
