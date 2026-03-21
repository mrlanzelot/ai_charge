# Smart EV Charging Algorithm – Implementation Plan

## Problem Statement
Automatically charge a **Volvo EX30** (69 kWh battery, 90% target) via a **Zaptec Go 2**
charger, keeping total current per phase ≤ 18A (house fuses), and ensuring the car is
fully charged by **06:00** every morning.

## Current System State (HA v2026.3.0)

| Component | Key entities |
|---|---|
| Zaptec Go 2 | `number.gpn007772_charger_max_current` (0–16A), `sensor.gpn007772_current_phase_1/2/3`, `sensor.gpn007772_charger_mode`, `switch.gpn007772_charging`, `zaptec.limit_current` service |
| Perific load balancer | `sensor.last_perific_last_current_l1/l2/l3` (total house current incl. charger) |
| Volvo EX30 | `sensor.volvo_ex30_battery` (%), `sensor.volvo_ex30_battery_capacity` (69 kWh), `sensor.volvo_ex30_target_battery_charge_level` (90%), `sensor.volvo_ex30_estimated_charging_time` (min) |
| Nord Pool | `sensor.nord_pool_se3_current_price` (SEK/kWh) |
| Existing webhook | `automation.send_current_sensors_to_python` → `rest_command.python_webhook` |

## Phase Mapping – CONFIRMED ✅
Zaptec Go 2 uses **phase rotation L3, L1, L2 (TN)**:

| Zaptec sensor | Grid phase | Perific sensor |
|---|---|---|
| `sensor.gpn007772_current_phase_1` | Grid **L3** | `sensor.last_perific_last_current_l3` |
| `sensor.gpn007772_current_phase_2` | Grid **L1** | `sensor.last_perific_last_current_l1` |
| `sensor.gpn007772_current_phase_3` | Grid **L2** | `sensor.last_perific_last_current_l2` |

## Core Algorithm

```
# Phase-corrected house load isolation
house_load_l1 = perific_l1 - charger_phase2   # L1 carries Zaptec phase 2
house_load_l2 = perific_l2 - charger_phase3   # L2 carries Zaptec phase 3
house_load_l3 = perific_l3 - charger_phase1   # L3 carries Zaptec phase 1

headroom_l1   = max_house_current - house_load_l1
headroom_l2   = max_house_current - house_load_l2
headroom_l3   = max_house_current - house_load_l3

# ── Phase mode decision ─────────────────────────────────────────────────────
# Charger constraint: all active phases MUST use the same current.
# Options: 3-phase (all same current) OR 1-phase (one phase only).
# Choose whichever delivers the most total power.

three_phase_current = clamp(min(headroom_l1, headroom_l2, headroom_l3), 6, 16)
three_phase_power   = three_phase_current * 3 * 230          # W  (or 0 if < 6A)

best_phase, best_headroom = phase with max(headroom_l1, headroom_l2, headroom_l3)
single_phase_current = clamp(best_headroom, 6, 16)
single_phase_power   = single_phase_current * 1 * 230        # W  (or 0 if < 6A)

# Prefer 3-phase when it delivers more power; fall back to 1-phase
IF min(headroom_l1, headroom_l2, headroom_l3) >= 6:
    IF three_phase_power >= single_phase_power:
        mode = "3-phase",  current = three_phase_current
    ELSE:
        mode = "1-phase",  current = single_phase_current, phase = best_phase
ELIF best_headroom >= 6:
    mode = "1-phase",  current = single_phase_current, phase = best_phase
ELSE:
    PAUSE charging entirely (all phases below 6A minimum)

# ── Deadline enforcement ────────────────────────────────────────────────────
hours_to_06          = time remaining until 06:00 (hours)
active_phases        = 3 if mode == "3-phase" else 1
energy_remaining     = (target_soc - current_soc) / 100 * 69.0   # kWh
min_for_deadline     = energy_remaining / (hours_to_06 * active_phases * 0.230)

# ── Nord Pool price optimization ────────────────────────────────────────────
# Fetch hourly prices via API, rank by cheapness, pick N cheapest hours
# N = hours needed at min current (6A) with 20% buffer
IF current_hour in cheapest_N_hours:
    final_current = min_for_deadline_spread_over_cheap_hours
ELIF charger in connected_requesting:
    WAIT (don't start charging in expensive hour)
ELSE:
    continue at current rate (don't stop mid-charge)

final_current = clamp(final_current, 6, 16)
```

**Crossover rule:** 3-phase beats 1-phase when `min_headroom ≥ max_headroom / 3`  
(e.g., headrooms 10/8/9 → 3-phase at 8A = 5,520W beats 1-phase at 10A = 2,300W)

**Key insight:** Perific measures total current (house + charger). Subtracting the
charger's measured current isolates house-only load, giving exact available headroom.

## Zaptec API – Phase Mode Control

The `zaptec.limit_current` service supports per-phase limits when called with
`available_current_phase1/2/3` (all three must be provided):

| Desired mode | Service call |
|---|---|
| 3-phase at X A | `available_current: X` |
| 1-phase on Zaptec P1 (grid L3) | `available_current_phase1: X, phase2: 0, phase3: 0` |
| 1-phase on Zaptec P2 (grid L1) | `available_current_phase1: 0, phase2: X, phase3: 0` |
| 1-phase on Zaptec P3 (grid L2) | `available_current_phase1: 0, phase2: 0, phase3: X` |
| Pause charging | `available_current: 0` + `switch.gpn007772_charging: off` |

> ⚠️ **Phase switching caveat**: Some EVs require cable re-negotiation when switching
> between 1-phase and 3-phase. The Volvo EX30 behavior on phase switching should be
> tested. Add hysteresis (don't switch modes more often than every 5 minutes) to avoid
> instability.

## Architecture

- **Logic engine**: `pyscript` (Python inside HA, installed via HACS)
  - Best balance: real Python expressiveness + native HA entity access
  - No external server required
  - All config exposed as HA input helpers for dashboard tuning
- **Triggers**: on Perific sensor state change + every 30s fallback timer
- **Output**: `zaptec.limit_current` service call
  - 3-phase mode: `available_current: X`
  - 1-phase mode: `available_current_phase1/2/3` with two phases set to 0
- **Hysteresis**: minimum 5 minutes between phase-mode switches to avoid EV renegotiation instability

## Configuration Helpers (input_* entities)

| Entity | Default | Purpose |
|---|---|---|
| `input_datetime.ev_charge_deadline` | 06:00 | Car must be full by this time |
| `input_number.ev_target_soc` | 90% | Charge target (%) |
| `input_number.ev_max_house_current` | 18A | Safety margin (actual fuses: 20A) |
| `input_boolean.ev_smart_charging_enabled` | on | Manual kill switch |
| `input_number.ev_charging_current_setpoint` | read-only | Current applied setpoint (for dashboard) |
| `input_select.ev_charging_mode` | read-only | Current mode: "3-phase", "1-phase-L1/L2/L3", "paused" |
| `input_number.ev_phase_switch_hysteresis_min` | 5 min | Min minutes between phase-mode switches |

> **Removed**: `input_number.ev_cheap_price_threshold` – superseded by Nord Pool API-based hourly ranking (no fixed threshold needed).

## Implementation Todos

### T1 – Create input helpers in HA
Create all `input_*` helpers via HA UI (Settings → Helpers) or YAML.

### T2 – Install pyscript via HACS
Install pyscript integration. Enable `allow_all_imports` in config.

### T3 – Implement core algorithm (`ev_charge_controller.py`)
Python script placed in `<config>/pyscript/ev_charge_controller.py`.
- Trigger: state change on Perific sensors + 30s timer
- Read all input helpers for config
- Calculate per-phase headroom (with confirmed phase mapping)
- **Decide mode**: 3-phase vs 1-phase based on which delivers more total power
- Apply deadline enforcement and Nord Pool price logic
- Call `zaptec.limit_current` with correct mode (single `available_current` for 3-phase, per-phase fields for 1-phase)
- Enforce 5-minute hysteresis on phase-mode switches
- Pause charging (switch off) if all headrooms < 6A

### T4 – Write unit tests (`tests/test_algorithm.py`)
Pure Python tests, no HA dependency. Cover:
- Normal night charging headroom calculation
- 3-phase vs 1-phase mode selection (power comparison)
- Phase-mode crossover boundary (min_headroom = max_headroom/3)
- Deadline urgency override
- Phase mapping correctness
- Edge cases: car full, car disconnected, all headrooms < 6A, one phase < 6A

### T5 – Create HA automations
- **`ev_charge_start_night`**: At 22:00, if car connected → enable smart charging
- **`ev_charge_stop_morning`**: At 06:00 or SOC ≥ target → disable smart charging
- **`ev_charge_deadline_warning`**: At 03:00, notify if car won't reach target by 06:00

### T6 – Verify phase mapping ✅ DONE
Zaptec phase rotation L3, L1, L2 (TN) confirmed from Zaptec Go app.

### T7 – Create dashboard card
Lovelace card showing:
- SOC bar (current vs target)
- Estimated completion time
- Per-phase current (house load vs charger contribution)
- Smart charging status + manual override toggle
- Current price vs threshold indicator

### T8 – Nord Pool price optimization ✅ DONE
Implemented via Nord Pool API (hourly price ranking, not fixed threshold).
`cheap_price_threshold` input helper no longer used — to be removed.

### T9 – Phase rotation change & 1-phase re-introduction (UPCOMING)
User will physically change Zaptec phase rotation wiring so 1-phase mode lands
on the least-loaded grid phase. After the change:
1. Update phase mapping constants in `ev_charge_controller.py`
2. Re-enable dynamic 3/1-phase switching with hysteresis
3. Keep `ev_phase_switch_hysteresis_min` and `ev_charging_mode` (1-phase options)

### T10 – Remove `cheap_price_threshold`
Delete unused `input_number.ev_cheap_price_threshold` from:
- `ha_config/input_helpers.yaml`
- `deploy.py`
- `ha_config/dashboard.yaml`
- HA instance (entity)

## Safety Rules
- Never command below 6A (Zaptec hardware minimum) on any active phase
- If all phases have headroom < 6A → pause charging entirely
- If only some phases have headroom < 6A → fall back to best single-phase if ≥ 6A
- Minimum 5-minute hysteresis between 3-phase ↔ 1-phase mode switches
- All changes logged to HA logbook with mode, current, and reason
- `input_boolean.ev_smart_charging_enabled = off` instantly halts all adjustments

## Time-Window Logic
- Smart control active: **22:00 – 06:00**
- Outside window + car connected: set to max (16A) – daytime fast charge
- Late arrival (after midnight, low SOC): algorithm runs immediately regardless

## Files to Create
```
/home/martin/dev/ai_charge/
├── PLAN.md                           # This file
├── pyscript/
│   └── ev_charge_controller.py      # Core algorithm (deploy to HA pyscript folder)
├── ha_config/
│   ├── input_helpers.yaml           # input_number / input_boolean / input_datetime
│   ├── automations.yaml             # Night start/stop + deadline warning automations
│   └── dashboard.yaml               # Lovelace card config
├── tests/
│   └── test_algorithm.py            # Unit tests (pure Python, no HA dependency)
└── README.md                        # Setup and deployment guide
```
