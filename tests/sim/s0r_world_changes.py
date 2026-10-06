"""World edits applied by the simulated backend and observed by real navigation."""
from tests.sim.backend import SLAB_ID
from tests.sim.runtime_faults import runtime_case, TERMINAL


def run_world_change(*, follow, edit_tick, material):
    with runtime_case(follow=follow) as case:
        position = (0, 64 if material == "obstacle" else 63, 5)
        block = {"obstacle": "minecraft:stone", "slab": SLAB_ID,
                 "grass": "minecraft:grass_block"}[material]
        case.backend.perturbations.world_edits[edit_tick] = {position: block}
        trace, entries = [], []
        for tick in range(1, 180):
            if case.driver.state in TERMINAL:
                case.driver.release("world-change-terminal-release")
                if case.driver.source is None:
                    break
            case.step()
            row = case.row()
            diagnostics = case.session.diagnostics
            validation = diagnostics.route_validation
            row["validations"] = []
            if validation is not None:
                for role in ("incumbent", "pending"):
                    item = getattr(validation, role)
                    if item is None:
                        continue
                    observed = {"role": role, "disposition": item.disposition.value,
                                "reason": item.reason.value, "queries_used": item.queries_used,
                                "affected_cells": item.affected_cells}
                    row["validations"].append(observed)
                    if item.queries_used:
                        # A structured result with nonzero queries is emitted
                        # after replay, unlike early safety stop dispositions.
                        entries.append({"movement_tick": row["movement_tick"], **observed})
            row["recovery_starts"] = diagnostics.recovery_total_starts
            trace.append(row)
            if follow and tick >= 120:
                case.manager.cancel()
                if case.manager.state.value == "ended":
                    break
        applied = [list(item) for item in case.backend.applied_perturbations]
        assert any(item[1] == edit_tick for item in applied), applied
        assert entries, "world-change premise did not replay a walk recipe"
        if material == "grass":
            assert any(item["disposition"] == "continue" for item in entries), entries
        else:
            assert any(item["disposition"] == "stop" for item in entries), entries
        assert case.backend.damage_taken == 0
        assert case.driver.state in TERMINAL, (case.driver.state, case.driver.reason)
        assert case.driver.source is None
        return {"passed": True, "follow": follow, "edit_tick": edit_tick, "material": material,
                "observed_replay_entries": entries, "applied_perturbations": applied,
                "terminal": case.row(), "damage": case.backend.damage_taken, "trace": trace}
