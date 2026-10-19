"""D105 T2: narrow only the same-release local receiver audit."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from mc2p.motion_nav import prepared_motion_validation as validation
from mc2p.motion_nav.motion_worker import MotionSolverWorker
from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.prepared_motion import (
    PreparedAdmissionStatus, PreparedMotionHandle, PreparedMotionHotView,
    PreparedRejectReason,
)
from tests.motion_nav import test_d102_receiver as receiver_fixtures


class _PluginMotionWorker(MotionSolverWorker):
    """A plugin may inherit the port shape but is outside the release boundary."""


class LocalReceiverTrustTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        receiver_fixtures.ReceiverTests.setUpClass()
        cls.request = receiver_fixtures.ReceiverTests.request
        cls.snapshot = receiver_fixtures.ReceiverTests.snapshot
        cls.bundle = receiver_fixtures.ReceiverTests.bundle
        cls.message = receiver_fixtures.ReceiverTests.message
        cls.current = receiver_fixtures.ReceiverTests.current

    def validate_local(self, message=None, *, request=None, current=None):
        return validation._validate_for_registration(
            message or self.message,
            request or self.request,
            self.snapshot,
            current or self.current,
            trusted_local_worker=True,
        ).admission

    def test_only_exact_bundled_worker_gets_local_release_trust(self) -> None:
        local = object.__new__(MotionSolverWorker)
        plugin = object.__new__(_PluginMotionWorker)
        self.assertTrue(validation.local_worker_result_is_trusted(local))
        self.assertFalse(validation.local_worker_result_is_trusted(plugin))
        self.assertFalse(validation.local_worker_result_is_trusted(receiver_fixtures.QueuedWorker()))

    def test_local_result_skips_only_duplicate_recursive_audits(self) -> None:
        canonical_roots = []
        original = validation.canonical_motion_digest

        def observed(value):
            canonical_roots.append(type(value))
            return original(value)

        with patch.object(validation, "require_immutable_graph",
                          side_effect=AssertionError("recursive graph audit ran")), \
                patch.object(validation, "_validate_values",
                             side_effect=AssertionError("constructor replay ran")), \
                patch.object(validation, "canonical_bundle_bytes",
                             side_effect=AssertionError("full bundle canonicalization ran")), \
                patch.object(validation, "validate_sealed_snapshot",
                             side_effect=AssertionError("snapshot canonicalization ran")), \
                patch.object(validation, "canonical_motion_digest", side_effect=observed):
            result = self.validate_local()

        self.assertIs(result.status, PreparedAdmissionStatus.REGISTERED)
        self.assertTrue(canonical_roots)
        self.assertEqual(set(canonical_roots), {tuple},
                         "the trusted path may still hash bounded parent inputs, not the proof graph")

    def test_local_result_keeps_message_boundary_and_parent_authority(self) -> None:
        cases = []
        cases.append(("message_digest", receiver_fixtures.raw_message(
                          self.bundle, payload_digest="0" * 64),
                      self.request, self.current, PreparedRejectReason.MESSAGE))

        request_changes = (
            ("request", {"goal_revision": self.request.goal_revision + 1}, PreparedRejectReason.REQUEST),
            ("generation", {"stream": replace(self.request.stream, generation_id="other-generation")},
             PreparedRejectReason.GENERATION),
            ("owner", {"stream": replace(self.request.stream, owner_id="other-owner")},
             PreparedRejectReason.OWNER),
            ("route_action", {"route_revision": self.request.route_revision + 1},
             PreparedRejectReason.ROUTE_ACTION),
            ("ledger", {"ledger_id": "other-ledger"}, PreparedRejectReason.LEDGER),
            ("world", {"snapshot_lease_token": "other-lease"}, PreparedRejectReason.WORLD_SNAPSHOT),
        )
        for name, changes, reason in request_changes:
            changed = receiver_fixtures.unchecked_replace(self.request, **changes)
            forged = receiver_fixtures.reseal(receiver_fixtures.unchecked_replace(
                self.bundle, request=changed))
            cases.append((name, receiver_fixtures.raw_message(
                              forged, job_token=self.request.job_token),
                          self.request, self.current, reason))

        other_inputs = self.request.expected_inputs[:-1] + (
            replace(self.request.expected_inputs[-1],
                    sprint=not self.request.expected_inputs[-1].sprint),
        )
        forged = receiver_fixtures.reseal(receiver_fixtures.unchecked_replace(
            self.bundle, inputs=other_inputs))
        cases.append(("inputs", receiver_fixtures.raw_message(forged),
                      self.request, self.current,
                      PreparedRejectReason.INPUTS))
        forged = receiver_fixtures.reseal(receiver_fixtures.unchecked_replace(
            self.bundle,
            ruleset=replace(self.bundle.ruleset, ruleset_id="other-rules"),
        ))
        cases.append(("ruleset", receiver_fixtures.raw_message(forged),
                      self.request, self.current,
                      PreparedRejectReason.RULESET))

        versions = self.current.dependency_versions
        damage_request = replace(
            self.request,
            damage_budget=replace(
                self.request.damage_budget,
                risk_policy_id="bounded-d105-test",
                maximum_expected_damage_points=1.,
            ),
            damage_remaining_points=1.,
        )
        damage_bundle = receiver_fixtures.reseal(receiver_fixtures.unchecked_replace(
            self.bundle, request=damage_request))
        cases.extend((
            ("window", self.message, self.request,
             replace(self.current, permitted_window=replace(
                 self.current.permitted_window,
                 earliest_start_tick=self.current.permitted_window.earliest_start_tick + 1,
                 latest_start_tick=self.current.permitted_window.latest_start_tick + 1)), None),
            ("damage", receiver_fixtures.raw_message(damage_bundle), damage_request,
             self.current, None),
            ("dependency", self.message, self.request,
             replace(self.current, dependency_versions=(
                 (versions[0][0], versions[0][1] + 1),) + versions[1:]),
             PreparedRejectReason.DEPENDENCY),
        ))

        for name, message, request, current, reason in cases:
            with self.subTest(name=name):
                result = self.validate_local(message, request=request, current=current)
                if reason is None:
                    self.assertIs(result.status, PreparedAdmissionStatus.STALE)
                    self.assertIsNone(result.handle)
                else:
                    self.assertIs(result.reason, reason)
                    self.assertIsNone(result.handle)

    def test_trusted_hot_view_rejects_dataclass_field_drift(self) -> None:
        native_fields = validation.fields

        def changed(value):
            current = native_fields(value)
            if value is PreparedMotionHotView:
                return current + (SimpleNamespace(name="future_hot_field"),)
            return current

        with patch.object(validation, "fields", side_effect=changed), \
                self.assertRaisesRegex(ContractViolation, "schema"):
            validation._trusted_hot_view(self.bundle)

    def test_trusted_handle_rejects_dataclass_field_drift(self) -> None:
        hot = validation._trusted_hot_view(self.bundle)
        native_fields = validation.fields

        def changed(value):
            current = native_fields(value)
            if value is PreparedMotionHandle:
                return current + (SimpleNamespace(name="future_handle_field"),)
            return current

        with patch.object(validation, "fields", side_effect=changed), \
                self.assertRaisesRegex(ContractViolation, "schema"):
            validation._trusted_handle(self.bundle, self.request, hot)


if __name__ == "__main__":
    unittest.main()
