package com.mc2p.surface;

import java.nio.*;
public final class CacheBridge {
 static ByteBuffer direct(int n){return ByteBuffer.allocateDirect(n).order(ByteOrder.nativeOrder());}
 static native long create(int tile);
 static native void destroy(long scene);
 static native void update(long scene,ByteBuffer boxes,ByteBuffer owners,int nb,ByteBuffer centers,ByteBuffer opaque,int no,ByteBuffer identities,ByteBuffer stats);
 static native int frame(long scene,ByteBuffer camera,ByteBuffer query,int n,ByteBuffer out,ByteBuffer areas,ByteBuffer times,ByteBuffer stats);
 static native int framePose(long scene,ByteBuffer camera,ByteBuffer query,int n,ByteBuffer out,ByteBuffer areas,ByteBuffer times,ByteBuffer stats);
 public static void main(String[] args){
  System.load(args[0]);long s=create(4);
  try{ByteBuffer b=direct(48),o=direct(4),c=direct(24),op=direct(1),ids=direct(16),st=direct(64);
   for(double v:new double[]{0,0,0,1,1,1})b.putDouble(v);for(double v:new double[]{.5,.5,.5})c.putDouble(v);op.put((byte)1);
   update(s,b,o,1,c,op,1,ids,st);long slot=ids.getLong(0);update(s,b,o,1,c,op,1,ids,st);
   if(st.getDouble(24)!=0||ids.getLong(0)!=slot)throw new AssertionError("unchanged geometry rebuilt");
   try{update(s,ByteBuffer.allocate(48),o,1,c,op,1,ids,st);throw new AssertionError("heap buffer accepted");}catch(IllegalStateException expected){}
   System.out.println("JNI cache smoke passed");
  }finally{destroy(s);}
 }
}
