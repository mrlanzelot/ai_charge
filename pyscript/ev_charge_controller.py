"""
Smart EV Charging Controller – single-file pyscript for Home Assistant.

Deploy to: /config/pyscript/ev_charge_controller.py  (this is the only file needed)

Strategy:
  - 3-phase charging only (Zaptec Go 2 "ThreeToOneFixed" = 1-phase on Grid L3)
  - Nord Pool price optimization: fetch hourly prices, charge during cheapest hours
  - Conservative rate: target the minimum current to finish within cheap hours
  - Ramp up slowly (+1A per cycle), ramp down immediately if over limit
  - Pause when min headroom < 6A
  - Triggers only on Perific sensor changes + 5-minute timer (NOT on charger mode)

Phase mapping (Zaptec Go 2 rotation L3, L1, L2 – TN):
  Grid L1 = Zaptec Phase 2
  Grid L2 = Zaptec Phase 3
  Grid L3 = Zaptec Phase 1
"""

import math
import json as _json
import datetime as _dt
import urllib.request as _urlreq
from dataclasses import dataclass

# ── Constants ───────────────────────────────────────────────────────────────────
INSTALLATION_ID = "8180b165-484b-47e0-9dc4-eb2630ae0dad"
CHARGER_MIN_A = 6
CHARGER_MAX_A = 16
VOLTAGE = 230
BATTERY_KWH = 69.0

CONNECTED = {"connected_charging", "connected_requesting", "connected_finished"}
RESUME_COOLDOWN_S = 300
NORDPOOL_API = ("https://dataportal-api.nordpoolgroup.com/api/DayAheadPrices"
                "?market=DayAhead&deliveryArea=SE3&currency=EUR&date=")

# ── Persistent state ───────────────────────────────────────────────────────────
_current_setpoint = CHARGER_MIN_A
_last_resume_time = None
_price_cache = []       # [(hour_epoch, avg_eur_mwh)] sorted by hour
_price_fetched_at = 0.0


@dataclass
class _P:
    l1: float; l2: float; l3: float
    def mn(self): return min(self.l1, self.l2, self.l3)
    def mx(self): return max(self.l1, self.l2, self.l3)


# ── Nord Pool price functions ──────────────────────────────────────────────────

def _fetch_url(url):
    """Blocking HTTP GET – called via task.executor to avoid blocking HA."""
    with _urlreq.urlopen(_urlreq.Request(url), timeout=10) as r:
        return _json.loads(r.read())


def _fetch_prices(now):
    """Fetch Nord Pool SE3 hourly prices (today + tomorrow), cache 1 hour."""
    global _price_cache, _price_fetched_at
    now_ts = now.timestamp()
    if _price_cache and now_ts - _price_fetched_at < 3600:
        return _price_cache

    entries = []
    for offset in (0, 1):
        d = (now + _dt.timedelta(days=offset)).strftime("%Y-%m-%d")
        try:
            data = task.executor(_fetch_url, NORDPOOL_API + d)
            for e in data.get("multiAreaEntries", []):
                ts = _dt.datetime.fromisoformat(
                    e["deliveryStart"].replace("Z", "+00:00")).timestamp()
                entries.append((ts, e["entryPerArea"].get("SE3", 9999)))
        except Exception as ex:
            log.warning("EV: Nord Pool fetch %s: %s", d, ex)

    if entries:
        hourly = {}
        for ts, p in entries:
            h = int(ts) // 3600 * 3600
            hourly.setdefault(h, []).append(p)
        _price_cache = sorted((h, sum(ps) / len(ps)) for h, ps in hourly.items())
        _price_fetched_at = now_ts
        log.info("EV: fetched %d hourly prices from Nord Pool", len(_price_cache))
    return _price_cache


def _price_schedule(now, deadline_dt, needed_kwh):
    """
    Determine if current hour is among the cheapest needed to finish by deadline.
    Returns (should_charge_now: bool, remaining_charge_hours: int).
    Falls back to (True, total_hours) if no price data.
    """
    now_ts = now.timestamp()
    dl_ts = deadline_dt.timestamp()
    total_hrs = max(1, int((dl_ts - now_ts) / 3600))

    prices = _fetch_prices(now)
    if not prices:
        return True, total_hrs

    remaining = [(h, p) for h, p in prices if h >= now_ts - 3600 and h < dl_ts]
    if not remaining:
        return True, total_hrs

    # Hours needed at minimum rate, with 20% buffer for headroom-limited periods
    kw_min = CHARGER_MIN_A * 3 * VOLTAGE / 1000.0
    hours_needed = max(1, math.ceil(needed_kwh / kw_min * 1.2))

    if hours_needed >= len(remaining):
        return True, len(remaining)

    cheapest = sorted(remaining, key=lambda x: x[1])[:hours_needed]
    cheap_set = {h for h, _ in cheapest}

    cur_hour = int(now_ts) // 3600 * 3600
    is_cheap = cur_hour in cheap_set
    remaining_cheap = max(1, sum(1 for h in cheap_set if h >= cur_hour))

    return is_cheap, remaining_cheap


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

    fuse   = float(state.get("input_number.ev_max_house_current") or 19)
    target = float(state.get("input_number.ev_target_soc") or 90)
    now    = _dt.datetime.now()

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
            log.info("EV: charger stopped at %.0f%% → resuming", soc)
            _last_resume_time = now
            button.press(entity_id="button.gpn007772_resume_charging")
        return

    # ── Headroom ────────────────────────────────────────────────────────────
    house = _P(perific.l1 - charger.l2, perific.l2 - charger.l3, perific.l3 - charger.l1)
    head  = _P(fuse - house.l1, fuse - house.l2, fuse - house.l3)
    max_safe = min(CHARGER_MAX_A, int(head.mn()))

    if max_safe < CHARGER_MIN_A:
        log.info("EV: headroom too low (L1=%.1f L2=%.1f L3=%.1f) → paused",
                 head.l1, head.l2, head.l3)
        _current_setpoint = CHARGER_MIN_A
        _display("paused", 0)
        return

    # ── Energy & deadline ──────────────────────────────────────────────────
    needed_kwh = (target - soc) / 100.0 * BATTERY_KWH

    dl_str = state.get("input_datetime.ev_charge_deadline") or "06:00:00"
    try:
        h, m = dl_str.split(":")[:2]
        deadline_time = _dt.time(int(h), int(m))
    except Exception:
        deadline_time = _dt.time(6, 0)
    dl = _dt.datetime.combine(now.date(), deadline_time)
    if dl <= now:
        dl += _dt.timedelta(days=1)

    # ── Price-optimized scheduling ─────────────────────────────────────────
    is_cheap, charge_hrs = _price_schedule(now, dl, needed_kwh)

    if not is_cheap and charger_mode == "connected_requesting":
        log.info("EV: waiting for cheaper hour (need=%.1fkWh, %dh cheap, SOC=%.0f%%, price=%.2f)",
                 needed_kwh, charge_hrs, soc, price)
        _display("paused", 0)
        return

    # ── Target current: finish within allocated charge hours ───────────────
    if charge_hrs > 0:
        min_a = int(math.ceil(needed_kwh / charge_hrs * 1000.0 / (3 * VOLTAGE)))
        min_a = max(CHARGER_MIN_A, min(CHARGER_MAX_A, min_a))
    else:
        min_a = CHARGER_MIN_A

    desired = min(min_a, max_safe)

    # ── Ramp: slow up (+1A), fast down (instant) ───────────────────────────
    if desired > _current_setpoint:
        new_current = _current_setpoint + 1
    elif desired < _current_setpoint:
        new_current = desired
    else:
        new_current = _current_setpoint

    new_current = max(CHARGER_MIN_A, min(max_safe, new_current))

    log.info("EV: 3ph %dA→%dA (safe=%d desired=%d min=%d) SOC=%.0f%% price=%.2f "
             "cheap=%s chg_hrs=%d | head L1=%.1f L2=%.1f L3=%.1f",
             _current_setpoint, new_current, max_safe, desired, min_a, soc, price,
             is_cheap, charge_hrs, head.l1, head.l2, head.l3)

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
