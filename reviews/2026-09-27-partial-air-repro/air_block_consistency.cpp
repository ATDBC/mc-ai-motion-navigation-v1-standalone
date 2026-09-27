// Round-15 review: consistency between the partial-visibility air rule and the
// block visibility rule, which the Java merge relies on ("a cell whose block is
// visible is never air").  For every full block in random scenes we ask the native
// air probe about the block's own cell: status 1 there while the block itself is
// not reported means the client would emit visual air for a cell that holds a
// full block.
// Build (from deployment/surface-depth-diagnostic/native of a 4b9e73e checkout):
//   g++ -std=c++17 -O2 "-D__declspec(x)=" -I. -Ivendor/clipper2/include \
//       <this file> vendor/clipper2/src/clipper.engine.cpp -o /tmp/air_block_consistency
// Run: /tmp/air_block_consistency [worlds]
#include "cache.cpp"
#include <cstdio>
#include <random>
#include <set>
#include <vector>
int main(int argc, char** argv) {
  int worlds = argc > 1 ? std::atoi(argv[1]) : 200;
  std::mt19937 rng(20260927);
  long checked = 0, airOnReported = 0, airOnHidden = 0, boundary = 0, interior = 0;
  double worstDistance = 0;
  for (int w = 0; w < worlds; ++w) {
    std::set<std::array<int, 3>> solid;
    std::uniform_real_distribution<double> u(0, 1);
    double density = 0.08 + 0.25 * u(rng);
    for (int x = -20; x <= 20; ++x) for (int z = -20; z <= 20; ++z) {
      solid.insert({x, 0, z});
      for (int y = 1; y <= 5; ++y) if (u(rng) < density * (y <= 2 ? 1.0 : 0.4)) solid.insert({x, y, z});
    }
    std::array<int, 3> eyeCell = {int(std::floor(-3 + 6 * u(rng))), 1, int(std::floor(-3 + 6 * u(rng)))};
    solid.erase(eyeCell); solid.erase({eyeCell[0], 2, eyeCell[2]});
    std::vector<std::array<int, 3>> list(solid.begin(), solid.end());
    void* c = cache_create(4);
    std::vector<double> boxes, centers; std::vector<int> owners; std::vector<unsigned char> opaque; int n = 0;
    for (auto p : list) {
      boxes.insert(boxes.end(), {double(p[0]), double(p[1]), double(p[2]), p[0] + 1., p[1] + 1., p[2] + 1.});
      owners.push_back(n); centers.insert(centers.end(), {p[0] + .5, p[1] + .5, p[2] + .5}); opaque.push_back(1); ++n;
    }
    std::vector<int64_t> ids(2 * n); double st[8];
    if (cache_update(c, boxes.data(), owners.data(), n, centers.data(), opaque.data(), n, ids.data(), st) < 0) return 1;
    double eye[3] = {eyeCell[0] + .3 + .4 * u(rng), 1 + 1.62, eyeCell[2] + .3 + .4 * u(rng)};
    double yaw = 360 * u(rng), pitch = -30 + 60 * u(rng);
    std::vector<unsigned char> query(n), out(n); std::vector<double> areas(n), times(3); std::vector<int64_t> stats(8);
    for (int i = 0; i < n; ++i) {  // SurfaceSensor.sample(): query only blocks within 16 of the eye
      double dx = std::max({list[i][0] - eye[0], eye[0] - list[i][0] - 1, 0.}), dy = std::max({list[i][1] - eye[1], eye[1] - list[i][1] - 1, 0.}), dz = std::max({list[i][2] - eye[2], eye[2] - list[i][2] - 1, 0.});
      query[i] = dx * dx + dy * dy + dz * dz <= 256;
    }
    double cam[5] = {eye[0], eye[1], eye[2], yaw, pitch};
    // The air call reports on at most 128 cells, so ask in batches (visible blocks are the same each time).
    for (int start = 0; start < n; start += 128) {
      int count = std::min(128, n - start);
      std::vector<int> pos; for (int i = 0; i < count; ++i) pos.insert(pos.end(), {list[start + i][0], list[start + i][1], list[start + i][2]});
      std::vector<unsigned char> air(count);
      if (cache_frame_pose_air(c, cam, query.data(), n, 1, out.data(), areas.data(), times.data(), stats.data(), pos.data(), count, 16., air.data()) < 0) return 2;
      for (int i = 0; i < count; ++i) {
        ++checked;
        if (air[i] != 1) continue;
        int k = start + i;
        if (out[k]) { ++airOnReported; continue; }
        ++airOnHidden;
        double dx = std::max({list[k][0] - eye[0], eye[0] - list[k][0] - 1, 0.}), dy = std::max({list[k][1] - eye[1], eye[1] - list[k][1] - 1, 0.}), dz = std::max({list[k][2] - eye[2], eye[2] - list[k][2] - 1, 0.});
        double d = std::sqrt(dx * dx + dy * dy + dz * dz);
        worstDistance = std::max(worstDistance, d);
        if (d > 15.0) ++boundary; else ++interior;
      }
    }
    cache_destroy(c);
  }
  std::printf("%d worlds, %ld full-block cells asked\n", worlds, checked);
  std::printf("  air status 1 and block reported (Java drops the air): %ld\n", airOnReported);
  std::printf("  air status 1 but block NOT reported (client emits air for a full block): %ld\n", airOnHidden);
  std::printf("    nearest point 15-16 blocks away: %ld, nearer than 15: %ld; farthest nearest-point %.3f\n", boundary, interior, worstDistance);
}
