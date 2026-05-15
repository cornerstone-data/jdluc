# Cornerstone jdLUC: scientific methodology

## Overview

This document describes an LSRS-compliant jdLUC methodology for estimating land use change (LUC) emissions attributable to US row crop production. The approach is designed to extend easily to other jurisdictions once validated in the United States.

The core of the methodology is the Global Land Cover and Land Use Change dataset produced by the University of Maryland and the Land & Carbon Lab (GLAD GLC; CC BY 4.0). This dataset provides the main underlying land use time series. It provides land cover at 5-year epochs from 2000-2020, at 30m resolution. We chose to build off this dataset because:

- **Accuracy**: An [independent validation](https://landcarbonlab.org/insights/global-land-cover-maps-accuracy-applications/) by Land & Carbon Lab and Wageningen University found GLAD GLC had the second-highest global accuracy of the leading high resolution land cover datasets, with the best forest accuracy. The dataset with the highest overall accuracy (ESA's WorldCover) has only 2020 and 2021 maps, generated with different algorithms, making it unsuitable for change detection.
- **Temporal consistency**: GLC uses the same classification model across all epochs (2000, 2005, 2010, 2015, 2020), which ensures detected changes reflect real land cover change rather than methodological drift. 
- **Simplicity**: GLC covers all land cover types, which eliminates the need to reconcile different datasets for forests, grasslands, etc.
- **Historical coverage**: GLC's coverage extends back to 2000, which eliminates the need for backfill methodologies.
- **Global extensibility**: GLC works worldwide.

The main downside of using the GLC dataset is that it ends in 2020, which is now fairly out of date. There are various ways we could project the dataset forward in the United States. For example, we could attempt to harmonize it with the USDA Cropland Data Layer (CDL), which is available annually up through 2025, and has ~92% agreement with GLC for row crops.  However these approaches generalize poorly for a global effort. We think the better investment of time, if we want to go beyond this initial proof of concept, will be in working with University of Maryland to extend the satellite processing through 2025, or to produce a similar high quality, temporally consistent landcover dataset with more recent coverage.

Once we've catalogued the landcover transitions from GLC, we add in data on peatland extent and climate domain, and then layer in carbon stock datasets to estimate carbon fluxes. These datasets cover above ground woody biomass for forests, above ground non-woody biomass for grassland and pastures, below ground root biomass, organic soil carbon, and peatland-specific soil carbon.

Finally, we use the USDA Cropland Data Layer to identify the specific row crop grown on each GLAD GLC cropland pixel in 2020. This lets us ascribe the carbon fluxes to specific 2020 crops, following the GHGP-prescribed 20-year lookback approach.

## Geospatial reference

### Pixels

We use the GLAD GLC native 30-meter resolution (Landsat-based, 0.00025 degrees) as the common geospatial reference for all data. All other raster inputs are resampled and/or reprojected to align with this grid.

### State boundaries

We use the US Census Bureau TIGER/Line state boundaries to assign each pixel to a US state. The data is available in the GEE catalog as `TIGER/2018/States`. For pixels that overlap state boundaries, we assign to the state containing the pixel centroid.

## Cataloging emissions drivers

For every ~30m pixel in the continental United States, we build a time series of land-cover transitions between the five epochs in the GLAD GLC dataset: 2000 → 2005 → 2010 → 2015 → 2020. 

In addition, we catalogue a binary value specifying whether the pixel is peatland and each pixel's IPCC climate domain.

### Land cover transitions

We load the GLAD GLCLUC v2 combined maps from GEE at `projects/glad/GLCLU2020/v2/LCLUC_{year}`.

GLAD GLCLUC encodes land cover as unsigned 8-bit values. We simplify to land use categories as follows:

| GLAD GLCLUC pixel values | Land use category |
|---|---|
| 25–48 | Forest (terra firma tree cover, by canopy height 3m to >25m) |
| 125–148 | Wetland forest |
| 1–24 | Short vegetation (grassland, shrubland, sparse vegetation gradient) |
| 100–124 | Wetland short vegetation |
| 244 | Cropland |
| 250 | Built-up |
| 200–207 | Water |
| 241 | Snow/ice |
| 0 | Bare (3% vegetation cover or less) |

We compare consecutive GLAD GLCLUC epochs (2000→2005, 2005→2010, 2010→2015, 2015→2020), and for each pixel, record whether a transition occurred, from which land use category, and to which. We record all transitions, but the only transitions that generate LUC emissions are those where land cover changes from a higher-carbon state (e.g. forest, short vegetation/grassland) to a lower-carbon state (e.g. cropland, built-up). Transitions in the reverse direction (e.g., cropland → forest) would represent carbon removals, which are not currently included in this methodology (see Appendix 2).

We do not attempt to assign transitions at higher temporal granularity than the 5-year windows. For any given pixel, the maximum timing error in this approach is ±2 years, corresponding to just ±1 percentage point on the GHGP lookback weight. For groups of pixels, the error is likely to be even less, since not all transition pixels will be off in the same direction.

### Peatland identification

We use the [Global Forest Watch Global Peatlands](https://data.globalforestwatch.org/datasets/gfw::global-peatlands) raster composite (CC BY 4.0). GFW publishes a 30m global mask assembled from five regional peatland datasets, with each region using the most authoritative source available at the time of compilation:

- **Above 40°N**: Xu et al. (2018) PEATMAP — meta-analytic global peatland extent. Covers the northern tier of CONUS (most of the Corn Belt and Great Plains).
- **Below 40°N (default)**: Gumbricht et al. (2017) — hydrological-modeling expert system for tropical wetlands and peatlands.
- **Indonesia and Malaysia**: Miettinen et al. (2016) — Landsat-based peatland land-cover mapping for the SE Asian peatland complex.
- **Lowland Peruvian Amazon**: Hastie et al. (2022) — peat-thickness mapping from field-validated remote sensing.
- **Congo basin**: Crezee et al. (2022) — central African peat-thickness and carbon-stock mapping from field data.

All input layers are rasterized or resampled to the 30m Hansen Global Forest Change grid. We identify each pixel with a binary value of either peatland or non-peatland. This is a single value per pixel, not a time series.

*References:*
- Global Forest Watch (2023). Global Peatlands. https://data.globalforestwatch.org/datasets/gfw::global-peatlands. CC BY 4.0.

### Climate domain identification

We use an independently recreated raster (Lewis, 2022) to determine the IPCC climate domain for each pixel. This dataset is built following the IPCC 2019 Refinement decision tree (Vol 4, Ch 3, Annex 3A.5), which classifies climate zones using mean annual temperature, precipitation, potential evapotranspiration ratio, frost days, and elevation. The raster is provided at 0.5° resolution (~50km), which is sufficient for climate domain classification. We use 10 of the 12 IPCC climate zones, excluding polar zones (negligible cropland). 

We store the climate domain as a single categorical value per pixel (not a time series).

*References:*
- Lewis, M. (2022). IPCC Climate Zones (from the 2019 Refinement to the 2006 IPCC Guidelines for National Greenhouse Gas Inventories). Zenodo. https://doi.org/10.5281/zenodo.7303808
- IPCC (2019). 2019 Refinement to the 2006 IPCC Guidelines for National Greenhouse Gas Inventories (Calvo Buendia, E. et al., eds.). Volume 4: Agriculture, Forestry and Other Land Use, Chapter 3, Annex 3A.5. IPCC, Switzerland.

## Calculating emissions

For each pixel with a detected land use transition, emissions are the sum of carbon fluxes from:

1. Vegetation carbon, defined as:
   1. Above-ground biomass
   2. Root biomass 
   3. Dead organic matter (forests only)
2. Soil organic carbon
   1. Mineral soil stock change
   2. Peatland drainage


For forests, we calculate 1.a-1.c separately. For grasslands and shrublands, we use a single overall value for vegetation. In both cases, soil organic carbon is a separate calculation, and is handled differently for mineral soils and peatland pixels. 

All categories of emissions are initially calculated as if the transition is instantaneous. Emissions are then subsequently allocated over time using the linear discounting approach described in the next section, following GHGP guidance.

Finally, we add in an additional ongoing (land management) component of peatland emissions for all peatland under ongoing cultivation, regardless of whether the land was transformed in the last 20 years. These emissions are annual and not subject to linear discounting - they reflect that peat soil continues emitting greenhouse gases for decades after initial drainage.

### Vegetation carbon (above-ground biomass, dead organic matter, and root biomass)

#### Forests

When a forest is converted to another land cover type, we calculate:

- for forest → grassland conversions, loss of the difference in vegetation carbon between the forest and the grassland 
- for forest → cropland or built-up land cover conversions, full loss of forest vegetation carbon

Forest vegetation carbon is calculated as follows. Soil carbon losses from these transitions are handled separately in the next section.

##### Above ground live biomass

For forests, above-ground live woody biomass comes from Harris et al. (2021). The dataset is in Mg/ha at ~30m resolution, representing circa year 2000 conditions.

*Reference:* Harris et al. (2021) Global maps of twenty-first century forest carbon fluxes. Nature Climate Change, 11, 234-240. https://doi.org/10.1038/s41558-020-00976-6

##### Root biomass

Below-ground biomass for forests comes from Huang et al. (2021) at ~1km resolution. Where Huang data is missing, root biomass is estimated from above ground biomass using the global forest mean root-to-shoot ratio of 0.25 ± 0.10 from Huang et al. (2021).

*Reference:* Huang et al. (2021) Global map of root biomass across the world's forests. https://essd.copernicus.org/articles/13/4263/2021/

##### Dead organic matter

Dead organic matter (dead wood + litter) is estimated as a fraction of above-ground biomass, following the UNFCCC CDM AR-TOOL-12 methodology. 

```
DOM_C = above_ground_biomass × (dead_wood_factor × 0.50 + litter_factor × 0.37)
DOM_CO2 = DOM_C × (44/12) × pixel_area_ha
```

Where AGB is calculated as described in the previous section, 0.50 (dead wood, temperate species) and 0.37 (litter) are the carbon fractions used by CDM AR-TOOL-12 v3.0, which sources them from IPCC GPG-LULUCF 2003 §3.2.1.2.1.1. The 0.37 litter CF is also the Tier 1 default given in IPCC 2006, Vol 4, Ch 2 §2.3.2.1, Eq. 2.19.

| Climate domain | Dead wood factor | Litter factor |
|---|---|---|
| Tropical, ≤2000m, dry (≤1000mm precip) | 0.02 | 0.04 |
| Tropical, ≤2000m, moist (1000–1600mm) | 0.01 | 0.01 |
| Tropical, ≤2000m, wet (>1600mm) | 0.06 | 0.01 |
| Tropical, >2000m (montane) | 0.07 | 0.01 |
| Temperate / Boreal | 0.08 | 0.04 |

For CONUS, nearly all forest pixels fall in the temperate/boreal row (dead wood = 8% of AGB, litter = 4%). For a typical US temperate forest with AGB = 150 Mg/ha, this yields DOM carbon of ~8.2 tC/ha (dead wood 6.0 + litter 2.2). 

*References:*
- CDM AR-TOOL-12 v3.0: Estimation of carbon stocks and change in carbon stocks in dead wood and litter in A/R CDM project activities. UNFCCC.
- Domke, G.M. et al. (2016). Estimating litter carbon stocks on forest land in the United States. Science of the Total Environment 557–558, 469–478. https://doi.org/10.1016/j.scitotenv.2016.03.090
- Woodall, C.W., Heath, L.S. & Smith, J.E. (2008). National inventories of down and dead woody material forest carbon stocks in the United States: Challenges and opportunities. Forest Ecology and Management 256(3), 221–228. https://doi.org/10.1016/j.foreco.2008.04.003

#### Grassland and shrubland

For grassland and shrubland, we assign a single total vegetation carbon density, which represents above ground biomass + below ground biomass. Values are adapted from the BLUE model (Hansis et al. 2015, supplementary Table S1), used in IPCC assessments and the Global Carbon Budget. We remap BLUE's PFT-level carbon densities to IPCC climate domains; the assignments for tropical dry, tropical montane, warm/cool temperate dry, and boreal moist climate zones represent our interpretation where the source provides no direct equivalent.

| IPCC climate domain      | Total vegetation C (tC/ha) | 
| ------------------------ | -------------------------- | 
| Tropical, Wet            | 18                         | 
| Tropical, Moist          | 18                         | 
| Tropical, Dry            | 7                          | 
| Tropical, Montane        | 7                          | 
| Warm Temperate, Moist    | 7                          | 
| Warm Temperate, Dry      | 5                          |
| Cool Temperate, Moist    | 7                          | 
| Cool Temperate, Dry      | 5                          |
| Boreal, Moist            | 6                          | 
| Boreal, Dry              | 3                          | 

For the majority of CONUS short vegetation conversions (Great Plains grassland in the warm/cool temperate zones), these values are at the high end of total vegetation carbon estimated from field-measured above-ground biomass and standard root-to-shoot ratios (Mokany et al., 2006; IPCC 2006 Vol 4, Table 6.1), providing slight conservatism for short/mixed grass prairie. However they underestimate vegetation carbon slightly for tallgrass prairie and significantly for western shrublands like California chaparral. Literature values for comparison:

| Vegetation type | Total vegetation C (tC/ha) | Source |
|---|---|---|
| Shortgrass steppe | 2–4 | Sala et al. 1988; Mokany et al. 2006 |
| Mixed grass prairie | 5–7 | Sala et al. 1988; Mokany et al. 2006 |
| Tallgrass prairie | 8–11 | Knapp et al. 1998; Mokany et al. 2006 |
| Sagebrush steppe (mature) | 6–12 | Cleary et al. 2010; Bradley et al. 2006 |
| CA chaparral (mature) | 17–28 | Bohlman et al. 2018 |

*Grassland total vegetation C values derived from field-measured peak AGB (converted with CF=0.47) plus root biomass using Mokany et al. R:S ratios (~4 for low-biomass temperate grassland). Shrubland values use species-specific R:S ratios (0.6 for chaparral per Kummerow et al. 1977; 0.8 for sagebrush per Cleary et al. 2010).*

Dead organic matter is excluded for grassland/shrubland conversions. IPCC Tier 1 assumes DOM pools are zero for all non-forest land categories (IPCC 2006, Vol 4, Ch 2, Section 2.3.2.2) because although grassland litter is real, it's small (typically 0.5–3 tC/ha) and decomposes rapidly (1–2 year turnover), unlike forest dead wood which accumulates over decades.

Thus total emissions from vegetation carbon biomass in grasslands is calculated as:

 ```
 Vegetation_CO2 = Vegetation_tC_per_ha × (44/12) × pixel_area_ha
 ```

Note that GLAD GLC does not distinguish managed pastureland from natural grassland: both fall into the "short vegetation" category (values 1–24) and are treated identically for purposes of forest → grassland conversions and grassland → cropland conversions. This probably results in a slight overestimate of emissions for pastureland → cropland conversions, but well within the overall uncertainty of the model.

_References:_
- Bohlman, G.N., Underwood, E.C. & Safford, H.D. (2018). Estimating biomass in California's chaparral and coastal sage scrub shrublands. Madroño 65, 28–46.
- Bradley, B.A., Houghton, R.A., Mustard, J.F. & Hamburg, S.P. (2006). Invasive grass reduces aboveground carbon stocks in shrublands of the Western US. Global Change Biology 12, 1815–1822.
- Cleary, M.B., Pendall, E. & Ewers, B.E. (2010). Aboveground and belowground carbon pools after fire in mountain big sagebrush steppe. Rangeland Ecology & Management 63, 187–196.
- Hansis, E., Davis, S.J. & Pongratz, J. (2015). Relevance of methodological choices for accounting of land use change carbon fluxes. Global Biogeochemical Cycles 30(11), 1230–1246. https://doi.org/10.1002/2014GB004997
- Knapp, A.K., Briggs, J.M., Hartnett, D.C. & Collins, S.L. (eds.) (1998). Grassland Dynamics: Long-Term Ecological Research in Tallgrass Prairie. Oxford University Press.
- Kummerow, J., Krause, D. & Jow, W. (1977). Root systems of chaparral shrubs. Oecologia 29, 163–177.
- Mokany, K., Raison, R.J. & Prokushkin, A.S. (2006). Critical analysis of root:shoot ratios in terrestrial biomes. Global Change Biology 12, 84–96. https://doi.org/10.1111/j.1365-2486.2005.001043.x
- Sala, O.E., Parton, W.J., Joyce, L.A. & Lauenroth, W.K. (1988). Primary production of the central grassland region of the United States. Ecology 69, 40–45.
- IPCC (2006). 2006 IPCC Guidelines for National Greenhouse Gas Inventories. Volume 4, Chapter 6: Grassland.

### Soil organic carbon for mineral soils

We calculate soil organic carbon (SOC) losses for non-peatland pixels using the IPCC stock change approach (IPCC 2019, Vol 4, Ch 5, Tables 5.5 and 5.10). This follows the IPCC approach of treating mineral and organic soils separately. See IPCC 2006 Vol 4, Ch 5, Section 5.2.3.2.

SoilGrids SOC stock is available in GEE at `projects/soilgrids-isric/ocs_mean` (0–30cm stock, units t/ha). Where SoilGrids has NoData (masked pixels), we use the IPCC default reference SOC stock for warm temperate moist mineral soil (low activity clay): 63 tC/ha (IPCC 2019 Refinement, Vol 4, Table 2.3). This affects <1% of conversion pixels.

For each pixel with a land use transition, we compute the SOC change as `SOC_stock × (1 - F_LU)`, where `F_LU` is the IPCC Tier 1 land-use stock change factor for the destination land use under the pixel's climate domain. We convert to tCO2 by multiplying by pixel area in hectares and the CO2/C mol ratio (44/12).

Under IPCC 2019 Tier 1, native forest and native (non-degraded) grassland are both reference conditions — `F_LU = 1` per Table 5.10 — so `F_LU` depends only on the destination category and climate, not the source. Concretely:

- Forest → cropland/built-up: `F_LU` from Table 5.5 "Long-term cultivated" row for the pixel's climate regime.
- Short vegetation → cropland/built-up: same `F_LU` as forest → cropland/built-up (same destination, same climate, same Table 5.5 row).
- Forest → short vegetation: `F_LU = 1` (both are reference conditions); SOC loss is zero.

Built-up destinations use the cropland `F_LU` as a proxy, since IPCC has no direct factor for urban conversion. Tropical montane has no explicit Table 5.5 row for long-term cultivated; per Table 5.5 footnote 4, montane factors are approximated as the mean of temperate and tropical stock changes (we use `F_LU = 0.76`, the mean of warm temperate moist `0.69` and tropical moist/wet `0.83`).

We set input and management factors to 1 (nominal input, full tillage) per IPCC defaults.

*Reference:* IPCC methodology, available at https://www.ipcc-nggip.iges.or.jp/public/2019rf/pdf/4_Volume4/19R_V4_Ch05_Cropland.pdf

### Peatland emissions

Peatland pixels are the most complex and novel component of the methodology. We use a two-part model that captures (1) a declining emission pulse over the first 20 years post-drainage, and (2) a flat steady-state emission rate thereafter.  The initial pulse is modeled as land use change under the GHGP framework, with GHGP's standard linear discounting providing the decline curve. The flat steady state emissions are modeled as annual land management emissions. Both values are calibrated based on the IPCC 2013 Wetlands Supplement Tier 1 emission factors and the scientific literature on rate of peatland emissions decay:

- Land use change = 621 t CO₂ ha⁻¹ 
- Land management = 37.3 t CO₂-eq ha⁻¹ yr⁻¹ 

The detailed model structure and derivation of these values is described in the [supplement](peatland_methodology_supplement.md). 

## Allocating emissions to crop years

We allocate emissions to crop years using the 20-year linearly-discounted lookback approach set by the GHGP Land Sector and Removals Standard (Section 7.2.1, Requirement 10). For now, we only run this calculation for 2020-allocated emissions, but it generalizes trivially.

Specifically, for each pixel, we create a weighted sum of transition emissions from each of the GLC epoch transitions: 2000→2005, 2005→2010, 2010→2015, 2015→2020, using the appropriate 2020 weights, and then add in any peatland land management emissions for 2020. That is:

```
2020_allocated_emissions = Σ (LUC_emissions[year] × weight[year]) + 2020_peatland_occupation_emissions
```

Where:

- `LUC_emissions[year]` is the total emissions (biomass + SOC + peatland transformation) from any transition in that year
- The sum is over all years from 2000 to 2020

The GHGP-prescribed linear discounting weights are calculated as:

```
weight(year) = 10.25% - 0.5% × years_since_conversion
```

Where `years_since_conversion = reporting_year - conversion_year + 1 (ranging from 1 to 20)`. Since our transition data only exists at GLAD epoch boundaries, we aggregate these 20 per-year weights into 4 epoch weights by averaging across the conversion years each epoch covers.

Each GLAD GLC map represents land cover at approximately the labeled year. A transition detected between consecutive maps therefore is predicted by the model to have occurred in one of the 5 years following the earlier map. We treat those 5 candidate conversion years as equally likely and use the unbiased estimator: the arithmetic mean of the 5 corresponding GHGP per-year weights. For the 2020 reporting year this gives:

| Transition | Conversion years | Mean years since | Weight |
| ---------- | ---------------- | ---------------- | ------ |
| 2000→2005  | 2001–2005        | 18               | 1.25%  |
| 2005→2010  | 2006–2010        | 13               | 3.75%  |
| 2010→2015  | 2011–2015        | 8                | 6.25%  |
| 2015→2020  | 2016–2020        | 3                | 8.75%  |

Note that these weights are *not* the GHGP weight evaluated at the continuous midpoint of each interval (e.g., 2017.5 for 2015→2020, which would give 8.50% rather than 8.75%). Because the GHGP weight schedule is defined on discrete integer years rather than as a continuous function of time, the unbiased per-year average is offset by 0.5 years from the continuous midpoint. For any individual pixel, the maximum residual timing error is ±2 years, corresponding to ±1 percentage point on the weight — negligible relative to the overall uncertainty of the model.

## Allocating emissions to crops

For each 30m pixel classified as cropland by GLAD GLC 2020 and as a row crop by CDL 2020, we:

1. Look up the CDL crop code
2. Map to crop group (corn, soybeans, wheat)
3. Apply the state-level average yield rate (kg/ha)
4. Multiply by pixel area in hectares

The CDL crop layer for 2020 is available on GEE at `USDA/NASS/CDL/2020`.

Because we apply crop yields only for those CDL pixels also identified as cropland by GLAD GLC 2020, we lose ~9% of total US production; however, since the excluded pixels are absent from both the emissions numerator and the production denominator, they should not significantly affect our final emissions factors on average. See Appendix 1 for details.

### Calculate crop yields

We use state-level USDA yield statistics to estimate 2020 crop production per pixel.  We use the USDA National Agricultural Statistics Service (NASS) QuickStats crops dataset, available at https://www.nass.usda.gov/datasets/, filtering for:

- `STATISTICCAT_DESC = 'YIELD'`
- `AGG_LEVEL_DESC = 'STATE'`
- `UNIT_DESC = 'BU / ACRE'`
- Commodities: CORN, SOYBEANS, WHEAT
- Years: 2017-2020

To smooth out outlier years in yields, we compute a 4-year arithmetic mean of state-level yields from 2017–2020. We apply this yield uniformly to all pixels of that crop in that state.

### Unit conversion

NASS reports yields in bushels per acre. We convert to kg/ha using standard bushel weights (USDA, 7 CFR 810):

| Crop | Bushel weight (lbs) | Bushel weight (kg) |
|------|--------------------|--------------------|
| Corn | 56 | 25.401 |
| Soybeans | 60 | 27.216 |
| Wheat | 60 | 27.216 |

```
yield_kg_per_ha = yield_bu_per_acre × bushel_weight_kg / HA_PER_ACRE
```

Where `HA_PER_ACRE = 0.40468564`.

### Calculate final EFs

For each (state, crop) combination, we compute the average emissions factor as:
```
EF[state, crop] = Σ(allocated_emissions[pixel]) / Σ(yield_kg_per_ha × pixel_area_ha) 
```
Where the sum runs over all 30m pixels classified as that crop by CDL 2020 and as cropland by GLAD GLC 2020 within the state. The numerator uses the 2020-allocated emissions from the previous section (linearly discounted LUC plus peatland land management). The denominator is total state production for that crop calculated as the per-ha yield x pixel area for the same pixel set.

National emissions factors are computed analogously by summing across CONUS.

## Final outputs

The pipeline produces two tables. Full schemas live in `pipeline_tech_design.md`.

- **`transitions`** — one row per (county, epoch_transition, emissions_type). Gives the area converted under each emissive transition (forest→cropland, short-veg→built-up, etc.), the gross emissions from that transition, and the 20-year linearly discounted allocation to 2020. Rolls up to per-state and CONUS totals, and to per-source-family or per-epoch summaries.
- **`crops`** — one row per (county, crop_group). Gives the per-crop emissions factor in kgCO2e per kg of crop, 2020 production and area, and the breakdown of allocated emissions by source family (forest, short-veg, peatland conversion, peatland occupation) and epoch.

## Appendix 1: GLAD GLC vs CDL row crop comparison

As described above, the methodology calculates emissions for pixels identified by GLC as cropland, using CDL to allocate among crops. This means that any CDL pixels not identified as cropland by GLAD GLC 2020 are excluded from both emissions and production. 

To test whether this exclusion likely biases our EFs, we computed confusion matrices crossing CDL row crop classification against GLAD GLC cropland (pixel value 244) for all 48 CONUS states + DC, comparing CDL 2020 × GLAD GLC 2020.

| | GLAD GLC cropland | GLAD GLC non-cropland | Total |
|---|---:|---:|---:|
| **CDL row crop** | 99,475,866 ha | 10,248,055 ha | 109,723,921 ha |
| **CDL not row crop** | 37,238,049 ha | 631,957,357 ha | 669,195,406 ha |
| **Total** | 136,713,915 ha | 642,205,412 ha | 778,919,327 ha |

90.7% of CDL row crop pixels are also identified as crops by GLAD. 

The 9.3% of "lost" CDL row crops are concentrated in regions with smaller, more fragmented fields:

| Region | GLAD GLC confirmation rate |
|---|---|
| Corn Belt (IA, IL, IN, NE, OH) | 91–95% |
| Great Plains (ND, SD, KS, MT) | 91–95% |
| Southeast (AL, FL, GA, SC) | 75–82% |
| Northeast (CT, MA, RI, PA) | 39–77% |

These pixels fall into two cases, which we analyzed for 10 key agricultural states, giving what we'd expect to be a representative picture for CONUS overall:

**GLC changed 2000→2020** (24% of excluded pixels)

| GLC 2000 source → GLC 2020 destination | Area (ha) | % of disagreement | Emissions relevance |
|---|---:|---:|---|
| Cropland → non-cropland | 1,130,028 | 21% | None |
| Short veg → non-cropland | 99,962 | 2% | Grassland conversion emissions |
| Forest → non-cropland | 32,392 | <1% | Forest conversion emissions |
| Wetland → non-cropland | 16,720 | <1% | Grassland/forest |
| Water/bare/other → non-cropland | 6,790 | <1% | Negligible |

**GLC stable 2000=2020** (76% of excluded pixels)

| GLC 2020 class (stable since 2000) | Area (ha) | % of disagreement | Emissions relevance |
|---|---:|---:|---|
| Built-up | 2,419,875 | 44% | None — farmsteads, grain bins, rural infrastructure. Zero biomass. |
| Short vegetation | 1,622,525 | 30% | Would generate grassland→cropland emissions if misclassified. |
| Forest | 77,956 | 1% | Would generate forest emissions if misclassified. |
| Wetland | 66,092 | 1% | Would generate forest or grassland emissions. |
| Water/bare/other | 8,429 | <1% | Negligible |

Because lost pixels are excluded from both the emissions numerator and the production denominator, what matters for EF accuracy is whether they are systematically different from the in-scope population. If so, it appears that it's in a way that biases the EF slightly upward rather than downward: only ~3% of lost pixels have a GLC history showing conversion from a non-crop source, compared with the ~6.5% conversion-from-non-crop rate observed among the in-scope CDL row crop population. The dropped pixels are, if anything, biased toward stable, non-converting land. 

See `analyses/cdl_glad_glc_comparison.ipynb` for the full analysis.

## Appendix 2: Areas for further research

This methodology is a first draft. We see a number of areas where further research could lead to additional improvements. These are listed in our estimated rough sense of impact/priority, though that certainly could be debated. Contributions would be particularly welcome on these topics.

### GLAD GLC vs Hansen TCL forest-detection globally

Although forest conversion emissions are very small in the US, they will become the dominant source of emissions as we expand globally. Our preliminary analysis (offline, not yet published in this repo) shows significant differences in the US in forest detections between the GLAD GLC layer we are using and the widely used Global Forest Watch tree cover loss dataset designed specifically for this purpose. There are few enough conversion events in the US that these disagreements could be noise. But knowing we want to extend globally over time, and with our land cover layer as the single most important methodology choice, we should do more investigation of this issue early. 

**Potential impact:** Minimal for US row-crop EFs; potentially substantial in other geographies.

### Higher resolution yield data

**Issue**: We currently use state-level USDA yield statistics uniformly across all pixels within a state. County-level data would provide better spatial resolution, but we have concerns about the quality and robustness of the data at that granularity. The task is to check quality and then potentially switch over. An even more ambitious version would be to evaluate pixel-level yield models, such as https://gee-community-catalog.org/projects/qdann/

**Potential impact:** This issue affects only the spatial distribution of emissions within each state, but could be significant at county-level.

### SOC data improvements

**Issue**: Two subissues, but closely related: (a) extending conversion-loss estimates from 30cm to 1m depth, and (b) switching 0–30cm SOC from current-state SoilGrids to Sanderman 2017 pre-disturbance reconstruction.

1.   **30cm-vs-1m depth.** The POC currently follows IPCC's Tier 1 calculation method: SoilGrids 0–30cm stock × Table 5.5 F_LU factors that were calibrated against paired-plot data to a 30cm depth (Annex 5A.1). Tier 1 explicitly "assumes management practice influences stocks to a depth of 30 cm" (§5.2.3.2), but acknowledges that sub-30cm losses are real and material, citing Angers et al. that "including soil C stock data below the depth of tillage is necessary to provide an accurate estimate." Sanderman 2017 and Spawn 2019 both find that the deeper-layer share of total cultivation loss is substantial — Sanderman reports global SOC losses of 37, 75, and 133 Pg C to 0.3 m, 1 m, and 2 m respectively (so the sub-30cm layer accounts for ~51% of cultivation loss in the 0–1 m column). To capture this loss we'd need to either (i) establish a defensible simple F_LU value at 1 m or (ii) adopt a Spawn-style carbon-response function over a depth-resolved soil map. Complex, but potentially worth it given that SOC represents the majority of carbon loss for US grassland conversion. Unfortunately, the simpler-looking shortcut of plugging a 1m SoilGrids stock into the current 30cm-calibrated F_LU is expressly prohibited by IPCC Vol 4 Ch 2 §2.3.3.1, which requires the SOC reference value and the stock-change factors to share a depth basis.
2.   **Current-state vs pre-disturbance stock.** The POC reads SoilGrids `ocs_mean` at the converted pixel — a post-cultivation value, not the native  stock IPCC Tier 1 imagines. Sanderman 2017 NoLU is a global 10 km raster of "what each pixel's SOC would be today if never cultivated", at the IPCC-canonical 0–30cm depth, and runs ~30% above POC-current across the Plains states. It could be a superior alternative, though requires further review.

**Potential impact:** If we adopted both changes, the expected combined effect on grassland per-event ΔC could be 2–3×, bringing CONUS pipelines area-average from ~68 tCO₂/ha toward Spawn's 190. We expect this would increase overall EFs for corn and soy by ~1.4×, and wheat by ~2.4× (since wheat has less peat-LM to dilute out the grassland impact).

### Peatland dataset choice

**Issue**: We use the GFW Global Peatlands raster composite. For CONUS, GFW uses Xu PEATMAP above 40°N; below 40°N it falls back to Gumbricht et al. (2017), a tropical-tuned hydrological model. This cutoff may be in the wrong spot for the temperate US — it affects Delaware, the mid-Atlantic, the Southeast, southern California, Arizona, New Mexico, and most of Texas. 

**Potential impact:**  National-level impact is likely modest because the Corn Belt sits above 40°N where Xu is the active source anyway; the issue concentrates in Southeast and mid-Atlantic states.

**Potential improvement path:** The most obvious potential fix would be to ingest Xu PEATMAP directly and build our own hybrid that uses it farther south than GFW, but there may be other better options.

### Short vegetation carbon stock for shrubland

**Issue:** GLAD GLC's "short vegetation" category (values 1–24) encodes only vegetation cover fraction (~7% to 100% cover), not vegetation type (Potapov et al., 2022). It does not distinguish grassland from shrubland. In the face of this limitation, the current methodology uses simple climate-zone-stratified carbon stock from the Houghton/BLUE bookkeeping parameterization for these pixels. The Houghton/BLUE values seem reasonably well-calibrated for herbaceous grassland (the dominant short vegetation type in US cropland conversion areas), but undercount AGB for woody shrubland: sagebrush steppe has 3–4 tC/ha (Fusco et al., 2019), and mature California chaparral has 17–28 tC/ha (Bohlman et al., 2018). Because most US short vegetation → cropland conversion occurs on Great Plains grassland rather than shrubland, the impact on national-level emission factors is likely small, but the undercount could be material for state-level factors in shrubland-heavy states, and when the methodology is extended globally.

**Potential impact:** Grassland conversion is the dominant driver of emissions for row crops in the United States. Even if shrubland is only 10-20% of the "short vegetation" conversions, the underestimate on those pixels could be large enough to matter. 

**Potential improvement path**: Two approaches, in increasing order of sophistication: (1) Overlay an auxiliary classification that distinguishes shrubland from grassland (e.g., ESA WorldCover at 10m or NLCD Shrub/Scrub class) and apply differentiated literature-based carbon densities (~6–12 tC/ha for sagebrush, ~17–28 tC/ha for chaparral, vs. the current 5–7 tC/ha for all short vegetation). (2) Replace the static lookup table entirely with satellite-derived, spatially explicit AGB estimates for non-forest vegetation, using a product like IB-AGC (Li et al., 2025) at 25km resolution, subtracting known forest and crop biomass contributions, and distributing the residual across 30m grassland/shrubland pixels as a continuous function of woody fractional cover from the Copernicus Global Land Service.

### Within-year double-cropping

**Issue**: In the mid-South (AR, TN, KY, MO Bootheel), mid-Atlantic (MD, DE, VA), and southern IL/IN, winter wheat is harvested in June–July and the same field is planted to "double-crop" soybeans, with both crops grown in the same calendar year. CDL encodes these as dedicated codes (e.g., 26 = Dbl Crop WinWht/Soybeans, plus several less common combinations: 225, 236, 238, 240, 254). The methodology is currently silent on the handling of these codes (and our current implementation only includes the single-crop codes (corn=1, soy=5, wheat=22/23/24), dropping double-cropped pixels out of both the emissions numerator and the production denominator). We should add a per-pixel attribution rule that splits emissions proportionally for the two component crops of within-year double cropping (e.g., 50/50 or by relative economic value), so that both the LUC emissions and the production yield are counted.

**Potential impact:** For _national_ emissions factors the impact is modest — dropped pixels exit both sides of the EF ratio symmetrically. For _state-level_ factors in the mid-South the impact may be larger: NASS reports double-crop soy is roughly 5–8% of US soybean acres nationally but can exceed 30% in Arkansas, and winter wheat in those same states is similarly affected. State EFs for soy and wheat in those states are missing a non-trivial chunk of production.

### Missing carbon sequestration

**Issue**: The methodology tracks only emissions from land use change, not carbon removals when cropland reverts to forest or grassland. This is consistent with the GHGP LSRS's approach, but it would be helpful to have carbon sequestration values available for comparing to national inventories or potentially for use of the dataset in LMU-level analyses. 

**Potential impact:** The US has had significant cropland→forest reversion (e.g. CRP enrollment, eastern reforestation). This is likely significant in any circumstance where GHGP allows these emissions to be counted.

### Dead organic matter mismatch to US-specific data

**Issue**: The CDM AR-TOOL-12 DOM factors for tropical forests appear to be an underestimate relative to US FIA field measurements, which show a national average of ~20 tC/ha total DOM (~10 tC/ha dead wood + ~10 tC/ha litter; Domke et al., 2016; Woodall et al., 2008) -- ~2.4x the 8.2 tC/ha typical value we calculated above. We've chosen to stick with the well-standardized and peer reviewed CDM AR-TOOL-12 approach for now, but it would be good to investigate and understand this difference.  One early hypothesis is a definitional  difference: FIA "forest floor" may include duff/humus (partially decomposed organic material above mineral soil), that is classified as soil rather than litter in the CDM/IPCC framework. Another factor could be that the 8.2 tC/ha "typical" value quoted above is below the area-weighted US average, which could be pulled up by outliers.

**Potential impact:** The ~2.4x gap between CDM AR-TOOL-12 (~8.2 tC/ha) and FIA measurements (~20 tC/ha) translates to ~43 tCO2/ha missing per forest pixel. That's roughly 8-12% of typical per-pixel forest conversion emissions. If forest conversion emissions are only 10% of total row crop emissions, that's ~1% of total emissions. But the gap may be partly definitional (FIA "forest floor" includes duff/humus classified as soil under IPCC), so the real impact could be considerably smaller. Worth investigating but uncertain.

### Harris above ground forest biomass: static year-2000 values

**Issue**: Harris et al. (2021) provides circa year-2000 biomass. For forest loss events in later years, actual biomass may differ due to growth or partial disturbance. 

**Potential impact:** Although grassland conversion is the dominant driver of U.S. row crop-driven emissions, forest conversion dominates per-hectare emissions where conversion events do occur. And although for US temperate forests 20 years of growth is a relatively small fraction of total standing biomass, the most recent transitions (2015-2020) get the highest allocation weights (8.75%) while also potentially having the largest AGB underestimate (potentially 15-30% growth over 15-20 years). Thus we could be underestimating total forest conversion emissions by 10-20% for recently cleared areas. 

### GLAD GLC forest definition vs. Accountability Framework 10% canopy threshold

**Issue**: The Accountability Framework / SBTi FLAG guidance provides specific rules on what degree of forest cover should be treated as forest. GLAD GLC defines forest by canopy height (values 25–48 = 3m to >25m trees). These definitions may not be perfectly equivalent. The task is to verify alignment and assess any differences in forest extent. Combined with the issues above, this might also push us to take a less categorical approach to estimation of above ground carbon stocks. The physical reality is that forest -> shrubland -> grassland is more a continuum than a set of discrete categories.

**Potential impact:** This shifts pixels between the forest category (high per-hectare emissions) and the short vegetation category (lower per-hectare emissions). The magnitude depends on how much area lies at the boundary between the two definitions. In US temperate forests, the 3m canopy height threshold probably captures most of what a 10% canopy cover threshold would — the ambiguous zone is likely sparse woodland and savanna edges. Maybe a few percent impact on total emissions, concentrated in transition zones. 

### GLAD GLC built-up classification is over-inclusive vs. NLCD

**Issue**: The GLAD GLCLUC v2 built-up class (value 250) is derived by a U-Net CNN trained on OpenStreetMap building and road data; its published validation (Potapov et al. 2022, Table 6) reports user's accuracy 63.7% ± 5 and producer's accuracy 39.1% ± 19.5 for stable built-up globally, indicating substantial omission of existing urban. CONUS-wide the over-inclusiveness is sharp in the opposite direction: GLAD 2020 built-up covers ~78.3 Mha vs. NLCD 2021 developed (classes 21–24) at ~31.5 Mha — a 2.5× mismatch. The GLAD signal appears to pick up rural infrastructure, small roads, and sub-pixel impervious that NLCD's developed classes exclude. This shows up directly in our pipeline as inflated `forest → built-up` and `short-veg → built-up` transition area (in Delaware, GLAD records 5.7× more forest→built-up than our prior Hansen-loss + CDL classification did, with ~60% of those "new built-up" pixels still reading as forest in 2023 per the legacy classifier).

**Potential impact:** State and national total LUC emissions are likely overstated by this effect, probably modest (built-up is a small fraction of total transitions) but systematic. Per-crop emissions factors are unaffected because the crop-EF numerator only counts allocated emissions on pixels that are cropland in 2020 — forest→built-up pixels don't enter the numerator. The direct impact is on state-total allocated LUC reporting and on any consumer of the `forest_to_built_up` or `short_veg_to_built_up` rows in the summary table. Worth cross-checking against NLCD in future validation work.

### Peat fire emissions

**Issue**: The two-phase peatland model excludes peat fire emissions entirely; episodic fires can dwarf annual oxidative losses in fire years.

**Potential impact:**  Peat fires are quite rare in the US -- maybe a few hectares per decade. Therefore this issue likely has negligible impact on US emission factors. For global extension (especially Indonesia), this would jump to the top of the list.

### References

- Angers, D.A. & Eriksen-Hamel, N.S. (2008). Full-inversion tillage and organic carbon distribution in soil profiles: a meta-analysis. Soil Science Society of America Journal 72(5), 1370–1374. https://doi.org/10.2136/sssaj2007.0342
- Fusco, E.J., Finn, J.T., Abatzoglou, J.T., Balch, J.K., Dadashi, S. & Bradley, B.A. (2019). Accounting for aboveground carbon storage in shrubland and woodland ecosystems in the Great Basin. Ecosphere 10(8), e02821. https://doi.org/10.1002/ecs2.2821
- Li, X., Ciais, P., Frappart, F. et al. (2025). IB-AGC: Annual 25 km global live biomass carbon product from SMOS L-band passive microwave vegetation optical depth. Scientific Data 12, 1156. https://doi.org/10.1038/s41597-025-05470-2
- Potapov, P., Hansen, M.C., Pickens, A. et al. (2022). The Global 2000–2020 Land Cover and Land Use Change Dataset Derived From the Landsat Archive: First Results. Frontiers in Remote Sensing 3, 856903. https://doi.org/10.3389/frsen.2022.856903
- Sanderman, J., Hengl, T. & Fiske, G.J. (2017). Soil carbon debt of 12,000 years of human land use. Proceedings of the National Academy of Sciences 114(36), 9575–9580. https://doi.org/10.1073/pnas.1706103114
- Spawn, S.A., Lark, T.J. & Gibbs, H.K. (2019). Carbon emissions from cropland expansion in the United States. Environmental Research Letters 14, 045009. https://doi.org/10.1088/1748-9326/ab0399

