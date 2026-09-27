"""Round-14 review: expand SurfaceVisibilityRules over the 1.21 block registry.

The see-through set and suffixes are read from the Java source, not retyped.
Run from the repository root of a b740098 checkout:
    python -B <this script>
"""
import json
import re
from pathlib import Path

RULES = Path("mc2p/backends/runtime_overlays/mc121_surface/com/mc2p/surface/SurfaceVisibilityRules.java")
REGISTRY = Path("config/motion-navigation/vanilla-block-registry-1_21.json")

source = RULES.read_text(encoding="utf-8")
listed = set(re.findall(r'"(minecraft:[a-z0-9_]+)"', source.split("SEE_THROUGH = Set.of(")[1].split(");")[0]))
suffixes = tuple(re.findall(r'endsWith\("(_[a-z_]+)"\)', source.split("boolean glass")[1]))
ids = json.loads(REGISTRY.read_text(encoding="utf-8"))["materials"]

see_through = sorted(i for i in ids if i in listed or i.endswith(suffixes))
by_suffix = sorted(i for i in see_through if i not in listed and not re.search(r"stained_glass(_pane)?$", i))
print(f"{len(ids)} registry ids; {len(listed)} listed ids; suffixes {suffixes}")
print(f"{len(see_through)} ids are classified see-through; {len(by_suffix)} of them only via the plant suffixes:")
for block in by_suffix:
    print("  ", block)
print()
print("Known opaque blocks classified see-through:",
      [i for i in by_suffix if i in ("minecraft:muddy_mangrove_roots",)],
      f"plus {sum(i.startswith('minecraft:potted_') for i in by_suffix)} flower pots")
print()
entity_rendered = {
    "signs": r"_sign$", "banners": r"_banner$", "heads/skulls": r"_(head|skull)$",
    "end portal/gateway": r":end_(portal|gateway)$", "moving piston": r":moving_piston$",
}
print("Blocks drawn only by a block-entity renderer in 1.21 (render type INVISIBLE per"
      " BlockWithEntity's default; verify by enumeration in the client):")
for name, pattern in entity_rendered.items():
    matches = [i for i in ids if re.search(pattern, i) and i != "minecraft:piston_head"]
    print(f"  {name:<20} {len(matches):>3}  e.g. {matches[:3]}")
