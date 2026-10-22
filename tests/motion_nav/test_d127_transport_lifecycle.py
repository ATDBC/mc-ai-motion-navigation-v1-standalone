"""D123-2 transport facts and irreversible body-responsibility boundary."""
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mc2p.contracts.action_receipt import behavior_receipt_from_mapping
from mc2p.contracts.action_v1 import ActionSnapshotV1, MovementTickWindowV1, MovementV1
from mc2p.contracts.common import ContractViolation
from mc2p.runtime.backend_v1 import BackendDeliveryUnknown, BackendChannelLost
from mc2p.runtime.transport_lifecycle import (
    TransportLifecycleKind, prove_remote_rejected_before_sampling,
)
from mc2p.skills.navigation_session_driver import (
    RuntimeNavigationDriver, RuntimeNavigationDriverState,
)
from tests.test_action_receipt import receipt_value


def _action():
    return ActionSnapshotV1(
        "ep", 26, 7, 2_000_000_000,
        movement=MovementV1(forward=1, jump=True),
        first_effect_window=MovementTickWindowV1(27, 27),
    )


def _receipt(*, status="rejected", reason="remote_rejected_before_sampling",
             applications=(), events=None):
    if events is None:
        events = ({"status": "not_applied", "movement_tick_id": 27,
                   "episode_id": "ep", "request_sequence_id": 26},)
    return behavior_receipt_from_mapping({
        **receipt_value(
            schema_version="mc2p.client_action_receipt.v4",
            episode_id="ep", request_sequence_id=26,
            generation_id=8, input_samples=35, leased_input_samples=9,
        ),
        "status": status,
        "reason": reason,
        "input_applications": list(applications),
        "dropped_input_samples": 0,
        "oldest_retained_input_tick": 26,
        "timing_events": list(events),
    })


class D127ProtocolFeasibilityTests(unittest.TestCase):
    def test_windowed_rejected_without_complete_not_applied_keeps_ledger_in_flight(self):
        from mc2p.motion_nav.online_motion import (
            InputApplicationLedger, InputApplicationStatus,
        )
        from mc2p.motion_nav.world_model import WorldSessionId
        for receipt in (
            _receipt(events=()),
            replace(_receipt(), generation_id=9),
            _receipt(events=({
                "status": "not_applied", "movement_tick_id": 27,
                "episode_id": "ep", "request_sequence_id": 25,
            },)),
        ):
            with self.subTest(receipt=receipt):
                ledger = InputApplicationLedger(action_timing_v2=True)
                ledger.submit(WorldSessionId("world"), _action(), requested_first_tick=27)
                ledger.observe_receipt(receipt)
                self.assertIs(
                    ledger.record(26).status,
                    InputApplicationStatus.IN_FLIGHT,
                )

    def test_rejected_with_target_application_does_not_rewrite_in_flight_ledger(self):
        from mc2p.motion_nav.online_motion import (
            InputApplicationLedger, InputApplicationStatus,
        )
        from mc2p.motion_nav.world_model import WorldSessionId
        application = {
            "schema_version": "mc2p.input-application.v1",
            "movement_tick_id": 27,
            "episode_id": "ep",
            "request_sequence_id": 26,
            "sampled_at_jvm_ns": 1,
            "state": "leased",
            "forward": 1.0, "strafe": 0.0, "jump": True,
            "sneak": False, "sprint": False,
        }
        ledger = InputApplicationLedger(action_timing_v2=True)
        ledger.submit(WorldSessionId("world"), _action(), requested_first_tick=27)
        ledger.observe_receipt(_receipt(applications=(application,)))
        self.assertIs(ledger.record(26).status, InputApplicationStatus.IN_FLIGHT)

    def test_java_timing_wire_emits_typed_not_applied_identity(self):
        root = Path(__file__).resolve().parents[2]
        source = root / "mc2p/backends/runtime_overlays/mc121_actions/ClientTimingSchedule.java"
        java = Path("D:/My_project/mc_ai/.venv/Library/bin")
        with TemporaryDirectory(prefix="mc2p-d127-not-applied-") as directory:
            built = subprocess.run([
                str(java / "javac.exe"), "-J-Duser.language=en",
                "-encoding", "UTF-8", "-d", directory,
                str(source), str(root / "tests/java/ClientNotAppliedTimingTest.java"),
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            tested = subprocess.run([
                str(java / "java.exe"), "-cp", directory,
                "ClientNotAppliedTimingTest",
            ], capture_output=True, text=True, timeout=30)
            self.assertEqual(tested.returncode, 0, tested.stdout + tested.stderr)
            self.assertIn("CLIENT_NOT_APPLIED_TIMING_OK", tested.stdout)

    def test_v4_full_request_rejection_proves_zero_application_at_causal_tick(self):
        fact = prove_remote_rejected_before_sampling(_action(), _receipt())
        self.assertIs(fact.kind, TransportLifecycleKind.REMOTE_REJECTED_BEFORE_SAMPLING)
        self.assertEqual(
            (fact.episode_id, fact.request_sequence_id, fact.generation_id),
            ("ep", 26, 8),
        )
        self.assertEqual((fact.earliest_effect_tick, fact.latest_effect_tick), (27, 27))
        self.assertEqual(fact.causal_movement_tick, 27)
        self.assertEqual(fact.input_applications, 0)
        self.assertTrue(fact.channel_healthy)

    def test_operation_rejected_never_proves_the_full_request_was_not_applied(self):
        with self.assertRaisesRegex(ContractViolation, "operation_rejected"):
            prove_remote_rejected_before_sampling(
                _action(), _receipt(status="operation_rejected"),
            )

    def test_missing_window_fact_or_target_sample_rejects_not_applied_proof(self):
        application = {
            "schema_version": "mc2p.input-application.v1",
            "movement_tick_id": 27,
            "episode_id": "ep",
            "request_sequence_id": 26,
            "sampled_at_jvm_ns": 1,
            "state": "leased",
            "forward": 1.0,
            "strafe": 0.0,
            "jump": True,
            "sneak": False,
            "sprint": False,
        }
        for receipt in (
            _receipt(events=()),
            _receipt(applications=(application,)),
            replace(_receipt(), generation_id=9),
        ):
            with self.subTest(receipt=receipt), self.assertRaises(ContractViolation):
                prove_remote_rejected_before_sampling(
                    _action(), receipt, expected_generation_id=8,
                )


class D127UnresolvedDriverTests(unittest.TestCase):
    def _driver(self):
        driver = RuntimeNavigationDriver.__new__(RuntimeNavigationDriver)
        driver._state = RuntimeNavigationDriverState.RUNNING
        driver.reason = "running"
        driver._prepared_deadline_ns = 123
        driver._prepared_proposal = object()
        driver._unresolved_evidence = None
        driver._local_resources_closed = False
        driver.source = SimpleNamespace(source_id="source-1", episode_id="ep")
        driver.runtime = SimpleNamespace(
            input_ledger=SimpleNamespace(snapshot=lambda: ("in-flight",)),
            seal_unresolved_after_owner_snapshot=lambda: None,
            close_resources_only=lambda: SimpleNamespace(
                backend_closed=True, trace_closed=True, open_handles=0,
                cleanup_failures=0,
            ),
        )
        driver.session = SimpleNamespace(
            report=SimpleNamespace(state="running"),
            body_route_snapshot="route-1",
            active_motion_mailboxes=("proof-1",),
            active_proof_responsibilities=("proof-1",),
            close_resources_only=lambda: SimpleNamespace(
                live_workers=0, live_receivers=0, live_lease_returners=0,
                live_queue_feeders=0, open_handles=0,
                retained_proof_responsibilities=1,
            ),
        )
        return driver

    def test_delivery_unknown_and_channel_loss_enter_irreversible_unresolved(self):
        for error, expected in (
            (BackendDeliveryUnknown("sent_without_receipt"),
             TransportLifecycleKind.DELIVERY_UNKNOWN),
            (BackendChannelLost("channel_sealed"),
             TransportLifecycleKind.CHANNEL_LOST),
        ):
            with self.subTest(error=type(error).__name__):
                driver = self._driver()
                driver._enter_transport_unresolved(error.lifecycle_fact(_action(), 8, 27))
                self.assertIs(driver.state, RuntimeNavigationDriverState.UNRESOLVED)
                self.assertEqual(driver.reason, "BODY_RESPONSIBILITY_UNRESOLVED")
                self.assertFalse(driver.has_prepared_frame)
                self.assertIs(
                    driver.unresolved_evidence.transport_fact.kind, expected,
                )
                self.assertEqual(driver.unresolved_evidence.ledger_records, ("in-flight",))
                self.assertEqual(driver.unresolved_evidence.source_id, "source-1")
                self.assertEqual(driver.unresolved_evidence.route_identity, "route-1")
                self.assertEqual(
                    driver.unresolved_evidence.proof_responsibilities,
                    ("proof-1",),
                )
                with self.assertRaises(ContractViolation):
                    driver.prepare_proposals(999)

                driver.close_local_resources()
                self.assertTrue(driver.local_resources_closed)
                self.assertIs(driver.state, RuntimeNavigationDriverState.UNRESOLVED)
                self.assertEqual(driver.reason, "BODY_RESPONSIBILITY_UNRESOLVED")
                self.assertIsNotNone(driver.source)

    def test_owner_and_registry_snapshot_precedes_backend_close(self):
        events = []
        driver = self._driver()
        driver.runtime.seal_unresolved_after_owner_snapshot = (
            lambda: events.append("backend_close")
        )
        class Session:
            report = SimpleNamespace(state="running")
            body_route_snapshot = "route-1"

            @property
            def active_proof_responsibilities(self):
                events.append("owner_snapshot")
                return (
                ("proof-executing", "executing"),
                ("proof-safe-tail", "safe_tail"),
            )

        driver.session = Session()
        fact = BackendDeliveryUnknown("sent_without_receipt").lifecycle_fact(
            _action(), 8, 27,
        )
        driver._enter_transport_unresolved(fact)
        self.assertEqual(events, ["owner_snapshot", "backend_close"])
        self.assertEqual(
            driver.unresolved_evidence.proof_responsibilities,
            (("proof-executing", "executing"),
             ("proof-safe-tail", "safe_tail")),
        )

    def test_resource_only_close_uses_aggregate_reports_instead_of_assuming_success(self):
        driver = self._driver()
        driver._state = RuntimeNavigationDriverState.UNRESOLVED
        driver.runtime.close_resources_only = lambda: SimpleNamespace(
            backend_closed=True, trace_closed=True, open_handles=0,
        )
        driver.session.close_resources_only = lambda: SimpleNamespace(
            live_workers=0, live_receivers=1, live_lease_returners=0,
            live_queue_feeders=0, open_handles=0,
            retained_proof_responsibilities=2,
        )
        driver.close_local_resources()
        self.assertFalse(driver.local_resources_closed)


class D127FormalRuntimeLifecycleTests(unittest.TestCase):
    def test_legacy_backend_step_result_remains_observation_invariant(self):
        from mc2p.runtime.backend import BackendStepResultV0
        case = self._case()
        native_step = case.backend.step
        try:
            def legacy(action, deadline, *, observation_request=None):
                result = native_step(
                    action, deadline, observation_request=observation_request,
                )
                return BackendStepResultV0(
                    result.observation, result.reward,
                    result.terminated, result.truncated,
                )
            case.backend.step = legacy
            result = case.tick()
            self.assertEqual(
                result.report.failure.code.value, "observation_invariant",
            )
        finally:
            case.close()

    def test_malformed_rejected_receipt_still_enters_typed_unresolved(self):
        from scripts.d122_formal_timing_probe import _factory
        from scripts.d121_handoff_execution_probe import ExecutionCase
        with patch(
            "scripts.d121_handoff_execution_probe.CalculatorBackend",
            _factory("explicit_reject"),
        ):
            case = ExecutionCase(
                step_window_ns=1_000_000_000, action_timing_v2=True,
            )
        try:
            case.prepare_successor()
            native_step = case.backend.step
            def malformed(action, deadline, *, observation_request=None):
                result = native_step(
                    action, deadline, observation_request=observation_request,
                )
                if result.receipt.status != "rejected":
                    return result
                sample = result.receipt.input_applications[0]
                malformed_receipt = replace(
                    result.receipt,
                    timing_events=(),
                )
                object.__setattr__(
                    malformed_receipt, "input_applications", (sample, sample),
                )
                return replace(result, receipt=malformed_receipt)
            case.backend.step = malformed
            for _ in range(40):
                result = case.tick()
                if (result.transport_fact is not None
                        or case.runtime.state.value != "ready"):
                    break
            self.assertIs(
                result.transport_fact.kind,
                TransportLifecycleKind.DELIVERY_UNKNOWN,
            )
            self.assertIs(
                case.driver.state, RuntimeNavigationDriverState.UNRESOLVED,
            )
            self.assertIsNotNone(case.driver.source)
            self.assertTrue(any(
                record.action.request_sequence_id == 26
                and record.status.value == "in_flight"
                for record in case.runtime.input_ledger.snapshot()
            ))
        finally:
            case.close()

    def test_transport_preflight_unlocks_only_the_registered_product_modes(self):
        from unittest.mock import Mock
        from scripts import d123_actor_calibration as preflight
        from scripts import d123_transport_preflight
        rows = {
            "timely_l1": {"passed": True},
            "late_l2": {"passed": True},
            "explicit_reject": {
                "passed": True,
                "transport_facts": [{
                    "kind": "remote_rejected_before_sampling",
                }],
            },
        }
        offline = {
            "status": "PASS",
            "transport_lifecycle": d123_transport_preflight.generate(
                runner=lambda policy: rows[policy],
            ),
        }
        for mode in ("smoke", "matrix"):
            with self.subTest(mode=mode):
                launch = Mock(return_value=0)
                self.assertEqual(preflight.dispatch(mode, offline, launch), 0)
                launch.assert_called_once_with()
        mutated = dict(offline)
        mutated["transport_lifecycle"] = {"status": "PASS"}
        launch = Mock()
        self.assertEqual(preflight.dispatch("smoke", mutated, launch), 2)
        launch.assert_not_called()

    def test_preflight_rejects_a_caller_supplied_pass_dictionary(self):
        from unittest.mock import Mock
        from scripts import d123_actor_calibration as preflight
        launch = Mock()
        forged = {
            "status": "PASS",
            "transport_lifecycle": {
                "status": "PASS",
                "formal_states": [
                    "timely", "late_window_rejected",
                    "remote_rejected_before_sampling",
                ],
            },
        }
        self.assertEqual(preflight.dispatch("smoke", forged, launch), 2)
        launch.assert_not_called()

    def test_repository_preflight_generator_requires_three_formal_chain_results(self):
        from scripts import d123_transport_preflight as transport_preflight

        rows = {
            "timely_l1": {"passed": True},
            "late_l2": {"passed": True},
            "explicit_reject": {
                "passed": True,
                "transport_facts": [{
                    "kind": "remote_rejected_before_sampling",
                }],
            },
        }
        result = transport_preflight.generate(
            runner=lambda policy: rows[policy],
        )
        self.assertTrue(transport_preflight.validate(result))
        self.assertEqual(result.formal_states, (
            "timely", "late_window_rejected",
            "remote_rejected_before_sampling",
        ))
        broken = dict(rows)
        broken["explicit_reject"] = {"passed": True, "transport_facts": []}
        with self.assertRaises(ContractViolation):
            transport_preflight.generate(runner=lambda policy: broken[policy])

    def test_transport_fact_kind_fields_and_classification_are_mutually_exclusive(self):
        from mc2p.runtime.transport_lifecycle import (
            TransportDispatchStage, TransportLifecycleFactV1,
            classify_transport_failure,
        )
        with self.assertRaises(ContractViolation):
            TransportLifecycleFactV1(
                TransportLifecycleKind.DELIVERY_UNKNOWN, "ep", 1, 2,
                3, 3, 3, 0, True, "bad",
                TransportDispatchStage.SENT_NO_RECEIPT,
            )
        delivery = classify_transport_failure(
            channel_sealed=False, outbound_started=True,
            outbound_completed=True, response_correlated=False,
        )
        self.assertIs(delivery[0], TransportLifecycleKind.DELIVERY_UNKNOWN)
        self.assertIs(delivery[1], TransportDispatchStage.SENT_NO_RECEIPT)
        self.assertIsNone(
            BackendDeliveryUnknown("sent_without_receipt").lifecycle_fact(
                _action(), 8, 27,
            ).channel_healthy,
        )
        sealed = classify_transport_failure(
            channel_sealed=True, outbound_started=True,
            outbound_completed=False, response_correlated=False,
        )
        self.assertIs(sealed[0], TransportLifecycleKind.CHANNEL_LOST)
        self.assertIs(sealed[1], TransportDispatchStage.CHANNEL_SEALED)

    def test_simulator_does_not_emit_not_applied_for_nonwindowed_rejection(self):
        from tests.sim.client_timing import ClientTimingSampler
        sampler = ClientTimingSampler("ep")
        action = ActionSnapshotV1("ep", 1, 0, 1_000_000_000)
        sampler.reject_admission(action, 1, 0, 0)
        self.assertFalse(any(event.status.value == "not_applied"
                             for event in sampler.events))

    def _case(self):
        from scripts.d121_handoff_execution_probe import ExecutionCase
        return ExecutionCase(
            action_timing_v2=True, step_window_ns=1_000_000_000,
        )

    def test_unknown_delivery_and_lost_channel_preserve_all_body_owners(self):
        for error_type, kind in (
            (BackendDeliveryUnknown, TransportLifecycleKind.DELIVERY_UNKNOWN),
            (BackendChannelLost, TransportLifecycleKind.CHANNEL_LOST),
        ):
            with self.subTest(error_type=error_type.__name__):
                case = self._case()
                case.prepare_successor()
                native_step = case.backend.step

                def lose_receipt(action, deadline, *, observation_request=None):
                    if (action.first_effect_window is not None
                            and action.first_effect_window.earliest_tick == 27
                            and action.movement.jump and action.safe_default is None):
                        raise error_type("transport_boundary_lost")
                    return native_step(
                        action, deadline, observation_request=observation_request,
                    )

                case.backend.step = lose_receipt
                result = None
                for _ in range(40):
                    result = case.tick()
                    if result.transport_fact is not None:
                        break
                self.assertIsNotNone(result)
                self.assertIs(result.transport_fact.kind, kind)
                self.assertEqual(result.report.phase, "BODY_RESPONSIBILITY_UNRESOLVED")
                self.assertEqual(case.runtime.state.value, "failed")
                self.assertIs(case.driver.state, RuntimeNavigationDriverState.UNRESOLVED)
                self.assertIsNotNone(case.driver.source)
                source = case.driver.source
                self.assertTrue(case.runtime.has_ordered_source(source))
                self.assertTrue(any(record.status.value == "in_flight"
                                    for record in case.runtime.input_ledger.snapshot()))
                before = case.driver.unresolved_evidence
                with self.assertRaises(ContractViolation):
                    case.tick()
                case.driver.close_local_resources()
                # The fixture's inline worker handles deliberately remain
                # live after close, so the aggregate must stay incomplete.
                self.assertFalse(case.driver.local_resources_closed)
                self.assertEqual(case.driver.unresolved_evidence, before)
                self.assertIsNotNone(case.driver.source)
                self.assertTrue(case.runtime.has_ordered_source(source))

    def test_remote_full_rejection_returns_fresh_observation_and_keeps_runtime_ready(self):
        from scripts.d122_formal_timing_probe import _factory
        from scripts.d121_handoff_execution_probe import ExecutionCase
        with patch(
            "scripts.d121_handoff_execution_probe.CalculatorBackend",
            _factory("explicit_reject"),
        ):
            case = ExecutionCase(
                action_timing_v2=True, step_window_ns=1_000_000_000,
            )
        case.prepare_successor()
        result = None
        for _ in range(50):
            result = case.tick()
            if (result.transport_fact is not None
                    and result.transport_fact.kind
                    is TransportLifecycleKind.REMOTE_REJECTED_BEFORE_SAMPLING):
                break
        self.assertIsNotNone(result)
        self.assertIs(
            result.transport_fact.kind,
            TransportLifecycleKind.REMOTE_REJECTED_BEFORE_SAMPLING,
        )
        self.assertEqual(result.report.phase, "REMOTE_REJECTED_BEFORE_SAMPLING")
        self.assertEqual(case.runtime.state.value, "ready")
        self.assertEqual(
            case.runtime.input_ledger.record(
                result.transport_fact.request_sequence_id,
            ).status.value,
            "rejected",
        )
        self.assertGreater(
            result.observation.self_state.value.movement_tick_id,
            result.transport_fact.causal_movement_tick,
        )
        for _ in range(8):
            if case.driver.source is None:
                break
            case.tick()
        self.assertIsNone(case.driver.source)
        self.assertFalse(case.driver.has_prepared_frame)

    def test_runtime_deadline_before_backend_call_is_local_not_dispatched(self):
        case = self._case()
        case.prepare_successor()
        native_write = case.trace.write
        backend_actions_before = None

        def expire_successor(record_type, payload):
            nonlocal backend_actions_before
            native_write(record_type, payload)
            if record_type != "dispatch":
                return
            action = payload["decision"].action
            if (action.first_effect_window is not None
                    and action.first_effect_window.earliest_tick == 27
                    and action.movement.jump and action.safe_default is None):
                backend_actions_before = len(case.backend.actions)
                case.clock[0] = action.deadline_monotonic_ns

        case.trace.write = expire_successor
        local = None
        for _ in range(40):
            local = case.tick()
            if (local.transport_fact is not None
                    and local.transport_fact.kind is TransportLifecycleKind.LOCAL_NOT_DISPATCHED):
                break
        self.assertIsNotNone(local)
        self.assertIs(local.transport_fact.kind, TransportLifecycleKind.LOCAL_NOT_DISPATCHED)
        self.assertEqual(len(case.backend.actions), backend_actions_before)
        self.assertEqual(case.runtime.state.value, "ready")
        self.assertEqual(case.driver.reason, "local_not_dispatched_observation_pending")
        failed_sequence = local.transport_fact.request_sequence_id

        followup = case.driver.tick(
            __import__("mc2p.contracts.behavior", fromlist=["BehaviorProfileV0"]).BehaviorProfileV0(),
            case.clock[0] + 1_000_000_000,
        )
        self.assertGreater(followup.decision.action.request_sequence_id, failed_sequence)
        self.assertEqual(
            followup.report.phase, "local_not_dispatched_observation_exchange",
        )
        self.assertGreaterEqual(
            followup.observation.self_state.value.movement_tick_id,
            local.transport_fact.causal_movement_tick,
        )
        self.assertTrue(any(
            record.action.safe_default is not None
            and record.default_resolved_at_tick is not None
            for record in case.runtime.input_ledger.snapshot()
        ))
        for _ in range(8):
            if case.driver.source is None:
                break
            case.tick()
        self.assertIsNone(case.driver.source)
        self.assertFalse(case.driver.has_prepared_frame)


if __name__ == "__main__":
    unittest.main()
