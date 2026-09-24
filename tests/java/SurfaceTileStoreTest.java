import java.util.*;
import mc2p.surface.DirtyTracker;
public class SurfaceTileStoreTest {
 static TileGeometryStore.Record cube(int x,int y,int z,boolean stable){return new TileGeometryStore.Record(x,y,z,"stone","stone",true,stable,new double[]{x,y,z,x+1,y+1,z+1});}
 public static void main(String[] args){
  Map<TileGeometryStore.Pos,TileGeometryStore.Record> world=new HashMap<>();
  world.put(new TileGeometryStore.Pos(0,0,0),cube(0,0,0,true));
  TileGeometryStore.Source source=(x,y,z,old)->world.get(new TileGeometryStore.Pos(x,y,z));
  TileGeometryStore s=new TileGeometryStore(4,source,-4,8);
  s.refresh(0.5,2,0.5);long initial=s.reads;assert initial>0;assert s.records().size()==1;
  s.refresh(0.51,2,0.51);assert s.reads==0:"tiny move must not rescan complete tiles";
  s.refresh(4.5,2,0.5);s.refresh(0.5,2,0.5);assert s.reads==0:"returning must reuse retained tiles";
  world.put(new TileGeometryStore.Pos(4,0,0),cube(4,0,0,true));s.blockChanged(4,0,0);s.refresh(.5,2,.5);assert s.records().size()==2;
  world.remove(new TileGeometryStore.Pos(0,0,0));s.blockChanged(0,0,0);s.refresh(.5,2,.5);assert s.records().size()==1;
  world.put(new TileGeometryStore.Pos(3,0,0),cube(3,0,0,false));s.blockChanged(3,0,0);s.refresh(.5,2,.5);
  world.put(new TileGeometryStore.Pos(3,0,0),new TileGeometryStore.Record(3,0,0,"dynamic","dynamic",true,false,new double[]{3,0,0,4,.5,1}));
  s.refresh(.5,2,.5);assert s.changed;assert s.records().stream().anyMatch(r->r.x==3&&r.boxes[4]==.5):"dynamic shapes must refresh without state event";
  world.put(new TileGeometryStore.Pos(3,0,0),new TileGeometryStore.Record(3,0,0,"dynamic","dynamic",true,false,new double[]{}));
  s.refresh(.5,2,.5);assert s.changed;assert s.records().stream().anyMatch(r->r.x==3&&r.boxes.length==0);
  world.put(new TileGeometryStore.Pos(3,0,0),cube(3,0,0,false));s.refresh(.5,2,.5);assert s.changed:"empty dynamic shapes must remain tracked";
  world.clear();s.chunkChanged(0,0);s.refresh(.5,2,.5);assert s.records().isEmpty():"chunk invalidation must clear old contents";
  for(double ex:new double[]{-32.01,-16.0,-.01,.01,15.99,32.01}){
   s.refresh(ex,2,-ex);for(int x=(int)Math.floor(ex)-17;x<=ex+17;x++)for(int y=-4;y<8;y++)for(int z=(int)Math.floor(-ex)-17;z<=-ex+17;z++){
    double dx=Math.max(Math.max(x-ex,ex-x-1),0),dy=Math.max(Math.max(y-2,2-y-1),0),dz=Math.max(Math.max(z+ex,-ex-z-1),0);
    if(dx*dx+dy*dy+dz*dz<=256)assert s.containsTile(x,y,z):"missing query cell";
   }
  }
  Object firstWorld=new Object(),secondWorld=new Object();int[] callbacks={0,0};
  DirtyTracker.Listener listener=new DirtyTracker.Listener(){public void block(int x,int y,int z){callbacks[0]++;}public void chunk(int x,int z){callbacks[1]++;}};
  DirtyTracker.bind(firstWorld,listener);DirtyTracker.block(secondWorld,0,0,0);assert callbacks[0]==0;
  DirtyTracker.block(firstWorld,0,0,0);DirtyTracker.chunk(firstWorld,0,0,true);DirtyTracker.chunk(firstWorld,0,0,false);
  assert DirtyTracker.blockEvents==1&&DirtyTracker.chunkLoads==1&&DirtyTracker.chunkUnloads==1&&DirtyTracker.chunkEvents==2;
  assert callbacks[0]==1&&callbacks[1]==2&&DirtyTracker.eventNanos>=0;
  DirtyTracker.bind(secondWorld,listener);assert DirtyTracker.blockEvents==0&&DirtyTracker.chunkEvents==0&&DirtyTracker.eventNanos==0;
  DirtyTracker.block(firstWorld,0,0,0);assert callbacks[0]==1:"old-world events must be ignored";
  DirtyTracker.clear();DirtyTracker.block(secondWorld,0,0,0);assert callbacks[0]==1:"cleared listener must be detached";
  System.out.println("complete tile cache invariants passed");
 }
}
