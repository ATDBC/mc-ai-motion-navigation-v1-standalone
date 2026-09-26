// Soundness property: an octant is claimed only if (a) it is wholly visible and
// (b) the formal sensor reports no visible block in that cell this frame.
// Ground truth then checks that no block shape intersects any claimed octant.
static bool boxes_overlap(const Box& a, const Box& b) {
  for (int i = 0; i < 3; ++i) if (!(a[i] < b[i + 3] - 1e-9 && b[i] < a[i + 3] - 1e-9)) return false;
  return true;
}
static bool g_rule_b = true;
static void soundness(int worlds) {
  std::mt19937 rng(20260926);
  long claimed = 0, violations = 0, probes = 0, fullAir = 0, trueAir = 0, rayClaims = 0, rayWrong = 0, centreClaims = 0, centreWrong = 0;
  for (int w = 0; w < worlds; ++w) {
    World world; std::map<Key, std::vector<Box>> shapes;
    std::uniform_real_distribution<double> u(0, 1);
    for (int x = -6; x <= 6; ++x) for (int y = 97; y <= 104; ++y) for (int z = -6; z <= 6; ++z) {
      if (std::abs(x) <= 1 && std::abs(z) <= 1 && y >= 100 && y <= 101) continue;  // eye pocket
      double r = u(rng); Box b; bool opaque = true;
      double X = x, Y = y, Z = z;
      if (r < .55) continue;
      else if (r < .80) b = {X, Y, Z, X + 1, Y + 1, Z + 1};
      else if (r < .85) { b = {X, Y, Z, X + 1, Y + 1, Z + 1}; opaque = false; }       // glass
      else if (r < .90) b = {X, Y, Z, X + 1, Y + .5, Z + 1};                             // slab
      else if (r < .95) b = {X + .375, Y, Z + .375, X + .625, Y + 1, Z + .625};         // post
      else { int side = rng() % 4; double t = 3. / 16;                                   // door-like plate
        b = side == 0 ? Box{X, Y, Z, X + t, Y + 1, Z + 1} : side == 1 ? Box{X + 1 - t, Y, Z, X + 1, Y + 1, Z + 1}
          : side == 2 ? Box{X, Y, Z, X + 1, Y + 1, Z + t} : Box{X, Y, Z + 1 - t, X + 1, Y + 1, Z + 1}; }
      world.blocks[{x, y, z}] = {b, opaque}; shapes[{x, y, z}].push_back(b);
    }
    Cache cache = build(world);
    V eye = {u(rng) * 2 - .5, 100 + .3 + u(rng) * 1.4, u(rng) * 2 - .5};
    double yaw = u(rng) * 360 - 180, pitch = u(rng) * 120 - 60;
    // formal sensor output for this pose
    int n = int(world.blocks.size());
    std::vector<unsigned char> query(n, 1), out(n); std::vector<double> areas(n), times(2); std::vector<int64_t> stats(8);
    double cam[5] = {eye[0], eye[1], eye[2], -yaw, pitch};
    cache.frame(cam, query.data(), n, 1, out.data(), areas.data(), times.data(), stats.data(), true);
    std::set<Key> visibleCells; int i = 0;
    for (auto& [k, v] : world.blocks) { if (out[i]) visibleCells.insert(k); ++i; }
    Frame fr = project(cache.scene, eye, yaw, pitch);
    for (int x = -6; x <= 6; ++x) for (int y = 97; y <= 104; ++y) for (int z = -6; z <= 6; ++z) {
      if (g_rule_b && visibleCells.count({x, y, z})) continue;       // rule (b)
      Probe p = probe(x, y, z, fr); ++probes;
      bool occupied = shapes.count({x, y, z}) > 0;
      if (nine_rays(x, y, z, fr)) { ++rayClaims; rayWrong += occupied; }
      if (p.center) { ++centreClaims; centreWrong += occupied; }
      if (p.octants == 0xFF) { ++fullAir; if (!shapes.count({x, y, z})) ++trueAir; }
      for (int k = 0; k < 8; ++k) if (p.octants & (1 << k)) {
        ++claimed;
        double ox = x + ((k & 1) ? .5 : 0), oy = y + ((k & 2) ? .5 : 0), oz = z + ((k & 4) ? .5 : 0);
        Box oct = {ox, oy, oz, ox + .5, oy + .5, oz + .5};
        auto it = shapes.find({x, y, z});
        if (it != shapes.end()) for (auto& s : it->second) if (boxes_overlap(s, oct)) { ++violations; break; }
      }
    }
  }
  std::printf("\nSoundness (rule b %s): %d random worlds, %ld probes, %ld octants claimed, %ld claimed octants intersect a block shape\n", g_rule_b ? "on" : "OFF",
              worlds, probes, claimed, violations);
  std::printf("  cells claimed fully empty in one frame: %ld (all truly air: %s)\n", fullAir, fullAir == trueAir ? "yes" : "NO");
  std::printf("  centre-line rule: %ld cells claimed air, %ld of them actually hold a block\n", centreClaims, centreWrong);
  std::printf("  9-ray rule (centre + inset corners): %ld cells claimed air, %ld of them actually hold a block\n", rayClaims, rayWrong);
}
