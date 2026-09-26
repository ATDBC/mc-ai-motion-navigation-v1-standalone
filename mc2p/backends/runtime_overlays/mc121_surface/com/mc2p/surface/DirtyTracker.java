package com.mc2p.surface;
/** Events never reach actor observations. Single client-thread owner. */
public final class DirtyTracker {
 public interface Listener {void block(int x,int y,int z);void chunk(int x,int z);}
 private static Object world;private static Listener listener;
 public static long blockEvents,chunkEvents,chunkLoads,chunkUnloads,eventNanos;
 public static void bind(Object w,Listener l){world=w;listener=l;blockEvents=chunkEvents=chunkLoads=chunkUnloads=eventNanos=0;}
 public static void clear(){listener=null;world=null;}
 public static void block(Object w,int x,int y,int z){if(w==world&&listener!=null){long start=System.nanoTime();try{blockEvents++;listener.block(x,y,z);}finally{eventNanos+=System.nanoTime()-start;}}}
 public static void chunk(Object w,int x,int z,boolean loaded){if(w==world&&listener!=null){long start=System.nanoTime();try{chunkEvents++;if(loaded)chunkLoads++;else chunkUnloads++;listener.chunk(x,z);}finally{eventNanos+=System.nanoTime()-start;}}}
}
