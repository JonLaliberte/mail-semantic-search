#import <Metal/Metal.h>
#import <objc/runtime.h>
#include <stdatomic.h>

static _Atomic unsigned long long gpu_nanoseconds = 0;
static _Atomic unsigned long long completed_buffers = 0;
static IMP original_commit;

// Attach one completion handler to each buffer in this benchmark process.
// The Metal timestamps exclude CPU tokenization and host-side waits.
static void timed_commit(id<MTLCommandBuffer> buffer, SEL selector) {
    [buffer addCompletedHandler:^(id<MTLCommandBuffer> completed) {
        double duration = completed.GPUEndTime - completed.GPUStartTime;
        if (completed.GPUStartTime > 0 && duration > 0) {
            atomic_fetch_add(&gpu_nanoseconds, (unsigned long long)(duration * 1e9));
            atomic_fetch_add(&completed_buffers, 1);
        }
    }];
    ((void (*)(id, SEL))original_commit)(buffer, selector);
}

const char *install_gpu_timer(void) {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        id<MTLCommandQueue> queue = [device newCommandQueue];
        id<MTLCommandBuffer> buffer = [queue commandBuffer];
        if (!buffer) {
            return NULL;
        }
        Class cls = object_getClass(buffer);
        SEL selector = @selector(commit);
        Method method = class_getInstanceMethod(cls, selector);
        if (!method) {
            return NULL;
        }
        original_commit = method_getImplementation(method);
        class_replaceMethod(cls, selector, (IMP)timed_commit, method_getTypeEncoding(method));
        return class_getName(cls);
    }
}

double gpu_seconds(void) {
    return atomic_load(&gpu_nanoseconds) / 1e9;
}

unsigned long long gpu_buffers(void) {
    return atomic_load(&completed_buffers);
}
