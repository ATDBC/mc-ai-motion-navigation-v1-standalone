"""The C1 fixture is a server-only, seed-audited local acceptance tool."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]


class FabricC1FixtureTests(unittest.TestCase):
    def test_descriptor_is_server_only_and_has_no_client_entrypoint(self):
        descriptor = json.loads((ROOT / "deployment/fabric-c1-fixture-server/src/main/resources/fabric.mod.json").read_text("utf-8"))
        self.assertEqual(descriptor["environment"], "server")
        self.assertEqual(descriptor["entrypoints"], {"main": ["com.mc2p.fixture.C1FixtureServer"]})
        self.assertNotIn("client", descriptor["entrypoints"])

    def test_source_sets_seed_before_spawn_and_restricts_console_commands(self):
        source = (ROOT / "deployment/fabric-c1-fixture-server/src/main/java/com/mc2p/fixture/C1FixtureServer.java").read_text("utf-8")
        self.assertIn("hasPermissionLevel(4)", source)
        self.assertLess(source.index("getRandom().setSeed(seed)"),
                        source.index("spawnEntity(zombie)"))
        self.assertIn("seed_applied_before_first_ai_tick", source)
        self.assertIn("c1-fixture-events.jsonl", source)
        self.assertIn("mc2p_c1_spawn", source)
        self.assertIn("mc2p_c1_remove", source)

    def test_damage_isolation_is_limited_to_tagged_fixture_zombie_attacks(self):
        source = (ROOT / "deployment/fabric-c1-fixture-server/src/main/java/com/mc2p/fixture/C1FixtureServer.java").read_text("utf-8")
        self.assertIn("ServerLivingEntityEvents.ALLOW_DAMAGE", source)
        self.assertIn("source.getAttacker()", source)
        self.assertIn("ServerPlayerEntity", source)
        self.assertIn("getCommandTags", source)
        self.assertNotIn("source.getSource() instanceof ServerPlayerEntity", source)

    def test_damage_mode_defaults_to_isolated_and_real_mode_records_before_allowing(self):
        source = (ROOT / "deployment/fabric-c1-fixture-server/src/main/java/com/mc2p/fixture/C1FixtureServer.java").read_text("utf-8")
        self.assertIn('StringArgumentType.getString(context, "damage_mode")', source)
        self.assertIn('"isolated"', source)
        self.assertIn('"real"', source)
        self.assertIn('damage_allowed', source)
        self.assertIn('world.getTime()', source)
        self.assertLess(source.index('damage_allowed'),
                        source.index('return true;', source.index('damage_allowed')))

    def test_controlled_strike_uses_vanilla_attack_without_writing_player_state(self):
        source = (ROOT / "deployment/fabric-c1-fixture-server/src/main/java/com/mc2p/fixture/C1FixtureServer.java").read_text("utf-8")
        self.assertIn('"mc2p_c1_strike"', source)
        self.assertIn("zombie.tryAttack(player)", source)
        self.assertIn("controlled_attack", source)
        self.assertIn("requested_phase", source)
        self.assertNotIn("player.setHealth", source)
        self.assertNotIn("player.setVelocity", source)
        self.assertNotIn("player.setPosition", source)

    def test_offline_artifact_records_exact_sources_and_server_manifest(self):
        from scripts.build_fabric_c1_fixture import build_fixture, inspect_fixture
        artifact = build_fixture(timeout_seconds=180)
        evidence = inspect_fixture(artifact.jar)
        self.assertTrue(evidence["server_only"])
        self.assertTrue(evidence["sources_match"])
        self.assertGreater(evidence["verified_classpath_file_count"], 0)
        with ZipFile(artifact.jar) as archive:
            self.assertIn("com/mc2p/fixture/C1FixtureServer.class", archive.namelist())

    def test_prepared_server_copies_only_a_hash_verified_fixture(self):
        from scripts.build_fabric_c1_fixture import build_fixture
        from scripts.fabric_deployment_sandbox import prepare_server
        artifact = build_fixture(timeout_seconds=180)
        with TemporaryDirectory(prefix="mc2p-c1-server-") as directory:
            target = Path(directory) / "server"
            evidence = prepare_server(
                target, seed=21001, port=25597, c1_fixture=artifact,
            )
            copied = target / "fixture-artifacts" / artifact.jar.name
            self.assertTrue(copied.is_file())
            self.assertEqual(evidence["server_mods"], [{
                "name": artifact.jar.name, "sha256": artifact.sha256,
            }])
            self.assertIn("-Dfabric.dli.env=server", evidence["server_launch"]["jvm_args"])


if __name__ == "__main__":
    unittest.main()
