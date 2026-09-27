// Round-14 review: which navigation-relevant air cells can profile-4 visual air
// ever confirm?  Uses the repository's own native core (cache.cpp/geometry.hpp at
// b740098) unchanged; only the Windows export macro is replaced.
//
// Build (from deployment/surface-depth-diagnostic/native of a b740098 checkout):
//   g++ -std=c++17 -O2 "-D__declspec(x)=" -I. -Ivendor/clipper2/include \
//       <this file> vendor/clipper2/src/clipper.engine.cpp -o /tmp/downward_air
//   /tmp/downward_air
#include "cache.cpp"
#include <cstdio>
#include <string>
#include <vector>

struct Blk { int x, y, z; double b[6]; };
static Blk full(int x, int y, int z) { return {x, y, z, {double(x), double(y), double(z), x + 1., y + 1., z + 1.}}; }
static Blk slab(int x, int y, int z) { return {x, y, z, {double(x), double(y), double(z), x + 1., y + .5, z + 1.}}; }

struct World {
  void* cache = nullptr; int no = 0;
  explicit World(const std::vector<Blk>& blocks) {
    cache = cache_create(4);
    std::vector<double> boxes, centers; std::vector<int> owners; std::vector<unsigned char> opaque;
    for (size_t i = 0; i < blocks.size(); ++i) {
      const auto& k = blocks[i];
      boxes.insert(boxes.end(), k.b, k.b + 6); owners.push_back(int(i));
      centers.insert(centers.end(), {k.x + .5, k.y + .5, k.z + .5}); opaque.push_back(1);
    }
    no = int(blocks.size()); std::vector<int64_t> ids(2 * no); double stats[8];
    if (cache_update(cache, boxes.data(), owners.data(), no, centers.data(), opaque.data(), no, ids.data(), stats) < 0) {
      std::printf("update failed: %s\n", cache_error()); std::exit(1);
    }
  }
  // Returns one flag per candidate, exactly as SurfaceSensor.sample() would.
  std::vector<int> air(double ex, double ey, double ez, double camYaw, double pitch, const std::vector<std::array<int, 3>>& cells) {
    double cam[5] = {ex, ey, ez, camYaw, pitch};
    std::vector<unsigned char> query(no, 1), out(no), airOut(cells.size());
    std::vector<double> areas(no), times(3); std::vector<int64_t> stats(8); std::vector<int> pos;
    for (auto& c : cells) pos.insert(pos.end(), {c[0], c[1], c[2]});
    if (cache_frame_pose_air(cache, cam, query.data(), no, 1, out.data(), areas.data(), times.data(), stats.data(),
                             pos.data(), int(cells.size()), 16.0, airOut.data()) < 0) {
      std::printf("frame failed: %s\n", cache_error()); std::exit(1);
    }
    return std::vector<int>(airOut.begin(), airOut.end());
  }
};

// Minecraft yaw -90 faces +x; SurfaceSensor passes -yaw, so camera yaw 90 faces +x.
struct Sweep { int poses = 0; std::vector<int> hits; std::vector<std::string> first; };

static Sweep sweep(World& w, const std::vector<std::array<int, 3>>& cells, double feetY,
                   const std::vector<std::array<double, 2>>& standXZ, double eyeHeight) {
  Sweep s; s.hits.assign(cells.size(), 0); s.first.assign(cells.size(), "-");
  for (auto xz : standXZ)
    for (double yaw = 0; yaw < 360; yaw += 15)
      for (double pitch = -90; pitch <= 90; pitch += 5) {
        auto r = w.air(xz[0], feetY + eyeHeight, xz[1], yaw, pitch, cells); ++s.poses;
        for (size_t i = 0; i < cells.size(); ++i) if (r[i]) {
          if (!s.hits[i]) { char b[96]; std::snprintf(b, sizeof b, "eye x=%.2f z=%.2f yaw=%.0f pitch=%.0f", xz[0], xz[1], yaw, pitch); s.first[i] = b; }
          ++s.hits[i];
        }
      }
  return s;
}

static void report(const char* title, const Sweep& s, const std::vector<std::array<int, 3>>& cells, const std::vector<const char*>& names) {
  std::printf("%s  (%d poses)\n", title, s.poses);
  for (size_t i = 0; i < cells.size(); ++i)
    std::printf("  %-34s (%d,%d,%d): confirmed in %5d poses; first: %s\n", names[i], cells[i][0], cells[i][1], cells[i][2], s.hits[i], s.first[i].c_str());
}

int main() {
  const double STAND = 1.62, SNEAK = 1.27;
  // Standing positions on the upper level: centre x <= -0.3 keeps the hitbox off the
  // edge; -0.3 < x < 0.3 overhangs the edge while still supported (sneak-edge poses).
  std::vector<std::array<double, 2>> onUpper, overhang;
  for (double x : {-8.5, -6.5, -4.5, -3.5, -2.5, -1.5, -1.0, -0.7, -0.5, -0.31})
    for (double z : {-3.5, -1.5, 0.5, 2.5, 4.5}) onUpper.push_back({x, z});
  for (double x : {-0.2, 0.0, 0.1, 0.2, 0.29}) overhang.push_back({x, 0.5});

  // Scene A: one-block ledge.  Upper floor top y=1 for x<0; lower floor top y=0.
  {
    std::vector<Blk> b;
    for (int x = -12; x <= 12; ++x) for (int z = -12; z <= 12; ++z) {
      b.push_back(full(x, -1, z)); if (x < 0) b.push_back(full(x, 0, z));
    }
    World w(b);
    std::vector<std::array<int, 3>> cells = {{0, 0, 0}, {1, 0, 0}, {0, 1, 0}, {3, 1, 0}};
    std::vector<const char*> names = {"drop body cell next to ledge", "drop cell one further", "upper cell above the drop", "control: open cell at y=1"};
    report("A. one-block ledge, standing on the upper level", sweep(w, cells, 1, onUpper, STAND), cells, names);
    report("A'. same ledge, sneaking with the body overhanging the edge", sweep(w, cells, 1, overhang, SNEAK), cells, names);
  }
  // Scene B: bottom-slab step down.  Slabs at y=1 for x<0 on a floor whose top is y=1.
  {
    std::vector<Blk> b;
    for (int x = -12; x <= 12; ++x) for (int z = -12; z <= 12; ++z) {
      b.push_back(full(x, 0, z)); if (x < 0) b.push_back(slab(x, 1, z));
    }
    World w(b);
    std::vector<std::array<int, 3>> cells = {{0, 1, 0}, {0, 2, 0}, {3, 1, 0}};
    std::vector<const char*> names = {"lower body cell after slab step", "upper body cell after slab step", "control: open cell at y=1"};
    report("B. half-slab step down, standing on the slabs", sweep(w, cells, 1.5, onUpper, STAND), cells, names);
  }
  // Scene C: 1x1 pit in a flat floor (floor top y=1, pit cell (0,0,0)).
  {
    std::vector<Blk> b;
    for (int x = -12; x <= 12; ++x) for (int z = -12; z <= 12; ++z) {
      b.push_back(full(x, -1, z)); if (x != 0 || z != 0) b.push_back(full(x, 0, z));
    }
    World w(b);
    std::vector<std::array<double, 2>> around;
    for (double x : {-6.5, -4.5, -2.5, -1.5, -0.5}) for (double z : {-2.5, 0.5, 3.5}) around.push_back({x, z});
    std::vector<std::array<int, 3>> cells = {{0, 0, 0}, {0, 1, 0}};
    std::vector<const char*> names = {"pit cell", "cell above the pit"};
    report("C. 1x1 pit, standing on the floor around it", sweep(w, cells, 1, around, STAND), cells, names);
  }
  return 0;
}
