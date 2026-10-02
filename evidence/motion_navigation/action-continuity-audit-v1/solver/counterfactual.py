"""Read-only diagnostic: replay measured entry against observed world facts.

Diagnostic candidates do not pass production admission or grant capabilities.
"""
from dataclasses import replace
import hashlib
import json
import math
from pathlib import Path
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from scripts.formal_observation_v3_trace import restore_formal_surface_observation_v3
from mc2p.motion_nav.runtime_adapter import NavigationObservationAdapter
from mc2p.motion_nav.physics_adapter import PhysicsWorldView, build_physics_state, PhysicsWorldBounds
from mc2p.motion_nav.physics_types import JAVA_1_21_RULESET, CalculationStatus
from mc2p.motion_nav.motion_residual import _ORDINARY_PLAYER_ASSUMPTIONS
from mc2p.motion_nav.online_motion import StateAnchor, MotionTickPhase, CandidateExecutionWindow, project_movement_command
from mc2p.motion_nav import motion_solver as solver
from mc2p.motion_nav.motion_solver import (
    GapSolveRequest, LandingRegion, AirTransitionCommandTemplate,
    MotionSolveKind, SolveStatus, MotionCommandTick,
)
from mc2p.contracts.action_v1 import MovementV1

DEMO = ROOT / 'artifacts/current-navigation-demo/20261002T130530628560Z-9880fb4f'
HISTORICAL_FIRST = '--historical-first' in sys.argv
OUT = Path(__file__).parent / ('counterfactual-first-match-relabelled-reconstruction.json'
                             if HISTORICAL_FIRST else 'counterfactual.json')


def observed_entry():
    target = next(json.loads(line) for line in (DEMO / 'navigation-frames.jsonl').read_text('utf-8').splitlines()
                  if json.loads(line)['tick'] == 16526)
    adapter = NavigationObservationAdapter()
    latest = -1
    n = 0
    with (DEMO / 'game/MC2PFollower/trace.jsonl').open('r', encoding='utf-8') as stream:
        for line_number, line in enumerate(stream, 1):
            item = json.loads(line)
            p = item['payload']
            raw = p.get('result', {}).get('observation') or p.get('backend_result', {}).get('observation')
            if not raw or raw['sequence_id'] <= latest:
                continue
            snapshot = restore_formal_surface_observation_v3(raw)
            frame = adapter.ingest(snapshot)
            latest = raw['sequence_id']
            n += 1
            if ((HISTORICAL_FIRST or snapshot.self_state.value.movement_tick_id == target['tick'])
                    and all(abs(a-b) <= 1e-10 for a,b in zip(frame.body.position, target['position']))):
                built = build_physics_state(frame, JAVA_1_21_RULESET, _ORDINARY_PLAYER_ASSUMPTIONS)
                if built.state is None:
                    raise RuntimeError(built)
                state = replace(built.state, movement_tick_id=target['tick'])
                anchor = StateAnchor(frame.session, latest, state.movement_tick_id,
                                     MotionTickPhase.AFTER_MOVEMENT, None, None,
                                     state.ruleset_id, state.state_schema, 'mc2p.input-projection.v1',
                                     state, raw['health_points']['value'], snapshot.self_state.value.absorption_points)
                # Observed facts only. The larger snapshot does not fill unknowns.
                world = PhysicsWorldView(frame.world, JAVA_1_21_RULESET).snapshot(
                    PhysicsWorldBounds(222, 226, 94, 106, -1, 13))
                return anchor, world, dict(trace_line=line_number, observation_sequence=latest,
                                          restored_observations=n, state=state.to_mapping(),
                                          own_movement_tick=snapshot.self_state.value.movement_tick_id,
                                          explicit_assumptions=_ORDINARY_PLAYER_ASSUMPTIONS,
                                          raw_observation_sha256=hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
                                          source_consistent_tick=(snapshot.self_state.value.movement_tick_id==state.movement_tick_id),
                                          historical_first_match_reconstruction=HISTORICAL_FIRST)
    raise RuntimeError('Measured entry not found')


def metrics(result, calls, elapsed):
    out = dict(status=result.status.value, reasons=result.reasons, missing=result.missing_cells,
               evaluated=result.candidates_evaluated, physics_step_calls=calls, elapsed_ns=elapsed)
    proof = result.proof
    if proof is None:
        return out
    airborne = [i for i, s in enumerate(proof.trajectory) if not s.on_ground]
    landing_i = next((i for i in range(1, len(proof.trajectory))
                      if proof.trajectory[i].on_ground and not proof.trajectory[i-1].on_ground), None)
    out.update(commands=[dict(forward=c.movement.forward, jump=c.movement.jump,
                              sprint=c.movement.sprint) for c in proof.commands],
               command_count=len(proof.commands), landing_index=landing_i,
               landing_position=proof.trajectory[landing_i].position if landing_i else None,
               landing_speed_bps=math.hypot(proof.trajectory[landing_i].velocity_blocks_per_tick[0],
                                           proof.trajectory[landing_i].velocity_blocks_per_tick[2])*20 if landing_i else None,
               exit_position=proof.exit_state.position,
               exit_speed_bps=math.hypot(proof.exit_state.velocity_blocks_per_tick[0],
                                       proof.exit_state.velocity_blocks_per_tick[2])*20,
               expected_damage=proof.maximum_expected_damage_points,
               dependency_count=len(proof.world_dependencies),
               dependencies=proof.world_dependencies,
               delayed_variants=[dict(start_tick=v.start_tick, exit_position=v.exit_state.position,
                                     exit_speed_bps=math.hypot(v.exit_state.velocity_blocks_per_tick[0],
                                                             v.exit_state.velocity_blocks_per_tick[2])*20,
                                     all_release_safe=len(v.release_safe_command_indices)==len(v.tick_inputs))
                                 for v in proof.delayed_start_variants],
               all_release_safe=len(proof.release_safe_command_indices)==len(proof.commands),
               recovery_horizon_ticks=proof.recovery_horizon_ticks)
    return out


def run(anchor, world, request):
    calls = 0
    original = solver.step
    validations = []
    original_validate = solver.validate_gap_trajectory
    def validating(trajectory, events, requested):
        result = original_validate(trajectory, events, requested)
        validations.append(dict(accepted=result.accepted,reasons=result.reasons,
                                final_position=trajectory[-1].position,
                                final_speed_bps=math.hypot(trajectory[-1].velocity_blocks_per_tick[0],
                                                          trajectory[-1].velocity_blocks_per_tick[2])*20))
        return result
    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)
    started = time.perf_counter_ns()
    with patch.object(solver, 'step', counted), patch.object(solver, 'validate_gap_trajectory', validating):
        result = solver.solve_air_transition(anchor, world, request)
    output = metrics(result, calls, time.perf_counter_ns()-started)
    output['validation_trace'] = validations
    return output, result


def replay_continuous(anchor, world, request, airborne_sprint):
    """A concrete 13-tick diagnostic list; normal template forbids post>10.

    Reuse production replay, release recovery, delayed-start and damage checks.
    This is not a published policy or actor candidate.
    """
    commands = (MotionCommandTick(MovementV1(forward=1,jump=True,sprint=True),0.0),)
    commands += (MotionCommandTick(MovementV1(forward=1,sprint=airborne_sprint),0.0),)*11
    commands += (MotionCommandTick(MovementV1(forward=1),0.0),)
    original = solver.step
    calls = 0
    def counted(*args,**kwargs):
        nonlocal calls
        calls += 1
        return original(*args,**kwargs)
    started = time.perf_counter_ns()
    with patch.object(solver,'step',counted):
        primary, failure = solver._replay_verified_commands(anchor.physics_state,commands,world,request,start_tick=16527)
        delayed, deps, resource, delayed_failure = solver._prove_delayed_starts(anchor,commands,world,request) if failure is None else ((),(),(),None)
    out = dict(airborne_sprint=airborne_sprint,command_count=len(commands),physics_step_calls=calls,
               elapsed_ns=time.perf_counter_ns()-started,recovery_horizon_ticks=request.recovery_horizon_ticks,
               generated_candidate_count=1,production_admitted=False)
    if failure or delayed_failure:
        failed = failure or delayed_failure
        out.update(status=failed.status.value,reasons=failed.reasons,missing=failed.missing_cells)
        return out
    maximum_damage=max(solver._trajectory_damage_points(primary.trajectory),
                       *(solver._trajectory_damage_points(v.trajectory) for v in delayed))
    allowed=request.damage_budget.allows(maximum_damage,health_points=anchor.health_points,absorption_points=anchor.absorption_points)
    landing_i=next(i for i in range(1,len(primary.trajectory)) if primary.trajectory[i].on_ground and not primary.trajectory[i-1].on_ground)
    out.update(status='diagnostic_all_original_checks_passed' if allowed else 'damage_budget_exceeded',
               maximum_expected_damage=maximum_damage,
               all_release_safe=len(primary.release_safe_command_indices)==len(commands),
               landing_index=landing_i,landing_position=primary.trajectory[landing_i].position,
               landing_speed_bps=math.hypot(primary.trajectory[landing_i].velocity_blocks_per_tick[0],primary.trajectory[landing_i].velocity_blocks_per_tick[2])*20,
               exit_position=primary.exit_state.position,
               exit_speed_bps=math.hypot(primary.exit_state.velocity_blocks_per_tick[0],primary.exit_state.velocity_blocks_per_tick[2])*20,
               dependencies=sorted(set(primary.world_dependencies)|set(deps)),
               dependency_count=len(set(primary.world_dependencies)|set(deps)),
               delayed_variants=[dict(start_tick=v.start_tick,exit_position=v.exit_state.position,
                                     exit_speed_bps=math.hypot(v.exit_state.velocity_blocks_per_tick[0],v.exit_state.velocity_blocks_per_tick[2])*20,
                                     all_release_safe=len(v.release_safe_command_indices)==len(commands)) for v in delayed],
               commands=[dict(forward=c.movement.forward,jump=c.movement.jump,sprint=c.movement.sprint) for c in commands])
    return out


def main():
    anchor, world, source = observed_entry()
    base = GapSolveRequest((0,1), LandingRegion(224.3,224.7,4.3,4.7,100),
                          CandidateExecutionWindow(16527,16528),
                          exit_direction=(0,1), exit_motion_ticks=1,
                          recovery_horizon_ticks=20).as_air_transition()
    payload = dict(scope='Diagnostic only; no actor admission; no game launched', source=source,
                   invariant='same measured entry, profile-4 known facts, original entry domain, 20 tick recovery, late-one-tick variant, original 4.4 bps exit cap',
                   variants={}, each_original_template=[], wider_sprint_forward_templates=[],continuous_forward_replays=[])
    for name, request in (
        ('A_original_region_original_templates',base),
        ('B_known_platform_region_original_templates',replace(base, landing=replace(base.landing,max_z=8.7))),
        ('C_original_region_non_sprint_templates',replace(base,policy=replace(base.policy,
            templates=tuple(replace(t,sprint=False) for t in base.policy.templates)))),
        ('D_original_region_forward_through_air',replace(base, policy=replace(base.policy,
            templates=(AirTransitionCommandTemplate(0,1,True,False,1,10),)))),
        ('E_known_platform_forward_through_air',replace(base, landing=replace(base.landing,max_z=8.7),
            policy=replace(base.policy,templates=(AirTransitionCommandTemplate(0,1,True,False,1,10),)))),
    ):
        output, result = run(anchor,world,request)
        payload['variants'][name]=output
    for i,t in enumerate(base.policy.templates):
        single = replace(base,policy=replace(base.policy, templates=(t,)),max_candidates=1)
        output, result = run(anchor,world,single)
        payload['each_original_template'].append(dict(index=i,template=dict(
            pre=t.pre_action_forward_ticks,sprint=t.sprint,post_axis=t.post_action_axis,
            post_ticks=t.post_action_ticks),**output))
    for ticks in (2,4,6,8,10):
        template = AirTransitionCommandTemplate(0,1,True,True,1,ticks)
        request = replace(base,landing=replace(base.landing,max_z=8.7),
                          policy=replace(base.policy,templates=(template,)),max_candidates=1)
        output, result = run(anchor,world,request)
        payload['wider_sprint_forward_templates'].append(dict(post_forward_ticks=ticks,**output))
    for airborne_sprint in (False,True):
        payload['continuous_forward_replays'].append(replay_continuous(
            anchor,world,replace(base,landing=replace(base.landing,max_z=8.7)),airborne_sprint))
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    for name, item in payload['variants'].items():
        print(name, json.dumps({k:v for k,v in item.items() if k not in {'dependencies','commands','validation_trace'}},ensure_ascii=False))
    print('each_original_template',json.dumps([dict(index=i['index'],status=i['status'],reasons=i['reasons'],
                                                  exit_z=i.get('exit_position',[None,None,None])[2],
                                                  exit_speed_bps=i.get('exit_speed_bps'),calls=i['physics_step_calls'])
                                                  for i in payload['each_original_template']],ensure_ascii=False))
    print('wider_sprint_forward_templates',json.dumps([{k:v for k,v in i.items()
                                                        if k not in {'dependencies','commands','validation_trace'}}
                                                       for i in payload['wider_sprint_forward_templates']],ensure_ascii=False))
    print('continuous_forward_replays',json.dumps([{k:v for k,v in i.items() if k not in {'dependencies','commands'}}
                                                  for i in payload['continuous_forward_replays']],ensure_ascii=False))


if __name__ == '__main__':
    main()
