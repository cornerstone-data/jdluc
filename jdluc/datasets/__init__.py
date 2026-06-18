import enum

from jdluc.datasets import (
    base,
    gfw_global_peatlands,
    gfw_harris_agb,
    glad_glcluc,
    huang_bgb,
    ipcc_climate_zones,
    soilgrids_ocs,
    usda_nass_cdl,
    usda_nass_quickstats,
    worldbank_jurisdictions,
)


class DatasetName(enum.Enum):
    @staticmethod
    def _generate_next_value_(
        name: str, start: int, count: int, last_values: list[str]
    ) -> str:
        return name

    GFW_GLOBAL_PEATLANDS = enum.auto()
    GFW_HARRIS_AGB = enum.auto()
    GLAD_GLCLUC = enum.auto()
    HUANG_BGB = enum.auto()
    IPCC_CLIMATE_ZONES = enum.auto()
    SOILGRIDS_OCS = enum.auto()
    USDA_NASS_CDL = enum.auto()
    USDA_NASS_QUICKSTATS = enum.auto()
    WORLD_BANK_ADMIN_0 = enum.auto()
    WORLD_BANK_ADMIN_1 = enum.auto()
    WORLD_BANK_ADMIN_2 = enum.auto()


NAME_TO_CLS: dict[
    DatasetName, base.RasterDataset | base.TabularDataset | base.VectorDataset
] = {
    DatasetName.GFW_GLOBAL_PEATLANDS: gfw_global_peatlands.DATASET,
    DatasetName.GFW_HARRIS_AGB: gfw_harris_agb.DATASET,
    DatasetName.GLAD_GLCLUC: glad_glcluc.DATASET,
    DatasetName.HUANG_BGB: huang_bgb.DATASET,
    DatasetName.IPCC_CLIMATE_ZONES: ipcc_climate_zones.DATASET,
    DatasetName.SOILGRIDS_OCS: soilgrids_ocs.DATASET,
    DatasetName.USDA_NASS_CDL: usda_nass_cdl.DATASET,
    DatasetName.USDA_NASS_QUICKSTATS: usda_nass_quickstats.DATASET,
    DatasetName.WORLD_BANK_ADMIN_0: worldbank_jurisdictions.ADMIN_0_DATASET,
    DatasetName.WORLD_BANK_ADMIN_1: worldbank_jurisdictions.ADMIN_1_DATASET,
    DatasetName.WORLD_BANK_ADMIN_2: worldbank_jurisdictions.ADMIN_2_DATASET,
}
assert set(DatasetName) == set(NAME_TO_CLS), (
    f"{set(DatasetName)=:}; {set(NAME_TO_CLS)=:}"
)
