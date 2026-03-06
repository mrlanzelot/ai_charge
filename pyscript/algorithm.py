"""
EV Charging Algorithm – pure Python, no Home Assistant dependency.

Phase mapping (Zaptec Go 2 rotation L3, L1, L2 – TN):
  Grid L1 = Zaptec Phase 2  →  sensor.gpn007772_current_phase_2
  Grid L2 = Zaptec Phase 3  →  sensor.gpn007772_current_phase_3
  Grid L3 = Zaptec Phase 1  →  sensor.gpn007772_current_phase_1
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Tuple

# ── Hardware constants ──────────────────────────────────────────────────────────
CHARGER_MIN_A: int = 6       # Zaptec absolute minimum
CHARGER_MAX_A: int = 16      # Zaptec absolute maximum
VOLTAGE: int = 230           # Volts per phase
BATTERY_CAPACITY_KWH: float = 69.0  # Volvo EX30 usable capacity


# ── Data classes ────────────────────────────────────────────────────────────────
@dataclass
class PhaseCurrents:
    """Current readings for the three grid phases."""
    l1: float
    l2: float
    l3: float

    def min(self) -> float:
        return min(self.l1, self.l2, self.l3)

    def max(self) -> float:
        return max(self.l1, self.l2, self.l3)


@dataclass
class ChargeDecision:
    """Output of the algorithm: what to command the charger to do."""
    mode: str           # "3-phase" | "1-phase-p1" | "1-phase-p2" | "1-phase-p3" | "paused"
    current: int        # Amps commanded (0 if paused)
    active_phases: int  # 1 or 3 (0 if paused)
    total_power_w: float
    reason: str


# ── Algorithm steps ─────────────────────────────────────────────────────────────

def calculate_house_loads(
    perific: PhaseCurrents,
    charger: PhaseCurrents,
) -> PhaseCurrents:
    """
    Isolate house-only load per grid phase by subtracting charger contribution.

    Zaptec phase rotation L3, L1, L2 (TN):
      Grid L1 carries Zaptec Phase 2
      Grid L2 carries Zaptec Phase 3
      Grid L3 carries Zaptec Phase 1
    """
    return PhaseCurrents(
        l1=perific.l1 - charger.l2,   # Grid L1 − Zaptec P2
        l2=perific.l2 - charger.l3,   # Grid L2 − Zaptec P3
        l3=perific.l3 - charger.l1,   # Grid L3 − Zaptec P1
    )


def calculate_headrooms(house_loads: PhaseCurrents, fuse_limit: float) -> PhaseCurrents:
    """Available current per phase = fuse safety limit − house-only load."""
    return PhaseCurrents(
        l1=fuse_limit - house_loads.l1,
        l2=fuse_limit - house_loads.l2,
        l3=fuse_limit - house_loads.l3,
    )


def decide_charge_mode(headrooms: PhaseCurrents) -> ChargeDecision:
    """
    Choose between 3-phase and 1-phase charging to maximise total delivered power.

    The Zaptec Go 2 constraint: all active phases must run at the same current.
    Two modes available:
      3-phase  – bottlenecked by the phase with least headroom
      1-phase  – use only the phase with most headroom

    Decision: whichever delivers more watts wins.
    Crossover: 3-phase wins when min_headroom ≥ max_headroom / 3.
    """
    # ── 3-phase option ──────────────────────────────────────────────────────
    min_h = headrooms.min()
    if min_h >= CHARGER_MIN_A:
        three_current = min(CHARGER_MAX_A, int(min_h))
        three_power = three_current * 3 * VOLTAGE
    else:
        three_current = 0
        three_power = 0

    # ── 1-phase option (best available phase) ───────────────────────────────
    # headroom_l3 → Zaptec P1, headroom_l1 → Zaptec P2, headroom_l2 → Zaptec P3
    phase_options: list[Tuple[float, str]] = [
        (headrooms.l3, "1-phase-p1"),   # L3 headroom → Zaptec Phase 1
        (headrooms.l1, "1-phase-p2"),   # L1 headroom → Zaptec Phase 2
        (headrooms.l2, "1-phase-p3"),   # L2 headroom → Zaptec Phase 3
    ]
    best_headroom, best_mode = max(phase_options, key=lambda x: x[0])

    if best_headroom >= CHARGER_MIN_A:
        single_current = min(CHARGER_MAX_A, int(best_headroom))
        single_power = single_current * VOLTAGE
    else:
        single_current = 0
        single_power = 0

    # ── No viable mode ──────────────────────────────────────────────────────
    if three_power == 0 and single_power == 0:
        return ChargeDecision(
            mode="paused", current=0, active_phases=0, total_power_w=0,
            reason=f"all headrooms below {CHARGER_MIN_A}A minimum "
                   f"(L1={headrooms.l1:.1f} L2={headrooms.l2:.1f} L3={headrooms.l3:.1f})",
        )

    # ── Choose best mode ────────────────────────────────────────────────────
    if three_power >= single_power:
        return ChargeDecision(
            mode="3-phase", current=three_current, active_phases=3,
            total_power_w=three_power,
            reason=f"3-phase@{three_current}A={three_power}W ≥ 1-phase@{single_current}A={single_power}W",
        )
    else:
        return ChargeDecision(
            mode=best_mode, current=single_current, active_phases=1,
            total_power_w=single_power,
            reason=f"1-phase@{single_current}A={single_power}W > 3-phase@{three_current}A={three_power}W",
        )


def min_current_for_deadline(
    current_soc: float,
    target_soc: float,
    battery_capacity_kwh: float,
    hours_until_deadline: float,
    active_phases: int,
) -> float:
    """
    Minimum charging current (A) required to reach target SOC by the deadline.
    Returns 0 if already at or above target, or if deadline has passed.
    """
    if hours_until_deadline <= 0 or current_soc >= target_soc:
        return 0.0
    if active_phases <= 0:
        return float("inf")
    energy_needed_kwh = (target_soc - current_soc) / 100.0 * battery_capacity_kwh
    power_needed_kw = energy_needed_kwh / hours_until_deadline
    return power_needed_kw * 1000.0 / (active_phases * VOLTAGE)


def apply_price_and_deadline(
    decision: ChargeDecision,
    current_soc: float,
    target_soc: float,
    battery_capacity_kwh: float,
    hours_until_deadline: float,
    current_price: float,
    cheap_threshold: float,
) -> ChargeDecision:
    """
    Refine current based on Nord Pool price and deadline urgency.

    Cheap hour  (price ≤ threshold): charge at max safe current.
    Expensive hour (price > threshold): throttle to minimum needed for deadline.
    Deadline override: if deadline minimum exceeds price throttle, raise current.
    """
    if decision.mode == "paused":
        return decision

    if current_soc >= target_soc:
        return ChargeDecision(
            mode="paused", current=0, active_phases=0, total_power_w=0,
            reason="target SOC reached",
        )

    min_a = min_current_for_deadline(
        current_soc, target_soc, battery_capacity_kwh,
        hours_until_deadline, decision.active_phases,
    )
    min_a_clamped = max(CHARGER_MIN_A, min(CHARGER_MAX_A, math.ceil(min_a)))

    if current_price <= cheap_threshold:
        final = decision.current
        reason = (
            f"cheap {current_price:.2f} ≤ {cheap_threshold:.2f} SEK/kWh → "
            f"max safe {final}A"
        )
    else:
        # Throttle to deadline minimum, but never below CHARGER_MIN_A
        # and never above what headroom allows
        final = max(min_a_clamped, CHARGER_MIN_A)
        final = min(final, decision.current)
        reason = (
            f"expensive {current_price:.2f} > {cheap_threshold:.2f} SEK/kWh → "
            f"deadline min {min_a_clamped}A"
        )

    return ChargeDecision(
        mode=decision.mode,
        current=final,
        active_phases=decision.active_phases,
        total_power_w=final * decision.active_phases * VOLTAGE,
        reason=reason,
    )


def hours_until_deadline(deadline: time, now: datetime | None = None) -> float:
    """Hours remaining until the next occurrence of deadline time."""
    if now is None:
        now = datetime.now()
    deadline_dt = datetime.combine(now.date(), deadline)
    if deadline_dt <= now:
        deadline_dt += timedelta(days=1)
    return (deadline_dt - now).total_seconds() / 3600.0


def run_algorithm(
    perific: PhaseCurrents,
    charger: PhaseCurrents,
    current_soc: float,
    target_soc: float,
    fuse_limit: float,
    deadline: time,
    current_price: float,
    cheap_threshold: float,
    now: datetime | None = None,
) -> ChargeDecision:
    """
    Full algorithm pipeline: load balancing → mode selection → price/deadline refinement.
    This is the single entry point used by both pyscript and tests.
    """
    house_loads = calculate_house_loads(perific, charger)
    headrooms = calculate_headrooms(house_loads, fuse_limit)
    decision = decide_charge_mode(headrooms)
    hours_left = hours_until_deadline(deadline, now)
    decision = apply_price_and_deadline(
        decision, current_soc, target_soc,
        BATTERY_CAPACITY_KWH, hours_left,
        current_price, cheap_threshold,
    )
    return decision
