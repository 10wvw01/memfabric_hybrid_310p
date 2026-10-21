# G1-D MemFabric Host 到达环取证（2026-10-21）

> 状态：诊断代码已提交，尚未在 310P3 编译或运行，不能把静态完成当作实机 PASS。

## 基线锁定

沿用镜像仓库 memfabric_hybrid_310p 的 G1-C 修复线，不切换原始仓库 wgm-dev-310p。
原始现网 Host libmf_hybm_core.so SHA256：88d392850733d7f166ce28615a8fdbf164b604979df9d3fc8cd0c10c64fd7bc1。
G1-C 设备诊断 KVER12 在上一轮结束时已删除设备侧文件。G1-D 前必须从保全产物重新核验并独立部署 v12，原 v11 不得覆盖。
G1-D 只修改 SdmaOrchestrator::SupervisorLoop 的 Host 采样，未改 AICPU、SQE、Doorbell、Epoch 或邮箱协议，**无需重新定义 v13 wire/设备库**。

## 开关与取证内容

显式设置 MF_SDMA_ORCH_DIAG=1 和 MF_SDMA_ORCH_G1D_TRACE=1 才会启用额外取证。
每秒 5 次短 D2H，总计读取 224B（非同一时刻原子快照）。

| 保留区偏移 | 字节 | 内容 |
|---|---:|---|
| 0x2A00 | 64 | ARR_STAMP[1020..1023]、ARR_MAIL[0] |
| 0xAA00 | 64 | ARR_MAIL[1023]、ArrHead |
| 0x0A20 | 32 | ARR_STAMP[0..3] |
| 0x09F0 | 32 | quiet[60..63] |
| 0x0810 | 32 | quiet[0..3] |

日志标记 [MF_G1D]，分别打印 devId、epoch、arrHead、边界 Stamp、邮件字段、quiet 戳；拷贝失败时打印五个独立 snapshotError 返回码。
地址、邮件字段只写入服务器原始日志；公开 GitHub 版本必须脱敏。

## 验收与禁令

先完成 Host 编译、ABI/符号对比及 C1 正确性，再运行真实 Gate E；执行者须保全双路径 Host .so 哈希以及 KVER11/12 安装库和 JSON 哈希。
不自动安装、不强制重启、不写 GVA、不修改 reqSeq、不覆盖现有系统库。实机失败须先归档日志后按 G1-C 既有 SOP 恢复。

ArrHead 与 Stamp 单次不匹配仅证明取样时看到不匹配，不证明永久丢写。Host D2H 的缓存视角不等于 AI Core；CP SQ 当前排空不等于每条 SQE 已正确落地。

## 关联材料

- [G1-D 全流程执行计划](https://github.com/10wvw01/vllm-ascend/blob/diag/310p3-g1d-minimal-forensics-20261021/csrc/_310P/memfabric_mm_ar_v2/docs/G1D_EXECUTION_PLAN_20261021.md)
- [G1-C 原始实机报告](https://github.com/10wvw01/vllm-ascend/blob/95096ad25d004074e45e3ef65c33de5eb9842644/csrc/_310P/memfabric_mm_ar_v2/docs/G1C_DEVICE_FIRST_STOP_REPORT_20261020.md)
- [MemFabric 内存布局](https://github.com/10wvw01/memfabric_hybrid_310p/blob/5345cbdd5cf745342c21f7b68b0b0907869c164a/src/smem/include/device/smem_shm_sdma_layout.h)
