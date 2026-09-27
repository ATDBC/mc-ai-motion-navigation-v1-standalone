// Round-14 review: sub-cell variant of the repository's cell_fully_visible().
// box_fully_visible() is the repository function with the unit cube replaced by an
// arbitrary box; the scene, projection, cutters and tolerances are unchanged.
// Build like downward_air_coverage.cpp (same flags, this file instead).
#include "cache.cpp"
#include <cstdio>
#include <vector>

static View make_view(const Scene& s, const double* cam) {
  View v; orient(v, cam, cam[4]);
  for (int i = 0; i < int(s.faces.size()); ++i) {  // same mode-3 filter as center_frame_pose_impl
    auto f = s.faces[i]; V near = v.eye; near[f.axis] = f.coord;
    near[(f.axis + 1) % 3] = std::clamp(near[(f.axis + 1) % 3], f.uv[0], f.uv[2]);
    near[(f.axis + 2) % 3] = std::clamp(near[(f.axis + 2) % 3], f.uv[1], f.uv[3]);
    if (sqdist(near, v.eye) > 256.) continue;
    Poly p = project_face(f, v); if (area(p) <= EPS) continue;
    v.faces.push_back({f.owner, i, p, quantize(p), bounds(p), coefficients(f, v)});
  }
  return v;
}
static bool box_fully_visible(const Scene& scene, const View& view, Box b, double maxDistance) {
  double limit = maxDistance * maxDistance;
  for (int dx : {0, 1}) for (int dy : {0, 1}) for (int dz : {0, 1})
    if (!point_inside_view({dx ? b[3] : b[0], dy ? b[4] : b[1], dz ? b[5] : b[2]}, view, limit)) return false;
  bool projected = false;
  for (int axis = 0; axis < 3; ++axis) for (int sign : {-1, 1}) {
    int u = (axis + 1) % 3, w = (axis + 2) % 3;
    Face face = {-1, axis, sign, sign > 0 ? b[axis + 3] : b[axis], {b[u], b[w], b[u + 3], b[w + 3]}};
    Poly p = project_face(face, view); double original = area(p); if (original <= EPS) continue; projected = true;
    Projected query = {-1, -1, p, quantize(p), bounds(p), coefficients(face, view)}; Paths64 cutters;
    for (const auto& blocker : view.faces) {
      if (!scene.opaque[blocker.owner] || !overlaps(query.rect, blocker.rect)) continue;
      V d = {blocker.inv[0] - query.inv[0], blocker.inv[1] - query.inv[1], blocker.inv[2] - query.inv[2]};
      if (std::abs(d[0]) + std::abs(d[1]) + std::abs(d[2]) < 1e-12) continue;
      Poly closer = halfplane(blocker.p, d[0], d[1], d[2]); if (area(closer) > EPS) cutters.push_back(quantize(closer));
    }
    if (!cutters.empty()) {
      Paths64 r = boolean_op({query.path}, cutters, ClipType::Difference); double vis = 0;
      for (auto& path : r) vis += Area(path) / (SCALE * SCALE); if (vis + 1e-10 < original) return false;
    }
  }
  return projected;
}
struct Blk { int x, y, z; double top; };
static Cache* build(const std::vector<Blk>& blocks) {
  auto* c = static_cast<Cache*>(cache_create(4));
  std::vector<double> boxes, centers; std::vector<int> owners; std::vector<unsigned char> opaque;
  for (size_t i = 0; i < blocks.size(); ++i) {
    auto k = blocks[i]; boxes.insert(boxes.end(), {double(k.x), double(k.y), double(k.z), k.x + 1., k.y + k.top, k.z + 1.});
    owners.push_back(int(i)); centers.insert(centers.end(), {k.x + .5, k.y + .5, k.z + .5}); opaque.push_back(1);
  }
  int no = int(blocks.size()); std::vector<int64_t> ids(2 * no); double st[8];
  cache_update(c, boxes.data(), owners.data(), no, centers.data(), opaque.data(), no, ids.data(), st); return c;
}
// Any pose among the given standing positions (yaw 0..345, pitch -90..90) that confirms the box.
static int count_poses(Cache* c, Box b, double feet, double eyeH, const std::vector<std::array<double, 2>>& xz) {
  int hits = 0;
  for (auto p : xz) for (double yaw = 0; yaw < 360; yaw += 15) for (double pitch = -90; pitch <= 90; pitch += 5) {
    double cam[5] = {p[0], feet + eyeH, p[1], yaw, pitch}; View v = make_view(c->scene, cam);
    hits += box_fully_visible(c->scene, v, b, 16.0);
  }
  return hits;
}
int main() {
  std::vector<std::array<double, 2>> upper, overhang;
  for (double x : {-4.5, -2.5, -1.5, -1.0, -0.7, -0.5, -0.31}) for (double z : {-1.5, 0.5, 2.5}) upper.push_back({x, z});
  for (double x : {0.0, 0.1, 0.2, 0.29}) overhang.push_back({x, 0.5});
  {  // one-block ledge; drop cell (0,0,0)
    std::vector<Blk> b; for (int x = -12; x <= 12; ++x) for (int z = -12; z <= 12; ++z) { b.push_back({x, -1, z, 1}); if (x < 0) b.push_back({x, 0, z, 1}); }
    Cache* c = build(b);
    std::printf("Ledge drop cell (0,0,0), standing on the upper level:\n");
    std::printf("  whole cell                      %5d poses\n", count_poses(c, {0, 0, 0, 1, 1, 1}, 1, 1.62, upper));
    std::printf("  upper half  y in [0.5,1]        %5d poses\n", count_poses(c, {0, .5, 0, 1, 1, 1}, 1, 1.62, upper));
    std::printf("  far part    x in [0.3,1]        %5d poses\n", count_poses(c, {.3, 0, 0, 1, 1, 1}, 1, 1.62, upper));
    std::printf("  near-bottom x<0.3, y<0.5        %5d poses\n", count_poses(c, {0, 0, 0, .3, .5, 1}, 1, 1.62, upper));
    std::printf("  whole cell, sneaking at the edge (eye x>=0) %d poses\n", count_poses(c, {0, 0, 0, 1, 1, 1}, 1, 1.27, overhang));
  }
  {  // bottom-slab step down; lower body cell (0,1,0), slabs at y=1 for x<0
    std::vector<Blk> b; for (int x = -12; x <= 12; ++x) for (int z = -12; z <= 12; ++z) { b.push_back({x, 0, z, 1}); if (x < 0) b.push_back({x, 1, z, .5}); }
    Cache* c = build(b);
    std::printf("Slab step-down cell (0,1,0), standing on the slabs:\n");
    std::printf("  whole cell                      %5d poses\n", count_poses(c, {0, 1, 0, 1, 2, 1}, 1.5, 1.62, upper));
    std::printf("  upper half  y in [1.5,2]        %5d poses\n", count_poses(c, {0, 1.5, 0, 1, 2, 1}, 1.5, 1.62, upper));
    std::printf("  whole cell, eye over the cell (x>=0) %d poses\n", count_poses(c, {0, 1, 0, 1, 2, 1}, 1.5, 1.62, overhang));
  }
  return 0;
}
