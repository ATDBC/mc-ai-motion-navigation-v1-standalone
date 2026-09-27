"""Runs Java cache tests without launching Minecraft."""
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from scripts.export_motion_navigation_standalone import discover_java_tools
ROOT=Path(__file__).resolve().parents[1]
class TileStoreTests(unittest.TestCase):
 def test_java_tile_store_invariants(self):
  tools=discover_java_tools()
  source=ROOT/'mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/TileGeometryStore.java'
  self.assertTrue(source.exists(),'complete-tile store has not been implemented')
  build=ROOT/'artifacts/surface-cache/tile-tests';build.mkdir(parents=True,exist_ok=True)
  subprocess.run([str(tools.javac),'-d',str(build),str(source),str(ROOT/'mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/DirtyTracker.java'),str(ROOT/'tests/java/SurfaceTileStoreTest.java')],check=True)
  subprocess.run([str(tools.java),'-ea','-cp',str(build),'com.mc2p.surface.SurfaceTileStoreTest'],check=True)

 def test_enclosed_full_cubes_are_removed_by_the_formal_surface_source(self):
  tools=discover_java_tools()
  source=ROOT/'mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface'
  with TemporaryDirectory(prefix='mc2p-surface-culling-') as directory:
   compiled=subprocess.run([
    str(tools.javac),'-J-Duser.language=en',
    '-J-Dfile.encoding=UTF-8','-encoding','UTF-8','-d',directory,
    str(source/'TileGeometryStore.java'),str(source/'SurfaceGeometryCuller.java'),
    str(ROOT/'tests/java/SurfaceEnclosedCullingTest.java')],capture_output=True,text=True)
   self.assertEqual(compiled.returncode,0,compiled.stdout+compiled.stderr)
   executed=subprocess.run([str(tools.java),'-ea','-cp',directory,
                             'SurfaceEnclosedCullingTest'],capture_output=True,text=True)
   self.assertEqual(executed.returncode,0,executed.stdout+executed.stderr)
   self.assertIn('SURFACE_ENCLOSED_CULLING_OK',executed.stdout)

 def test_surface_visibility_rules_are_one_explicit_versioned_table(self):
  tools=discover_java_tools()
  source=ROOT/'mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface'
  with TemporaryDirectory(prefix='mc2p-surface-rules-') as directory:
   compiled=subprocess.run([
    str(tools.javac),'-J-Duser.language=en',
    '-J-Dfile.encoding=UTF-8','-encoding','UTF-8','-d',directory,
    str(source/'SurfaceVisibilityRules.java'),
    str(ROOT/'tests/java/SurfaceVisibilityRulesTest.java')],capture_output=True,text=True)
   self.assertEqual(compiled.returncode,0,compiled.stdout+compiled.stderr)
   executed=subprocess.run([str(tools.java),'-ea','-cp',directory,
                             'SurfaceVisibilityRulesTest'],capture_output=True,text=True)
   self.assertEqual(executed.returncode,0,executed.stdout+executed.stderr)
   self.assertIn('SURFACE_VISIBILITY_RULES_OK',executed.stdout)
if __name__=='__main__':unittest.main()
