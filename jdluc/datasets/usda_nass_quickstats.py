"""USDA National Agricultural Statistics Service | Quick Stats (survey)

license: public domain

year: 1866, ..., 2025

United States Department of Agriculture (USDA) National Agricultural Statistics Service (NASS), Quick Stats: USDA NASS, Washington, D.C.

https://quickstats.nass.usda.gov/
https://www.nass.usda.gov/Surveys/

# Methodology

- Tabular statistics from farmer/rancher questionnaires (NOT remote sensing)
- Sample-based: hundreds of surveys per year (e.g. Crop Production,
  Agricultural Prices, Grain Stocks, Cattle/Hog inventory, ARMS)
- Samples drawn from a list frame (known operations) plus an area frame
  (land segments), combined to cover operations missing from the list
- Collected by mail, phone (CATI), online (agcounts.usda.gov), and field
  enumeration
- Responses expanded to population estimates; final published values set by
  the Agricultural Statistics Board
- Only aggregated values published; small/identifying cells suppressed for
  confidentiality (Title 7 U.S.C., CIPSEA)
- API access: max 50,000 records/request (use bulk file downloads for more)
"""

import csv
import dataclasses
import io
import logging
import typing
import urllib.parse

import pandas

from jdluc import config, storage, utils
from jdluc.datasets import base, worldbank_jurisdictions

logger = logging.getLogger(__name__)

HA_PER_ACRE = 0.40468564
KG_PER_LB = 0.45359237
# 7 CFR 810 (US Grain Standards Act)
CROP_NAME_TO_LB_PER_BUSHEL = {"CORN": 56, "SOYBEANS": 60, "WHEAT": 60}

STATE_FIPS_TO_ADMIN_ID = {
    1: "USA001",  # Alabama
    2: "USA002",  # Alaska
    4: "USA003",  # Arizona
    5: "USA004",  # Arkansas
    6: "USA005",  # California
    8: "USA006",  # Colorado
    9: "USA007",  # Connecticut
    10: "USA008",  # Delaware
    11: "USA009",  # District of Columbia
    12: "USA010",  # Florida
    13: "USA011",  # Georgia
    15: "USA012",  # Hawaii
    16: "USA013",  # Idaho
    17: "USA014",  # Illinois
    18: "USA015",  # Indiana
    19: "USA016",  # Iowa
    20: "USA017",  # Kansas
    21: "USA018",  # Kentucky
    22: "USA019",  # Louisiana
    23: "USA020",  # Maine
    24: "USA021",  # Maryland
    25: "USA022",  # Massachusetts
    26: "USA023",  # Michigan
    27: "USA024",  # Minnesota
    28: "USA025",  # Mississippi
    29: "USA026",  # Missouri
    30: "USA027",  # Montana
    31: "USA028",  # Nebraska
    32: "USA029",  # Nevada
    33: "USA030",  # New Hampshire
    34: "USA031",  # New Jersey
    35: "USA032",  # New Mexico
    36: "USA033",  # New York
    37: "USA034",  # North Carolina
    38: "USA035",  # North Dakota
    39: "USA036",  # Ohio
    40: "USA037",  # Oklahoma
    41: "USA038",  # Oregon
    42: "USA039",  # Pennsylvania
    44: "USA040",  # Rhode Island
    45: "USA041",  # South Carolina
    46: "USA042",  # South Dakota
    47: "USA043",  # Tennessee
    48: "USA044",  # Texas
    49: "USA045",  # Utah
    50: "USA046",  # Vermont
    51: "USA047",  # Virginia
    53: "USA048",  # Washington
    54: "USA049",  # West Virginia
    55: "USA050",  # Wisconsin
    56: "USA051",  # Wyoming
}


@dataclasses.dataclass
class Yield:
    admin_id: str
    admin_level: str
    crop_name: str
    jurisdiction_name: str
    year: int
    yield_kg_per_ha: float

    @classmethod
    def from_dict(cls, d: dict[str, float | str]) -> typing.Self:
        crop_name = str(d["commodity_desc"])
        bu_per_acre = float(str(d["Value"]).replace(",", ""))
        return cls(
            admin_id=STATE_FIPS_TO_ADMIN_ID[int(d["state_fips_code"])],
            admin_level=worldbank_jurisdictions.AdminLevel.PROVINCIAL.name,
            crop_name=crop_name,
            jurisdiction_name=str(d["state_name"]),
            year=int(d["year"]),
            yield_kg_per_ha=(
                bu_per_acre
                * CROP_NAME_TO_LB_PER_BUSHEL[crop_name]
                * KG_PER_LB
                / HA_PER_ACRE
            ),
        )


def get_yield_dicts_from_api(api_key: str) -> list[dict[str, float | str]]:
    param_tuples = (
        ("key", api_key),
        ("source_desc", "SURVEY"),
        ("sector_desc", "CROPS"),
        ("statisticcat_desc", "YIELD"),
        ("agg_level_desc", "STATE"),
        ("unit_desc", "BU / ACRE"),
        ("freq_desc", "ANNUAL"),
        ("reference_period_desc", "YEAR"),
        ("class_desc", "ALL CLASSES"),
        ("prodn_practice_desc", "ALL PRODUCTION PRACTICES"),
        *[("commodity_desc", c) for c in ("CORN", "SOYBEANS", "WHEAT")],
        ("format", "CSV"),
    )
    with utils.get_requests_session().request(
        method="GET",
        params=urllib.parse.urlencode(param_tuples),
        url="https://quickstats.nass.usda.gov/api/api_GET/",
    ) as response:
        response.raise_for_status()
        body = response.text

    return list(csv.DictReader(io.StringIO(body)))


def _get_records_for_tile(tile_id: str) -> list[dict[str, str | float]]:
    api_key = config.Config.from_dot_env().usda_nass_api_key
    yield_dicts = get_yield_dicts_from_api(api_key=api_key)
    logger.info(f"Received {len(yield_dicts):d} raw yields")

    yields = [
        Yield.from_dict(yield_dict)
        for yield_dict in yield_dicts
        if int(yield_dict["state_fips_code"]) in STATE_FIPS_TO_ADMIN_ID
        if yield_dict["Value"] not in {"(D)", "(Z)", "(S)", "(NA)"}
    ]
    logger.info(f"After filtering: {len(yields):d} raw yields")

    return list(map(dataclasses.asdict, yields))


DATASET = base.TabularDataset(
    get_records_for_tile_id=_get_records_for_tile,
    idx_column_names=[
        "admin_level",
        "admin_id",
        "jurisdiction_name",
        "crop_name",
        "year",
    ],
    product_name="quickstats",
    source_name="usda-nass",
    version="2025",
)


def load() -> pandas.DataFrame:
    uri = storage.join_uri(
        root=config.Config.from_dot_env().ingest_root,
        prefix=DATASET.get_prefix(tile_id="world"),
    )
    logger.info(f"Loading yields from {uri=:s}")
    return pandas.read_parquet(path=uri)
