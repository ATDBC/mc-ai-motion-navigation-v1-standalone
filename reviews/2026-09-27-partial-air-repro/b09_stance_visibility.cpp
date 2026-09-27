// Round-15 review: which cells around the B09 gap fixture can the formal
// partial-visibility air rule ever confirm from the gap start stance?
// Geometry copies scripts/air_motion_runtime.py: a 5x5x5 air volume
// (x,z in -2..2, y in 62..66) cut into a superflat world (grass 63, dirt 60..62),
// with the start support (0,63,0) and four targets two cells away.
// Build (from deployment/surface-depth-diagnostic/native of a 4b9e73e checkout):
//   g++ -std=c++17 -O2 "-D__declspec(x)=" -I. -Ivendor/clipper2/include \
//       <this file> vendor/clipper2/src/clipper.engine.cpp -o /tmp/b09_stance
//   /tmp/b09_stance > /tmp/stance_visibility.json        # union over all yaw/pitch
//   printf '0 -30\n' | /tmp/b09_stance poses               # one line per 'camYaw pitch' pose
#include "cache.cpp"
#include <cstdio>
#include <string>
#include <set>
#include <vector>
int main(int argc, char** argv) {
  bool perPose = argc > 1 && std::string(argv[1]) == "poses";
  std::set<std::array<int, 3>> solid;
  for (int x = -12; x <= 12; ++x) for (int z = -12; z <= 12; ++z) for (int y = 60; y <= 63; ++y) {
    bool inVolume = x >= -2 && x <= 2 && z >= -2 && z <= 2 && y >= 62 && y <= 66;
    if (!inVolume) solid.insert({x, y, z});
  }
  for (auto p : std::vector<std::array<int, 3>>{{0, 63, 0}, {0, 63, 2}, {2, 63, 0}, {0, 63, -2}, {-2, 63, 0}}) solid.insert(p);
  void* c = cache_create(4);
  std::vector<double> boxes, centers; std::vector<int> owners; std::vector<unsigned char> opaque; int n = 0;
  for (auto p : solid) {
    boxes.insert(boxes.end(), {double(p[0]), double(p[1]), double(p[2]), p[0] + 1., p[1] + 1., p[2] + 1.});
    owners.push_back(n); centers.insert(centers.end(), {p[0] + .5, p[1] + .5, p[2] + .5}); opaque.push_back(1); ++n;
  }
  std::vector<int64_t> ids(2 * n); double st[8];
  if (cache_update(c, boxes.data(), owners.data(), n, centers.data(), opaque.data(), n, ids.data(), st) < 0) return 1;
  std::vector<std::array<int, 3>> cells;
  for (int x = -3; x <= 3; ++x) for (int y = 60; y <= 67; ++y) for (int z = -3; z <= 3; ++z)
    if (!solid.count({x, y, z})) cells.push_back({x, y, z});
  if (perPose) {
    // One JSON line per pose: the cells whose visual-air status is 1 at that pose.
    double yaw, pitch;
    while (std::scanf("%lf %lf", &yaw, &pitch) == 2) {
      std::printf("[");
      bool first = true;
      for (size_t start = 0; start < cells.size(); start += 128) {
        int count = int(std::min<size_t>(128, cells.size() - start));
        std::vector<int> pos; for (int i = 0; i < count; ++i) pos.insert(pos.end(), {cells[start + i][0], cells[start + i][1], cells[start + i][2]});
        std::vector<unsigned char> q(n, 1), out(n), air(count); std::vector<double> areas(n), times(3); std::vector<int64_t> stats(8);
        double cam[5] = {.5, 64 + 1.62, .5, yaw, pitch};
        if (cache_frame_pose_air(c, cam, q.data(), n, 1, out.data(), areas.data(), times.data(), stats.data(), pos.data(), count, 16., air.data()) < 0) return 2;
        for (int i = 0; i < count; ++i) if (air[i] == 1) { std::printf("%s[%d,%d,%d]", first ? "" : ",", cells[start + i][0], cells[start + i][1], cells[start + i][2]); first = false; }
      }
      std::printf("]\n");
    }
    return 0;
  }
  std::vector<int> ever1(cells.size()), ever0(cells.size()), blockSeen(n);
  std::vector<std::array<int, 3>> solidList(solid.begin(), solid.end());
  for (double yaw = 0; yaw < 360; yaw += 15) for (double pitch = -90; pitch <= 90; pitch += 5)
    for (size_t start = 0; start < cells.size(); start += 128) {
      int count = int(std::min<size_t>(128, cells.size() - start));
      std::vector<int> pos; for (int i = 0; i < count; ++i) pos.insert(pos.end(), {cells[start + i][0], cells[start + i][1], cells[start + i][2]});
      std::vector<unsigned char> q(n, 1), out(n), air(count); std::vector<double> areas(n), times(3); std::vector<int64_t> stats(8);
      double cam[5] = {.5, 64 + 1.62, .5, yaw, pitch};
      if (cache_frame_pose_air(c, cam, q.data(), n, 1, out.data(), areas.data(), times.data(), stats.data(), pos.data(), count, 16., air.data()) < 0) return 2;
      for (int i = 0; i < count; ++i) { ever1[start + i] |= air[i] == 1; ever0[start + i] |= air[i] == 0; }
      for (int i = 0; i < n; ++i) blockSeen[i] |= out[i];
    }
  std::printf("{\"confirmable\":[");
  bool first = true;
  for (size_t i = 0; i < cells.size(); ++i) if (ever1[i]) { std::printf("%s[%d,%d,%d]", first ? "" : ",", cells[i][0], cells[i][1], cells[i][2]); first = false; }
  std::printf("],\"occluded_only\":[");
  first = true;
  for (size_t i = 0; i < cells.size(); ++i) if (!ever1[i] && ever0[i]) { std::printf("%s[%d,%d,%d]", first ? "" : ",", cells[i][0], cells[i][1], cells[i][2]); first = false; }
  std::printf("],\"visible_blocks\":[");
  first = true;
  for (int i = 0; i < n; ++i) if (blockSeen[i]) { std::printf("%s[%d,%d,%d]", first ? "" : ",", solidList[i][0], solidList[i][1], solidList[i][2]); first = false; }
  std::printf("]}\n");
}
