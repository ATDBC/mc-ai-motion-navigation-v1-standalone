// Persistent world geometry cache. All access belongs to one client thread.
#include "geometry.hpp"
#include <memory>
#include <stdexcept>
#include <string>
#include <limits>

using Key=std::array<int,3>;
constexpr int MAX_BLOCKS=25000,MAX_BOXES=100000;
static thread_local std::string error;
struct Block{Key key;std::vector<Box> boxes;int slot=-1;bool opaque=false;};
struct Tile{std::set<Key> blocks;std::vector<Face> faces;};
struct Cache{
 int edge;bool valid=true;std::map<Key,Block> blocks;std::map<Key,Tile> tiles;
 std::map<Key,std::set<Key>> grid;std::vector<int> freeSlots,denseSlots,generation;
 Scene scene;std::vector<unsigned char> scratchOut;std::vector<double> scratchArea;
 explicit Cache(int e):edge(e){if(e!=4&&e!=8)throw std::invalid_argument("tile edge must be 4 or 8");}
 Key tile(Key k)const{return {int(floor(double(k[0])/edge)),int(floor(double(k[1])/edge)),int(floor(double(k[2])/edge))};}
 template<class F> void cells(const Box& b,F fn)const{
  for(int x=int(floor(b[0]));x<=int(floor(b[3]));++x)for(int y=int(floor(b[1]));y<=int(floor(b[4]));++y)for(int z=int(floor(b[2]));z<=int(floor(b[5]));++z)fn(Key{x,y,z});
 }
 void affect(const Block& b,std::set<Key>& dirty){
  dirty.insert(tile(b.key));
  for(auto& box:b.boxes)cells(box,[&](Key cell){auto it=grid.find(cell);if(it!=grid.end())for(auto k:it->second)dirty.insert(tile(k));});
 }
 void removeGrid(const Block& b){for(auto& box:b.boxes)cells(box,[&](Key k){auto it=grid.find(k);if(it!=grid.end()){it->second.erase(b.key);if(it->second.empty())grid.erase(it);}});}
 void addGrid(const Block& b){for(auto& box:b.boxes)cells(box,[&](Key k){grid[k].insert(b.key);});}
 void rebuild(Key key){
  auto& t=tiles.at(key);t.faces.clear();
  for(auto bk:t.blocks){const auto& block=blocks.at(bk);
   for(size_t bi=0;bi<block.boxes.size();++bi){const Box& box=block.boxes[bi];std::set<Key> neighbors;
    cells(box,[&](Key cell){auto it=grid.find(cell);if(it!=grid.end())neighbors.insert(it->second.begin(),it->second.end());});
    for(int axis=0;axis<3;++axis)for(int sign:{-1,1}){int u=(axis+1)%3,v=(axis+2)%3;double coord=box[axis+(sign>0?3:0)];std::vector<Rect> pieces={{box[u],box[v],box[u+3],box[v+3]}};
     for(auto nk:neighbors){const auto& n=blocks.at(nk);if(!n.opaque&&n.slot!=block.slot)continue;
      for(size_t ni=0;ni<n.boxes.size();++ni){if(n.slot==block.slot&&ni==bi)continue;const auto& b=n.boxes[ni];bool beyond=sign>0?(b[axis]<=coord&&b[axis+3]>coord):(b[axis]<coord&&b[axis+3]>=coord);if(!beyond)continue;
       std::vector<Rect> next;for(auto r:pieces){auto cut=subtract_rect(r,{b[u],b[v],b[u+3],b[v+3]});next.insert(next.end(),cut.begin(),cut.end());}pieces=std::move(next);if(pieces.empty())break;
      }if(pieces.empty())break;
     }
     for(auto r:pieces)t.faces.push_back({block.slot,axis,sign,coord,r});
    }
   }
  }
 }
 void update(const double* boxData,const int* owners,int nb,const double* centers,const unsigned char* opaque,int no,int64_t* ids,double* stats){
  if(!valid)throw std::runtime_error("cache invalid after previous failure");
  if(nb<0||nb>MAX_BOXES||no<0||no>MAX_BLOCKS)throw std::invalid_argument("cache capacity exceeded");
  auto start=Clock::now();std::vector<Block> incoming(no);std::set<Key> seen;
  for(int i=0;i<no;++i){for(int a=0;a<3;++a){double v=centers[i*3+a]-.5;if(!std::isfinite(v)||v!=floor(v)||std::abs(v)>30000000)throw std::invalid_argument("invalid grid center");incoming[i].key[a]=int(v);}if(opaque[i]>1||!seen.insert(incoming[i].key).second)throw std::invalid_argument("duplicate block or invalid opacity");incoming[i].opaque=opaque[i]!=0;}
  for(int j=0;j<nb;++j){if(owners[j]<0||owners[j]>=no)throw std::invalid_argument("invalid shape owner");Box b;std::copy(boxData+j*6,boxData+j*6+6,b.begin());auto key=incoming[owners[j]].key;
   for(int a=0;a<3;++a)if(!std::isfinite(b[a])||!std::isfinite(b[a+3])||b[a]>=b[a+3]||b[a]<key[a]-2.||b[a+3]>key[a]+3.)throw std::invalid_argument("unsupported shape extent");incoming[owners[j]].boxes.push_back(b);}
  for(auto& b:incoming){if(b.boxes.empty())throw std::invalid_argument("empty block shape");std::sort(b.boxes.begin(),b.boxes.end());}
  std::set<Key> dirty;int added=0,removed=0,changed=0;
  for(auto it=blocks.begin();it!=blocks.end();){if(seen.count(it->first)){++it;continue;}auto b=it->second;affect(b,dirty);removeGrid(b);tiles[tile(b.key)].blocks.erase(b.key);scene.query[b.slot]=-1;scene.opaque[b.slot]=0;freeSlots.push_back(b.slot);it=blocks.erase(it);++removed;}
  denseSlots.resize(no);
  for(int i=0;i<no;++i){auto& b=incoming[i];auto old=blocks.find(b.key);
   if(old!=blocks.end()){b.slot=old->second.slot;if(b.boxes==old->second.boxes&&b.opaque==old->second.opaque){denseSlots[i]=b.slot;continue;}affect(old->second,dirty);removeGrid(old->second);++changed;}
   else{++added;if(freeSlots.empty()){b.slot=int(generation.size());if(b.slot>=MAX_BLOCKS)throw std::runtime_error("slot capacity exceeded");generation.push_back(1);scene.centers.push_back({});scene.opaque.push_back(0);scene.query.push_back(-1);}else{b.slot=freeSlots.back();freeSlots.pop_back();if(generation[b.slot]==std::numeric_limits<int>::max())throw std::runtime_error("slot generation exhausted");++generation[b.slot];}tiles[tile(b.key)].blocks.insert(b.key);}
   scene.centers[b.slot]={b.key[0]+.5,b.key[1]+.5,b.key[2]+.5};scene.opaque[b.slot]=b.opaque;blocks[b.key]=b;addGrid(b);affect(b,dirty);denseSlots[i]=b.slot;
  }
  auto compared=Clock::now();
  for(auto key:dirty){auto it=tiles.find(key);if(it==tiles.end())continue;if(it->second.blocks.empty())tiles.erase(it);else rebuild(key);}
  if(!dirty.empty()){scene.faces.clear();for(auto& [key,t]:tiles)scene.faces.insert(scene.faces.end(),t.faces.begin(),t.faces.end());}
  for(int i=0;i<no;++i){ids[2*i]=denseSlots[i];ids[2*i+1]=generation[denseSlots[i]];}
  auto end=Clock::now();double s[8]={double(added),double(removed),double(changed),double(dirty.size()),double(tiles.size()),double(scene.faces.size()),ms(start,compared),ms(compared,end)};std::copy(s,s+8,stats);
 }
 int frame(const double* cam,const unsigned char* query,int no,int fast,unsigned char* output,double* areas,double* times,int64_t* stats,bool posed=false){
  if(!valid||no!=int(denseSlots.size()))throw std::invalid_argument("invalid cache or output count");
  for(int i=0;i<(posed?5:4);++i)if(!std::isfinite(cam[i]))throw std::invalid_argument("nonfinite camera");
  double pose[5]={cam[0],cam[1],cam[2],cam[3],posed?cam[4]:0};if(std::abs(pose[4])>90)throw std::invalid_argument("pitch outside -90..90");
  std::fill(scene.query.begin(),scene.query.end(),-1);for(int i=0;i<no;++i)scene.query[denseSlots[i]]=query[i]?i:-1;
  scratchOut.resize(scene.centers.size());scratchArea.resize(scene.centers.size());
  center_frame_pose(&scene,pose,fast?3:4,scratchOut.data(),scratchArea.data(),times,stats);int count=0;
  for(int i=0;i<no;++i){output[i]=scratchOut[denseSlots[i]];areas[i]=fast?0.:scratchArea[denseSlots[i]];count+=output[i];}return count;
 }
};
extern "C" {
__declspec(dllexport) const char* cache_error(){return error.c_str();}
__declspec(dllexport) void* cache_create(int edge){try{error.clear();return new Cache(edge);}catch(const std::exception& e){error=e.what();return nullptr;}}
__declspec(dllexport) void cache_destroy(void* ptr){delete static_cast<Cache*>(ptr);}
__declspec(dllexport) int cache_update(void* ptr,const double* boxes,const int* owners,int nb,const double* centers,const unsigned char* opaque,int no,int64_t* ids,double* stats){
 try{if(!ptr)throw std::invalid_argument("null cache");static_cast<Cache*>(ptr)->update(boxes,owners,nb,centers,opaque,no,ids,stats);return 0;}catch(const std::exception& e){error=e.what();if(ptr)static_cast<Cache*>(ptr)->valid=false;return -1;}}
__declspec(dllexport) int cache_frame(void* ptr,const double* cam,const unsigned char* query,int no,int fast,unsigned char* output,double* areas,double* times,int64_t* stats){
 try{if(!ptr)throw std::invalid_argument("null cache");return static_cast<Cache*>(ptr)->frame(cam,query,no,fast,output,areas,times,stats);}catch(const std::exception& e){error=e.what();return -1;}}
__declspec(dllexport) int cache_frame_pose(void* ptr,const double* cam,const unsigned char* query,int no,int fast,unsigned char* output,double* areas,double* times,int64_t* stats){
 try{if(!ptr)throw std::invalid_argument("null cache");return static_cast<Cache*>(ptr)->frame(cam,query,no,fast,output,areas,times,stats,true);}catch(const std::exception& e){error=e.what();return -1;}}
}
