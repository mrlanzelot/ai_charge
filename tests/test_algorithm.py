"""
Unit tests for the EV charging algorithm.
Version: 1.0.0

Run with:  pytest tests/test_algorithm.py -v
"""

import sys
import math
import pytest
from datetime import datetime, time

sys.path.insert(0, "pyscript/modules")
from algorithm import (
    PhaseCurrents,
    ChargeDecision,
    calculate_house_loads,
    calculate_headrooms,
    decide_charge_mode,
    min_current_for_deadline,
    apply_price_and_deadline,
    hours_until_deadline,
    run_algorithm,
    CHARGER_MIN_A,
    CHARGER_MAX_A,
    VOLTAGE,
    BATTERY_CAPACITY_KWH,
)


# ── Fixtures ────────────────────────────────────────────────────────────────────

@pytest.fixture
def balanced_perific():
    """House drawing ~7A per phase, charger on top."""
    return PhaseCurrents(l1=13.0, l2=13.0, l3=13.0)


@pytest.fixture
def balanced_charger():
    """Charger drawing 6A per phase."""
    return PhaseCurrents(l1=6.0, l2=6.0, l3=6.0)


# ── Phase mapping tests ─────────────────────────────────────────────────────────

class TestCalculateHouseLoads:
    def test_correct_phase_subtraction(self):
        """Grid L1 = Zaptec P2, L2 = Zaptec P3, L3 = Zaptec P1."""
        perific = PhaseCurrents(l1=14.0, l2=12.0, l3=10.0)
        charger = PhaseCurrents(l1=8.0, l2=6.0, l3=4.0)
        loads = calculate_house_loads(perific, charger)
        assert loads.l1 == pytest.approx(14.0 - 6.0)   # L1 - ZapP2
        assert loads.l2 == pytest.approx(12.0 - 4.0)   # L2 - ZapP3
        assert loads.l3 == pytest.approx(10.0 - 8.0)   # L3 - ZapP1

    def test_zero_charger(self):
        """When charger is off, house load equals perific reading."""
        perific = PhaseCurrents(l1=5.0, l2=7.0, l3=3.0)
        charger = PhaseCurrents(l1=0.0, l2=0.0, l3=0.0)
        loads = calculate_house_loads(perific, charger)
        assert loads.l1 == pytest.approx(5.0)
        assert loads.l2 == pytest.approx(7.0)
        assert loads.l3 == pytest.approx(3.0)


# ── Headroom tests ──────────────────────────────────────────────────────────────

class TestCalculateHeadrooms:
    def test_headroom_calculation(self):
        house = PhaseCurrents(l1=7.0, l2=5.0, l3=9.0)
        headrooms = calculate_headrooms(house, fuse_limit=18.0)
        assert headrooms.l1 == pytest.approx(11.0)
        assert headrooms.l2 == pytest.approx(13.0)
        assert headrooms.l3 == pytest.approx(9.0)

    def test_negative_headroom(self):
        """Over-fuse scenario – headroom goes negative."""
        house = PhaseCurrents(l1=20.0, l2=5.0, l3=5.0)
        headrooms = calculate_headrooms(house, fuse_limit=18.0)
        assert headrooms.l1 < 0


# ── Mode decision tests ─────────────────────────────────────────────────────────

class TestDecideChargeMode:
    def test_3phase_when_all_phases_have_headroom(self):
        """Balanced headroom → 3-phase wins."""
        headrooms = PhaseCurrents(l1=10.0, l2=10.0, l3=10.0)
        d = decide_charge_mode(headrooms)
        assert d.mode == "3-phase"
        assert d.current == 10

    def test_3phase_limited_by_worst_phase(self):
        """3-phase bottlenecked by minimum headroom."""
        headrooms = PhaseCurrents(l1=14.0, l2=8.0, l3=12.0)
        d = decide_charge_mode(headrooms)
        assert d.mode == "3-phase"
        assert d.current == 8

    def test_1phase_wins_when_phases_unbalanced(self):
        """L1 severely loaded: 1-phase on best phase gives more power."""
        # 3-phase: min=2A < 6A → no 3-phase
        # 1-phase: L3→ZapP1 has 14A headroom → 14A × 230 = 3220W
        headrooms = PhaseCurrents(l1=2.0, l2=3.0, l3=14.0)
        d = decide_charge_mode(headrooms)
        assert d.mode == "1-phase-p1"   # L3 headroom maps to Zaptec Phase 1
        assert d.current == 14
        assert d.active_phases == 1

    def test_1phase_picks_best_phase(self):
        """Among 1-phase options, picks the phase with most headroom."""
        # 3-phase: min=3A < 6A → no
        # L1 headroom=3A→ZapP2=3A, L2 headroom=5A→ZapP3=5A, L3 headroom=12A→ZapP1=12A
        headrooms = PhaseCurrents(l1=3.0, l2=5.0, l3=12.0)
        d = decide_charge_mode(headrooms)
        assert d.mode == "1-phase-p1"
        assert d.current == 12

    def test_pause_when_all_below_minimum(self):
        headrooms = PhaseCurrents(l1=2.0, l2=3.0, l3=4.0)
        d = decide_charge_mode(headrooms)
        assert d.mode == "paused"
        assert d.current == 0

    def test_3phase_capped_at_max(self):
        headrooms = PhaseCurrents(l1=20.0, l2=20.0, l3=20.0)
        d = decide_charge_mode(headrooms)
        assert d.mode == "3-phase"
        assert d.current == CHARGER_MAX_A

    def test_1phase_capped_at_max(self):
        headrooms = PhaseCurrents(l1=2.0, l2=2.0, l3=20.0)
        d = decide_charge_mode(headrooms)
        assert d.current == CHARGER_MAX_A

    def test_crossover_boundary(self):
        """
        3-phase beats 1-phase when min_headroom >= max_headroom / 3.
        min=7, max=9: 7×3×230=4830W vs 9×230=2070W → 3-phase wins.
        """
        headrooms = PhaseCurrents(l1=7.0, l2=7.0, l3=9.0)
        d = decide_charge_mode(headrooms)
        assert d.mode == "3-phase"

    def test_crossover_boundary_flipped(self):
        """
        min=2, max=12: 2A < 6A → 3-phase invalid. 1-phase at 12A wins.
        """
        headrooms = PhaseCurrents(l1=2.0, l2=7.0, l3=12.0)
        d = decide_charge_mode(headrooms)
        assert d.mode == "1-phase-p1"  # L3=12A → ZapP1


# ── Deadline tests ──────────────────────────────────────────────────────────────

class TestMinCurrentForDeadline:
    def test_plenty_of_time(self):
        """8 hours, 20% remaining of 69kWh on 3 phases → small current needed."""
        a = min_current_for_deadline(
            current_soc=70, target_soc=90,
            battery_capacity_kwh=69.0,
            hours_until_deadline=8.0,
            active_phases=3,
        )
        # energy = 20% × 69 = 13.8kWh; power = 13.8/8 = 1.725kW; I = 1725/(3×230) ≈ 2.5A
        assert a == pytest.approx(2.5, abs=0.1)

    def test_urgent_deadline(self):
        """30 minutes left, 10% remaining → high current needed."""
        a = min_current_for_deadline(
            current_soc=80, target_soc=90,
            battery_capacity_kwh=69.0,
            hours_until_deadline=0.5,
            active_phases=3,
        )
        # energy = 6.9kWh; power = 13.8kW; I = 13800/690 = 20A (exceeds cap → algorithm clamps)
        assert a > CHARGER_MAX_A

    def test_already_at_target(self):
        assert min_current_for_deadline(90, 90, 69.0, 5.0, 3) == 0.0

    def test_deadline_passed(self):
        assert min_current_for_deadline(80, 90, 69.0, 0.0, 3) == 0.0

    def test_single_phase(self):
        """Same energy, 1 phase needs 3× the current vs 3-phase."""
        a3 = min_current_for_deadline(70, 90, 69.0, 8.0, 3)
        a1 = min_current_for_deadline(70, 90, 69.0, 8.0, 1)
        assert a1 == pytest.approx(a3 * 3, rel=0.01)


# ── Price optimisation tests ────────────────────────────────────────────────────

class TestApplyPriceAndDeadline:
    def _base_decision(self, current=12) -> ChargeDecision:
        return ChargeDecision(
            mode="3-phase", current=current, active_phases=3,
            total_power_w=current * 3 * VOLTAGE, reason="test",
        )

    def test_cheap_price_charges_at_max_safe(self):
        d = apply_price_and_deadline(
            self._base_decision(12),
            current_soc=70, target_soc=90,
            battery_capacity_kwh=69.0,
            hours_until_deadline=8.0,
            current_price=0.50, cheap_threshold=0.80,
        )
        assert d.current == 12   # kept at safe max

    def test_expensive_price_throttles_to_deadline(self):
        d = apply_price_and_deadline(
            self._base_decision(12),
            current_soc=70, target_soc=90,
            battery_capacity_kwh=69.0,
            hours_until_deadline=8.0,
            current_price=1.20, cheap_threshold=0.80,
        )
        # deadline min ≈ 2.5A → clamped to 6A minimum
        assert d.current == CHARGER_MIN_A

    def test_expensive_but_urgent_deadline(self):
        """Even expensive, if deadline requires more, use it (up to safe limit)."""
        d = apply_price_and_deadline(
            self._base_decision(12),
            current_soc=85, target_soc=90,
            battery_capacity_kwh=69.0,
            hours_until_deadline=0.3,    # very little time
            current_price=1.50, cheap_threshold=0.80,
        )
        # deadline min >> 12A → capped at safe headroom (12)
        assert d.current == 12

    def test_target_soc_reached_pauses(self):
        d = apply_price_and_deadline(
            self._base_decision(12),
            current_soc=90, target_soc=90,
            battery_capacity_kwh=69.0,
            hours_until_deadline=6.0,
            current_price=0.50, cheap_threshold=0.80,
        )
        assert d.mode == "paused"

    def test_paused_decision_propagated(self):
        paused = ChargeDecision(mode="paused", current=0, active_phases=0,
                                total_power_w=0, reason="test")
        d = apply_price_and_deadline(
            paused, 70, 90, 69.0, 8.0, 0.50, 0.80,
        )
        assert d.mode == "paused"


# ── Hours until deadline ────────────────────────────────────────────────────────

class TestHoursUntilDeadline:
    def test_6am_deadline_at_11pm(self):
        now = datetime(2026, 3, 6, 23, 0, 0)
        h = hours_until_deadline(time(6, 0), now)
        assert h == pytest.approx(7.0, abs=0.01)

    def test_deadline_already_passed_today(self):
        """If 06:00 has passed (it's 08:00), next deadline is tomorrow."""
        now = datetime(2026, 3, 6, 8, 0, 0)
        h = hours_until_deadline(time(6, 0), now)
        assert h == pytest.approx(22.0, abs=0.01)


# ── Full pipeline integration test ──────────────────────────────────────────────

class TestRunAlgorithm:
    def test_normal_night_charging(self):
        """Typical overnight scenario: balanced load, cheap price."""
        decision = run_algorithm(
            perific=PhaseCurrents(l1=14.0, l2=14.0, l3=14.0),
            charger=PhaseCurrents(l1=6.0, l2=6.0, l3=6.0),
            current_soc=70.0,
            target_soc=90.0,
            fuse_limit=18.0,
            deadline=time(6, 0),
            current_price=0.50,
            cheap_threshold=0.80,
            now=datetime(2026, 3, 6, 23, 0, 0),
        )
        # house_l1 = 14-6=8 → headroom=10; same for all → 3-phase at 10A
        assert decision.mode == "3-phase"
        assert decision.current == 10

    def test_one_phase_overloaded(self):
        """One phase heavily loaded → switches to 1-phase on best phase."""
        decision = run_algorithm(
            perific=PhaseCurrents(l1=17.0, l2=17.0, l3=8.0),  # L1,L2 heavy
            charger=PhaseCurrents(l1=6.0, l2=6.0, l3=6.0),
            current_soc=70.0,
            target_soc=90.0,
            fuse_limit=18.0,
            deadline=time(6, 0),
            current_price=0.50,
            cheap_threshold=0.80,
            now=datetime(2026, 3, 6, 23, 0, 0),
        )
        # house_l1=17-6=11→headroom=7, house_l2=17-6=11→headroom=7
        # house_l3=8-6=2→headroom=16 → 3-phase min=7A=4830W vs 1-phase L3→ZapP1 16A=3680W
        # 3-phase wins (4830 > 3680)
        assert decision.mode == "3-phase"
        assert decision.current == 7

    def test_car_full_pauses(self):
        decision = run_algorithm(
            perific=PhaseCurrents(l1=10.0, l2=10.0, l3=10.0),
            charger=PhaseCurrents(l1=6.0, l2=6.0, l3=6.0),
            current_soc=90.0,
            target_soc=90.0,
            fuse_limit=18.0,
            deadline=time(6, 0),
            current_price=0.50,
            cheap_threshold=0.80,
            now=datetime(2026, 3, 6, 23, 0, 0),
        )
        assert decision.mode == "paused"
