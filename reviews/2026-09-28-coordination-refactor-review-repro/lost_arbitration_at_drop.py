"""One higher-priority neutral movement frame wins arbitration right after the first verified drop command
(e.g. a strike or safety guard sharing the body).  The whole navigation task ends and the body is released
at the lip of the ledge."""
from mc2p.contracts.action import ActionPriorityV0
from mc2p.contracts.action_v1 import ActionIntentV1, MovementV1
from mc2p.contracts.behavior import BehaviorProfileV0
from mc2p.contracts.intent_source import ControlFrameProposalV1, OrderedIntentV1, ordered_intent_id
from tests.sim.runner import Scenario, run
from tests.sim.scenarios import drop_ledge
from tests.test_player_runtime import _task
import sys

height = int(sys.argv[1]) if len(sys.argv) > 1 else 2
budget = float(max(0, height - 3))

state = {"competitor": None, "sequence": 0, "lost": 0, "saw_reservation": False}


def control(context):
    driver = context.driver
    runtime = driver.runtime
    deadline = context.clock[0] + 500_000_000
    proposals = driver.prepare_proposals(deadline)
    risk_ids = [a.action_id for a in context.diagnostics.risk_actions if a.submitted_sequences]
    external = ()
    # Lose exactly one frame: the first one after the drop command was selected once.
    if risk_ids and not state["saw_reservation"]:
        state["saw_reservation"] = True
    elif state["saw_reservation"] and state["lost"] == 0:
        state["competitor"] = runtime.register_ordered_source("test-strike-competitor")
        competitor = state["competitor"]
        state["sequence"] += 1
        intent = ActionIntentV1(ordered_intent_id(competitor, state["sequence"]), competitor.source_id,
                                competitor.episode_id, runtime.observation.sequence_id,
                                ActionPriorityV0.SAFETY, context.clock[0], deadline, movement=MovementV1())
        proposals += (ControlFrameProposalV1(intents=(OrderedIntentV1(competitor, state["sequence"], intent),)),)
        external = (intent.intent_id,)
        state["lost"] = 1
    result = runtime.control_frame(_task(deadline), BehaviorProfileV0(), deadline, proposals=proposals)
    driver.adopt_result(result)
    if state["competitor"] is not None:
        runtime.unregister_ordered_source(state["competitor"])
        state["competitor"] = None
    return external


r = run(Scenario(f"drop{height}_one_lost_frame", drop_ledge(height), (.5, 64.0, .5), (.5, 64.0 - height, 4.5),
                 damage_points=budget, max_ticks=200),
        control_step=control)
print(f"drop {height} budget {budget:g}: lost movement frames={state['lost']} -> {r.outcome} / {r.reason}  final={r.final_position} "
      f"damage={r.damage:g} violations={[(t, c) for t, c, _ in r.violations]}")
for row in r.trace:
    if 34 <= row["tick"] <= 42:
        print(row["tick"], tuple(round(v, 2) for v in row["position"]), row["session_state"], row["session_reason"],
              row["controller_ids"], "ground" if row["on_ground"] else "AIR",
              "fwd", row["applied_movement"]["forward"], "sneak", row["sneaking"],
              "source_bound", row["source_bound"])
