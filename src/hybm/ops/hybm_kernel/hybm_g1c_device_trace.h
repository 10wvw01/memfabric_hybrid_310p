#ifndef HYBM_G1C_DEVICE_TRACE_H
#define HYBM_G1C_DEVICE_TRACE_H
// G1-C 专用诊断布局：占已有 4096B orchestrator workspace 的 0x100..0x13f。
// 与对称内存双环、epoch 参数和返回值分离；默认构建完全不使用。
#include <cstdint>
namespace ock { namespace mf {
constexpr uint64_t HYBM_G1C_DEVICE_TRACE_OFF = 0x100;
enum HybmG1cPhase : uint64_t {
    G1C_EPOCH_START = 1, G1C_MAIL_READ = 2, G1C_SQE_READY = 3,
    G1C_BEFORE_DOORBELL = 4, G1C_AFTER_DOORBELL = 5,
    G1C_EPOCH_EXIT = 6, G1C_DEVICE_ERROR = 7
};
struct alignas(64) HybmG1cDeviceTrace {
    uint64_t seqBegin;  // 帧标识，用于剔除混合快照
    uint64_t phase;     // 见 HybmG1cPhase
    uint64_t reqSeq;    // 最近一封请求的序号
    uint64_t shadowTailDepth; // 高32位 sqDepth，低32位软件 shadowTail
    uint64_t liveHeadTail;    // 高32位硬件 SQ Tail，低32位硬件 SQ Head
    uint64_t queryRet;  // 高32位 tail 查询 rc，低32位 head 查询 rc
    uint64_t error;     // 最近一次设备侧非零错误码
    uint64_t seqEnd;    // 与 seqBegin 一致才解释其余字段
};
static_assert(sizeof(HybmG1cDeviceTrace) == 64, "G1-C workspace trace must fit 64B");
} }
#endif
