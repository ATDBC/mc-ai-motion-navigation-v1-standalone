"""Read the frozen D097-A parameter table inside the isolated D098 experiment.

The table is imported as data, not regenerated at runtime.  Its original byte
hash and generation identity are part of the contract.  The project-owned
controller has its own tests against representative frozen candidates.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .template_controller import ControllerParams


SCHEMA = "d097a-m1-table-v1"
SOURCE_COMMIT = "f5e43e74"
FROZEN_TABLE_SHA256 = "fc33cec9c5d610c8b8bfb1106f9255235b7f5c35252294bcf1af03b1319c3fc7"
FROZEN_TABLE_PATH = Path(__file__).with_name("data") / "d097a-m1-table.json"
ORIGINAL_IDENTITY = {
    "alphabet": "cde2dc9a2425362d4760397ca3c9026df806d8020545f133697d3919082115cd",
    "bins": "3170bdfc30e1dc897e90cea4a1461e319d2eb0c6afb9dd8b783817e743e19084",
    "entries_module": "d097a-entries-v1",
    "generator": "282fcf8cc2f83c0f88261aeba2602f60ce04ed18390a917075c3492c692f8d80",
    "grid": "aec537992e2f67e2176e820caec832699170b382f594ee824fe9a1a7d59962b7",
    "pool_size": 30,
    "rules": "0c1b2714dce913f693719e886a1c0c65ccfdbc392d83e0531b6f35f235747635",
    "ruleset": "465a989404d0c462d05c6977e49bfdcf3b382e689224d629db0f9534231bff97",
    "ruleset_id": "minecraft-java-1_21-player-motion-r1",
    "ruleset_impl": "2020dde8bf327e51156c9fae5c3dbb70d63759b88f2d39bb7f8b639a0de70fea",
    "samples": [200, 20261010],
    "state_schema": "mc2p.physics-state.v1",
    "templates": "61c053ce16e953eab9746483d81c0ce0c43aa22347424d030d1e9c3aa7bf3cfa",
}


class FrozenTableError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MotionTable:
    rows: dict[tuple[str, str], dict]
    sha256: str
    source_commit: str

    def params(self, template_id: str, bin_id: str) -> tuple[ControllerParams, ...]:
        row = self.rows.get((template_id, bin_id))
        if row is None:
            raise KeyError((template_id, bin_id))
        return tuple(ControllerParams.from_dict(item["params"]) for item in row["params"])


def load_table(path: Path | str, *, expected_identity: dict | None = None,
               expected_sha256: str = FROZEN_TABLE_SHA256) -> MotionTable:
    path = Path(path)
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise FrozenTableError(f"table bytes differ: {digest}")
    payload = json.loads(raw)
    identity = ORIGINAL_IDENTITY if expected_identity is None else expected_identity
    if payload.get("schema") != SCHEMA:
        raise FrozenTableError(f"unexpected table schema: {payload.get('schema')!r}")
    if payload.get("identity") != identity:
        keys = sorted(set(identity) | set(payload.get("identity", {})))
        differing = [key for key in keys if identity.get(key) != payload.get("identity", {}).get(key)]
        raise FrozenTableError(f"table identity differs in {differing}")
    rows = {(row["template"], row["bin"]): row for row in payload.get("rows", ())}
    if len(rows) != len(payload.get("rows", ())):
        raise FrozenTableError("table has duplicate template/bin rows")
    return MotionTable(rows, digest, SOURCE_COMMIT)


def load_frozen_table() -> MotionTable:
    return load_table(FROZEN_TABLE_PATH)
