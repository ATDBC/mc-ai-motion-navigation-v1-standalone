// Prototype: legal air confirmation from the formal surface-depth core.
//
// Build and run from deployment/surface-depth-diagnostic/native at commit
// 3e0c64b (the repository sources are used unchanged; only the Windows export
// macro is neutralised by the #define below):
//
//   g++ -std=c++17 -O2 -I. -I<this-dir> -Ivendor/clipper2/include \
//       <this-dir>/air_probe_prototype.cpp vendor/clipper2/src/clipper.engine.cpp -o /tmp/air_probe
//   /tmp/air_probe 500              # scenes, timings, soundness over 500 worlds
//   /tmp/air_probe 100 no-rule-b    # the same soundness test without rule (b)
//
// Idea: if a cell is empty, which part of it the eye can see depends only on
// geometry in FRONT of it. So the visible part of the cell is its projected
// silhouette minus the projections of closer opaque faces. The frame already
// projects every face; probing a cell reuses them (no extra rays).
//   (a) a box (the cell, or one of its 2x2x2 octants) counts as seen only when
//       it lies wholly inside the frustum and 16-block range and its whole
//       silhouette is inside the visible region;
//   (b) nothing is claimed in a cell where the formal sensor reports a visible
//       block in the same frame.
// A cell becomes AIR when all 8 octants have been seen (in one frame or,
// with accumulation, over a short window). The centre-line rule and a 9-point
// rule are computed alongside for comparison; the 9-point test is also used as
// a cheap pre-filter in front of the exact test (it can only reject: for an
// empty cell, "whole box seen" implies all nine points are seen).
#define __declspec(x)
#include "cache.cpp"
#include <cstdio>
#include <random>

constexpr int BINS = 48;
struct Frame { View v; std::vector<Projected> occluders; std::vector<std::vector<int>> bins; mutable std::vector<int> stamp; mutable int epoch = 0; };
static int bin_of(double t) { return std::clamp(int((t + 1) * .5 * BINS), 0, BINS - 1); }

static Frame project(const Scene& s, V eye, double mcYaw, double pitch) {
  Frame fr; double cam[5] = {eye[0], eye[1], eye[2], -mcYaw, pitch};
  orient(fr.v, cam, pitch);
  for (int i = 0; i < int(s.faces.size()); ++i) {
    const Face& f = s.faces[i];
    if (!s.opaque[f.owner]) continue;
    Poly p = project_face(f, fr.v); if (area(p) <= EPS) continue;
    fr.occluders.push_back({f.owner, i, p, quantize(p), bounds(p), coefficients(f, fr.v)});
  }
  fr.bins.assign(BINS * BINS, {}); fr.stamp.assign(fr.occluders.size(), 0);
  for (int i = 0; i < int(fr.occluders.size()); ++i) {
    const Rect& r = fr.occluders[i].rect;
    for (int bx = bin_of(r[0]); bx <= bin_of(r[2]); ++bx) for (int by = bin_of(r[1]); by <= bin_of(r[3]); ++by) fr.bins[bx * BINS + by].push_back(i);
  }
  return fr;
}

static std::vector<Face> box_faces(const Box& b) {
  std::vector<Face> out;
  for (int axis = 0; axis < 3; ++axis) for (int sign : {-1, 1}) {
    int u = (axis + 1) % 3, w = (axis + 2) % 3;
    out.push_back({-1, axis, sign, b[axis + (sign > 0 ? 3 : 0)], {b[u], b[w], b[u + 3], b[w + 3]}});
  }
  return out;
}

// Whole box inside the frustum and within 16 blocks (else never "fully seen").
static bool in_view(const Box& b, const View& v) {
  for (int m = 0; m < 8; ++m) {
    V p = {b[(m & 1) ? 3 : 0], b[(m & 2) ? 4 : 1], b[(m & 4) ? 5 : 2]};
    V c = camera(p, v); if (c[2] <= .01) return false;
    if (std::abs(c[0] * S / c[2]) > 1 || std::abs(c[1] * S / c[2]) > 1) return false;
    if (sqdist(p, v.eye) > 256.) return false;
  }
  return true;
}

static Paths64 silhouette(const Box& b, const View& v) {
  Paths64 paths;
  for (auto& f : box_faces(b)) { Poly p = project_face(f, v); if (area(p) > EPS) paths.push_back(quantize(p)); }
  return boolean_op(paths, {}, ClipType::Union);
}

static double paths_area(const Paths64& p) { double a = 0; for (auto& q : p) a += Area(q) / (SCALE * SCALE); return a; }

// Visible region of an (assumed empty) box: each front face minus closer occluders.
static Paths64 visible_region(const Box& b, const Frame& fr) {
  Paths64 visible;
  for (auto& f : box_faces(b)) {
    Poly p = project_face(f, fr.v); if (area(p) <= EPS) continue;
    Projected q = {-1, -1, p, quantize(p), bounds(p), coefficients(f, fr.v)};
    Paths64 cutters; ++fr.epoch;
    for (int bx = bin_of(q.rect[0]); bx <= bin_of(q.rect[2]); ++bx) for (int by = bin_of(q.rect[1]); by <= bin_of(q.rect[3]); ++by)
    for (int oi : fr.bins[bx * BINS + by]) {
      if (fr.stamp[oi] == fr.epoch) continue; fr.stamp[oi] = fr.epoch;
      const auto& o = fr.occluders[oi];
      if (!overlaps(q.rect, o.rect)) continue;
      V d = {o.inv[0] - q.inv[0], o.inv[1] - q.inv[1], o.inv[2] - q.inv[2]};
      if (std::abs(d[0]) + std::abs(d[1]) + std::abs(d[2]) < 1e-12) continue;
      Poly closer = halfplane(o.p, d[0], d[1], d[2]);
      if (area(closer) > EPS) cutters.push_back(quantize(closer));
    }
    Paths64 part = cutters.empty() ? Paths64{q.path} : boolean_op({q.path}, cutters, ClipType::Difference);
    visible.insert(visible.end(), part.begin(), part.end());
  }
  return boolean_op(visible, {}, ClipType::Union);
}

static bool covered(const Box& b, const Paths64& visible, const View& v) {
  if (!in_view(b, v)) return false;
  Paths64 sil = silhouette(b, v); double total = paths_area(sil);
  if (total <= EPS) return false;
  return paths_area(boolean_op(sil, visible, ClipType::Difference)) <= 1e-9 * total;
}

struct Probe { bool cell; int octants; bool center; double fraction; };

// Visibility of one point: inside the frustum and range, and no closer occluder covers it.
static bool point_visible(V w, const Frame& fr) {
  V c = camera(w, fr.v);
  if (c[2] <= .01 || sqdist(w, fr.v.eye) > 256.) return false;
  double px = c[0] * S / c[2], py = c[1] * S / c[2];
  if (std::abs(px) > 1 || std::abs(py) > 1) return false;
  Point64 pt(int64_t(std::llround(px * SCALE)), int64_t(std::llround(py * SCALE)));
  for (int oi : fr.bins[bin_of(px) * BINS + bin_of(py)]) {
    const auto& o = fr.occluders[oi];
    if (px < o.rect[0] || px > o.rect[2] || py < o.rect[1] || py > o.rect[3]) continue;
    if (PointInPolygon(pt, o.path) == PointInPolygonResult::IsOutside) continue;
    double zo = 1 / (o.inv[0] * px + o.inv[1] * py + o.inv[2]);
    if (zo < c[2] - 1e-9) return false;
  }
  return true;
}
// "More rays" rule: centre plus the 8 corners inset by 0.05 must all be visible.
static bool nine_rays(int x, int y, int z, const Frame& fr) {
  if (!point_visible({x + .5, y + .5, z + .5}, fr)) return false;
  for (int m = 0; m < 8; ++m)
    if (!point_visible({x + ((m & 1) ? .95 : .05), y + ((m & 2) ? .95 : .05), z + ((m & 4) ? .95 : .05)}, fr)) return false;
  return true;
}
static bool nine_points(const Box& b, const Frame& fr) {
  V c = {(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2};
  if (!point_visible(c, fr)) return false;
  double e = .05 * (b[3] - b[0]);
  for (int m = 0; m < 8; ++m)
    if (!point_visible({(m & 1) ? b[3] - e : b[0] + e, (m & 2) ? b[4] - e : b[1] + e, (m & 4) ? b[5] - e : b[2] + e}, fr)) return false;
  return true;
}
// Same answer as probe().octants, but cheap point tests reject most boxes first.
static int probe_hybrid(int x, int y, int z, const Frame& fr) {
  Box cell = {double(x), double(y), double(z), x + 1., y + 1., z + 1.};
  Paths64 vis; bool have = false;
  auto region = [&]() -> const Paths64& { if (!have) { vis = visible_region(cell, fr); have = true; } return vis; };
  if (nine_points(cell, fr) && covered(cell, region(), fr.v)) return 0xFF;
  int mask = 0;
  for (int k = 0; k < 8; ++k) {
    double ox = x + ((k & 1) ? .5 : 0), oy = y + ((k & 2) ? .5 : 0), oz = z + ((k & 4) ? .5 : 0);
    Box oct = {ox, oy, oz, ox + .5, oy + .5, oz + .5};
    if (nine_points(oct, fr) && covered(oct, region(), fr.v)) mask |= 1 << k;
  }
  return mask;
}
static bool g_octants = true;
static Probe probe(int x, int y, int z, const Frame& fr) {
  Box cell = {double(x), double(y), double(z), x + 1., y + 1., z + 1.};
  Paths64 vis = visible_region(cell, fr);
  Probe r{covered(cell, vis, fr.v), 0, false, 0};
  Paths64 sil = silhouette(cell, fr.v); double total = paths_area(sil);
  r.fraction = total > EPS ? paths_area(boolean_op(sil, vis, ClipType::Intersection)) / total : 0;
  if (r.cell) r.octants = 0xFF;
  else if (g_octants && r.fraction > 0) for (int k = 0; k < 8; ++k) {
    double ox = x + ((k & 1) ? .5 : 0), oy = y + ((k & 2) ? .5 : 0), oz = z + ((k & 4) ? .5 : 0);
    if (covered({ox, oy, oz, ox + .5, oy + .5, oz + .5}, vis, fr.v)) r.octants |= 1 << k;
  }
  // centre-line rule: centre projects into the frustum and no closer occluder covers it
  V c = camera({x + .5, y + .5, z + .5}, fr.v);
  if (c[2] > .01 && sqdist({x + .5, y + .5, z + .5}, fr.v.eye) <= 256.) {
    double px = c[0] * S / c[2], py = c[1] * S / c[2];
    if (std::abs(px) <= 1 && std::abs(py) <= 1) {
      r.center = true;
      Point64 pt(int64_t(std::llround(px * SCALE)), int64_t(std::llround(py * SCALE)));
      for (int oi : fr.bins[bin_of(px) * BINS + bin_of(py)]) {
        const auto& o = fr.occluders[oi];
        if (px < o.rect[0] || px > o.rect[2] || py < o.rect[1] || py > o.rect[3]) continue;
        if (PointInPolygon(pt, o.path) == PointInPolygonResult::IsOutside) continue;
        double zo = 1 / (o.inv[0] * px + o.inv[1] * py + o.inv[2]);
        if (zo < c[2] - 1e-9) { r.center = false; break; }
      }
    }
  }
  return r;
}

struct World { std::map<Key, std::pair<Box, bool>> blocks; };
static void cube(World& w, int x, int y, int z, bool opaque = true) { w.blocks[{x, y, z}] = {{double(x), double(y), double(z), x + 1., y + 1., z + 1.}, opaque}; }
static void part(World& w, int x, int y, int z, Box b) { w.blocks[{x, y, z}] = {b, true}; }

static Cache build(const World& w) {
  Cache c(4); std::vector<double> boxes, centers; std::vector<int> owners; std::vector<unsigned char> opaque;
  int i = 0;
  for (auto& [k, v] : w.blocks) {
    for (double d : v.first) boxes.push_back(d);
    owners.push_back(i++); centers.insert(centers.end(), {k[0] + .5, k[1] + .5, k[2] + .5}); opaque.push_back(v.second);
  }
  std::vector<int64_t> ids(2 * i); double stats[8];
  c.update(boxes.data(), owners.data(), i, centers.data(), opaque.data(), i, ids.data(), stats);
  return c;
}

static void report(const char* label, const Probe& p) {
  std::printf("  %-44s whole-cell=%s octants=%d/8 visible=%3.0f%% centre-line=%s\n", label,
              p.cell ? "yes" : "no ", __builtin_popcount(p.octants), p.fraction * 100, p.center ? "yes" : "no");
}

#include "soundness.inc"
int main(int argc, char** argv) {
  // Scene A: flat floor y=99 with a fence post and a wall corner in front.
  World a;
  for (int x = -8; x <= 8; ++x) for (int z = -8; z <= 12; ++z) cube(a, x, 99, z);
  part(a, 0, 100, 0, {.375, 100, .375, .625, 101.5, .625});             // fence post
  cube(a, 3, 100, 2); cube(a, 3, 101, 2);                                // wall column
  Cache ca = build(a);
  V eye = {.5, 101.62, -3.5};
  Frame f0 = project(ca.scene, eye, 0, 0);
  std::puts("Scene A, eye (0.5,101.62,-3.5) looking +z:");
  report("open cell (-3,100,4)", probe(-3, 100, 4, f0));
  report("behind the fence post (0,100,3)", probe(0, 100, 3, f0));
  bool shownA = false, shownB = false;
  for (int x = -6; x <= 7 && !(shownA && shownB); ++x) for (int z = 1; z <= 10 && !(shownA && shownB); ++z) {
    if (a.blocks.count({x, 100, z})) continue;
    Probe p = probe(x, 100, z, f0); char label[64];
    if (!shownA && p.center && p.fraction > .05 && p.fraction < .95) { std::snprintf(label, 64, "centre seen, part hidden (%d,100,%d)", x, z); report(label, p); shownA = true; }
    if (!shownB && !p.center && p.fraction >= .5) { std::snprintf(label, 64, "centre hidden, most seen (%d,100,%d)", x, z); report(label, p); shownB = true; }
  }
  // accumulate over side steps
  int mask = 0; const double xs[] = {.5, 1.3, -.3};
  for (double x : xs) { Frame f = project(ca.scene, {x, 101.62, -3.5}, 0, 0); mask |= probe(0, 100, 3, f).octants; }
  std::printf("  behind the post, after eye x = 0.5, 1.3, -0.3: octants %d/8\n", __builtin_popcount(mask));
  int wmask = 0; const double wx[] = {.5, 1.5, 2.5};
  for (double x : wx) { Frame f = project(ca.scene, {x, 101.62, -3.5}, 0, 0); wmask |= probe(5, 100, 5, f).octants; }
  std::printf("  centre behind the wall, after eye x = 0.5, 1.5, 2.5: octants %d/8\n", __builtin_popcount(wmask));

  // Scene B: rolling terrain with trees; time 128 probes per frame.
  World b; std::mt19937 rng(7);
  auto h = [](int x, int z) { return 100 + int(std::lround(2.5 * std::sin(x / 3.5) + 2 * std::cos(z / 4.5))); };
  for (int x = -16; x <= 16; ++x) for (int z = -16; z <= 16; ++z) {
    int top = h(x, z); for (int y = top - 3; y <= top; ++y) cube(b, x, y, z);
    if (rng() % 23 == 0) { for (int y = top + 1; y <= top + 4; ++y) cube(b, x, y, z);
      for (int dx = -2; dx <= 2; ++dx) for (int dz = -2; dz <= 2; ++dz) for (int dy = 3; dy <= 5; ++dy)
        if ((dx || dz) && std::abs(dx) + std::abs(dz) + (dy - 4 < 0 ? 4 - dy : dy - 4) <= 3) cube(b, x + dx, top + dy, z + dz); }
  }
  Cache cb = build(b);
  V beye = {.5, h(0, 0) + 1.62, .5};
  std::vector<std::array<int, 3>> cells;
  for (int x = -12; x <= 12; ++x) for (int z = 1; z <= 14; ++z) { int y = h(x, z) + 1; if (!b.blocks.count({x, y, z})) cells.push_back({x, y, z}); }
  std::shuffle(cells.begin(), cells.end(), rng); cells.resize(std::min<size_t>(128, cells.size()));
  int whole = 0, centre = 0, centreOnlyPartly = 0, anyOct = 0; double worst = 0, sum = 0, projectMs = 0; const int reps = 20;
  for (int r = 0; r < reps; ++r) {
    auto t0 = Clock::now();
    Frame fr = project(cb.scene, beye, 0, 15);
    projectMs += ms(t0, Clock::now());
    int w = 0, c = 0, cp = 0, o = 0;
    for (auto& k : cells) { Probe p = probe(k[0], k[1], k[2], fr); w += p.cell; c += p.center; cp += p.center && !p.cell && p.fraction < .999; o += p.octants != 0; }
    double t = ms(t0, Clock::now()); worst = std::max(worst, t); sum += t;
    whole = w; centre = c; centreOnlyPartly = cp; anyOct = o;
    if (r == 0) std::printf("\nScene B: %zu blocks, %zu faces, %zu projected occluders, %zu probes\n",
                            b.blocks.size(), cb.scene.faces.size(), fr.occluders.size(), cells.size());
  }
  std::printf("  whole cell seen: %d, centre-line yes: %d (of which only partly seen: %d), some octant seen: %d\n",
              whole, centre, centreOnlyPartly, anyOct);
  std::printf("  project (reused from the frame in production): mean %.2f ms\n", projectMs / reps);
  std::printf("  project + 128 probes: mean %.2f ms, worst %.2f ms over %d frames\n", sum / reps, worst, reps);
  std::printf("  probes only: mean %.2f ms\n", (sum - projectMs) / reps);
  g_octants = false; double noOct = 0;
  for (int r = 0; r < reps; ++r) { Frame fr = project(cb.scene, beye, 0, 15); auto t0 = Clock::now(); for (auto& k : cells) probe(k[0], k[1], k[2], fr); noOct += ms(t0, Clock::now()); }
  std::printf("  probes without octant masks: mean %.2f ms\n", noOct / reps);
  // A more typical request: 32 cells.
  g_octants = true; double small = 0;
  for (int r = 0; r < reps; ++r) { Frame fr = project(cb.scene, beye, 0, 15); auto t0 = Clock::now(); for (int i = 0; i < 32; ++i) probe(cells[i][0], cells[i][1], cells[i][2], fr); small += ms(t0, Clock::now()); }
  std::printf("  32 probes with octant masks: mean %.2f ms\n", small / reps);
  g_rule_b = !(argc > 2 && std::string(argv[2]) == "no-rule-b");
  {
    double tRay = 0, tExact = 0, tHybrid = 0; int nRay = 0, nExact = 0, nHybrid = 0;
    for (int r = 0; r < reps; ++r) {
      Frame fr = project(cb.scene, beye, 0, 15);
      auto t0 = Clock::now(); int c = 0; for (auto& k : cells) c += nine_rays(k[0], k[1], k[2], fr); tRay += ms(t0, Clock::now()); nRay = c;
      g_octants = false; t0 = Clock::now(); c = 0; for (auto& k : cells) c += probe(k[0], k[1], k[2], fr).cell; tExact += ms(t0, Clock::now()); nExact = c;
      t0 = Clock::now(); c = 0; for (auto& k : cells) c += nine_rays(k[0], k[1], k[2], fr) && probe(k[0], k[1], k[2], fr).cell; tHybrid += ms(t0, Clock::now()); nHybrid = c;
    }
    std::printf("\nScene B, 128 cells per frame (whole-cell answer only):\n");
    std::printf("  9-ray rule:                 %.3f ms, %d cells claimed\n", tRay / reps, nRay);
    std::printf("  exact visibility:           %.3f ms, %d cells claimed\n", tExact / reps, nExact);
    std::printf("  9-ray prefilter + exact:    %.3f ms, %d cells claimed\n", tHybrid / reps, nHybrid);
    double tOct = 0, tOctExact = 0; int same = 0, partial = 0;
    for (int r = 0; r < reps; ++r) {
      Frame fr = project(cb.scene, beye, 0, 15);
      std::vector<int> fast, exact;
      auto t0 = Clock::now(); for (auto& k : cells) fast.push_back(probe_hybrid(k[0], k[1], k[2], fr)); tOct += ms(t0, Clock::now());
      g_octants = true; t0 = Clock::now(); for (auto& k : cells) exact.push_back(probe(k[0], k[1], k[2], fr).octants); tOctExact += ms(t0, Clock::now());
      same = 0; partial = 0; for (size_t i = 0; i < cells.size(); ++i) { same += fast[i] == exact[i]; partial += fast[i] != 0 && fast[i] != 0xFF; }
    }
    std::printf("  octant masks, exact:        %.3f ms\n", tOctExact / reps);
    std::printf("  octant masks, prefiltered:  %.3f ms (%d/%zu masks identical to exact, %d partial masks)\n", tOct / reps, same, cells.size(), partial);
  }
  soundness(argc > 1 ? std::atoi(argv[1]) : 30);
}
