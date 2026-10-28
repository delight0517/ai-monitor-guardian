# Contributing

- Read `MODULE_MAP.md`, `MODULE_CONTRACT.md`, and `feature-ownership.json` before editing.
- Hold the smallest registered feature claim for the complete edit and review period using `scripts/hold_feature_claim.py`. Use the exclusive `integration` claim only after feature claims are released.
- Do not edit the same feature concurrently or merge divergent candidates automatically. Validate candidate hashes and focused tests before sequential integration.
- Run `python -m unittest discover -s tests -v` on Windows. Tests are source-level and fake-backed unless explicitly described otherwise.
- Do not install experimental display drivers on a host, change physical monitor topology, or move arbitrary user windows. Runtime driver work requires a disposable guest and separate review.
- Never submit credentials, user app data, private evidence, screenshots, local machine reports, or consumer application source to this repository.
