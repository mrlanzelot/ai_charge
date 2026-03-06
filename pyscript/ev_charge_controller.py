"""
Smart EV Charging Controller – Home Assistant pyscript module.

Deploys to: <HA config>/pyscript/ev_charge_controller.py
Requires:   pyscript integration (HACS), algorithm.py in same folder.

Behaviour:
  • Triggers on Perific current sensor changes + 30 s timer fallback
  • Night window 22:00–06:00: smart load-balanced + price-aware charging
  • Outside window: charge at full 16 A (daytime fast charge)
  • Switches between 3-phase and 1-phase to maximise delivered power
  • Enforces 5-minute hysteresis on phase-mode switches
  • Pauses charging if all phase headrooms drop below 6 A
"""

import math
import datetime

from algorithm import (
    PhaseCurrents,
    ChargeDecision,
    run_algorithm,
    CHARGER_MIN_A,
    CHARGER_MAX_A,
    VOLTAGE,
)

# ── Zaptec installation ID ──────────────────────────────────────────────────────
INSTALLATION_ID = "8180b165-484b-47e0-9dc4-eb2630ae0dad"

# ── Entity IDs ──────────────────────────────────────────────────────────────────
E_PERIFIC_L1   = "sensor.last_perific_last_current_l1"
E_PERIFIC_L2   = "sensor.last_perific_last_current_l2"
E_PERIFIC_L3   = "sensor.last_perific_last_current_l3"

E_CHARGER_P1   = "sensor.gpn007772_current_phase_1"
E_CHARGER_P2   = "sensor.gpn007772_current_phase_2"
E_CHARGER_P3   = "sensor.gpn007772_current_phase_3"
E_CHARGER_MODE = "sensor.gpn007772_charger_mode"
E_CHARGER_SW   = "switch.gpn007772_charging"

E_VOLVO_SOC    = "sensor.volvo_ex30_battery"
E_PRICE        = "sensor.nord_pool_se3_current_price"

# ── Input helper entity IDs (created via deploy.py) ─────────────────────────────
E_ENABLED       = "input_boolean.ev_smart_charging_enabled"
E_DEADLINE      = "input_datetime.ev_charge_deadline"
E_TARGET_SOC    = "input_number.ev_target_soc"
E_FUSE_LIMIT    = "input_number.ev_max_house_current"
E_PRICE_THRESH  = "input_number.ev_cheap_price_threshold"
E_SETPOINT      = "input_number.ev_charging_current_setpoint"
E_MODE_DISPLAY  = "input_select.ev_charging_mode"

# ── Hysteresis state ────────────────────────────────────────────────────────────
MODE_SWITCH_HYSTERESIS_MIN = 5
_last_mode_switch: datetime.datetime | None = None
_last_applied_mode: str = ""


# ── Startup ─────────────────────────────────────────────────────────────────────
@time_trigger("startup")
def ev_charge_init():
    log.info(
        f"EV Charge Controller started. "
        f"Installation: {INSTALLATION_ID} | "
        f"Min {CHARGER_MIN_A}A / Max {CHARGER_MAX_A}A"
    )


# ── Main trigger ────────────────────────────────────────────────────────────────
@state_trigger(
    "sensor.last_perific_last_current_l1",
    "sensor.last_perific_last_current_l2",
    "sensor.last_perific_last_current_l3",
    "sensor.gpn007772_charger_mode",
)
@time_trigger("period(now, 30s)")
def ev_charge_control(**kwargs):
    """Debounced entry point – prevents rapid re-runs on burst sensor updates."""
    task.unique("ev_charge_control", kill_me=True)
    task.sleep(3)
    _run_charge_control()


# ── Core logic ──────────────────────────────────────────────────────────────────
def _run_charge_control():
    global _last_mode_switch, _last_applied_mode

    # ── Guard: smart charging enabled ───────────────────────────────────────
    if state.get(E_ENABLED) != "on":
        log.debug("EV smart charging disabled – skipping.")
        return

    # ── Guard: car connected and charging / requesting ───────────────────────
    charger_mode = state.get(E_CHARGER_MODE)
    if charger_mode not in ("connected_charging", "connected_requesting"):
        _update_display("disconnected", 0)
        log.debug(f"Charger not active (mode={charger_mode}).")
        return

    # ── Read sensor values ───────────────────────────────────────────────────
    try:
        perific = PhaseCurrents(
            l1=float(state.get(E_PERIFIC_L1)),
            l2=float(state.get(E_PERIFIC_L2)),
            l3=float(state.get(E_PERIFIC_L3)),
        )
        charger = PhaseCurrents(
            l1=float(state.get(E_CHARGER_P1)),
            l2=float(state.get(E_CHARGER_P2)),
            l3=float(state.get(E_CHARGER_P3)),
        )
        current_soc   = float(state.get(E_VOLVO_SOC))
        current_price = float(state.get(E_PRICE))
    except (ValueError, TypeError) as exc:
        log.warning(f"EV Controller: sensor read error – {exc}")
        return

    # ── Read config helpers ─────────────────────────────────────────────────
    fuse_limit      = float(state.get(E_FUSE_LIMIT)     or 18.0)
    target_soc      = float(state.get(E_TARGET_SOC)     or 90.0)
    cheap_threshold = float(state.get(E_PRICE_THRESH)   or 0.80)

    # Parse deadline time from input_datetime (state = "HH:MM:SS")
    deadline_str = state.get(E_DEADLINE) or "06:00:00"
    try:
        parts = deadline_str.split(":")
        deadline = datetime.time(int(parts[0]), int(parts[1]))
    except Exception:
        deadline = datetime.time(6, 0)

    # ── Time-window check (22:00–06:00) ─────────────────────────────────────
    now  = datetime.datetime.now()
    hour = now.hour
    in_night_window = hour >= 22 or hour < 6

    if not in_night_window:
        log.debug("Outside night window – setting max current (daytime).")
        _apply_3phase(CHARGER_MAX_A, "daytime-max")
        return

    # ── Run algorithm ────────────────────────────────────────────────────────
    decision = run_algorithm(
        perific=perific,
        charger=charger,
        current_soc=current_soc,
        target_soc=target_soc,
        fuse_limit=fuse_limit,
        deadline=deadline,
        current_price=current_price,
        cheap_threshold=cheap_threshold,
        now=now,
    )

    log.info(
        f"EV decision: mode={decision.mode} current={decision.current}A "
        f"power={decision.total_power_w:.0f}W SOC={current_soc:.1f}% | {decision.reason}"
    )

    # ── Pause if needed ──────────────────────────────────────────────────────
    if decision.mode == "paused":
        _pause_charging(decision.reason)
        return

    # ── Hysteresis check for mode switches ───────────────────────────────────
    mode_changed = decision.mode != _last_applied_mode
    if mode_changed and _last_mode_switch is not None:
        elapsed_min = (now - _last_mode_switch).total_seconds() / 60.0
        if elapsed_min < MODE_SWITCH_HYSTERESIS_MIN:
            log.info(
                f"Mode switch {_last_applied_mode}→{decision.mode} suppressed "
                f"(hysteresis: {elapsed_min:.1f} < {MODE_SWITCH_HYSTERESIS_MIN} min)"
            )
            return

    # ── Apply to charger ─────────────────────────────────────────────────────
    if decision.mode == "3-phase":
        _apply_3phase(decision.current, decision.reason)
    else:
        # "1-phase-p1" / "1-phase-p2" / "1-phase-p3"
        zap_phase = int(decision.mode[-1])
        _apply_1phase(decision.current, zap_phase, decision.reason)

    if mode_changed:
        _last_mode_switch = now
    _last_applied_mode = decision.mode
    _update_display(decision.mode, decision.current)


# ── Charger commands ─────────────────────────────────────────────────────────────
def _apply_3phase(current: int, reason: str):
    """Command 3-phase charging at uniform current."""
    zaptec.limit_current(
        installation_id=INSTALLATION_ID,
        available_current=current,
    )
    if state.get(E_CHARGER_SW) != "on":
        switch.turn_on(entity_id=E_CHARGER_SW)
    log.info(f"  → 3-phase {current}A ({reason})")


def _apply_1phase(current: int, zap_phase: int, reason: str):
    """Command single-phase charging on the specified Zaptec phase (1, 2 or 3)."""
    zaptec.limit_current(
        installation_id=INSTALLATION_ID,
        available_current_phase1=current if zap_phase == 1 else 0,
        available_current_phase2=current if zap_phase == 2 else 0,
        available_current_phase3=current if zap_phase == 3 else 0,
    )
    if state.get(E_CHARGER_SW) != "on":
        switch.turn_on(entity_id=E_CHARGER_SW)
    log.info(f"  → 1-phase P{zap_phase} {current}A ({reason})")


def _pause_charging(reason: str):
    """Stop charging."""
    switch.turn_off(entity_id=E_CHARGER_SW)
    _update_display("paused", 0)
    log.info(f"  → charging paused: {reason}")


def _update_display(mode: str, current: int):
    """Update dashboard input helpers."""
    try:
        input_number.set_value(entity_id=E_SETPOINT, value=float(current))
        input_select.select_option(entity_id=E_MODE_DISPLAY, option=mode)
    except Exception as exc:
        log.debug(f"Display update skipped: {exc}")
