// Round-14 review: NavigationSession._information_look() treats a missing cell as
// "already in view" when its centre is within 60 degrees of yaw and pitch; the
// native visual-air rule needs all eight corners inside the frustum.  Flat floor,
// player standing at (0.5, 1, 0.5) looking toward +x (Minecraft yaw -90).
// Build like downward_air_coverage.cpp (same flags, this file instead).
#include "cache.cpp"
#include <cmath>
#include <cstdio>
#include <vector>
int main() {
  void* c = cache_create(4);
  std::vector<double> boxes, centers; std::vector<int> owners; std::vector<unsigned char> opaque; int n = 0;
  for (int x = -12; x <= 12; ++x) for (int z = -12; z <= 12; ++z) {
    boxes.insert(boxes.end(), {double(x), 0., double(z), x + 1., 1., z + 1.}); owners.push_back(n);
    centers.insert(centers.end(), {x + .5, .5, z + .5}); opaque.push_back(1); ++n;
  }
  std::vector<int64_t> ids(2 * n); double st[8];
  cache_update(c, boxes.data(), owners.data(), n, centers.data(), opaque.data(), n, ids.data(), st);
  const double ex = .5, ey = 1 + 1.62, ez = .5;
  int cells[][3] = {{1, 1, 0}, {2, 1, 0}, {1, 1, 1}};
  for (auto& cell : cells) {
    double dx = cell[0] + .5 - ex, dy = cell[1] + .5 - ey, dz = cell[2] + .5 - ez;
    double centrePitch = -std::atan2(dy, std::hypot(dx, dz)) * 180 / M_PI;
    double centreYaw = std::atan2(dz, dx) * 180 / M_PI;  // relative to +x
    std::printf("cell (%d,%d,%d): centre offset yaw %.1f, pitch %.1f -> _information_look treats it as %s at pitch 0\n",
                cell[0], cell[1], cell[2], centreYaw, centrePitch,
                std::abs(centreYaw) <= 60 && std::abs(centrePitch) <= 60 ? "IN VIEW (no look)" : "out of view");
    std::printf("  native visual air at pitch:");
    for (double pitch = 0; pitch <= 90; pitch += 10) {
      std::vector<unsigned char> q(n, 1), out(n), air(1); std::vector<double> areas(n), times(3); std::vector<int64_t> stats(8);
      double cam[5] = {ex, ey, ez, 90, pitch}; int pos[3] = {cell[0], cell[1], cell[2]};
      cache_frame_pose_air(c, cam, q.data(), n, 1, out.data(), areas.data(), times.data(), stats.data(), pos, 1, 16., air.data());
      std::printf(" %g:%s", pitch, air[0] ? "yes" : "no");
    }
    std::printf("\n");
  }
}
