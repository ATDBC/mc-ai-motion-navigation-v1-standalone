#!/usr/bin/env bash
# Run every repro against a d34a674 checkout.  Usage, from that checkout's repository root:
#   bash <this directory>/run_all.sh > <this directory>/outputs-d34a674.txt 2>&1
# old_twin.py is the only script for the 8898cf9 checkout; it runs through the step-0 prototype harness:
#   PYTHONPATH=.:<reviews>/2026-09-28-refactor-plan/harness python -B <this directory>/old_twin.py
set -u
D="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH=.
section() { printf '\n==================== %s ====================\n' "$*"; }
py() { timeout 3000 python3 -B "$@"; }

section "P1-1 isolated one-block step down after a walking approach"
py "$D/fabric_twin_drop.py"
py "$D/isolated_step_down.py"

section "P1-2 goal revision around the drop start (budget fits one drop)"
py "$D/risk_revision_sweep.py"
py "$D/risk_revision_sweep.py" late
section "P1-2 trace, revision at tick 37"
py "$D/trace_revise_in_drop.py" 37 | head -12
section "P1-2 planner jobs"
py "$D/trace_planner_results.py" | grep -v '^[0-9]'
section "P1-2 same case with budget 4"
py "$D/trace_revise_in_drop.py" 37 1.5,59,4.5 4 | head -1

section "P1-3 risk records per drop"
py "$D/risk_record_growth.py"
section "P1-3 one task ledger, repeated drops"
py "$D/risk_capacity_same_task.py"
section "P1-3 trace at capacity"
py "$D/risk_capacity_trace.py" | tail -8

section "P1-4 constant one-tick latency"
py "$D/fixed_latency.py"
section "P1-4 trace direct_drop_2 under constant latency"
py "$D/trace_fixed_latency.py" direct_drop_2 200
section "P1-4 input responsibility while stopping"
py "$D/why_not_clear.py" | grep '^responsibility'

section "P1-5 landing support removed before departure"
py "$D/landing_removed_timing.py"

section "P2-1 corner landing column and single late tick sweep"
py "$D/corner_and_late.py"
section "P2-1 trace corner landing"
py "$D/trace_corner.py" 2 | head -16
section "P2-1 preparation failure behind 'cancelled'"
py "$D/corner_reason.py" 2 | grep -v '^[0-9]'
py "$D/who_cancels_executor.py" | grep 'cancel:'

section "P2-2 outcome distribution of the frozen seed families"
py "$D/late_success_rate.py" direct_drop_2,half_steps_up_down,direct_drop_5_budget_2 0.2,0.05,0.02
section "P2-2 one lost movement frame at the drop start"
py "$D/lost_arbitration_at_drop.py" 2
py "$D/lost_arbitration_at_drop.py" 5 | head -1
py "$D/lost_arbitration_at_drop.py" 8 | head -1

section "S4 evidence: terminal or stopping state overwritten"
py "$D/terminal_resurrection.py"

section "Supplementary probe interruptions"
py "$D/probe_interruptions.py"
