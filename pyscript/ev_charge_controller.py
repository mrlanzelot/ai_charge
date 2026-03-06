"""
Smart EV Charging Controller – pyscript module for Home Assistant.

Triggers on Perific current-sensor changes and a 5-minute periodic timer.
When smart charging is enabled, runs the load-balancing algorithm and
sets the Zaptec Go 2 charging current via zaptec.limit_current.

Deploy to:  /config/pyscript/ev_charge_controller.py
Also needs: /config/pyscript/modules/algorithm.py
"""

import datetime

from algorithm import PhaseCurrents, run_algorithm

INSTALLATION_ID = "8180b165-484b-47e0-9dc4-eb2630ae0dad"

CHARGEABLE = {"connected_charging", "connected_requesting"}


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
    """Debounced entry – kills previous pending run on rapid sensor bursts."""
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
        perific = PhaseCurrents(
            l1=float(state.get("sensor.last_perific_last_current_l1")),
            l2=float(state.get("sensor.last_perific_last_current_l2")),
            l3=float(state.get("sensor.last_perific_last_current_l3")),
        )
        charger = PhaseCurrents(
            l1=float(state.get("sensor.gpn007772_current_phase_1")),
            l2=float(state.get("sensor.gpn007772_current_phase_2")),
            l3=float(state.get("sensor.gpn007772_current_phase_3")),
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
        deadline = datetime.time(int(h), int(m))
    except Exception:
        deadline = datetime.time(6, 0)

    now = datetime.datetime.now()
    decision = run_algorithm(
        perific=perific, charger=charger,
        current_soc=soc, target_soc=target,
        fuse_limit=fuse, deadline=deadline,
        current_price=price, cheap_threshold=threshold,
        now=now,
    )

    log.info("EV: %s %dA (%.0fW) SOC=%.0f%% | %s",
             decision.mode, decision.current, decision.total_power_w, soc, decision.reason)

    # Apply via zaptec.limit_current only (switch.turn_on/off is unreliable)
    if decision.mode == "paused":
        zaptec.limit_current(installation_id=INSTALLATION_ID, available_current=0)
    elif decision.mode == "3-phase":
        zaptec.limit_current(installation_id=INSTALLATION_ID,
                             available_current=decision.current)
    else:
        p = int(decision.mode[-1])  # "1-phase-p1" → 1
        zaptec.limit_current(
            installation_id=INSTALLATION_ID,
            available_current_phase1=decision.current if p == 1 else 0,
            available_current_phase2=decision.current if p == 2 else 0,
            available_current_phase3=decision.current if p == 3 else 0,
        )

    _display(decision.mode, decision.current)


def _display(mode, current):
    try:
        input_number.set_value(entity_id="input_number.ev_charging_current_setpoint",
                               value=float(current))
        input_select.select_option(entity_id="input_select.ev_charging_mode",
                                   option=mode)
    except Exception:
        pass
