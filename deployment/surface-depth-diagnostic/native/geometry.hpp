// Continuous surface geometry, derived from the frozen 2026-09-11 reference.
// Modes 0-2 retain reference behavior; 3 is boolean-only, 4 retains full areas.
#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <map>
#include <set>
#include <tuple>
#include <vector>
#include "clipper2/clipper.engine.h"
using Clipper2Lib::Paths64; using Clipper2Lib::Path64; using Clipper2Lib::Point64; using Clipper2Lib::Clipper64; using Clipper2Lib::ClipType; using Clipper2Lib::FillRule; using Clipper2Lib::Area; using Clipper2Lib::PointInPolygon; using Clipper2Lib::PointInPolygonResult;
using Clock=std::chrono::steady_clock;using V=std::array<double,3>;using P=std::array<double,2>;using Box=std::array<double,6>;using Rect=std::array<double,4>;using Poly=std::vector<P>;
constexpr double SCALE=1e12,S=0.5773502691896257645,EPS=1e-12;
static double ms(Clock::time_point a,Clock::time_point b){return std::chrono::duration<double,std::milli>(b-a).count();}
static double cross(P a,P b,P c){return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);}
static double area(const Poly& p){double a=0;for(size_t i=0;i<p.size();++i){auto x=p[i],y=p[(i+1)%p.size()];a+=x[0]*y[1]-x[1]*y[0];}return a*.5;}
static Poly halfplane(const Poly& p,double a,double b,double c){
 if(p.empty())return {};Poly out;P prev=p.back();double dp=a*prev[0]+b*prev[1]+c;
 for(P cur:p){double dc=a*cur[0]+b*cur[1]+c;if((dp>=0)!=(dc>=0)){double t=dp/(dp-dc);out.push_back({prev[0]+t*(cur[0]-prev[0]),prev[1]+t*(cur[1]-prev[1])});}if(dc>=0)out.push_back(cur);prev=cur;dp=dc;}return out;
}
static Paths64 boolean_op(const Paths64& subject,const Paths64& clip,ClipType type){
 if(subject.empty())return {};Clipper64 c;c.PreserveCollinear(false);c.AddSubject(subject);if(!clip.empty())c.AddClip(clip);Paths64 result;c.Execute(type,FillRule::NonZero,result);return result;
}
static Path64 quantize(const Poly& p){Path64 r;for(P a:p)r.emplace_back(int64_t(std::llround(a[0]*SCALE)),int64_t(std::llround(a[1]*SCALE)));if(Area(r)<0)std::reverse(r.begin(),r.end());return r;}
static Rect bounds(const Poly& p){Rect r={1e30,1e30,-1e30,-1e30};for(P a:p){r[0]=std::min(r[0],a[0]);r[1]=std::min(r[1],a[1]);r[2]=std::max(r[2],a[0]);r[3]=std::max(r[3],a[1]);}return r;}
static bool overlaps(Rect a,Rect b){return std::max(a[0],b[0])<std::min(a[2],b[2])-EPS&&std::max(a[1],b[1])<std::min(a[3],b[3])-EPS;}
static std::vector<Rect> subtract_rect(Rect a,Rect b){
 double x0=std::max(a[0],b[0]),y0=std::max(a[1],b[1]),x1=std::min(a[2],b[2]),y1=std::min(a[3],b[3]);if(x1<=x0||y1<=y0)return {a};std::vector<Rect> out;
 if(a[0]<x0)out.push_back({a[0],a[1],x0,a[3]});if(x1<a[2])out.push_back({x1,a[1],a[2],a[3]});if(a[1]<y0)out.push_back({x0,a[1],x1,y0});if(y1<a[3])out.push_back({x0,y1,x1,a[3]});return out;
}
struct Face{int owner,axis,sign;double coord;Rect uv;};
struct Scene{std::vector<Box> boxes;std::vector<int> owner,query;std::vector<V> centers;std::vector<unsigned char> opaque;std::vector<Face> faces;double prep[5]={};};
struct Projected{int owner,face;Poly p;Path64 path;Rect rect;V inv;};
struct View{std::vector<Projected> faces;std::vector<Paths64> silhouettes;std::vector<Rect> rects;std::vector<double> z,dist;V eye,right,up,forward;};
static void orient(View& v,const double* cam,double pitch){
 v.eye={cam[0],cam[1],cam[2]};double yaw=cam[3]*3.14159265358979323846/180.,p=pitch*3.14159265358979323846/180.;double co=cos(yaw),si=sin(yaw),cp=cos(p),sp=sin(p);
 v.right={co,0,-si};v.up={si*sp,cp,co*sp};v.forward={si*cp,-sp,co*cp};
}
static V worldpoint(const Face& f,double u,double v){V p={};p[f.axis]=f.coord;p[(f.axis+1)%3]=u;p[(f.axis+2)%3]=v;return p;}
static V camera(V p,const View& v){V out={};for(int i=0;i<3;i++){double d=p[i]-v.eye[i];out[0]+=v.right[i]*d;out[1]+=v.up[i]*d;out[2]+=v.forward[i]*d;}return out;}
static Poly project_face(const Face& f,const View& v){
 if((v.eye[f.axis]-f.coord)*f.sign<=EPS)return {};
 std::vector<V> a;for(P p:Poly{{f.uv[0],f.uv[1]},{f.uv[2],f.uv[1]},{f.uv[2],f.uv[3]},{f.uv[0],f.uv[3]}})a.push_back(camera(worldpoint(f,p[0],p[1]),v));
 std::vector<V> b;V prev=a.back();for(V cur:a){if((prev[2]>=.01)!=(cur[2]>=.01)){double t=(.01-prev[2])/(cur[2]-prev[2]);b.push_back({prev[0]+t*(cur[0]-prev[0]),prev[1]+t*(cur[1]-prev[1]),.01});}if(cur[2]>=.01)b.push_back(cur);prev=cur;}
 Poly p;for(V q:b)p.push_back({q[0]*S/q[2],q[1]*S/q[2]});if(area(p)<0)std::reverse(p.begin(),p.end());
 p=halfplane(p,1,0,1);p=halfplane(p,-1,0,1);p=halfplane(p,0,1,1);p=halfplane(p,0,-1,1);return p;
}
static V coefficients(const Face& f,const View& v){double d=f.coord-v.eye[f.axis];return {v.right[f.axis]/S/d,v.up[f.axis]/S/d,v.forward[f.axis]/d};}
static V unproject(P p,const Projected& f,const View& v){double z=1/(f.inv[0]*p[0]+f.inv[1]*p[1]+f.inv[2]),x=p[0]*z/S,y=p[1]*z/S;V out=v.eye;for(int i=0;i<3;i++)out[i]+=v.right[i]*x+v.up[i]*y+v.forward[i]*z;return out;}
static double sqdist(V a,V b){double d=0;for(int k=0;k<3;++k)d+=(a[k]-b[k])*(a[k]-b[k]);return d;}
static double segdist(V p,V a,V b){double num=0,den=0;for(int k=0;k<3;++k){num+=(p[k]-a[k])*(b[k]-a[k]);den+=(b[k]-a[k])*(b[k]-a[k]);}double t=den>0?std::clamp(num/den,0.,1.):0.;V q;for(int k=0;k<3;++k)q[k]=a[k]+t*(b[k]-a[k]);return sqdist(p,q);}
static bool in_range(const Paths64& paths,const Projected& pf,const Scene& s,const View& v){
 double total=0;for(auto& p:paths)total+=Area(p)/(SCALE*SCALE);if(total<=EPS)return false;
 for(auto& p:paths)for(size_t i=0;i<p.size();++i){auto a=p[i],b=p[(i+1)%p.size()];if(segdist(v.eye,unproject({a.x/SCALE,a.y/SCALE},pf,v),unproject({b.x/SCALE,b.y/SCALE},pf,v))<256.-1e-10)return true;}
 // If the perpendicular foot is inside the filled region, it can be closer than
 // every boundary segment. Hole orientation is preserved by Clipper NonZero.
 const Face& f=s.faces[pf.face];V foot=v.eye;foot[f.axis]=f.coord;V c=camera(foot,v);if(c[2]<=.01)return false;
 Point64 p(int64_t(std::llround(c[0]*S/c[2]*SCALE)),int64_t(std::llround(c[1]*S/c[2]*SCALE)));int winding=0;
 for(auto& poly:paths){auto loc=PointInPolygon(p,poly);if(loc==PointInPolygonResult::IsOn)return sqdist(foot,v.eye)<256.-1e-10;if(loc==PointInPolygonResult::IsInside)winding+=Area(poly)>0?1:-1;}
 return winding!=0&&sqdist(foot,v.eye)<256.-1e-10;
}
extern "C" {
__declspec(dllexport) void* center_create(const double* boxes,const int* owners,int nb,const double* centers,const unsigned char* opaque,const int* query,int no,int trim,double* stats){
 auto start=Clock::now();Scene* s=new Scene;s->boxes.resize(nb);s->owner.assign(owners,owners+nb);s->centers.resize(no);s->opaque.assign(opaque,opaque+no);s->query.assign(query,query+no);
 for(int i=0;i<nb;++i)std::copy(boxes+6*i,boxes+6*i+6,s->boxes[i].begin());for(int i=0;i<no;++i)std::copy(centers+3*i,centers+3*i+3,s->centers[i].begin());
 std::map<std::tuple<int,int,int>,std::vector<int>> grid;
 auto cells=[&](const Box& b,auto fn){for(int x=int(floor(b[0]));x<=int(floor(b[3]));++x)for(int y=int(floor(b[1]));y<=int(floor(b[4]));++y)for(int z=int(floor(b[2]));z<=int(floor(b[5]));++z)fn(std::make_tuple(x,y,z));};
 if(trim)for(int i=0;i<nb;++i)cells(s->boxes[i],[&](auto key){grid[key].push_back(i);});
 for(int i=0;i<nb;++i){auto box=s->boxes[i];int owner=s->owner[i];std::set<int> neighbors;if(trim)cells(box,[&](auto key){for(int j:grid[key])if(i!=j&&(s->opaque[s->owner[j]]||s->owner[j]==owner))neighbors.insert(j);});
  for(int axis=0;axis<3;++axis)for(int sign:{-1,1}){int u=(axis+1)%3,v=(axis+2)%3;double coord=box[axis+(sign>0?3:0)];Rect rect={box[u],box[v],box[u+3],box[v+3]};s->prep[0]++;s->prep[2]+=(rect[2]-rect[0])*(rect[3]-rect[1]);std::vector<Rect> pieces={rect};
   for(int j:neighbors){auto b=s->boxes[j];bool beyond=sign>0?(b[axis]<=coord&&b[axis+3]>coord):(b[axis]<coord&&b[axis+3]>=coord);if(!beyond)continue;
    Rect cutter={b[u],b[v],b[u+3],b[v+3]};std::vector<Rect> next;for(auto p:pieces){auto r=subtract_rect(p,cutter);next.insert(next.end(),r.begin(),r.end());}pieces=std::move(next);if(pieces.empty())break;
   }
   for(Rect p:pieces){s->faces.push_back({owner,axis,sign,coord,p});s->prep[3]+=(p[2]-p[0])*(p[3]-p[1]);}
  }
 }
 s->prep[1]=double(s->faces.size());s->prep[4]=ms(start,Clock::now());std::copy(s->prep,s->prep+5,stats);return s;
}
__declspec(dllexport) void center_destroy(void* ptr){delete static_cast<Scene*>(ptr);}
// Diagnostic-only merged outer boundary, retaining holes; not assigned a single
// depth when it spans multiple blocks. Per-block labels remain in center_frame.
__declspec(dllexport) void center_contours(void* ptr,const double* cam,double* stats){
 const Scene& s=*static_cast<Scene*>(ptr);View v;orient(v,cam,0);Paths64 paths;
 for(auto& f:s.faces){Poly p=project_face(f,v);if(area(p)>EPS)paths.push_back(quantize(p));}
 paths=boolean_op(paths,{},ClipType::Union);std::fill(stats,stats+4,0.);stats[0]=double(paths.size());for(auto& p:paths){stats[1]+=p.size();stats[2]+=(Area(p)<0);stats[3]+=Area(p)/(SCALE*SCALE);}
}
// mode 0: camera-space block center depth; 1: Euclidean center distance;
// mode 2: position-dependent real surface depth (continuous geometry reference).
__declspec(dllexport) int center_frame_pose(void* ptr,const double* cam,int mode,unsigned char* output,double* areas,double* times,int64_t* stats){
 const Scene& s=*static_cast<Scene*>(ptr);int no=int(s.centers.size());auto t0=Clock::now();std::fill(output,output+no,0);std::fill(areas,areas+no,0.);std::fill(stats,stats+8,0);
 View v;orient(v,cam,cam[4]);
 if(mode<3){v.silhouettes.resize(no);v.rects.resize(no,{1e30,1e30,-1e30,-1e30});v.z.resize(no);v.dist.resize(no);
 for(int i=0;i<no;++i){v.z[i]=camera(s.centers[i],v)[2];v.dist[i]=sqdist(s.centers[i],v.eye);}}
 for(int i=0;i<int(s.faces.size());++i){auto f=s.faces[i];
  if(mode==3){V near=v.eye;near[f.axis]=f.coord;near[(f.axis+1)%3]=std::clamp(near[(f.axis+1)%3],f.uv[0],f.uv[2]);near[(f.axis+2)%3]=std::clamp(near[(f.axis+2)%3],f.uv[1],f.uv[3]);if(sqdist(near,v.eye)>256.)continue;}
  Poly p=project_face(f,v);if(area(p)<=EPS)continue;Projected pf={f.owner,i,p,quantize(p),bounds(p),coefficients(f,v)};v.faces.push_back(pf);
  if(mode<3){v.silhouettes[f.owner].push_back(pf.path);auto& r=v.rects[f.owner];r[0]=std::min(r[0],pf.rect[0]);r[1]=std::min(r[1],pf.rect[1]);r[2]=std::max(r[2],pf.rect[2]);r[3]=std::max(r[3],pf.rect[3]);}}
 stats[0]=v.faces.size();
 for(auto& paths:v.silhouettes)if(!paths.empty()){paths=boolean_op(paths,{},ClipType::Union);stats[1]+=paths.size();for(auto& p:paths){stats[2]+=p.size();stats[3]+=(Area(p)<0);}}
 auto t1=Clock::now();
 for(const auto& q:v.faces){int owner=q.owner;if(s.query[owner]<0||(mode==3&&output[owner]))continue;Paths64 cutters;
  if(mode<2){double qdepth=mode==0?v.z[owner]:v.dist[owner];for(int b=0;b<no;++b){if(b==owner||!s.opaque[b]||v.silhouettes[b].empty())continue;double bd=mode==0?v.z[b]:v.dist[b];if(!(bd<qdepth||(bd==qdepth&&b<owner)))continue;++stats[5];if(!overlaps(q.rect,v.rects[b]))continue;cutters.insert(cutters.end(),v.silhouettes[b].begin(),v.silhouettes[b].end());}}
  else{for(const auto& b:v.faces){if(b.owner==owner||!s.opaque[b.owner])continue;++stats[5];if(!overlaps(q.rect,b.rect))continue;V d={b.inv[0]-q.inv[0],b.inv[1]-q.inv[1],b.inv[2]-q.inv[2]};if(std::abs(d[0])+std::abs(d[1])+std::abs(d[2])<1e-12)continue;Poly closer=halfplane(b.p,d[0],d[1],d[2]);if(area(closer)>EPS)cutters.push_back(quantize(closer));}}
  Paths64 result;if(cutters.empty())result={q.path};else{++stats[4];result=boolean_op({q.path},cutters,ClipType::Difference);}
  double a=0;for(auto& p:result){a+=Area(p)/(SCALE*SCALE);++stats[6];stats[7]+=p.size();}areas[owner]+=a;
  if(a>EPS&&in_range(result,q,s,v))output[owner]=1;
 }
 auto t2=Clock::now();times[0]=ms(t0,t1);times[1]=ms(t1,t2);int count=0;for(int i=0;i<no;++i)count+=output[i];return count;
}
__declspec(dllexport) int center_frame(void* ptr,const double* cam,int mode,unsigned char* output,double* areas,double* times,int64_t* stats){
 double pose[5]={cam[0],cam[1],cam[2],cam[3],0};return center_frame_pose(ptr,pose,mode,output,areas,times,stats);
}
}

