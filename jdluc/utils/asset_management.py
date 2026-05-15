"""GEE asset management helpers.

Reusable low-level helpers for listing, version-parsing, and safe-deletion of
GEE assets produced by the pipeline.
"""

import dataclasses
import datetime as dt
import logging
import re
from typing import Any, cast

import ee

from jdluc.utils.constants import GEE_ASSET_ROOT

logger = logging.getLogger(__name__)

# Version suffix patterns recognized at the end of an asset's terminal segment.
# Anchored at end-of-string; the capture group is the bare version string.
#
# - Transform assets: ``{HEAD[:12]}`` or ``{HEAD[:12]}-dirty-{diff[:8]}``
# - Publish assets: compound ``{t_sha}_{p_sha}`` tail — the same
#   stage-SHA pattern appears twice, separated by an underscore.
#   ``parse_compound_version_from_asset_id`` handles that case.
# - Extract: ``v{digits}`` optionally followed by ``_{digits}`` groups
#   (e.g. ``v2021``, ``v2017_2020``)
_VERSION_PATTERNS = [
    r'[0-9a-f]{12}-dirty-[0-9a-f]{8}',
    r'[0-9a-f]{12}',
    r'v\d+(?:_\d+)*',
]
_VERSION_REGEX = re.compile(r'_(' + '|'.join(_VERSION_PATTERNS) + r')$')

# Publish assets carry two stage SHAs: transform first, publish second.
# Compound regex is end-anchored and greedy-prefers the two-SHA tail.
_STAGE_SHA_PATTERN = r'(?:[0-9a-f]{12}-dirty-[0-9a-f]{8}|[0-9a-f]{12})'
_COMPOUND_VERSION_REGEX = re.compile(
    rf'_({_STAGE_SHA_PATTERN})_({_STAGE_SHA_PATTERN})$'
)


@dataclasses.dataclass(frozen=True)
class AssetInfo:
    """Asset ID + last-update timestamp returned by ``listAssets``.

    ``update_time`` is a timezone-aware UTC ``datetime``; the GEE API
    returns RFC3339 strings, parsed once at the source so callers can
    compare against ``datetime.now(timezone.utc)`` directly.
    """

    asset_id: str
    update_time: dt.datetime


def _list_assets_page_iter(prefix: str) -> Any:
    """Yield ``ee.data.listAssets`` response dicts, paginated.

    Pulled out as a helper so ``list_assets_matching`` and
    ``list_assets_matching_with_metadata`` share one source of truth on
    pagination + prefix-matching.
    """
    full_prefix = f'{GEE_ASSET_ROOT}/{prefix}'
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {'parent': GEE_ASSET_ROOT}
        if page_token:
            params['pageToken'] = page_token
        # ee.data.listAssets' return type annotation (``dict[str, list[Any]]``)
        # is narrower than the real response (which also includes a string
        # ``nextPageToken``). Cast once to ``dict[str, Any]`` to avoid a
        # per-key type fight.
        result = cast(dict[str, Any], ee.data.listAssets(params))
        for asset in result.get('assets') or []:
            name = asset.get('name') or asset.get('id') or ''
            if name.startswith(full_prefix):
                yield asset
        next_token = result.get('nextPageToken')
        if not next_token:
            return
        page_token = next_token


def list_assets_matching(prefix: str) -> list[str]:
    """Return fully-qualified asset IDs under ``GEE_ASSET_ROOT`` whose terminal
    segment begins with ``prefix``.

    ``prefix`` is relative to the jdLUC asset root (e.g.
    ``land_use_delaware``); the root is prepended internally. Pagination is
    handled transparently via ``ee.data.listAssets``' ``nextPageToken``.
    """
    return [
        asset.get('name') or asset.get('id') or ''
        for asset in _list_assets_page_iter(prefix)
    ]


def list_assets_matching_with_metadata(prefix: str) -> list[AssetInfo]:
    """Same as ``list_assets_matching`` but returns ``AssetInfo``s.

    ``AssetInfo.update_time`` parses the asset's ``updateTime`` field
    (RFC3339, e.g. ``'2026-04-23T11:32:00.123456Z'``) into a
    timezone-aware UTC ``datetime``.

    Assets without an ``updateTime`` are skipped with a warning — the
    GEE API consistently returns it for every asset family we publish,
    so its absence indicates an unfamiliar asset shape that the
    cleanup tool shouldn't blindly accept.
    """
    out: list[AssetInfo] = []
    for asset in _list_assets_page_iter(prefix):
        asset_id = asset.get('name') or asset.get('id') or ''
        update_time_raw = asset.get('updateTime')
        if not update_time_raw:
            logger.warning(f'asset {asset_id} has no updateTime; skipping')
            continue
        try:
            update_time = _parse_rfc3339_utc(update_time_raw)
        except ValueError as exc:
            logger.warning(
                f'asset {asset_id} has unparseable updateTime '
                f'{update_time_raw!r}: {exc}; skipping'
            )
            continue
        out.append(AssetInfo(asset_id=asset_id, update_time=update_time))
    return out


def _parse_rfc3339_utc(text: str) -> dt.datetime:
    """Parse an RFC3339 timestamp into a tz-aware UTC ``datetime``.

    GEE returns timestamps with a ``Z`` suffix (UTC); ``fromisoformat``
    in Python 3.11 accepts the ``Z`` form on most builds but not all,
    so we normalize to ``+00:00`` first.
    """
    normalized = text.replace('Z', '+00:00') if text.endswith('Z') else text
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        # Defensive: an RFC3339 timestamp is always tz-aware, but if
        # ``fromisoformat`` returns a naïve dt for any reason, treat it
        # as UTC explicitly.
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def parse_version_from_asset_id(asset_id: str) -> str | None:
    """Extract the single trailing version suffix from an asset ID.

    Recognizes the transform-stage SHA forms
    (``[0-9a-f]{12}`` optionally followed by ``-dirty-[0-9a-f]{8}``) and the
    extract-stage ``v{digits}[_{digits}…]`` form. Anything else returns
    ``None``. Publish assets carry a compound ``{t_sha}_{p_sha}`` tail —
    use ``parse_compound_version_from_asset_id`` for those.
    """
    terminal = asset_id.rsplit('/', 1)[-1]
    match = _VERSION_REGEX.search(terminal)
    if match is None:
        return None
    return match.group(1)


def parse_compound_version_from_asset_id(
    asset_id: str,
) -> tuple[str, str] | None:
    """Extract ``(transform_sha, publish_sha)`` from a publish-asset ID.

    Publish assets (BigQuery tables) use a compound
    ``{prefix}_{region}_{t_sha}_{p_sha}`` naming scheme. Returns the pair
    when both stage SHAs match (either clean or dirty form each); returns
    ``None`` if the terminal segment only has one trailing SHA
    (i.e. a transform-stage asset rather than a publish-stage asset).
    """
    terminal = asset_id.rsplit('/', 1)[-1]
    match = _COMPOUND_VERSION_REGEX.search(terminal)
    if match is None:
        return None
    return match.group(1), match.group(2)


def delete_asset_safely(asset_id: str, dry_run: bool = False) -> bool:
    """Delete a GEE asset, logging and returning ``False`` on failure.

    When ``dry_run`` is true, logs what would be deleted and returns ``True``
    without touching GEE.
    """
    if dry_run:
        logger.info(f'[dry-run] would delete {asset_id}')
        return True
    try:
        ee.data.deleteAsset(asset_id)
        logger.info(f'deleted {asset_id}')
        return True
    except ee.EEException as exc:
        logger.error(f'failed to delete {asset_id}: {exc}')
        return False
