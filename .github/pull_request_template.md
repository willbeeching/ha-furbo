## What changes

<!-- User-visible or engineering outcome. -->

## Why

<!-- The failure mode or requirement being addressed. -->

## Invariants preserved

<!-- Ownership, teardown, read-after-write, availability, etc. -->

## Testing

- [ ] `ruff check` and `ruff format --check` pass
- [ ] `mypy --strict custom_components/furbo` passes
- [ ] Tests pass on the minimum lane (HA 2025.2)
- [ ] Tests pass on the latest lane
- [ ] Per-module coverage >= 95%, config flow 100%
- Hardware/live-service verification performed: <!-- model, firmware, what -->
- Not tested and why: <!-- ... -->

## Release / quality scale

- [ ] Version bump intended? <!-- yes/no -->
- [ ] quality_scale.yaml statuses updated with evidence? <!-- yes/no/n_a -->
