/* v-imp4 (5 Nov): import refusals never latch backboardd, and idle IOSurface imports are evicted and imported again on use.
   Production Backend on a software ICD with host import; the kernel's refusals are injected (failHostImportsForTest, failAllocationsForTest). */
#include "Backend.hpp"
#include <cassert>
#include <cstdio>
#include <cstring>
#include <vector>
#include <fcntl.h>
#include <stdlib.h>
using namespace fuxi;
// h-daily2's 'v024' frame layout (lavapipe imports it in place): luma R8 332x198 at 64, CbCr RG8 166x99 at 79936, rows 384.
struct Plane {uint32_t width,height,texel,format;size_t row,offset;};
static const Plane luma{332,198,1,10,384,64},chroma{166,99,2,30,384,79936};
static constexpr size_t frameBytes=131072;
static uint8_t pattern(const Plane &p,size_t x,size_t y,unsigned frame){return uint8_t((x*7+y*13+p.offset/64+frame*29)%251);}
static void fill(uint8_t *host,const Plane &p,unsigned frame){for(size_t y=0;y<p.height;++y)for(size_t x=0;x<p.width*p.texel;++x)host[p.offset+y*p.row+x]=pattern(p,x,y,frame);}
static void readBack(Backend &b,Texture *t,const Plane &p,unsigned frame){
 auto *out=b.allocate(size_t(p.width)*p.height*p.texel);memset(out->contents,0x5c,out->length);
 Operation read{};read.texture=t;read.dst=out;b.submit({read});
 for(size_t y=0;y<p.height;++y)for(size_t x=0;x<p.width*p.texel;++x)assert(((uint8_t *)out->contents)[y*p.width*p.texel+x]==pattern(p,x,y,frame));
 b.destroy(out);
}
template<class F> static bool softly(F body){try{body();}catch(const SoftRefusal &){return true;}return false;}
struct Frame {uint8_t *host;Texture *y,*c;};
static Frame frame(Backend &b,uint64_t id,unsigned n){
 Frame f{};assert(!posix_memalign((void **)&f.host,65536,frameBytes));memset(f.host,0xa7,frameBytes);fill(f.host,luma,n);fill(f.host,chroma,n);
 f.y=b.importTexture(f.host,frameBytes,luma.row,luma.width,luma.height,luma.offset,id,luma.format,true);
 f.c=b.importTexture(f.host,frameBytes,chroma.row,chroma.width,chroma.height,chroma.offset,id,chroma.format,true);
 return f;
}
static void drop(Backend &b,Frame &f){b.destroy(f.y);b.destroy(f.c);free(f.host);f={};}
int main(int argc,char **argv){
 // An armed gate, as in backboardd: any fault() latches it (blocked()), so every soft path below is checked against a live gate.
 char dir[]="/tmp/fuxi-importsafe-XXXXXX";assert(mkdtemp(dir));
 assert(backboardGate.admit("aaaaaaaa-1111-2222-3333-444444444444",open(dir,O_RDONLY|O_DIRECTORY)) && !backboardGate.blocked());
 Backend b;b.slabsEnabled=false;
 if(!b.supportsHostImport()){puts("SKIP: import safety requires VK_EXT_external_memory_host");return 0;}
 const auto baseline=b.resourceBytes(),importBaseline=b.importedBytes();
 // Warm-up: the first submits create per-slot objects that stay; the memory object baseline is taken after them.
 {auto f=frame(b,1,0);readBack(b,f.y,luma,0);readBack(b,f.c,chroma,0);readBack(b,f.y,luma,0);drop(b,f);b.trimMemory();}
 const auto objects=liveMemoryObjects.load();assert(b.resourceBytes()==baseline && b.importedBytes()==importBaseline);
 if(argc>1 && !strcmp(argv[1],"lost")){
  // A lost device during an import is a real fault: it still poisons and latches (the CPU fallback stays for device faults).
  void *host=nullptr;assert(!posix_memalign(&host,65536,frameBytes));memset(host,0,frameBytes);
  b.hostImportResultForTest=VK_ERROR_DEVICE_LOST;b.failHostImportsForTest=1;
  bool threw=false;try{b.importTexture(host,frameBytes,luma.row,luma.width,luma.height,luma.offset,7,luma.format,true);}catch(const SoftRefusal &){assert(!"a lost device is not a soft refusal");}catch(const std::exception &){threw=true;}
  assert(threw && b.poisoned && backboardGate.blocked());
  puts("PASS: a lost device during a host import still poisons the device and latches backboardd");return 0;
 }
 // 1. v-imp4's chain. The kernel refuses the import (BO table full: Turnip answers VK_ERROR_INVALID_EXTERNAL_HANDLE, -1000072003),
 //    the sampled plane goes to the copy path, and the copy's own buffer allocation is refused too (Backend.hpp:754 in d335af6, which
 //    latched the gate through check()). Now both are KernelRefusal: the texture fails alone, nothing latches, nothing leaks.
 {
  void *host=nullptr;assert(!posix_memalign(&host,65536,frameBytes));memset(host,0xa7,frameBytes);fill((uint8_t *)host,luma,0);
  const auto failures=b.hostImportFailures;
  b.hostImportResultForTest=VK_ERROR_INVALID_EXTERNAL_HANDLE;b.failHostImportsForTest=1;b.failAllocationsForTest=100;
  assert(softly([&]{b.importTexture(host,frameBytes,luma.row,luma.width,luma.height,luma.offset,40,luma.format,true);}));
  assert(!b.poisoned && !backboardGate.blocked() && b.hostImportFailures==failures+1 && b.allocationRefusals>0);
  assert(b.surfaceMemory.empty() && b.imports.empty() && b.importedBytes()==importBaseline);
  // A CA texture, a render target import and a no-copy buffer refused the same way fail alone too.
  b.failAllocationsForTest=100;
  assert(softly([&]{b.allocateTexture(80,5,512,512);}));
  b.failAllocationsForTest=0;b.importBackoffUntil=0;b.failHostImportsForTest=1;
  assert(softly([&]{b.importTexture(host,frameBytes,luma.row,luma.width,luma.height,luma.offset,41,luma.format,false);}));
  b.importBackoffUntil=0;b.failHostImportsForTest=1;
  assert(softly([&]{b.importBuffer(host,frameBytes);}));
  assert(!b.poisoned && !backboardGate.blocked());
  // With allocations back, the next refused sampled import (backoff) is a working copy.
  b.importBackoffUntil=0;b.failHostImportsForTest=1;
  auto *y=b.importTexture(host,frameBytes,luma.row,luma.width,luma.height,luma.offset,42,luma.format,true);
  assert(y->hostSource && !y->imported);readBack(b,y,luma,0);b.destroy(y);
  b.importBackoffUntil=0;b.hostImportResultForTest=VK_ERROR_OUT_OF_DEVICE_MEMORY;b.trimMemory();
  assert(b.resourceBytes()==baseline && b.importedBytes()==importBaseline && liveMemoryObjects==objects);free(host);
  puts("PASS: v-imp4 chain: a refused import whose copy allocation is refused fails that texture alone; CA texture, render target and no-copy buffer refusals too; no latch, no leak");
 }
 // 2. YouTube in Safari: each decoded frame a new surface, CA keeps every past frame's two plane textures. Idle imports past the keep
 //    level are evicted; the textures stay valid and import again (both planes into one shared import) the next time a submit reads them.
 {
  b.importKeep=4;b.importHigh=1000;b.importLow=900;b.boHigh=1<<30;b.boLow=1<<30;b.evictIdle=6;b.evictMinIdle=2;b.evictEvery=1;
  std::vector<Frame> frames;
  for(unsigned n=0;n<12;++n){frames.push_back(frame(b,100+n,n));readBack(b,frames.back().y,luma,n);readBack(b,frames.back().c,chroma,n);}
  const size_t live=b.surfaceMemory.size();b.trimMemory(); // pooled readback buffers are memory objects too
  assert(b.importsEvicted>=6 && live<=6 && live>=4);
  assert(liveMemoryObjects==objects+int64_t(live) && b.importedBytes()==importBaseline+live*frameBytes && b.resourceBytes()==baseline);
  auto &old=frames[0];assert(old.y->evicted && old.c->evicted && !old.y->image && !old.y->memory && !old.y->surfaceShared);
  // CA wraps the same plane again: the cache hands back the evicted texture, which imports again when drawn.
  auto *again=b.importTexture(old.host,frameBytes,luma.row,luma.width,luma.height,luma.offset,100,luma.format,true);assert(again==old.y && old.y->references==2);b.destroy(again);
  const auto restored=b.importsRestored;
  fill(old.host,luma,20);fill(old.host,chroma,20);
  readBack(b,old.y,luma,20);assert(!old.y->evicted && old.y->surfaceShared && b.importsRestored==restored+1 && old.c->evicted);
  readBack(b,old.c,chroma,20);assert(!old.c->evicted && old.c->memory==old.y->memory && b.importsRestored==restored+2);
  {auto it=b.surfaceMemory.find(old.y->surfaceKey);assert(it!=b.surfaceMemory.end() && it->second.references==2 && it->second.images.size()==2);}
  // A newer frame never evicted reads as before.
  readBack(b,frames.back().y,luma,11);
  for(auto &f:frames)drop(b,f);
  assert(b.surfaceMemory.empty() && b.imports.empty());b.trimMemory();
  assert(b.resourceBytes()==baseline && b.importedBytes()==importBaseline && liveMemoryObjects==objects && !b.poisoned && !backboardGate.blocked());
  printf("PASS: idle imports evicted past the keep level (12 frames, %zu live, %llu evicted), textures kept, imported again on use into one shared import\n",live,(unsigned long long)b.importsEvicted);
 }
 // 3. A refused import first evicts idle imports and is tried again: it imports in place, with no copy and no backoff.
 {
  b.importKeep=1000;b.evictIdle=1u<<30;b.evictMinIdle=2;
  std::vector<Frame> frames;for(unsigned n=0;n<3;++n){frames.push_back(frame(b,200+n,n));readBack(b,frames.back().y,luma,n);}
  for(unsigned n=0;n<3;++n)readBack(b,frames.back().c,chroma,2); // the first two frames go idle
  const auto failures=b.hostImportFailures,evicted=b.importsEvicted,retries=b.importRetries;
  b.failHostImportsForTest=1;
  auto fresh=frame(b,300,5);
  assert(!b.failHostImportsForTest && fresh.y->imported && !fresh.y->hostSource && fresh.y->memory==fresh.c->memory);
  assert(b.hostImportFailures==failures && !b.importBackoff() && b.importRetries==retries+1 && b.importsEvicted>evicted);
  readBack(b,fresh.y,luma,5);readBack(b,frames[0].y,luma,0);
  // An own allocation refused once: idle imports go, the retry succeeds.
  for(unsigned n=0;n<3;++n)readBack(b,fresh.c,chroma,5);
  const auto allocRetries=b.allocationRetries;b.failAllocationsForTest=1;
  auto *big=b.allocate(3u<<20);assert(big && !b.failAllocationsForTest && b.allocationRetries==allocRetries+1);b.destroy(big);
  drop(b,fresh);for(auto &f:frames)drop(b,f);b.trimMemory();
  assert(b.surfaceMemory.empty() && b.resourceBytes()==baseline && b.importedBytes()==importBaseline && liveMemoryObjects==objects && !b.poisoned && !backboardGate.blocked());
  puts("PASS: a refused import or allocation evicts idle imports and is tried again; the import stays in place, no backoff");
 }
 b.close();puts("PASS: import safety");
}
