# Run from the repository root of a 340cac6 checkout: PYTHONPATH=. python -B <script> [family ...]
"""Families outside the frozen 192-run interrupt manifest.

A  other geometries (L walkway, two-ledge terrace, one-block step down after a walk) x revise/cancel x late none/+1/+2
B  longer disturbance after the interruption: late +1..+3 consecutive, late +5, omitted receipts +1..+3
C  one lost movement frame (higher-priority neutral competitor) at every tick of the drop window
D  knockback impulse at every tick of the drop window (toward and away from the drop)
E  goal flapping: revise between two lower-floor goals every k ticks from the probe onward
Every run must reach a terminal state without invariant violations; a bounded safe failure is acceptable.
"""
import sys
from collections import Counter
from dataclasses import replace
from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id
from mc2p.motion_nav.motion_risk import TaskDamageBudget
from tests.sim.backend import Perturbations
from tests.sim.runner import Event, Scenario, _goal, lane, run
from tests.sim.scenarios import SCENARIOS, columns
from tests.test_player_runtime import _task

TERMINAL = {"success", "failed", "cancelled"}


class Raised:
    """A caller-side API call raised inside an event; recorded as its own outcome."""

    def __init__(self, error):
        self.outcome = "RAISED"
        self.reason = f"{type(error).__name__}: {error}"
        self.violations = []


def safe_run(*args, **kwargs):
    try:
        return run(*args, **kwargs)
    except Exception as error:  # noqa: BLE001
        return Raised(error)
BY_NAME = {s.name: s for s in SCENARIOS}


def revise_to(position, revision=2):
    def action(context):
        goal = _goal(position, context.risk_policy_id)
        context.driver.replace_goal("goal", revision, goal, context.clock[0],
                                    damage_budget=TaskDamageBudget(context.risk_policy_id, context.damage_points))
        context.goal_state = goal
        context.goal_position = position
    return action


def cancel(context):
    context.driver.release("harness_cancel")


def report(label, results):
    tally = Counter()
    bad = []
    for key, r in results:
        terminal = r.outcome in TERMINAL
        tally[("terminal" if terminal else "NOT_TERMINAL")] += 1
        tally[f"{r.outcome}/{r.reason}"] += 1
        if not terminal or r.violations:
            bad.append(f"{key}:{r.outcome}/{r.reason}/{','.join(sorted({v[1] for v in r.violations}))}")
    print(f"{label}: runs={len(results)} {dict(tally)}", flush=True)
    for item in bad[:12]:
        print("    ", item, flush=True)
    if len(bad) > 12:
        print(f"     ... {len(bad) - 12} more", flush=True)


def family_a():
    step_down = Scenario("step_down_after_walk", lane(columns([65] * 3 + [64] * 3), width=3),
                         (.5, 65.0, .5), (.5, 64.0, 4.5))
    cases = [
        (BY_NAME["far_landing_L_walkway"], (7.5, 61.0, 7.5), range(80, 100)),
        (BY_NAME["terrace_two_ledges"], (.5, 60.0, 7.5), range(20, 70, 2)),
        (step_down, (-0.5, 64.0, 4.5), range(20, 40)),
    ]
    for base, other, ticks in cases:
        for label, action in (("revise", revise_to(other)), ("cancel", cancel)):
            results = []
            for at in ticks:
                for delay in (None, 1, 2):
                    late = frozenset() if delay is None else frozenset({at + delay})
                    r = safe_run(replace(base, events=[Event(label, lambda c, at=at: c.tick >= at, action)],
                                         perturbations=Perturbations(late_ticks=late), max_ticks=400))
                    results.append((f"@{at}+{delay}", r))
            report(f"A {base.name} {label}", results)


def family_b():
    for name, other in (("direct_drop_2", (-0.5, 62.0, 4.5)), ("direct_drop_5_budget_2", (-0.5, 59.0, 4.5))):
        base = BY_NAME[name]
        for label, action in (("revise", revise_to(other)), ("cancel", cancel)):
            results = []
            for at in range(34, 46):
                disturbances = {
                    "late+1..+3": Perturbations(late_ticks=frozenset({at + 1, at + 2, at + 3})),
                    "late+5": Perturbations(late_ticks=frozenset({at + 5})),
                    "omit+1..+3": Perturbations(omitted_receipt_ticks=frozenset({at + 1, at + 2, at + 3})),
                }
                for dname, perturbation in disturbances.items():
                    r = safe_run(replace(base, events=[Event(label, lambda c, at=at: c.tick >= at, action)],
                                         perturbations=perturbation, max_ticks=300))
                    results.append((f"@{at}:{dname}", r))
            report(f"B {name} {label}", results)
    for name in ("direct_drop_2", "direct_drop_5_budget_2"):
        base = BY_NAME[name]
        results = []
        for at in range(34, 52):
            for span in (1, 3, 6):
                r = run(replace(base, perturbations=Perturbations(
                    omitted_receipt_ticks=frozenset(range(at, at + span))), max_ticks=300))
                results.append((f"omit@{at}x{span}", r))
        report(f"B {name} omitted receipts without interruption", results)


def lost_frame_control(target_tick):
    state = {"competitor": None, "sequence": 0}

    def control(context):
        driver = context.driver
        runtime = driver.runtime
        deadline = context.clock[0] + 500_000_000
        proposals = driver.prepare_proposals(deadline)
        external = ()
        if context.tick == target_tick:
            state["competitor"] = runtime.register_ordered_source("test-competitor")
            competitor = state["competitor"]
            state["sequence"] += 1
            intent = ActionIntentV1(ordered_intent_id(competitor, state["sequence"]), competitor.source_id,
                                    competitor.episode_id, runtime.observation.sequence_id,
                                    ActionPriorityV0.SAFETY, context.clock[0], deadline, movement=MovementV1())
            proposals += (ControlFrameProposalV1(intents=(OrderedIntentV1(competitor, state["sequence"], intent),)),)
            external = (intent.intent_id,)
        result = runtime.control_frame(_task(deadline), BehaviorProfileV0(), deadline, proposals=proposals)
        driver.adopt_result(result)
        if state["competitor"] is not None:
            runtime.unregister_ordered_source(state["competitor"])
            state["competitor"] = None
        return external
    return control


def family_c():
    for name in ("direct_drop_2", "direct_drop_5_budget_2"):
        base = BY_NAME[name]
        results = []
        for at in range(30, 52):
            r = run(replace(base, max_ticks=300), control_step=lost_frame_control(at))
            results.append((f"lost@{at}", r))
        report(f"C {name} one lost movement frame", results)


def family_d():
    for name in ("direct_drop_2", "direct_drop_5_budget_2"):
        base = BY_NAME[name]
        results = []
        for at in range(30, 52):
            for vz in (0.3, -0.3):
                r = run(replace(base, perturbations=Perturbations(impulses={at: (0.0, 0.0, vz)}), max_ticks=300))
                results.append((f"push@{at}:{vz:+}", r))
        report(f"D {name} knockback", results)


def family_e():
    goals = ((-0.5, 62.0, 4.5), (0.5, 62.0, 4.5))
    base = BY_NAME["direct_drop_2"]
    for period in (3, 5, 8, 13):
        events = []
        for index, at in enumerate(range(12, 120, period)):
            events.append(Event(f"flap{index}", lambda c, at=at: c.tick >= at,
                                revise_to(goals[index % 2], revision=index + 2)))
        try:
            r = run(replace(base, events=events, max_ticks=500))
        except Exception as error:  # noqa: BLE001 - a raised caller API call is itself a finding
            print(f"E flap every {period} ticks: replace_goal RAISED {type(error).__name__}: {error}", flush=True)
            continue
        retries = r.trace[-1]["retry_total_failures"] if r.trace else None
        report(f"E flap every {period} ticks (retries={retries}, final={r.final_position})", [("run", r)])


FAMILIES = {"A": family_a, "B": family_b, "C": family_c, "D": family_d, "E": family_e}
for key in (sys.argv[1:] or list(FAMILIES)):
    FAMILIES[key]()
