#include <core/Allocator.h>
#include <core/Stream.h>
#include <runtime/CachingDeviceAllocator.h>
#include <runtime/CachingHostAllocator.h>
#include <runtime/Exception.h>
#include <tensor/Context.h>

namespace xpu {
namespace dpcpp {

/// Device Allocator
class DeviceAllocator final : public at::Allocator {
 public:
  DeviceAllocator() {}

  // Singleton
  static DeviceAllocator* Instance();

  static void deleter(void* ptr) {
    auto* ctx = static_cast<at::AtenIpexTypeXPU::DPCPPTensorContext*>(ptr);
    auto data = ctx->data();
    Instance()->alloc()->free(data);
    delete ctx;
  }

  DataPtr allocate(size_t size) const override {
    DeviceIndex curDevID;
    AT_DPCPP_CHECK(dpcppGetDevice(&curDevID));
    void* r = nullptr;
    if (size != 0) {
      auto stream = getCurrentDPCPPStream(curDevID);
      Instance()->alloc()->malloc(&r, size, &stream.queue());
    }
    auto ctx = new at::AtenIpexTypeXPU::DPCPPTensorContext(r);
    return {r, ctx, &deleter, Device(DeviceType::XPU, curDevID)};
  }

  at::DeleterFnPtr raw_deleter() const override {
    return &deleter;
  }

  void emptyCache() {
    alloc()->emptyCache();
  }

  void cacheInfo(
      DeviceId deviceIndex,
      size_t* cachedAndFree,
      size_t* largestBlock) {
    alloc()->cacheInfo(deviceIndex, cachedAndFree, largestBlock);
  }

  void* getBaseAllocation(void* ptr, size_t* size) {
    return alloc()->getBaseAllocation(ptr, size);
  }

  void recordStream(const at::DataPtr& ptr, DPCPPStream stream) {
    if (!ptr.get()) {
      return;
    }

    if (ptr.get_deleter() != &deleter) {
      return;
    }

    alloc()->recordQueue(ptr.get(), &stream.queue());
  }

  std::mutex* getFreeMutex() {
    return alloc()->getDPCPPFreeMutex();
  }

  DeviceStats getDeviceStats(DeviceIndex device_index) {
    return alloc()->getStatsForDevice(device_index);
  }

  void resetAccumulatedStats(DeviceIndex device_index) {
    alloc()->resetAccumulatedStats(device_index);
  }

  void resetPeakStats(DeviceIndex device_index) {
    alloc()->resetPeakStats(device_index);
  }

  void dumpMemoryStatus(DeviceIndex device_index) {
    alloc()->dumpMemoryStatus(device_index);
  }

  std::vector<SegmentInfo> snapshot() {
    return alloc()->snapshot();
  }

 private:
  CachingDeviceAllocator* alloc() {
    return CachingDeviceAllocator::Instance();
  }
};

static DeviceAllocator myInstance;

REGISTER_ALLOCATOR(kXPU, &myInstance);

DeviceAllocator* DeviceAllocator::Instance() {
  return &myInstance;
}

at::Allocator* getDeviceAllocator() {
  return DeviceAllocator::Instance();
}

void emptyCacheInDevAlloc() {
  DeviceAllocator::Instance()->emptyCache();
}

void cacheInfoFromDevAlloc(
    DeviceIndex deviceIndex,
    size_t* cachedAndFree,
    size_t* largestBlock) {
  DeviceAllocator::Instance()->cacheInfo(
      deviceIndex, cachedAndFree, largestBlock);
}

void* getBaseAllocationFromDevAlloc(void* ptr, size_t* size) {
  return DeviceAllocator::Instance()->getBaseAllocation(ptr, size);
}

void recordStreamInDevAlloc(const DataPtr& ptr, DPCPPStream stream) {
  DeviceAllocator::Instance()->recordStream(ptr, stream);
}

DeviceStats getDeviceStatsFromDevAlloc(DeviceIndex device_index) {
  return DeviceAllocator::Instance()->getDeviceStats(device_index);
}

void resetAccumulatedStatsInDevAlloc(DeviceIndex device_index) {
  DeviceAllocator::Instance()->resetAccumulatedStats(device_index);
}

void resetPeakStatsInDevAlloc(DeviceIndex device_index) {
  DeviceAllocator::Instance()->resetPeakStats(device_index);
}

void dumpMemoryStatusFromDevAlloc(DeviceIndex device_index) {
  DeviceAllocator::Instance()->dumpMemoryStatus(device_index);
}

std::vector<SegmentInfo> snapshotOfDevAlloc() {
  return DeviceAllocator::Instance()->snapshot();
}

std::mutex* getFreeMutexOfDevAlloc() {
  return DeviceAllocator::Instance()->getFreeMutex();
}

/// Host Allocator
class HostAllocator final : public at::Allocator {
 public:
  // Singleton
  static HostAllocator* Instance() {
    static HostAllocator myInstance;
    return &myInstance;
  }

  static void deleter(void* ptr) {
    Instance()->release(ptr);
  }

  at::DataPtr allocate(size_t size) const override {
    void* ptr = nullptr;
    Instance()->alloc()->malloc(&ptr, size);
    return {ptr, ptr, &deleter, at::DeviceType::CPU};
  }

  at::DeleterFnPtr raw_deleter() const override {
    return &deleter;
  }

  bool isHostPtr(void* ptr) {
    return alloc()->isHostPtr(ptr);
  }

  void emptyCache() {
    alloc()->emptyCache();
  }

  void recordEvent(void* ptr, sycl::event& e) {
    alloc()->recordEvent(ptr, e);
  }

 private:
  CachingHostAllocator* alloc() {
    return CachingHostAllocator::Instance();
  }

  void release(void* ptr) {
    alloc()->release(ptr);
  }
};

Allocator* getHostAllocator() {
  return HostAllocator::Instance();
}

void emptyCacheInHostAlloc() {
  HostAllocator::Instance()->emptyCache();
}

bool isAllocatedByHostAlloc(void* ptr) {
  return HostAllocator::Instance()->isHostPtr(ptr);
}

} // namespace dpcpp
} // namespace xpu