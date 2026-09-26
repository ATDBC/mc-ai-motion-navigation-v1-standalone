package com.mc2p.surface;

import java.util.*;
import net.minecraft.client.MinecraftClient;
import net.minecraft.block.ShapeContext;
import net.minecraft.registry.Registries;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Box;

/** Fresh Minecraft reads. Only the verified full-cube IDs use event-only refresh. */
public final class SurfaceWorldSource implements TileGeometryStore.Source {
 private static final Set<String> STATIC=Set.of("minecraft:stone","minecraft:dirt","minecraft:grass_block","minecraft:bedrock","minecraft:gold_block","minecraft:redstone_block");
 private static final Set<String> TRANSPARENT=Set.of("minecraft:cobweb","minecraft:short_grass","minecraft:tall_grass","minecraft:fern","minecraft:large_fern","minecraft:dead_bush","minecraft:dandelion","minecraft:poppy","minecraft:vine","minecraft:glass","minecraft:glass_pane","minecraft:ice","minecraft:packed_ice","minecraft:blue_ice","minecraft:slime_block","minecraft:honey_block");
 private final MinecraftClient client;private final BlockPos.Mutable cursor=new BlockPos.Mutable();private ShapeContext context;
 public SurfaceWorldSource(MinecraftClient client){this.client=client;}
 public void begin(){context=ShapeContext.of(client.player);}
 public TileGeometryStore.Record read(int x,int y,int z,TileGeometryStore.Record old){
  cursor.set(x,y,z);var state=client.world.getBlockState(cursor);if(state.isAir())return null;
  if(old!=null&&old.stable&&old.token==state)return old;
  // Keep non-air empty shapes: player context can make them nonempty later.
  var fluid=state.getFluidState();
  var parts=state.getOutlineShape(client.world,cursor,context).getBoundingBoxes();
  String id=old!=null&&old.token==state?old.id:Registries.BLOCK.getId(state.getBlock()).toString();
  boolean same=old!=null&&old.token==state&&old.boxes.length==parts.size()*6;int j=0;
  if(same)for(Box b:parts){if(old.boxes[j++]!=x+b.minX||old.boxes[j++]!=y+b.minY||old.boxes[j++]!=z+b.minZ||old.boxes[j++]!=x+b.maxX||old.boxes[j++]!=y+b.maxY||old.boxes[j++]!=z+b.maxZ){same=false;break;}}
  if(same)return old;
  double[] boxes;
  if(parts.isEmpty()&&!fluid.isEmpty())boxes=new double[]{x,y,z,x+1,y+Math.max(fluid.getHeight(client.world,cursor),.001),z+1};
  else{boxes=new double[parts.size()*6];j=0;for(Box b:parts){boxes[j++]=x+b.minX;boxes[j++]=y+b.minY;boxes[j++]=z+b.minZ;boxes[j++]=x+b.maxX;boxes[j++]=y+b.maxY;boxes[j++]=z+b.maxZ;}}
  // Exact full shape check prevents an unexpected shape from taking the static path.
  boolean full=parts.size()==1&&boxes[0]==x&&boxes[1]==y&&boxes[2]==z&&boxes[3]==x+1&&boxes[4]==y+1&&boxes[5]==z+1;
  boolean transparent=TRANSPARENT.contains(id)||id.endsWith("_stained_glass")||id.endsWith("_stained_glass_pane")||!fluid.isEmpty();
  var record=new TileGeometryStore.Record(x,y,z,id,state.toString(),!transparent,full&&STATIC.contains(id),boxes);record.token=state;return record;
 }
 public long audit(TileGeometryStore store){
  long reads=0;Map<TileGeometryStore.Pos,TileGeometryStore.Record> cached=new HashMap<>();for(var b:store.records())cached.put(new TileGeometryStore.Pos(b.x,b.y,b.z),b);
  int edge=store.edge();for(var t:store.tiles())for(int x=t.x()*edge;x<(t.x()+1)*edge;x++)for(int y=Math.max(client.world.getBottomY(),t.y()*edge);y<Math.min(client.world.getTopY(),(t.y()+1)*edge);y++)for(int z=t.z()*edge;z<(t.z()+1)*edge;z++){
   var actual=read(x,y,z,null);var old=cached.remove(new TileGeometryStore.Pos(x,y,z));reads++;
   if((actual==null)!=(old==null)||(actual!=null&&!actual.same(old)))throw new IllegalStateException("geometry audit mismatch at "+x+","+y+","+z);
  }
  if(!cached.isEmpty())throw new IllegalStateException("orphan cache records");return reads;
 }
}
