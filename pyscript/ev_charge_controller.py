"""
Smart EV Charging Controller – single-file pyscript for Home Assistant.

Deploy to: /config/pyscript/ev_charge_controller.py  (this is the only file needed)

Strategy:
  - 3-phase charging only (Zaptec Go 2 "ThreeToOneFixed" can only do 1-phase
    on Zaptec Phase 1 = Grid L3, which is typically the most loaded phase)
  - Ramp up slowly (+1A per cycle), ramp down immediately if over limit
  - Pause when min headroom < 6A (can't safely charge on any 3-phase config)
  - Triggers only on Perific sensor changes + 5-minute timer (NOT on charger mode)

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
BATTERY_KWH = 69.0

CONNECTED = {"connected_charging", "connected_requesting", "connected_finished"}
RESUME_COOLDOWN_S = 300  # only attempt resume every 5 minutes

# ── Persistent state ───────────────────────────────────────────────────────────
_current_setpoint = CHARGER_MIN_A  # last commanded current
_last_resume_time = None           # last time we called resume_charging


@dataclass
class _P:
    l1: float; l2: float; l3: float
    def mn(self): return min(self.l1, self.l2, self.l3)
    def mx(self): return max(self.l1, self.l2, self.l3)


# ── Pyscript triggers ──────────────────────────────────────────────────────────
@time_trigger("startup")
def ev_startup():
    log.info("EV Charge Controller loaded (installation %s)", INSTALLATION_ID)


@state_trigger(
    "sensor.last_perific_last_current_l1",
    "sensor.last_perific_last_current_l2",
    "sensor.last_perific_last_current_l3",
)
@time_trigger("period(now, 300s)")
def ev_control(**kwargs):
    task.unique("ev_control", kill_me=True)
    task.sleep(15)
    _run()


def _run():
    global _current_setpoint, _last_resume_time

    if state.get("input_boolean.ev_smart_charging_enabled") != "on":
        return

    charger_mode = state.get("sensor.gpn007772_charger_mode")
    if charger_mode not in CONNECTED:
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

    fuse      = float(state.get("input_number.ev_max_house_current") or 19)
    target    = float(state.get("input_number.ev_target_soc") or 90)
    threshold = float(state.get("input_number.ev_cheap_price_threshold") or 0.8)
    now       = _dt.datetime.now()

    # ── SOC check ───────────────────────────────────────────────────────────
    if soc >= target:
        log.info("EV: SOC %.0f%% ≥ target %.0f%% → done", soc, target)
        _send(0, 0, 0)
        _current_setpoint = CHARGER_MIN_A
        _display("paused", 0)
        return

    # ── Resume if charger stopped prematurely (with cooldown) ───────────────
    if charger_mode == "connected_finished":
        if _last_resume_time is None or (now - _last_resume_time).total_seconds() >= RESUME_COOLDOWN_S:
            log.info("EV: charger stopped at %.0f%% → resuming (cooldown %ds)", soc, RESUME_COOLDOWN_S)
            _last_resume_time = now
            button.press(entity_id="button.gpn007772_resume_charging")
        else:
            elapsed = (now - _last_resume_time).total_seconds()
            log.debug("EV: resume cooldown (%ds / %ds)", elapsed, RESUME_COOLDOWN_S)
        return  # don't send current commands while resuming

    # ── Calculate headroom per grid phase ───────────────────────────────────
    house = _P(perific.l1 - charger.l2, perific.l2 - charger.l3, perific.l3 - charger.l1)
    head  = _P(fuse - house.l1, fuse - house.l2, fuse - house.l3)

    # 3-phase: bottlenecked by phase with least headroom
    max_safe = min(CHARGER_MAX_A, int(head.mn()))

    # ── Pause if insufficient headroom ──────────────────────────────────────
    if max_safe < CHARGER_MIN_A:
        log.info("EV: headroom too low (L1=%.1f L2=%.1f L3=%.1f, min=%d < %dA) → paused",
                 head.l1, head.l2, head.l3, max_safe, CHARGER_MIN_A)
        _current_setpoint = CHARGER_MIN_A
        _display("paused", 0)
        return

    # ── Deadline minimum ────────────────────────────────────────────────────
    dl_str = state.get("input_datetime.ev_charge_deadline") or "06:00:00"
    try:
        h, m = dl_str.split(":")[:2]
        deadline = _dt.time(int(h), int(m))
    except Exception:
        deadline = _dt.time(6, 0)

    dl = _dt.datetime.combine(now.date(), deadline)
    if dl <= now:
        dl += _dt.timedelta(days=1)
    hrs = (dl - now).total_seconds() / 3600.0

    if hrs > 0:
        needed_kwh = (target - soc) / 100.0 * BATTERY_KWH
        min_a = int(math.ceil(needed_kwh / hrs * 1000.0 / (3 * VOLTAGE)))
        min_a = max(CHARGER_MIN_A, min(CHARGER_MAX_A, min_a))
    else:
        min_a = CHARGER_MIN_A

    # ── Price-aware target ──────────────────────────────────────────────────
    if price <= threshold:
        desired = max_safe  # cheap: charge as fast as safely possible
    else:
        desired = min_a     # expensive: only what deadline requires

    # ── Ramp: slow up (+1A), fast down (instant) ───────────────────────────
    if desired > _current_setpoint:
        new_current = _current_setpoint + 1  # ramp up by 1A
    elif desired < _current_setpoint:
        new_current = desired                # ramp down immediately
    else:
        new_current = _current_setpoint      # hold steady

    new_current = max(CHARGER_MIN_A, min(max_safe, new_current))

    log.info("EV: 3ph %dA→%dA (safe=%d desired=%d deadline=%d) SOC=%.0f%% price=%.2f | "
             "head L1=%.1f L2=%.1f L3=%.1f | house L1=%.1f L2=%.1f L3=%.1f",
             _current_setpoint, new_current, max_safe, desired, min_a, soc, price,
             head.l1, head.l2, head.l3, house.l1, house.l2, house.l3)

    _current_setpoint = new_current
    _send(new_current, new_current, new_current)
    _display("3-phase", new_current)


def _send(p1, p2, p3):
    zaptec.limit_current(
        installation_id=INSTALLATION_ID,
        available_current_phase1=p1,
        available_current_phase2=p2,
        available_current_phase3=p3,
    )


def _display(mode, current):
    try:
        input_number.set_value(entity_id="input_number.ev_charging_current_setpoint", value=float(current))
        select_mode = mode if mode in ("3-phase", "1-phase-p1", "1-phase-p2", "1-phase-p3",
                                        "paused", "disconnected") else "paused"
        input_select.select_option(entity_id="input_select.ev_charging_mode", option=select_mode)
    except Exception:
        pass
