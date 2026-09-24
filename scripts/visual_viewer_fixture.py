"""One extra ordinary player record in a new offline demo world; no actor world-write API."""
from __future__ import annotations

import os
from pathlib import Path

from scripts.fabric_deployment_launch import ROOT, _hash
from scripts.fabric_deployment_sandbox import JAVA
from scripts.visibility_fixture_world import _classpath, _no_links, _run

VIEWER_FILE = "playerdata/ee03c7fa-ed38-3fe4-85b6-9e30bc3cc504.dat"


def prepare_viewer_player(world: Path, work: Path, *, seed: int) -> dict:
    if type(seed) is not int or seed not in (21001,21002,21003): raise ValueError("undeclared viewer fixture seed")
    world,work = Path(world).absolute(),Path(work).absolute()
    if ".." in world.parts or ".." in work.parts: raise ValueError("viewer fixture parent escape")
    _no_links(world)
    _no_links(work.parent)
    if (world/VIEWER_FILE).exists() or work.exists(): raise FileExistsError("viewer initializer requires fresh outputs")
    sources = [ROOT/"scripts/java/VisualViewerInitializer.java",ROOT/"tests/java/VanillaObjectTestHost.java"]
    before = {str(p):_hash(p) for p in sources}
    original = {str(p):_hash(p) for p in world.rglob("*") if p.is_file()}
    for name in original: _no_links(Path(name))
    work.mkdir(exist_ok=False)
    classpath = _classpath()
    _run([str(JAVA.with_name("javac.exe")),"-encoding","UTF-8","-proc:none","-cp",classpath,"-d",str(work),
          *map(str,sources)],work,"compile")
    _run([str(JAVA),"-cp",str(work)+os.pathsep+classpath,"VanillaObjectTestHost","VisualViewerInitializer",
          str(world),str(seed)],work,"initialize")
    if before != {str(p):_hash(p) for p in sources} or original != {name:_hash(Path(name)) for name in original}:
        raise ValueError("viewer initializer changed source or existing world files")
    return dict(username="MC2PViewer",verified=True,player_sha256=_hash(world/VIEWER_FILE),sources=before,
                position=[12.5+seed-21001,-60,4.5],yaw=90,scope="offline ordinary survival player initializer")
