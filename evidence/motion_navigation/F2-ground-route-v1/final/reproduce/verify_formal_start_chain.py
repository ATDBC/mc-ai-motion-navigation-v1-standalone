"""Record actual owner-generated start windows and Runtime receipts."""
from pathlib import Path
import hashlib,json,sys,unittest
from unittest.mock import patch

ROOT=next(p for p in Path(__file__).resolve().parents
    if (p/"mc2p").is_dir() and (p/"AGENTS.md").is_file())
sys.path.insert(0,str(ROOT))
from tests.motion_nav.test_f2_ground_start_window import F2GroundStartFormalChainTests
from mc2p.motion_nav import action_route_executor as executor_module

mutation_detected=None
if "--old-gate-mutant" in sys.argv:
    original=executor_module._ordinary_walk_start_window
    def old_gate(action,*args,**kwargs):
        return None if len(action.fixed_route.points)!=2 else original(action,*args,**kwargs)
    suite=unittest.TestSuite([F2GroundStartFormalChainTests(
        "test_planned_look_wins_and_first_move_applies_on_time")])
    with (ROOT/".tmp/f2-task5-start-chain-oldgate-red.log").open("w",encoding="utf-8") as stream:
        with patch.object(executor_module,"_ordinary_walk_start_window",old_gate):
            result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    mutation_detected=not result.wasSuccessful() and len(result.failures)==1 and not result.errors
    assert mutation_detected,"The old multi-point exclusion was not detected"
case=F2GroundStartFormalChainTests()
rows=[case.assert_formal_start(late=late,look_wins=not losing)
    for losing,late in ((False,0),(False,1),(False,2),(True,0))]
test="tests/motion_nav/test_f2_ground_start_window.py"
payload={"passed":True,"cases":rows,"test_file":test,
    "old_multi_point_exclusion_mutation_detected":mutation_detected,
    "test_sha256":hashlib.sha256((ROOT/test).read_bytes()).hexdigest(),
    "formal_chain":"NavigationSession -> RuntimeNavigationDriver -> ActionRouteExecutor -> PlayerRuntimeV1 -> InputApplicationLedger",
    "fixture":"Real planner multi-point Walk; calculator settles neutral entry; declared 15-degree planned look."}
output=ROOT/".tmp/f2-task5-start-chain.json"
output.write_text(json.dumps(payload,indent=2,default=str)+"\n","utf-8")
print("Formal chain 4/4; output",output)
