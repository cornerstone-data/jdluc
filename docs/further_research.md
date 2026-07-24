# Areas for further research

A supplement to [`methodology.md`](methodology.md): areas where further research could
improve the methodology.

This methodology is a first draft. We see a number of areas where further research could lead to additional improvements. These are listed in our estimated rough sense of impact/priority, though that certainly could be debated. Contributions would be particularly welcome on these topics.

## GLAD GLC vs Hansen TCL forest-detection globally

Although forest conversion emissions are very small in the US, they will become the dominant source of emissions as we expand globally. Our preliminary analysis (offline, not yet published in this repo) shows significant differences in the US in forest detections between the GLAD GLC layer we are using and the widely used Global Forest Watch tree cover loss dataset designed specifically for this purpose. There are few enough conversion events in the US that these disagreements could be noise. But knowing we want to extend globally over time, and with our land cover layer as the single most important methodology choice, we should do more investigation of this issue early.

**Potential impact:** Minimal for US row-crop EFs; potentially substantial in other geographies.

**Potential improvement path:** Localize where GLAD misses oil-palm-driven forest loss and benchmark GLAD forest detection against Global Forest Watch tree-cover-loss on those frontiers before relying on the global expansion.

## sLUC grassland attribution (expansion-share over-attribution)

**Issue**: The statistical (sLUC) leg attributes each coarse cell's conversion emissions to crops by their share of crop *expansion* within the cell (`statistical.py`; see §3, Statistical). A post-refactor validation found this hands corn/soy/wheat substantially more grassland conversion than the per-pixel jurisdictional-direct (jdLUC) leg does on the *same* emissions layer (grassland EF: corn 0.032 vs 0.012, soy 0.126 vs 0.054 kg CO₂e/kg), driving the US corn+soy+wheat total to ~76 MtCO₂e — the highest of the satellite EF models and above jdLUC (61). Grassland (30.9 Mt) is the largest single US source and the biggest change from the refactor, yet it has **no reliable external anchor** since WRI is forest-only. The one weak reference — EPA's whole-cropland Grassland→Cropland (16.3 Mt, *all* crops) — sits *below* the sLUC corn+soy+wheat *subset* (30.9), i.e. a subset nearly doubling the whole, though the comparison is muddied by a temporal-basis mismatch (EPA single-year 2022 annual vs sLUC 20-yr committed/discounted). Whether expansion-share allocation over-attributes grassland — or the per-pixel CDL leg under-attributes it — is unresolved.

**Potential impact:** Grassland is the dominant driver of US row-crop LUC emissions and the largest sLUC pool, so this directly sets the headline US emission factors and totals; resolving it could move the US corn+soy+wheat total by tens of MtCO₂e. It is also the largest source of divergence between the two attribution legs (state-level sLUC/jdLUC spans 0.19–39×).

**Potential improvement path:** (1) Reconcile the time bases (annualize sLUC or accumulate the EPA annual figure) so the EPA grassland baseline becomes a like-for-like check rather than a subset-vs-whole comparison. (2) Anchor grassland — and peat, which is equally unanchored (WRI forest-only) — externally and non-circularly: biome-stratified grassland carbon from Spawn et al. (2019) / IPCC, and IPCC drained-organic-soil (peat) emission factors from the 2013 Wetlands Supplement. This is the only route to a validation that does not lean on sLUC's own carbon densities. (3) Stress-test the expansion-share rule itself — in particular its behavior on cells where the converted pixel's CDL destination is non-crop — against the jdLUC state-level head-to-head.

## Higher resolution yield data

**Issue**: We currently use state-level USDA yield statistics uniformly across all pixels within a state. County-level data would provide better spatial resolution, but we have concerns about the quality and robustness of the data at that granularity. The task is to check quality and then potentially switch over. An even more ambitious version would be to evaluate pixel-level yield models, such as https://gee-community-catalog.org/projects/qdann/

**Potential impact:** This issue affects only the spatial distribution of emissions within each state, but could be significant at county-level.

## SOC data improvements

**Issue**: Two subissues, but closely related: (a) extending conversion-loss estimates from 30cm to 1m depth, and (b) switching 0–30cm SOC from current-state SoilGrids to Sanderman 2017 pre-disturbance reconstruction.

1.   **30cm-vs-1m depth.** The POC currently follows IPCC's Tier 1 calculation method: SoilGrids 0–30cm stock × Table 5.5 F_LU factors that were calibrated against paired-plot data to a 30cm depth (Annex 5A.1). Tier 1 explicitly "assumes management practice influences stocks to a depth of 30 cm" (§5.2.3.2), but acknowledges that sub-30cm losses are real and material, citing Angers et al. that "including soil C stock data below the depth of tillage is necessary to provide an accurate estimate." Sanderman 2017 and Spawn 2019 both find that the deeper-layer share of total cultivation loss is substantial — Sanderman reports global SOC losses of 37, 75, and 133 Pg C to 0.3 m, 1 m, and 2 m respectively (so the sub-30cm layer accounts for ~51% of cultivation loss in the 0–1 m column). To capture this loss we'd need to either (i) establish a defensible simple F_LU value at 1 m or (ii) adopt a Spawn-style carbon-response function over a depth-resolved soil map. Complex, but potentially worth it given that SOC represents the majority of carbon loss for US grassland conversion. Unfortunately, the simpler-looking shortcut of plugging a 1m SoilGrids stock into the current 30cm-calibrated F_LU is expressly prohibited by IPCC Vol 4 Ch 2 §2.3.3.1, which requires the SOC reference value and the stock-change factors to share a depth basis.
2.   **Current-state vs pre-disturbance stock.** The POC reads SoilGrids `ocs_mean` at the converted pixel — a post-cultivation value, not the native  stock IPCC Tier 1 imagines. Sanderman 2017 NoLU is a global 10 km raster of "what each pixel's SOC would be today if never cultivated", at the IPCC-canonical 0–30cm depth, and runs ~30% above POC-current across the Plains states. It could be a superior alternative, though requires further review.

**Potential impact:** If we adopted both changes, the expected combined effect on grassland per-event ΔC could be 2–3×, bringing CONUS pipelines area-average from ~68 tCO₂/ha toward Spawn's 190. We expect this would increase overall EFs for corn and soy by ~1.4×, and wheat by ~2.4× (since wheat has less peat-LM to dilute out the grassland impact).

## Peatland dataset choice

**Issue**: We use the GFW Global Peatlands raster composite. For CONUS, GFW uses Xu PEATMAP above 40°N; below 40°N it falls back to Gumbricht et al. (2017), a tropical-tuned hydrological model. This cutoff may be in the wrong spot for the temperate US — it affects Delaware, the mid-Atlantic, the Southeast, southern California, Arizona, New Mexico, and most of Texas.

**Potential impact:**  National-level impact is likely modest because the Corn Belt sits above 40°N where Xu is the active source anyway; the issue concentrates in Southeast and mid-Atlantic states.

**Potential improvement path:** The most obvious potential fix would be to ingest Xu PEATMAP directly and build our own hybrid that uses it farther south than GFW, but there may be other better options.

## Short vegetation carbon stock for shrubland

**Issue:** GLAD GLC's "short vegetation" category (values 1–24) encodes only vegetation cover fraction (~7% to 100% cover), not vegetation type (Potapov et al., 2022). It does not distinguish grassland from shrubland. In the face of this limitation, the current methodology uses simple climate-zone-stratified carbon stock from the Houghton/BLUE bookkeeping parameterization for these pixels. The Houghton/BLUE values seem reasonably well-calibrated for herbaceous grassland (the dominant short vegetation type in US cropland conversion areas), but undercount AGB for woody shrubland: sagebrush steppe has 3–4 tC/ha (Fusco et al., 2019), and mature California chaparral has 17–28 tC/ha (Bohlman et al., 2018). Because most US short vegetation → cropland conversion occurs on Great Plains grassland rather than shrubland, the impact on national-level emission factors is likely small, but the undercount could be material for state-level factors in shrubland-heavy states, and when the methodology is extended globally.

**Potential impact:** Grassland conversion is the dominant driver of emissions for row crops in the United States. Even if shrubland is only 10-20% of the "short vegetation" conversions, the underestimate on those pixels could be large enough to matter.

**Potential improvement path**: Two approaches, in increasing order of sophistication: (1) Overlay an auxiliary classification that distinguishes shrubland from grassland (e.g., ESA WorldCover at 10m or NLCD Shrub/Scrub class) and apply differentiated literature-based carbon densities (~6–12 tC/ha for sagebrush, ~17–28 tC/ha for chaparral, vs. the current 5–7 tC/ha for all short vegetation). (2) Replace the static lookup table entirely with satellite-derived, spatially explicit AGB estimates for non-forest vegetation, using a product like IB-AGC (Li et al., 2025) at 25km resolution, subtracting known forest and crop biomass contributions, and distributing the residual across 30m grassland/shrubland pixels as a continuous function of woody fractional cover from the Copernicus Global Land Service.

## Within-year double-cropping

**Issue**: In the mid-South (AR, TN, KY, MO Bootheel), mid-Atlantic (MD, DE, VA), and southern IL/IN, winter wheat is harvested in June–July and the same field is planted to "double-crop" soybeans, with both crops grown in the same calendar year. CDL encodes these as dedicated codes (e.g., 26 = Dbl Crop WinWht/Soybeans, plus several less common combinations: 225, 236, 238, 240, 254). The methodology is currently silent on the handling of these codes (and our current implementation only includes the single-crop codes (corn=1, soy=5, wheat=22/23/24), dropping double-cropped pixels out of both the emissions numerator and the production denominator). We should add a per-pixel attribution rule that splits emissions proportionally for the two component crops of within-year double cropping (e.g., 50/50 or by relative economic value), so that both the LUC emissions and the production yield are counted.

**Potential impact:** For _national_ emissions factors the impact is modest — dropped pixels exit both sides of the EF ratio symmetrically. For _state-level_ factors in the mid-South the impact may be larger: NASS reports double-crop soy is roughly 5–8% of US soybean acres nationally but can exceed 30% in Arkansas, and winter wheat in those same states is similarly affected. State EFs for soy and wheat in those states are missing a non-trivial chunk of production.

## Missing carbon sequestration

**Issue**: The methodology tracks only emissions from land use change, not carbon removals when cropland reverts to forest or grassland. This is consistent with the GHGP LSRS's approach, but it would be helpful to have carbon sequestration values available for comparing to national inventories or potentially for use of the dataset in LMU-level analyses.

**Potential impact:** The US has had significant cropland→forest reversion (e.g. CRP enrollment, eastern reforestation). This is likely significant in any circumstance where GHGP allows these emissions to be counted.

## Dead organic matter mismatch to US-specific data

**Issue**: The CDM AR-TOOL-12 DOM factors for tropical forests appear to be an underestimate relative to US FIA field measurements, which show a national average of ~20 tC/ha total DOM (~10 tC/ha dead wood + ~10 tC/ha litter; Domke et al., 2016; Woodall et al., 2008) -- ~2.4x the 8.2 tC/ha typical value we calculated above. We've chosen to stick with the well-standardized and peer reviewed CDM AR-TOOL-12 approach for now, but it would be good to investigate and understand this difference.  One early hypothesis is a definitional  difference: FIA "forest floor" may include duff/humus (partially decomposed organic material above mineral soil), that is classified as soil rather than litter in the CDM/IPCC framework. Another factor could be that the 8.2 tC/ha "typical" value quoted above is below the area-weighted US average, which could be pulled up by outliers.

**Potential impact:** The ~2.4x gap between CDM AR-TOOL-12 (~8.2 tC/ha) and FIA measurements (~20 tC/ha) translates to ~43 tCO2/ha missing per forest pixel. That's roughly 8-12% of typical per-pixel forest conversion emissions. If forest conversion emissions are only 10% of total row crop emissions, that's ~1% of total emissions. But the gap may be partly definitional (see above), so the real impact could be considerably smaller. Worth investigating but uncertain.

## Harris above ground forest biomass: static year-2000 values

**Issue**: Harris et al. (2021) provides circa year-2000 biomass. For forest loss events in later years, actual biomass may differ due to growth or partial disturbance.

**Potential impact:** Although grassland conversion is the dominant driver of U.S. row crop-driven emissions, forest conversion dominates per-hectare emissions where conversion events do occur. And although for US temperate forests 20 years of growth is a relatively small fraction of total standing biomass, the most recent transitions (2015-2020) get the highest allocation weights (8.75%) while also potentially having the largest AGB underestimate (potentially 15-30% growth over 15-20 years). Thus we could be underestimating total forest conversion emissions by 10-20% for recently cleared areas.

## GLAD GLC forest definition vs. Accountability Framework 10% canopy threshold

**Issue**: The Accountability Framework / SBTi FLAG guidance provides specific rules on what degree of forest cover should be treated as forest. GLAD GLC defines forest by canopy height (values 25–48 = 3m to >25m trees). These definitions may not be perfectly equivalent. The task is to verify alignment and assess any differences in forest extent. Combined with the issues above, this might also push us to take a less categorical approach to estimation of above ground carbon stocks. The physical reality is that forest -> shrubland -> grassland is more a continuum than a set of discrete categories.

**Potential impact:** This shifts pixels between the forest category (high per-hectare emissions) and the short vegetation category (lower per-hectare emissions). The magnitude depends on how much area lies at the boundary between the two definitions. In US temperate forests, the 3m canopy height threshold probably captures most of what a 10% canopy cover threshold would — the ambiguous zone is likely sparse woodland and savanna edges. Maybe a few percent impact on total emissions, concentrated in transition zones.

## GLAD GLC built-up classification is over-inclusive vs. NLCD

**Issue**: The GLAD GLCLUC v2 built-up class (value 250) is derived by a U-Net CNN trained on OpenStreetMap building and road data; its published validation (Potapov et al. 2022, Table 6) reports user's accuracy 63.7% ± 5 and producer's accuracy 39.1% ± 19.5 for stable built-up globally, indicating substantial omission of existing urban. CONUS-wide the over-inclusiveness is sharp in the opposite direction: GLAD 2020 built-up covers ~78.3 Mha vs. NLCD 2021 developed (classes 21–24) at ~31.5 Mha — a 2.5× mismatch. The GLAD signal appears to pick up rural infrastructure, small roads, and sub-pixel impervious that NLCD's developed classes exclude. This shows up directly in our pipeline as inflated `forest → built-up` and `short-veg → built-up` transition area (in Delaware, GLAD records 5.7× more forest→built-up than our prior Hansen-loss + CDL classification did, with ~60% of those "new built-up" pixels still reading as forest in 2023 per the legacy classifier).

**Potential impact:** State and national total LUC emissions are likely overstated by this effect, probably modest (built-up is a small fraction of total transitions) but systematic. Per-crop emissions factors are unaffected because the crop-EF numerator only counts allocated emissions on pixels that are cropland in 2020 — forest→built-up pixels don't enter the numerator. The direct impact is on state-total allocated LUC reporting and on any consumer of the `forest_to_built_up` or `short_veg_to_built_up` rows in the summary table. Worth cross-checking against NLCD in future validation work.

## Peat fire emissions

**Issue**: The two-phase peatland model excludes peat fire emissions entirely; episodic fires can dwarf annual oxidative losses in fire years.

**Potential impact:**  Peat fires are quite rare in the US -- maybe a few hectares per decade. Therefore this issue likely has negligible impact on US emission factors. For global extension (especially Indonesia), this would jump to the top of the list.

## References

- Angers, D.A. & Eriksen-Hamel, N.S. (2008). Full-inversion tillage and organic carbon distribution in soil profiles: a meta-analysis. Soil Science Society of America Journal 72(5), 1370–1374. https://doi.org/10.2136/sssaj2007.0342
- Bohlman, G.N., Underwood, E.C. & Safford, H.D. (2018). Estimating biomass in California's chaparral and coastal sage scrub shrublands. Madroño 65, 28–46.
- Domke, G.M. et al. (2016). Estimating litter carbon stocks on forest land in the United States. Science of the Total Environment 557–558, 469–478. https://doi.org/10.1016/j.scitotenv.2016.03.090
- Fusco, E.J., Finn, J.T., Abatzoglou, J.T., Balch, J.K., Dadashi, S. & Bradley, B.A. (2019). Accounting for aboveground carbon storage in shrubland and woodland ecosystems in the Great Basin. Ecosphere 10(8), e02821. https://doi.org/10.1002/ecs2.2821
- Gumbricht, T., Roman-Cuesta, R.M., Verchot, L., Herold, M., Wittmann, F., Householder, E., Herold, N. & Murdiyarso, D. (2017). An expert system model for mapping tropical wetlands and peatlands reveals South America as the largest contributor. Global Change Biology 23(9), 3581–3599. https://doi.org/10.1111/gcb.13689
- IPCC (2014). 2013 Supplement to the 2006 IPCC Guidelines for National Greenhouse Gas Inventories: Wetlands (Hiraishi, T. et al., eds.). IPCC, Switzerland. [Drained organic (peat) soil emission factors.]
- Li, X., Ciais, P., Frappart, F. et al. (2025). IB-AGC: Annual 25 km global live biomass carbon product from SMOS L-band passive microwave vegetation optical depth. Scientific Data 12, 1156. https://doi.org/10.1038/s41597-025-05470-2
- Potapov, P., Hansen, M.C., Pickens, A. et al. (2022). The Global 2000–2020 Land Cover and Land Use Change Dataset Derived From the Landsat Archive: First Results. Frontiers in Remote Sensing 3, 856903. https://doi.org/10.3389/frsen.2022.856903
- Sanderman, J., Hengl, T. & Fiske, G.J. (2017). Soil carbon debt of 12,000 years of human land use. Proceedings of the National Academy of Sciences 114(36), 9575–9580. https://doi.org/10.1073/pnas.1706103114
- Spawn, S.A., Lark, T.J. & Gibbs, H.K. (2019). Carbon emissions from cropland expansion in the United States. Environmental Research Letters 14, 045009. https://doi.org/10.1088/1748-9326/ab0399
- Woodall, C.W., Heath, L.S. & Smith, J.E. (2008). National inventories of down and dead woody material forest carbon stocks in the United States: Challenges and opportunities. Forest Ecology and Management 256(3), 221–228. https://doi.org/10.1016/j.foreco.2008.04.003
