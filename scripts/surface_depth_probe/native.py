"""Python test/replay wrapper. The DLL is built explicitly, never downloaded."""
import ctypes as C
from pathlib import Path
import time
import numpy as np
ROOT=Path(__file__).resolve().parents[2]

def _load_native_library():
 library=C.CDLL(str(ROOT/'artifacts/surface-cache/native-build/Release/surface_cache.dll'))
 library.cache_create.argtypes=[C.c_int];library.cache_create.restype=C.c_void_p
 library.cache_destroy.argtypes=[C.c_void_p]
 library.cache_error.restype=C.c_char_p
 library.cache_update.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p,C.c_int,C.c_void_p,C.c_void_p,C.c_int,C.c_void_p,C.c_void_p]
 library.cache_frame.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p,C.c_int,C.c_int]+[C.c_void_p]*4
 library.cache_frame_pose.argtypes=library.cache_frame.argtypes
 library.cache_frame_pose_air.argtypes=library.cache_frame.argtypes+[C.c_void_p,C.c_int,C.c_double,C.c_void_p]
 library.cache_visible_boxes.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p,C.c_int,C.c_double,C.c_void_p]
 library.center_create.argtypes=[C.c_void_p,C.c_void_p,C.c_int,C.c_void_p,C.c_void_p,C.c_void_p,C.c_int,C.c_int,C.c_void_p];library.center_create.restype=C.c_void_p
 library.center_frame.argtypes=[C.c_void_p,C.c_void_p,C.c_int]+[C.c_void_p]*4
 library.center_destroy.argtypes=[C.c_void_p]
 return library

class _LazyNativeLibrary:
 def __init__(self):self._library=None
 def __getattr__(self,name):
  if self._library is None:self._library=_load_native_library()
  return getattr(self._library,name)

lib=_LazyNativeLibrary()
def arrays(d):
 b=np.array(d['boxes'],np.float64).reshape(-1,6);c=np.array(d['centers'],np.float64).reshape(-1,3)
 owners=np.asarray(d['owners']);opaque=np.asarray(d['opaque'])
 if owners.shape!=(len(b),) or opaque.shape!=(len(c),):raise ValueError('owner/opacity length mismatch')
 if len(b)>100000 or len(c)>25000:raise ValueError('native capacity exceeded')
 if not np.isfinite(b).all() or not np.isfinite(c).all() or not np.isfinite(owners).all():raise ValueError('nonfinite geometry')
 if np.any(b[:,:3]>=b[:,3:]) or np.any(owners!=np.floor(owners)) or np.any(owners<0) or np.any(owners>=len(c)):raise ValueError('invalid box or owner')
 if not np.isin(opaque,[0,1]).all():raise ValueError('invalid opacity')
 return b,np.array(owners,np.int32),c,np.array(opaque,np.uint8)
def camera_values(cam):
 c=np.asarray(cam,dtype=np.float64)
 if c.shape!=(4,) or not np.isfinite(c).all():raise ValueError('camera must contain four finite values')
 return np.ascontiguousarray(c)
def query_values(query,n):
 q=np.ones(n,np.uint8) if query is None else np.asarray(query)
 if q.shape!=(n,) or not np.isin(q,[0,1]).all():raise ValueError('invalid query mask')
 return np.array(q,np.uint8)
class Cache:
 def __init__(self,tile=4):
  self.ptr=lib.cache_create(tile)
  if not self.ptr:raise ValueError(lib.cache_error().decode())
 def __enter__(self):return self
 def __exit__(self,*args):self.close()
 def close(self):
  if self.ptr:lib.cache_destroy(self.ptr);self.ptr=None
 def update(self,data):
  b,o,c,op=arrays(data);self.n=len(c);self.identities=np.zeros((self.n,2),np.int64);self.stats=np.zeros(8);start=time.perf_counter_ns()
  r=lib.cache_update(self.ptr,b.ctypes.data,o.ctypes.data,len(b),c.ctypes.data,op.ctypes.data,self.n,self.identities.ctypes.data,self.stats.ctypes.data)
  self.update_ms=(time.perf_counter_ns()-start)/1e6
  if r<0:raise ValueError(lib.cache_error().decode())
  return self.stats
 def frame(self,cam,fast=True,query=None):
  c=camera_values(cam);q=query_values(query,self.n)
  out=np.zeros(self.n,np.uint8);area=np.zeros(self.n);times=np.zeros(2);stats=np.zeros(8,np.int64)
  r=lib.cache_frame(self.ptr,c.ctypes.data,q.ctypes.data,self.n,int(fast),out.ctypes.data,area.ctypes.data,times.ctypes.data,stats.ctypes.data)
  if r<0:raise ValueError(lib.cache_error().decode())
  return out,area,times,stats
 def frame_pose(self,cam,fast=True,query=None):
  c=np.ascontiguousarray(cam,dtype=np.float64)
  if c.shape!=(5,) or not np.isfinite(c).all() or abs(c[4])>90:raise ValueError('invalid camera pose')
  q=query_values(query,self.n);out=np.zeros(self.n,np.uint8);area=np.zeros(self.n);times=np.zeros(2);stats=np.zeros(8,np.int64)
  r=lib.cache_frame_pose(self.ptr,c.ctypes.data,q.ctypes.data,self.n,int(fast),out.ctypes.data,area.ctypes.data,times.ctypes.data,stats.ctypes.data)
  if r<0:raise ValueError(lib.cache_error().decode())
  return out,area,times,stats
 def frame_pose_air(self,cam,positions,fast=True,query=None,max_distance=16.):
  c=np.ascontiguousarray(cam,dtype=np.float64)
  if c.shape!=(5,) or not np.isfinite(c).all() or abs(c[4])>90:raise ValueError('invalid camera pose')
  p=np.asarray(positions,dtype=np.int32)
  if p.size==0:p=np.empty((0,3),dtype=np.int32)
  if p.shape[1:]!=(3,) or len(p)>128:raise ValueError('invalid visual-air positions')
  q=query_values(query,self.n);out=np.zeros(self.n,np.uint8);area=np.zeros(self.n);times=np.zeros(3);stats=np.zeros(8,np.int64);air=np.zeros(len(p),np.uint8)
  r=lib.cache_frame_pose_air(self.ptr,c.ctypes.data,q.ctypes.data,self.n,int(fast),out.ctypes.data,area.ctypes.data,times.ctypes.data,stats.ctypes.data,p.ctypes.data,len(p),float(max_distance),air.ctypes.data)
  if r<0:raise ValueError(lib.cache_error().decode())
  return out,air,times,stats
 def visible_boxes(self,cam,boxes,max_distance):
  c=np.ascontiguousarray(cam,dtype=np.float64);b=np.asarray(boxes,dtype=np.float64)
  if c.shape!=(5,) or not np.isfinite(c).all() or abs(c[4])>90:raise ValueError('invalid camera pose')
  if b.size==0:b=np.empty((0,6),dtype=np.float64)
  if b.shape[1:]!=(6,) or len(b)>256 or not np.isfinite(b).all() or np.any(b[:,:3]>=b[:,3:]):raise ValueError('invalid visible boxes')
  if not np.isfinite(max_distance) or max_distance<=0:raise ValueError('invalid visible-box range')
  b=np.ascontiguousarray(b);out=np.zeros(len(b),np.uint8)
  r=lib.cache_visible_boxes(self.ptr,c.ctypes.data,b.ctypes.data,len(b),float(max_distance),out.ctypes.data)
  if r<0:raise ValueError(lib.cache_error().decode())
  return out
def reference(data,cam,mode=2):
 b,o,c,op=arrays(data);rawq=np.asarray(data.get('query',list(range(len(c)))))
 if rawq.shape!=(len(c),) or not np.isfinite(rawq).all() or np.any(rawq!=np.floor(rawq)) or np.any(rawq< -1) or np.any(rawq>25000):raise ValueError('invalid reference query')
 camera=camera_values(cam);q=np.array(rawq,dtype=np.int32);prep=np.zeros(5)
 ptr=lib.center_create(b.ctypes.data,o.ctypes.data,len(b),c.ctypes.data,op.ctypes.data,q.ctypes.data,len(c),1,prep.ctypes.data)
 out=np.zeros(len(c),np.uint8);area=np.zeros(len(c));times=np.zeros(2);stats=np.zeros(8,np.int64)
 try:lib.center_frame(ptr,camera.ctypes.data,mode,out.ctypes.data,area.ctypes.data,times.ctypes.data,stats.ctypes.data)
 finally:lib.center_destroy(ptr)
 return out,area,times,stats
