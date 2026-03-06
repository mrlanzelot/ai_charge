"""
Smart EV Charging Controller – single-file pyscript for Home Assistant.

Deploy to: /config/pyscript/ev_charge_controller.py  (this is the only file needed)

Triggers on Perific current-sensor changes and a 5-minute periodic timer.
When smart charging is enabled, runs the load-balancing algorithm and
sets the Zaptec Go 2 charging current via zaptec.limit_current.

Phase mapping (Zaptec Go 2 rotation L3, L1, L2 – TN):
  Grid L1 = Zaptec Phase 2
  Grid L2 = Zaptec Phase 3
  Grid L3 = Zaptec Phase 1
"""

import math
import datetime as _dt
from dataclasses import dataclass

# ── Constants ───────────────────────────────────────────────────────────────────
INSTALLATION_ID = "8180b165-484b-47e0-9dc4-eb2630ae0dad"
CHARGER_MIN_A = 6
CHARGER_MAX_A = 16
VOLTAGE = 230
BATTERY_KWH = 69.0  # Volvo EX30 usable capacity

CHARGEABLE = {"connected_charging", "connected_requesting"}


# ── Data classes ────────────────────────────────────────────────────────────────
@dataclass
class _P:
    """Three-phase current values."""
    l1: float; l2: float; l3: float
    def mn(self): return min(self.l1, self.l2, self.l3)
    def mx(self): return max(self.l1, self.l2, self.l3)


@dataclass
class _D:
    """Charge decision."""
    mode: str; current: int; phases: int; watts: float; reason: str


# ── Algorithm ───────────────────────────────────────────────────────────────────
def _algorithm(perific, charger, soc, target, fuse, deadline_t, price, threshold, now):
    # 1. House-only load (subtract charger, phase-rotated)
    house = _P(perific.l1 - charger.l2, perific.l2 - charger.l3, perific.l3 - charger.l1)

    # 2. Headroom per phase
    head = _P(fuse - house.l1, fuse - house.l2, fuse - house.l3)

    # 3. Choose mode: 3-phase vs 1-phase (whichever delivers more watts)
    if head.mn() >= CHARGER_MIN_A:
        c3 = min(CHARGER_MAX_A, int(head.mn()))
        w3 = c3 * 3 * VOLTAGE
    else:
        c3, w3 = 0, 0

    opts = [(head.l3, "1-phase-p1"), (head.l1, "1-phase-p2"), (head.l2, "1-phase-p3")]
    best_h, best_m = max(opts, key=lambda x: x[0])
    if best_h >= CHARGER_MIN_A:
        c1 = min(CHARGER_MAX_A, int(best_h))
        w1 = c1 * VOLTAGE
    else:
        c1, w1 = 0, 0

    if w3 == 0 and w1 == 0:
        return _D("paused", 0, 0, 0, f"headroom too low (L1={head.l1:.1f} L2={head.l2:.1f} L3={head.l3:.1f})")

    if w3 >= w1:
        dec = _D("3-phase", c3, 3, w3, f"3ph@{c3}A={w3}W")
    else:
        dec = _D(best_m, c1, 1, w1, f"1ph@{c1}A={w1}W")

    # 4. SOC check
    if soc >= target:
        return _D("paused", 0, 0, 0, "target SOC reached")

    # 5. Deadline + price refinement
    dl = _dt.datetime.combine(now.date(), deadline_t)
    if dl <= now:
        dl += _dt.timedelta(days=1)
    hrs = (dl - now).total_seconds() / 3600.0

    if hrs > 0 and dec.phases > 0:
        needed_kwh = (target - soc) / 100.0 * BATTERY_KWH
        min_a = needed_kwh / hrs * 1000.0 / (dec.phases * VOLTAGE)
        min_a = max(CHARGER_MIN_A, min(CHARGER_MAX_A, math.ceil(min_a)))
    else:
        min_a = CHARGER_MIN_A

    if price <= threshold:
        final = dec.current
        reason = f"cheap {price:.2f}≤{threshold:.2f} → max {final}A"
    else:
        final = min(max(min_a, CHARGER_MIN_A), dec.current)
        reason = f"expensive {price:.2f}>{threshold:.2f} → deadline {min_a}A"

    return _D(dec.mode, final, dec.phases, final * dec.phases * VOLTAGE, reason)


# ── Pyscript triggers ──────────────────────────────────────────────────────────
@time_trigger("startup")
def ev_startup():
    log.info("EV Charge Controller loaded (installation %s)", INSTALLATION_ID)


@state_trigger(
    "sensor.last_perific_last_current_l1",
    "sensor.last_perific_last_current_l2",
    "sensor.last_perific_last_current_l3",
    "sensor.gpn007772_charger_mode",
)
@time_trigger("period(now, 300s)")
def ev_control(**kwargs):
    task.unique("ev_control", kill_me=True)
    task.sleep(5)
    _run()


def _run():
    if state.get("input_boolean.ev_smart_charging_enabled") != "on":
        return

    mode = state.get("sensor.gpn007772_charger_mode")
    if mode not in CHARGEABLE:
        _display("disconnected", 0)
        return

    try:
        perific = _P(
            float(state.get("sensor.last_perific_last_current_l1")),
            float(state.get("sensor.last_perific_last_current_l2")),
            float(state.get("sensor.last_perific_last_current_l3")),
        )
        charger = _P(
            float(state.get("sensor.gpn007772_current_phase_1")),
            float(state.get("sensor.gpn007772_current_phase_2")),
            float(state.get("sensor.gpn007772_current_phase_3")),
        )
        soc   = float(state.get("sensor.volvo_ex30_battery"))
        price = float(state.get("sensor.nord_pool_se3_current_price"))
    except (ValueError, TypeError) as e:
        log.warning("EV: sensor read error – %s", e)
        return

    fuse      = float(state.get("input_number.ev_max_house_current") or 18)
    target    = float(state.get("input_number.ev_target_soc") or 90)
    threshold = float(state.get("input_number.ev_cheap_price_threshold") or 0.8)

    dl_str = state.get("input_datetime.ev_charge_deadline") or "06:00:00"
    try:
        h, m = dl_str.split(":")[:2]
        deadline = _dt.time(int(h), int(m))
    except Exception:
        deadline = _dt.time(6, 0)

    now = _dt.datetime.now()
    d = _algorithm(perific, charger, soc, target, fuse, deadline, price, threshold, now)

    log.info("EV: %s %dA (%.0fW) SOC=%.0f%% | %s", d.mode, d.current, d.watts, soc, d.reason)

    if d.mode == "paused":
        zaptec.limit_current(installation_id=INSTALLATION_ID, available_current=0)
    elif d.mode == "3-phase":
        zaptec.limit_current(installation_id=INSTALLATION_ID, available_current=d.current)
    else:
        p = int(d.mode[-1])
        zaptec.limit_current(
            installation_id=INSTALLATION_ID,
            available_current_phase1=d.current if p == 1 else 0,
            available_current_phase2=d.current if p == 2 else 0,
            available_current_phase3=d.current if p == 3 else 0,
        )

    _display(d.mode, d.current)


def _display(mode, current):
    try:
        input_number.set_value(entity_id="input_number.ev_charging_current_setpoint", value=float(current))
        input_select.select_option(entity_id="input_select.ev_charging_mode", option=mode)
    except Exception:
        pass
