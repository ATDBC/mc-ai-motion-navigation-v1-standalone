from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.export_b06_block_registry import export_registry


class B06RegistryExportTests(unittest.TestCase):
    def test_export_is_sorted_namespaced_and_records_source_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jar = root / "client.jar"
            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("assets/minecraft/blockstates/stone.json", "{}")
                archive.writestr("assets/minecraft/blockstates/acacia_door.json", "{}")
                archive.writestr("assets/example/blockstates/ignored.json", "{}")
            target = root / "registry.json"

            document = export_registry(jar, target, minecraft_version="1.21")

            self.assertEqual(document["materials"], [
                "minecraft:acacia_door", "minecraft:stone",
            ])
            self.assertEqual(document["material_count"], 2)
            self.assertEqual(len(document["source_jar_sha256"]), 64)
            self.assertEqual(json.loads(target.read_text("utf-8")), document)


if __name__ == "__main__":
    unittest.main()
