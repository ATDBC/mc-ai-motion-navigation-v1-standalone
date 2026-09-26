#include <jni.h>
#include <cstdint>
#include <stdexcept>
extern "C" {
void* cache_create(int);void cache_destroy(void*);const char* cache_error();
int cache_update(void*,const double*,const int*,int,const double*,const unsigned char*,int,int64_t*,double*);
int cache_frame(void*,const double*,const unsigned char*,int,int,unsigned char*,double*,double*,int64_t*);
int cache_frame_pose(void*,const double*,const unsigned char*,int,int,unsigned char*,double*,double*,int64_t*);
}
static void* addr(JNIEnv* e,jobject b,jlong n){if(!b||n<0||e->GetDirectBufferCapacity(b)<n)throw std::invalid_argument("direct buffer capacity");auto p=e->GetDirectBufferAddress(b);if(!p)throw std::invalid_argument("direct buffer required");return p;}
static void fail(JNIEnv* e,const char* s){e->ThrowNew(e->FindClass("java/lang/IllegalStateException"),s);}
extern "C" {
JNIEXPORT jlong JNICALL Java_CacheBridge_create(JNIEnv* e,jclass,jint edge){auto p=cache_create(edge);if(!p)fail(e,cache_error());return reinterpret_cast<jlong>(p);}
JNIEXPORT void JNICALL Java_CacheBridge_destroy(JNIEnv*,jclass,jlong p){cache_destroy(reinterpret_cast<void*>(p));}
JNIEXPORT void JNICALL Java_CacheBridge_update(JNIEnv* e,jclass,jlong ptr,jobject boxes,jobject owners,jint nb,jobject centers,jobject opaque,jint no,jobject ids,jobject stats){
 try{if(nb<0||nb>100000||no<0||no>25000)throw std::invalid_argument("capacity exceeded");
  if(cache_update(reinterpret_cast<void*>(ptr),(double*)addr(e,boxes,nb*48LL),(int*)addr(e,owners,nb*4LL),nb,(double*)addr(e,centers,no*24LL),(unsigned char*)addr(e,opaque,no),no,(int64_t*)addr(e,ids,no*16LL),(double*)addr(e,stats,64))<0)fail(e,cache_error());
 }catch(const std::exception& x){fail(e,x.what());}}
JNIEXPORT jint JNICALL Java_CacheBridge_frame(JNIEnv* e,jclass,jlong ptr,jobject camera,jobject query,jint no,jobject out,jobject areas,jobject times,jobject stats){
 try{if(no<0||no>25000)throw std::invalid_argument("capacity exceeded");int n=cache_frame(reinterpret_cast<void*>(ptr),(double*)addr(e,camera,32),(unsigned char*)addr(e,query,no),no,1,(unsigned char*)addr(e,out,no),(double*)addr(e,areas,no*8LL),(double*)addr(e,times,16),(int64_t*)addr(e,stats,64));if(n<0)fail(e,cache_error());return n;
 }catch(const std::exception& x){fail(e,x.what());return -1;}}
JNIEXPORT jint JNICALL Java_CacheBridge_framePose(JNIEnv* e,jclass,jlong ptr,jobject camera,jobject query,jint no,jobject out,jobject areas,jobject times,jobject stats){
 try{if(no<0||no>25000)throw std::invalid_argument("capacity exceeded");int n=cache_frame_pose(reinterpret_cast<void*>(ptr),(double*)addr(e,camera,40),(unsigned char*)addr(e,query,no),no,1,(unsigned char*)addr(e,out,no),(double*)addr(e,areas,no*8LL),(double*)addr(e,times,16),(int64_t*)addr(e,stats,64));if(n<0)fail(e,cache_error());return n;
 }catch(const std::exception& x){fail(e,x.what());return -1;}}
JNIEXPORT jlong JNICALL Java_com_mc2p_surface_CacheBridge_create(JNIEnv* e,jclass c,jint edge){return Java_CacheBridge_create(e,c,edge);}
JNIEXPORT void JNICALL Java_com_mc2p_surface_CacheBridge_destroy(JNIEnv* e,jclass c,jlong ptr){Java_CacheBridge_destroy(e,c,ptr);}
JNIEXPORT void JNICALL Java_com_mc2p_surface_CacheBridge_update(JNIEnv* e,jclass c,jlong ptr,jobject boxes,jobject owners,jint nb,jobject centers,jobject opaque,jint no,jobject ids,jobject stats){Java_CacheBridge_update(e,c,ptr,boxes,owners,nb,centers,opaque,no,ids,stats);}
JNIEXPORT jint JNICALL Java_com_mc2p_surface_CacheBridge_frame(JNIEnv* e,jclass c,jlong ptr,jobject camera,jobject query,jint no,jobject out,jobject areas,jobject times,jobject stats){return Java_CacheBridge_frame(e,c,ptr,camera,query,no,out,areas,times,stats);}
JNIEXPORT jint JNICALL Java_com_mc2p_surface_CacheBridge_framePose(JNIEnv* e,jclass c,jlong ptr,jobject camera,jobject query,jint no,jobject out,jobject areas,jobject times,jobject stats){return Java_CacheBridge_framePose(e,c,ptr,camera,query,no,out,areas,times,stats);}
}
