from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from mc2p.contracts.common import ContractViolation
from mc2p.motion_nav.environment_identity import (
    FROZEN_ENVIRONMENT_SHA256,
    load_frozen_environment,
)


ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENT = ROOT / "config/motion-navigation/environment-v1.json"


class B06EnvironmentIdentityTests(unittest.TestCase):
    def test_repository_environment_is_frozen_to_the_existing_fabric_stack(self):
        identity = load_frozen_environment(ENVIRONMENT)

        self.assertEqual(identity.environment_id, "fabric-1_21-motion-v1")
        self.assertEqual(identity.minecraft_version, "1.21")
        self.assertEqual(identity.fabric_loader_version, "0.15.11")
        self.assertEqual(identity.fabric_api_version, "0.100.6+1.21")
        self.assertEqual(identity.yarn_mappings, "1.21+build.9")
        self.assertEqual(identity.java_major, 21)
        self.assertEqual(identity.python_version, (3, 11))
        self.assertEqual(identity.tick_seconds, 0.05)
        self.assertFalse(identity.automatic_jump)
        self.assertEqual(identity.observation_protocol, "mc2p.client_observation.v3")
        self.assertEqual(identity.action_protocol, "mc2p.action-snapshot.v1")
        self.assertEqual(identity.client_mod_id, "mc2p_deployment_probe")
        self.assertEqual(identity.client_mod_version, "0.1.0")
        self.assertEqual(identity.server_kind, "vanilla-dedicated")
        self.assertEqual(
            identity.baseline_source_commit,
            "1114d84fc8acb19ec2d5e5dcd29bbce67ec04267",
        )
        self.assertEqual(
            set(identity.evidence_roles),
            {"component_fixture", "fabric_observation", "test_oracle"},
        )

    def test_hash_or_version_change_is_rejected_before_profiles_load(self):
        document = json.loads(ENVIRONMENT.read_text("utf-8"))
        document["stack"]["minecraft_version"] = "1.21.1"
        with tempfile.TemporaryDirectory() as directory:
            changed = Path(directory) / "environment.json"
            changed.write_text(json.dumps(document), "utf-8")
            with self.assertRaisesRegex(ContractViolation, "environment configuration hash"):
                load_frozen_environment(changed)

        with self.assertRaisesRegex(ContractViolation, "environment configuration hash"):
            load_frozen_environment(
                ENVIRONMENT,
                expected_sha256="0" * 64 if FROZEN_ENVIRONMENT_SHA256 != "0" * 64 else "1" * 64,
            )

    def test_profiles_must_name_the_same_environment(self):
        identity = load_frozen_environment(ENVIRONMENT)
        identity.require_profile_environment("fabric-1_21-motion-v1")
        with self.assertRaisesRegex(ContractViolation, "profile environment"):
            identity.require_profile_environment("fabric-1_21-other")

    def test_public_motion_navigation_entry_exports_b06_contracts(self):
        from mc2p import motion_nav

        for name in (
            "BlockMotionCatalog", "GoalState", "MovementTransition",
            "ResourceState", "load_frozen_environment",
        ):
            with self.subTest(name=name):
                self.assertIn(name, motion_nav.__all__)
                self.assertTrue(hasattr(motion_nav, name))

    def test_public_motion_navigation_entry_exports_b07_contracts(self):
        from mc2p import motion_nav

        for name in (
            "SupportSurface", "SurfaceNodeId", "StepProfile", "StepController",
            "SurfaceGraph", "SurfacePlanningRequest", "build_surface_graph",
        ):
            with self.subTest(name=name):
                self.assertIn(name, motion_nav.__all__)
                self.assertTrue(hasattr(motion_nav, name))

    def test_public_motion_navigation_entry_exports_b08_contracts(self):
        from mc2p import motion_nav

        for name in (
            "GroundModeProfile", "GroundModeProfiles", "ModeReadiness",
            "evaluate_ground_mode", "load_ground_mode_profiles",
            "movement_for_ground_mode", "observed_ground_mode",
        ):
            with self.subTest(name=name):
                self.assertTrue(hasattr(motion_nav, name))
                self.assertIn(name, motion_nav.__all__)


if __name__ == "__main__":
    unittest.main()
