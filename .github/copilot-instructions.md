# Copilot Instructions – ai_charge

## What this is

Smart EV charging controller for Home Assistant. A Volvo EX30 charges overnight via a Zaptec Go 2 charger, load-balanced against house current (Perific sensor), price-optimized via Nord Pool SE3 hourly prices, guaranteed full by a configurable deadline (default 07:00).

## Architecture

Two-layer design:

1. **`pyscript/ev_charge_controller.py`** — The authoritative single-file pyscript deployed to Home Assistant (`/config/pyscript/`). Contains all HA integration: triggers, sensor reads, state exposure, Zaptec service calls, Nord Pool price fetching. Uses HA-specific globals (`state`, `task`, `log`, `zaptec`, `input_boolean`, etc.) that only exist inside the pyscript runtime — these are **not importable** in tests or standard Python.

2. **`pyscript/modules/algorithm.py`** — Legacy pure-Python algorithm extracted for unit testing. No HA dependencies. Contains load-balancing math, phase mode selection, deadline enforcement, and an older fixed-threshold price model. This module is imported by tests but is **not** used by the controller at runtime. When modifying charging logic, the controller is the source of truth — update `algorithm.py` and its tests only to keep test coverage aligned.

### Controller vs algorithm module

The controller has evolved past the algorithm module in two areas:

- **Charging strategy**: The controller uses 3-phase-only (8A minimum ensures all phases run or charging pauses entirely). The algorithm module still supports 1-phase fallback with a 6A hardware minimum.
- **Price optimization**: The controller fetches Nord Pool SE3 hourly prices via API and ranks hours by cost — charging happens during the cheapest hours needed to finish by the deadline. The algorithm module uses a simpler fixed `cheap_threshold` comparison.

## Phase mapping (critical)

The Zaptec Go 2 has a non-standard phase rotation (L3, L1, L2 — TN). This mapping is load-bearing throughout the codebase:

| Grid phase | Perific sensor | Zaptec sensor | Subtraction |
|---|---|---|---|
| L1 | `last_perific_last_current_l1` | `gpn007772_current_phase_2` | `house_l1 = perific_l1 − charger_phase2` |
| L2 | `last_perific_last_current_l2` | `gpn007772_current_phase_3` | `house_l2 = perific_l2 − charger_phase3` |
| L3 | `last_perific_last_current_l3` | `gpn007772_current_phase_1` | `house_l3 = perific_l3 − charger_phase1` |

Getting this wrong will cause overcurrent on fuses. Always preserve these cross-phase subtractions.

## Testing

```bash
# Full suite (28 tests)
pytest tests/ -v

# Single test
pytest tests/test_algorithm.py::TestDecideChargeMode::test_3phase_when_all_phases_have_headroom -v

# Single test class
pytest tests/test_algorithm.py::TestDecideChargeMode -v
```

Tests use `sys.path.insert(0, "pyscript/modules")` so run pytest from the repo root.

Only `pyscript/modules/algorithm.py` is testable. The controller (`ev_charge_controller.py`) depends on the HA pyscript runtime and cannot be tested outside HA.

## Deployment

```bash
# Deploy helpers and automations to HA via WebSocket API
pip install -r requirements.txt
python deploy.py --token YOUR_TOKEN [--host 192.168.68.88]

# Pyscript: manually copy ev_charge_controller.py to /config/pyscript/ on the HA instance
```

`deploy.py` is idempotent — safe to run repeatedly.

## Conventions

- **Versioning**: Every Python file carries an independent semver version in its docstring (e.g., `Version: 1.1.0`). Bump on every change following semver rules.
- **Charger limits**: Current is clamped to [8, 16] amps. The 8A minimum (`CHARGER_MIN_A = 8` in the controller) is intentional — it ensures all 3 phases sustain charging. If headroom drops below 8A on any phase, charging pauses entirely rather than falling back to 1-phase. The algorithm module still uses 6A (hardware minimum) for its 1-phase fallback logic.
- **Safety**: Never command below 8A on any active phase. If any phase headroom falls below 8A, pause charging entirely.
- **Price optimization**: Charge during the cheapest Nord Pool hours needed to meet the deadline (default 07:00). The system must never risk missing the deadline to save on price — deadline enforcement always wins.
- **Observability**: The controller exposes runtime state as `pyscript.ev_*` entities (e.g., `pyscript.ev_controller_status`, `pyscript.ev_headroom`, `pyscript.ev_schedule`) for dashboard visibility.
- **HA config as YAML reference**: Files in `ha_config/` are reference/documentation. The actual deployment is done programmatically by `deploy.py`.
