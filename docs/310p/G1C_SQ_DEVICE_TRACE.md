# 310P3 G1-C：AICPU / CP SQ 第一停止点取证

> 仅诊断。**2026-10-20 实机执行完毕**：CANN 9.1 编译（KVER=12 + `MF_SDMA_ORCH_G1C_DEVICE_TRACE=ON`）通过；
> Gate C1 双 Rank 链路自检通过（HAL 查询 rc=0、SQ 游标实时推进、干净回绕）；
> Gate C2 会话 C2R2 复现双 Rank AICore timeout，**停滞瞬间双设备 CP SQ 均为空
> （hwHead==hwTail、请求环排空、纪健康轮转）**——设备侧直接证据反驳 H1/H2。
> 全部证据、时间线与判别见 vllm-ascend G1-C 分支
> `csrc/_310P/memfabric_mm_ar_v2/docs/G1C_DEVICE_FIRST_STOP_REPORT_20261020.md`。
> 本仓库 G1-C 分支无源码修改（实机兼容修复均在 vllm-ascend 侧的诊断脚本/Host 采样器）。
> 上层完整 G1-C 分析与复原流程见 vllm-ascend 同名 G1-C 分支的 `docs/G1C_DEVICE_FIRST_STOP_DESIGN_20261020.md`。

## 目标与边界

G1-B 已证实 Host 请求环与 Epoch 没有先停止，但没有直接测得 CP SQ 的持续消费情况。这里新增只读取证，不修复 `submitSqe` 容量判断，不改 SQE 配方、SMEM 邮箱、请求头尾布局、Epoch 心跳或 wire ABI。

## 埋点内容

- AICPU 核心：`src/hybm/ops/hybm_kernel/hybm_sdma_orch_kernel.cc`
- 独立 64B 只读取证布局：`hybm_g1c_device_trace.h`
- Host 1Hz 采集：`src/hybm/csrc/entity/hybm_sdma_orchestrator.cpp`

私有工作区 `resultAddr+0x100`，避开 result（0）、overflow（64）、stop（128）、heartbeat（136），不占 SMEM 段尾通信环。阶段：1 纪入口、2 读邮件、3 SQE 已生成、4 Doorbell 前、5 Doorbell 后、6 纪退出、7 出错、8 空闲期 SQ 查询。每跨 32 封请求的批次或长空闲约 8192 次循环采样 `halSqCqQuery(SQ_HEAD/SQ_TAIL)`，其他阶段沿用最近一次硬件样本。Host 有效帧为 `seqBegin == seqEnd != 0`，仍需结合查询返回码解释，严禁称每条日志都有实时硬件 Head/Tail。

## 编译和运行门控

设备诊断编译时，在独立 CMake 构建目录设置：

```text
-DMF_SDMA_ORCH_G1C_DEVICE_TRACE=ON
-DMF_SDMA_ORCH_KVER=12
```

这会构建含埋点 `libmf_sdma_orch.so`，并生成指向 **libmf_sdma_orch_v12.so** 的 launch JSON。v12 用于规避 CP1 同名动态库缓存，**不是 wire ABI 由 v11 变为 v12**。不得在现网路径覆盖原 v11 文件，也不得让安装元数据替代实际 SO 哈希核对。

诊断 Host `libmf_hybm_core.so` 必须来自本分支，现场 `/usr/local` 与 site-packages 两处保留原库备份及 SHA256；验证 `MF_SDMA_ORCH_JSON` 指向对应的 v12 launch JSON 之后才可启用：

```bash
export MF_SDMA_ORCH_DIAG=1
export MF_SDMA_ORCH_G1C_TRACE=1
unset MF_SDMA_ORCH_EPOCH_BUDGET_S  # 保持 15 秒 Epoch；禁止无限常驻
```

`[MF_G1C]` WARN 日志 1Hz 输出设备侧最后帧；`[MF_DIAG]` 的 INFO 日志仍需 G1-B 已验证的日志级别配置。必须做第一个 `reqSeq` 和 Head/Tail 实际变化的证据门，不能以日志标记存在代替链路验证。

## 判别要求与回退

1. 若 `phase=4` 停住，而软件已生成多封 SQE，核验 HAL Query 返回码、SQ 硬件 Head/Tail 与 V2 `signal/wait` 阶段；Doorbell 前停顿不等于硬件 SQ 卡死。
2. 若 `phase=5/8` 持续出现，而硬件 Head 长期不动、软件有积压，才考虑 CP SQ / 固件消费链为第一停止环节候选。
3. `reqPending=0` 是 Host 请求环状态，不能替代 CP SQ 的 `used/depth`；结果只有在连续多个有效样本以及双 Rank 时间线对齐时可判读。
4. 诊断 HAL 查询和工作区 cache clean 改变 AICPU 时间线；不能与未埋点 F01/B02 混算故障复现率。
5. 失败先保存原始日志与设备 slog/plog，不在 G1-C 修改 SQ 判断或清理其他进程。停止所有测试进程后，恢复两处 Host 原库并哈希复核。原 G1-B worktree、KVER11 部署库和历史证据均不覆盖。

状态：**已实机验证（2026-10-20，310P3 双 Rank）**；仅诊断用途，不具备生产启用条件。
实机结论：CP SQ 消费链在故障瞬间健康排空，第一停止点位于 V2 到达邮件等待侧（详见
vllm-ascend G1-C 执行报告）；根因仍未定。
