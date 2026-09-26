package com.mc2p.surface;

import java.util.*;

/** Complete world-anchored tiles; all calls are made on the client thread. */
public final class TileGeometryStore {
 public record Pos(int x,int y,int z) implements Comparable<Pos> {
  public int compareTo(Pos p){int c=Integer.compare(x,p.x);if(c==0)c=Integer.compare(y,p.y);return c==0?Integer.compare(z,p.z):c;}
 }
 public static final class Record {
  public final int x,y,z;public final String id,state;public final boolean opaque,stable;public final double[] boxes;public Object token;
  public Record(int x,int y,int z,String id,String state,boolean opaque,boolean stable,double[] boxes){this.x=x;this.y=y;this.z=z;this.id=id;this.state=state;this.opaque=opaque;this.stable=stable;this.boxes=boxes;}
  public boolean same(Record b){return this==b||(b!=null&&opaque==b.opaque&&stable==b.stable&&id.equals(b.id)&&state.equals(b.state)&&Arrays.equals(boxes,b.boxes));}
 }
 public interface Source {Record read(int x,int y,int z,Record previous);}
 private final int edge,bottom,top;private final Source source;
 private final Set<Pos> tiles=new HashSet<>(),dirty=new HashSet<>();
 private final TreeMap<Pos,Record> blocks=new TreeMap<>();
 private final Set<Pos> dynamic=new HashSet<>();
 public long reads;public int addedTiles,removedTiles,dirtyTiles;public boolean changed;
 public TileGeometryStore(int edge,Source source,int bottom,int top){if(edge!=4&&edge!=8)throw new IllegalArgumentException("tile edge");this.edge=edge;this.source=source;this.bottom=bottom;this.top=top;}
 private Pos tile(int x,int y,int z){return new Pos(Math.floorDiv(x,edge),Math.floorDiv(y,edge),Math.floorDiv(z,edge));}
 private double distance2(Pos t,double x,double y,double z){double dx=Math.max(Math.max(t.x*edge-x,x-(t.x+1)*edge),0),dy=Math.max(Math.max(t.y*edge-y,y-(t.y+1)*edge),0),dz=Math.max(Math.max(t.z*edge-z,z-(t.z+1)*edge),0);return dx*dx+dy*dy+dz*dz;}
 public void blockChanged(int x,int y,int z){
  for(int a=x-2;a<=x+2;a++)for(int b=y-2;b<=y+2;b++)for(int c=z-2;c<=z+2;c++){Pos t=tile(a,b,c);if(tiles.contains(t))dirty.add(t);}
 }
 public void chunkChanged(int x,int z){for(Pos t:tiles)if(Math.floorDiv(t.x*edge,16)==x&&Math.floorDiv(t.z*edge,16)==z)dirty.add(t);}
 private void read(int x,int y,int z){
  Pos p=new Pos(x,y,z);Record old=blocks.get(p),next=source.read(x,y,z,old);reads++;
  if(next==null){if(old!=null){blocks.remove(p);dynamic.remove(p);changed=true;}}
  else{if(old==null||!old.same(next)){blocks.put(p,next);changed=true;}if(next.stable)dynamic.remove(p);else dynamic.add(p);}
 }
 public void refresh(double x,double y,double z){
  if(!Double.isFinite(x)||!Double.isFinite(y)||!Double.isFinite(z))throw new IllegalArgumentException("nonfinite eye");
  reads=0;addedTiles=removedTiles=dirtyTiles=0;changed=false;
  for(Iterator<Pos> it=tiles.iterator();it.hasNext();){Pos t=it.next();if(distance2(t,x,y,z)<=21*21)continue;it.remove();dirty.remove(t);removedTiles++;
   for(int a=t.x*edge;a<(t.x+1)*edge;a++)for(int b=t.y*edge;b<(t.y+1)*edge;b++)for(int c=t.z*edge;c<(t.z+1)*edge;c++){Pos p=new Pos(a,b,c);if(blocks.remove(p)!=null){dynamic.remove(p);changed=true;}}
  }
  Pos center=tile((int)Math.floor(x),(int)Math.floor(y),(int)Math.floor(z));int r=(int)Math.ceil(17./edge)+1;
  for(int a=center.x-r;a<=center.x+r;a++)for(int b=Math.max(Math.floorDiv(bottom,edge),center.y-r);b<=Math.min(Math.floorDiv(top-1,edge),center.y+r);b++)for(int c=center.z-r;c<=center.z+r;c++){
   Pos t=new Pos(a,b,c);if(distance2(t,x,y,z)<=17*17&&tiles.add(t)){dirty.add(t);addedTiles++;}
  }
  Set<Pos> refreshed=new HashSet<>(dirty);dirtyTiles=dirty.size();
  for(Pos t:dirty)for(int a=t.x*edge;a<(t.x+1)*edge;a++)for(int b=Math.max(bottom,t.y*edge);b<Math.min(top,(t.y+1)*edge);b++)for(int c=t.z*edge;c<(t.z+1)*edge;c++)read(a,b,c);
  dirty.clear();
  for(Pos p:new ArrayList<>(dynamic))if(!refreshed.contains(tile(p.x,p.y,p.z)))read(p.x,p.y,p.z);
 }
 public Collection<Record> records(){return Collections.unmodifiableCollection(blocks.values());}
 public Set<Pos> tiles(){return Collections.unmodifiableSet(tiles);}
 public boolean containsTile(int x,int y,int z){return tiles.contains(tile(x,y,z));}
 public int edge(){return edge;}
}
