"""Separate vanilla-codec playground initial state; never available to an actor."""
from __future__ import annotations
import json
import os
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory

from scripts.control_probe_core import write_json_atomic
from scripts.follow_fixture_world import PLAYERS, offline_uuid, _files
from scripts.fabric_deployment_sandbox import JAVA
from scripts.visibility_fixture_world import ROOT, _classpath, _hash, _no_links, _run

SOURCES = ('scripts/java/FollowPlaygroundInitializer.java', 'scripts/fixtures/visibility-level.snbt',
           'tests/java/VanillaObjectTestHost.java')
HOTBAR = ['minecraft:'+name for name in ('stone','cobblestone','oak_planks','glass','white_wool','torch',
                                       'iron_pickaxe','iron_axe','iron_shovel')]


def _validate(seed: int, scenario: str = 'playground') -> None:
    if type(seed) is not int or seed not in (21001,21002,21003): raise ValueError('undeclared playground seed')
    if scenario not in ('playground','playground_tracking','playground_search'): raise ValueError('undeclared playground scenario')


def _sources() -> dict:
    return {name: _hash(ROOT/name) for name in SOURCES}


def _native(mode: str, world: Path, seed: int, work: Path, scenario: str) -> None:
    classpath = _classpath()
    _run([str(JAVA.parent/'javac.exe'), '-J-Duser.language=en', '-encoding','UTF-8','-proc:none','-cp',classpath,
          '-d',str(work),str(ROOT/SOURCES[0]),str(ROOT/SOURCES[2])], work, 'compile')
    _run([str(JAVA), '-cp',str(work)+os.pathsep+classpath,'VanillaObjectTestHost','FollowPlaygroundInitializer',
          mode,str(ROOT/SOURCES[1]),str(world),str(seed),scenario], work, mode)


def _manifest(world: Path, seed: int, sources: dict, scenario: str) -> dict:
    return dict(schema_version='mc2p.follow-playground-fixture.v1', seed=seed,
        scenario=scenario,
        purpose='offline_test_initial_state_not_actor_capability', source_fingerprints=sources,
        players={name: offline_uuid(name) for name in PLAYERS}, files=_files(world),
        human_game_mode='creative', actor_game_mode='survival', human_hotbar_items=HOTBAR,
        human_hotbar_counts=[64]*6+[1]*3, force_gamemode=False)


def prepare_playground(output: Path, seed: int, *, scenario: str = 'playground') -> dict:
    _validate(seed,scenario)
    target = Path(output).absolute()
    if '..' in target.parts: raise ValueError('playground parent escape')
    _no_links(target.parent)
    before = _sources()
    target.mkdir(exist_ok=False)
    classes = target/'classes'
    classes.mkdir()
    _native('build', target/'world', seed, classes,scenario)
    if before != _sources(): raise ValueError('playground sources changed')
    manifest = _manifest(target/'world', seed, before,scenario)
    write_json_atomic(target/'fixture-manifest.json', manifest)
    return manifest


def verify_playground(fixture: Path) -> dict:
    fixture = Path(fixture).absolute()
    _no_links(fixture/'fixture-manifest.json')
    manifest = json.loads((fixture/'fixture-manifest.json').read_text('utf-8'))
    _validate(manifest.get('seed'),manifest.get('scenario'))
    before = _sources()
    if manifest != _manifest(fixture/'world', manifest['seed'], before,manifest['scenario']):
        raise ValueError('playground provenance/content mismatch')
    with TemporaryDirectory(prefix='mc2p-playground-verifier-') as temporary:
        try: _native('verify', fixture/'world', manifest['seed'], Path(temporary),manifest['scenario'])
        except RuntimeError as error: raise ValueError('native playground verification failed') from error
    if before != _sources() or manifest['files'] != _files(fixture/'world'):
        raise ValueError('playground changed during verification')
    return manifest


def install_playground(fixture: Path, target: Path) -> Path:
    target = Path(target).absolute()
    if '..' in target.parts: raise ValueError('playground target escape')
    _no_links(target.parent)
    if target.exists(): raise FileExistsError('playground installation must be new')
    manifest = verify_playground(fixture)
    target.mkdir(exist_ok=False)
    for relative in sorted(manifest['files']):
        path = target/relative
        path.parent.mkdir(exist_ok=True)
        shutil.copyfile(Path(fixture)/'world'/relative, path)
    if _files(target) != manifest['files']: raise ValueError('playground install changed')
    return target
