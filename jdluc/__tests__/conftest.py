"""Test configuration for luc_high_res tests.

Integration tests under this package define their own session-scoped
``gee_init`` and ``delaware_geometry`` fixtures locally; only shared
constants (region anchors) live here.
"""

DELAWARE_FIPS = '10'
