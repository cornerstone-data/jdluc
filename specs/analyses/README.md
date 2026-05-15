# Analyses

This directory holds one-time analyses that informed methodology decisions for the main pipeline, preserved for traceability. We typically use Jupyter notebooks for this, which have several well-known drawbacks, but at least keep the analysis and the code that produced it tightly linked for future reference. The code may not actually run against the current production code branch, but it at least provides detailed documentation of what we did once upon a time. We also use this directory as a convenient spot for working files on ephemeral analysis branches.

**Catalog:**

- [`cdl_glad_glc_comparison.ipynb`](cdl_glad_glc_comparison.ipynb) — Quantifies disagreement between USDA CDL row crops and GLAD GLC cropland, decomposes the disagreement into emissions-relevant categories. Supports Appendix 1 of `specs/methodology.md`.
- [`peatland_emissions_modeling.ipynb`](peatland_emissions_modeling.ipynb) — Fits a two-pool decay model to Qiu et al. (2021), Swails et al. (2022), and IPCC peatland data, then parameterizes the GHGP LUC + LM peatland emissions model. Supports `specs/peatland_methodology_supplement.md`.

**Note for AI editors**: For non-trivial edits, consider writing a throwaway Python builder that emits the `.ipynb` and deleting it after — much easier than hand-editing JSON. But don't commit the builder: notebooks should stay the source of truth.