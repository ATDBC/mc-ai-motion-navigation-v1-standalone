// Round-15 review: how large is the visible region behind a status-1 visual-air
// answer?  visible_area() below is cell_visibility_status() from geometry.hpp with
// the early return replaced by "keep the largest per-face visible area".
// Screen units: the 120-degree view spans [-1,1]^2 (area 4).  One pixel of an
// 854x480 image is about 9.8e-6.
// Build (from deployment/surface-depth-diagnostic/native of a 4b9e73e checkout):
//   g++ -std=c++17 -O2 "-D__declspec(x)=" -I. -Ivendor/clipper2/include \
//       <this file> vendor/clipper2/src/clipper.engine.cpp -o /tmp/air_sliver_areas
// Run: /tmp/air_sliver_areas [worlds]
#include "cache.cpp"
#include <cstdio>
#include <random>
#include <set>
#include <vector>
static View make_view(const Scene& s, const double* cam) {
  View v; orient(v, cam, cam[4]);
  for (int i = 0; i < int(s.faces.size()); ++i) {
    auto f = s.faces[i]; V near = v.eye; near[f.axis] = f.coord;
    near[(f.axis + 1) % 3] = std::clamp(near[(f.axis + 1) % 3], f.uv[0], f.uv[2]);
    near[(f.axis + 2) % 3] = std::clamp(near[(f.axis + 2) % 3], f.uv[1], f.uv[3]);
    if (sqdist(near, v.eye) > 256.) continue;
    Poly p = project_face(f, v); if (area(p) <= EPS) continue;
    v.faces.push_back({f.owner, i, p, quantize(p), bounds(p), coefficients(f, v)});
  }
  return v;
}
static double visible_area(const Scene& scene, const View& view, int x, int y, int z) {
  Box box = {double(x), double(y), double(z), double(x + 1), double(y + 1), double(z + 1)}; V nearest;
  for (int a = 0; a < 3; ++a) nearest[a] = std::clamp(view.eye[a], box[a], box[a + 3]);
  if (sqdist(nearest, view.eye) > 256 + EPS) return -1;
  double best = 0;
  for (int axis = 0; axis < 3; ++axis) for (int sign : {-1, 1}) {
    int u = (axis + 1) % 3, w = (axis + 2) % 3;
    Face face = {-1, axis, sign, box[axis + (sign > 0 ? 3 : 0)], {box[u], box[w], box[u + 3], box[w + 3]}};
    Poly p = project_face(face, view); if (area(p) <= EPS) continue;
    Projected q = {-1, -1, p, quantize(p), bounds(p), coefficients(face, view)}; Paths64 cutters;
    for (const auto& b : view.faces) {
      if (!scene.opaque[b.owner] || !overlaps(q.rect, b.rect)) continue;
      V d = {b.inv[0] - q.inv[0], b.inv[1] - q.inv[1], b.inv[2] - q.inv[2]};
      if (std::abs(d[0]) + std::abs(d[1]) + std::abs(d[2]) < 1e-12) continue;
      Poly closer = halfplane(b.p, d[0], d[1], d[2]); if (area(closer) > EPS) cutters.push_back(quantize(closer));
    }
    Paths64 r = cutters.empty() ? Paths64{q.path} : boolean_op({q.path}, cutters, ClipType::Difference);
    double vis = 0; for (auto& path : r) vis += Area(path) / (SCALE * SCALE);
    best = std::max(best, vis);
  }
  return best;
}
int main(int argc, char** argv) {
  int worlds = argc > 1 ? std::atoi(argv[1]) : 100;
  std::mt19937 rng(7);
  std::uniform_real_distribution<double> u(0, 1);
  long buckets[5] = {0, 0, 0, 0, 0}; const double edges[4] = {1e-10, 1e-8, 1e-6, 1e-5};
  long positives = 0;
  for (int w = 0; w < worlds; ++w) {
    std::set<std::array<int, 3>> solid;
    for (int x = -12; x <= 12; ++x) for (int z = -12; z <= 12; ++z) {
      solid.insert({x, 0, z});
      for (int y = 1; y <= 4; ++y) if (u(rng) < 0.3) solid.insert({x, y, z});
    }
    solid.erase({0, 1, 0}); solid.erase({0, 2, 0});
    auto* c = static_cast<Cache*>(cache_create(4));
    std::vector<double> boxes, centers; std::vector<int> owners; std::vector<unsigned char> opaque; int n = 0;
    for (auto p : solid) {
      boxes.insert(boxes.end(), {double(p[0]), double(p[1]), double(p[2]), p[0] + 1., p[1] + 1., p[2] + 1.});
      owners.push_back(n); centers.insert(centers.end(), {p[0] + .5, p[1] + .5, p[2] + .5}); opaque.push_back(1); ++n;
    }
    std::vector<int64_t> ids(2 * n); double st[8];
    cache_update(c, boxes.data(), owners.data(), n, centers.data(), opaque.data(), n, ids.data(), st);
    double cam[5] = {.3 + .4 * u(rng), 2.62, .3 + .4 * u(rng), 360 * u(rng), -20 + 50 * u(rng)};
    View v = make_view(c->scene, cam);
    for (int x = -12; x <= 12; ++x) for (int y = 1; y <= 5; ++y) for (int z = -12; z <= 12; ++z) {
      if (solid.count({x, y, z})) continue;
      double a = visible_area(c->scene, v, x, y, z);
      if (a <= EPS) continue;
      ++positives; int b = 0; while (b < 4 && a >= edges[b]) ++b; ++buckets[b];
    }
    cache_destroy(c);
  }
  std::printf("%ld status-1 air cells in %d worlds; largest visible face area:\n", positives, worlds);
  const char* names[5] = {"< 1e-10", "1e-10 .. 1e-8", "1e-8 .. 1e-6", "1e-6 .. 1e-5 (under one 854x480 pixel)", ">= 1e-5"};
  for (int b = 0; b < 5; ++b) std::printf("  %-42s %7ld (%.2f%%)\n", names[b], buckets[b], 100.0 * buckets[b] / std::max(1L, positives));
}
