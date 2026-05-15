"""State and region geometry helpers.

Single-state helper (``get_multi_state_boundary``) is the transform-stage
primitive used by ``transform/land_use.py`` and ``transform/emissions.py``.
"""

from __future__ import annotations

import ee

from jdluc.utils._ee_types import EEGeometry
from jdluc.utils.constants import GEE_TIGER_STATES


def get_multi_state_boundary(state_fips_list: list[str]) -> EEGeometry:
    """Get the union of several states' boundaries as an ee.Geometry.

    Args:
        state_fips_list: List of state FIPS codes (e.g., ['10', '44']).
    """
    states = ee.FeatureCollection(GEE_TIGER_STATES)
    filtered = states.filter(ee.Filter.inList('STATEFP', state_fips_list))
    return filtered.geometry()
