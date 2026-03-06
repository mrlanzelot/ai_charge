# Smart EV Charging – ai_charge

Intelligent overnight EV charging for a **Volvo EX30** via a **Zaptec Go 2** charger,
managed by Home Assistant with a Perific load balancer and Nord Pool electricity pricing.

## What it does

- **Load balances** charging current so total house current stays below 18 A per phase
- **Guarantees** the car is fully charged by 06:00 every morning
- **Optimises cost** by charging at full speed during cheap Nord Pool hours and throttling during expensive hours
- **Switches** between 3-phase and 1-phase charging to maximise delivered power
- All parameters are **adjustable from the HA dashboard** – no code changes needed

## Hardware

| Device | Role |
|---|---|
| Zaptec Go 2 (GPN007772) | EV charger (6–16 A per phase, 3-phase or 1-phase) |
| Perific | Whole-house current monitor (L1, L2, L3) |
| Volvo EX30 | EV (69 kWh, 90% charge target) |

**Phase wiring (Zaptec rotation L3, L1, L2 – TN):**

| Grid phase | Carries | Perific sensor | Zaptec sensor |
|---|---|---|---|
| L1 | Zaptec Phase 2 | `last_perific_last_current_l1` | `gpn007772_current_phase_2` |
| L2 | Zaptec Phase 3 | `last_perific_last_current_l2` | `gpn007772_current_phase_3` |
| L3 | Zaptec Phase 1 | `last_perific_last_current_l3` | `gpn007772_current_phase_1` |

## Project structure

```
ai_charge/
├── pyscript/
│   └── algorithm.py              ← Core algorithm (pure Python, no HA dependency)
├── ha_config/
│   ├── input_helpers.yaml        ← Helper definitions (reference / manual fallback)
│   ├── automations.yaml          ← HA automation YAML snippets
│   └── dashboard.yaml            ← Lovelace dashboard card
├── tests/
│   └── test_algorithm.py         ← 28 pytest unit tests
├── deploy.py                     ← Creates helpers + automations in HA
├── requirements.txt              ← Runtime: websockets
├── requirements-dev.txt          ← Dev: pytest
└── PLAN.md                       ← Full design document
```

The algorithm logic lives in `pyscript/algorithm.py` and is also deployed to
`/home/martin/dev/ha_pipeline/src/algorithm.py` for use by the ha_pipeline service.

## Architecture

```
Perific/Zaptec sensor change
        │
        ▼
HA WebSocket event (state_changed)
        │
        ▼
ha_pipeline service (192.168.68.114:8787)  ← always running, subscribed
        │  algorithm.py + ev_charger.py
        ▼
zaptec.limit_current via HA REST API
        │
        ▼
Zaptec Go 2 adjusts charging current
```

The `ha_pipeline` service (`/home/martin/dev/ha_pipeline`) runs as a **systemd user
service** and subscribes to HA's WebSocket to receive sensor changes in real time.
It also runs the algorithm every 5 minutes for deadline enforcement.

## Setup

### 1. Deploy helpers and automations

```bash
pip install -r requirements.txt
python deploy.py
```

This creates all input helpers and 4 automations in HA. It is **idempotent** – safe to
run multiple times.

### 2. ha_pipeline service (EV algorithm runner)

The EV charging algorithm runs in the `ha_pipeline` service on this machine.
It is already installed as a systemd user service:

```bash
systemctl --user status ha_pipeline.service   # check status
systemctl --user restart ha_pipeline.service  # restart after code changes
journalctl --user -u ha_pipeline.service -f   # follow logs
```

Log file: `/home/martin/dev/ha_pipeline/log/ha_bridge.log`

**Key env vars in `/home/martin/dev/ha_pipeline/.env`:**
- `HA_BASE_URL=http://192.168.68.88:8123`
- `HA_TOKEN=<long-lived token>`
- `HA_WS_URL=ws://192.168.68.88:8123/api/websocket`

### 3. Verify

## Configuration

All settings are adjustable from the HA dashboard with no code changes:

| Helper | Default | Description |
|---|---|---|
| `input_boolean.ev_smart_charging_enabled` | on | Master on/off switch |
| `input_datetime.ev_charge_deadline` | 06:00 | Car must be full by this time |
| `input_number.ev_target_soc` | 90% | Charge target SOC |
| `input_number.ev_max_house_current` | 18 A | Safety margin (actual fuses: 20 A) |
| `input_number.ev_cheap_price_threshold` | 0.80 SEK/kWh | Below = charge at max safe speed |
| `input_number.ev_phase_switch_hysteresis_min` | 5 min | Min time between 3-phase ↔ 1-phase switches |

## Algorithm

```
# 1. Isolate house-only load (subtract charger contribution, phase-corrected)
house_l1 = perific_l1 − charger_phase2
house_l2 = perific_l2 − charger_phase3
house_l3 = perific_l3 − charger_phase1

# 2. Compute headroom per phase
headroom_lX = fuse_limit − house_lX          # default fuse_limit = 18 A

# 3. Choose charging mode (maximise delivered power)
3-phase current = min(headroom_l1, l2, l3)   # all phases same current
1-phase current = max(headroom_l1, l2, l3)   # best single phase
→ pick whichever gives more watts

# 4. Enforce charge deadline
min_current = energy_remaining / (hours_until_06:00 × phases × 0.230)

# 5. Nord Pool price optimisation
cheap hour  → charge at max safe current
expensive   → charge at max(min_for_deadline, 6 A)

# 6. Clamp to [6 A, 16 A] and command Zaptec
```

## Running tests

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
pytest tests/ -v
```

Expected: **28 passed**

## Automations created in HA

| Automation | Trigger | Action |
|---|---|---|
| EV – Enable smart charging at night | 22:00 | Turn on `ev_smart_charging_enabled` if car connected |
| EV – Disable smart charging in the morning | 06:00 | Turn off, restore 16 A |
| EV – Deadline warning at 03:00 | 03:00 | Notify if car won't make target |
| EV – Start charging on arrival if night window | Car connects 22:00–06:00 | Enable smart charging immediately |

## Troubleshooting

| Symptom | Check |
|---|---|
| Algorithm not running | Is pyscript installed? Check HA logs |
| Wrong phase current subtracted | Re-verify phase wiring; check `last_perific_last_current_l*` vs `gpn007772_current_phase_*` |
| Charger not responding | Check `binary_sensor.gpn007772_online` and `sensor.gpn007772_charger_mode` |
| Duplicate input helpers | Run the cleanup snippet in `deploy.py` comments |
| Car not charging at night | Check `input_boolean.ev_smart_charging_enabled` = on |
