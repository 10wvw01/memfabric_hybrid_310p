# 09_310p_aicore_matmul_allreduce 设计文档

> 状态：**已实现，待实机验证**（`.asc` + `README.md` 已产出；本机已完成 bisheng dav-2002
> 统一编译 + 链接验证（CANN 9.1.0-beta.1），实机数值/性能验收见 README 验收标准与风险注记）
> 基线：`08_310p_aicore_aicpu_overlap`（ex08Kernel），本示例为独立目录，仅含 ex09Kernel。

## 1. 目标与语义

Atlas 300I Duo（Ascend310P3）单卡双 die 2-rank SHM 对称池上，实现 **TP=2 K 维切分 matmul + all-reduce 的计算通信融合 kernel**：

- 矩阵 `A=[2048,4096]`、`B=[4096,2048]`、`C=[2048,2048]`，dtype=FP16，TP=2
- **K 维切分**：rank r 持 `A_half = A[:, r*2048:(r+1)*2048]`、`B_half = B[r*2048:(r+1)*2048, :]`（各 `[2048,2048]`=8MB）
- 各 rank AICore matmul 算部分和 `C_partial[2048,2048]` → 逐 chunk 经 AICPU 编排 SDMA **互传** → 对端 AICore **FP16 读改写累加** → 双端各得完整 `C = A×B`
- **对称双产双收**：两 rank 跑完全相同的 kernel 逻辑（无 role 参数），真 TP 语义
- **梯度扫描**：chunk 粒度 128KB/256KB/512KB/1MB 四档，fused/serial/pure 三模式，测出最佳划分

## 2. 需求决议汇总

| 维度 | 决议 |
|------|------|
| TP 语义 | K 维切 + all-reduce（读改写累加） |
| 角色模型 | 对称双产双收（单 block 纪律，沿用 ex08） |
| chunk 划分 | 沿 M 切（行主序连续），梯度 128KB/256KB/512KB/1MB = 32/64/128/256 行/chunk = 64/32/16/8 chunks |
| matmul | AscendC Matmul 高层 API（`lib/matmul_intf.h`），单 block，M-tile 逐 chunk 迭代 |
| 内存 | `EX_LOCAL_SIZE` 32MB→**64MB** 全入池 |
| 数据/校验 | 双端同种子 U[-1,1]，CPU FP64 参考，atol=0.1 + rtol=1e-2，双端各自全量校验 |
| 性能 | 4 档 × 3 模式全矩阵，每配置 3 次取平均，1 次全局预热不计入 |
| serial 语义 | bulk 整体串行：算完全部 chunk → 统一 notify → 逐 chunk poll+累加 |
| 目录/文件 | `09_310p_aicore_matmul_allreduce/`，仅 ex09Kernel，08 目录原样保留 |

硬约束（继承 08）：单 chunk ≤ 1MB（AICPU SQE 上限）、chunks ≤ 64（邮箱槽数）、每 rank 段末 2MB 协同标志保留区、SDMA 经邮箱描述符只能传连续线性区域。

## 3. 总体架构与数据流

```
   rank r (die r)                                    rank 1-r (die 1-r)
┌───────────────────────────┐              ┌───────────────────────────┐
│ AICore ex09Kernel(单block) │              │ AICore ex09Kernel(单block) │
│  matmul chunk c → C 行块   │              │  matmul chunk c → C 行块   │
│  clean C_c(dcci ENTIRE)   │              │  clean C_c(dcci ENTIRE)   │
│  notify 邮箱槽 c(16B)      │              │  notify 邮箱槽 c(16B)      │
│          │                │              │          │                │
│          ▼                │              │          ▼                │
│ AICPU 编排 kernel(host     │              │ AICPU 编排 kernel(host     │
│  overlap_launch 触发)      │              │  overlap_launch 触发)      │
│   代投 SDMA SQE 对 ════════╪══SDMA 数据══▶ │ recv_c + 到达标志 c        │
│   (C_c→对端 recv_c         │              │  (FIFO: 标志到 = 数据到)   │
│    + 8B 标志→对端槽 c)      │              │                           │
│                            │◀═SDMA 数据═══╪   (对称: 对端→本端)         │
│  poll 本端到达标志 c-1      │              │  poll 本端到达标志 c-1      │
│  accumulate: C += recv     │              │  accumulate: C += recv     │
│  clean C_{c-1}(dcci)       │              │  clean C_{c-1}(dcci)       │
└───────────────────────────┘              └───────────────────────────┘
```

fused 模式稳态：本端 `compute c → notify c` 之后，`poll/accumulate c-1` 消费上一拍到达的数据，
chunk c 的 SDMA 传输与 `compute c+1` 重叠；仅首 chunk 通信暴露（流水填充）。

## 4. 内存布局（每 rank 64MB 段）

```
ownSeg = gva + symSize * rank（symSize 为对称段大小，本端物理贡献 64MB）

 0x0000000 ┌─────────────────────────────┐
           │ A_half  [2048,2048] FP16     │ 8MB  本 rank 的 A[:, r*2048:]（K 半）
 0x0800000 ├─────────────────────────────┤
           │ B_half  [2048,2048] FP16     │ 8MB  本 rank 的 B[r*2048:, :]（K 半）
 0x1000000 ├─────────────────────────────┤
           │ C       [2048,2048] FP16     │ 8MB  部和 → 累加后 = 完整 C（matmul 覆写 + RMW）
 0x1800000 ├─────────────────────────────┤
           │ recv    [2048,2048] FP16     │ 8MB  对端部和线性镜像（SDMA 落地区，只读）
 0x2000000 ├─────────────────────────────┤
           │ （预留 30MB 未用）            │      池其余区域不动
 0x3E00000 ├─────────────────────────────┤
           │ 协同标志保留区 2MB            │      到达标志槽（8B/槽，轮次分区，见 §5）
 0x4000000 └─────────────────────────────┘
```

- A/B/C/recv 区内 chunk c 均为**连续** `chunkRows*2048*2` 字节（行主序 M 切天然连续，SDMA 直传）
- 对端 `overlap_launch` 的 `dstGva = mySeg + RECV_OFF`，AICPU 编排按 `dstGva + c*chunkBytes` 落 chunk c
- 数据链初始化：host 生成**完整** A/B（双端同种子），H2D 只拷本 rank 的 K 半入池

## 5. 槽位与轮次分配

- **邮箱 ring**（workspace，普通设备内存）：槽 c ↔ chunk c，`ResetWorkspace` 每 run 前清零（复用 ex08）
- **到达标志槽**（池内保留区，8B/槽）：按 `runId` 分区免清池内标志（ex08 轮次槽思路）
  - `runId`：全局递增；warmup=0，此后 `runId = 1 + g*9 + m*3 + i`（g=档 0..3，m=模式 0=fused/1=serial/2=pure，i=迭代 0..2）
  - 本 run 槽区间 `[runId*64, runId*64 + chunks)`；pure 模式不 launch、不占槽
  - 最大 runId = 36 → 槽占用 ≤ 2368×8B ≈ 18.9KB << 2MB ✓
  - kernel `flagArrival = ownFlagBase + runId*64*8`；对端 launch `flagGva = peerFlagBase + runId*64*8`

## 6. ex09Kernel 设计

### 6.1 签名

```cpp
extern "C" __global__ __aicore__ void ex09Kernel(
    __gm__ uint8_t *ownSeg,        /* 本端段基址(64MB) */
    uint64_t physSize,             /* 段物理大小(标志保留区定位) */
    uint32_t mode,                 /* 0=fused 1=serial(bulk) 2=pure */
    uint32_t chunks,               /* 8/16/32/64 */
    uint32_t chunkRows,            /* 256/128/64/32 */
    uint32_t chunkBytes,           /* chunkRows*2048*2 */
    __gm__ uint8_t *workspace,     /* 协同工作区(邮箱 ring) */
    __gm__ uint64_t *flagArrival,  /* 本端到达标志数组(8B×chunks, 槽基址已折算) */
    __gm__ uint32_t *status)       /* 状态面包屑(GM) */
```

A/B/C/recv 地址由 `ownSeg + 段内偏移常量` 推导；tiling 在 kernel 内构造（见 §8）。
状态码约定（面包屑）：`0xF0` 入口 → `0xEE` 超时/失败；各模式完成码 fused=`0xC1`、serial=`0xB1`、pure=`0xA1`。

### 6.2 三模式状态机（对称角色，无 role 分支）

```
入口: blockIdx!=0 → return（单 block 纪律）; mm.Init(tiling)

mode=PURE(2):
  for c in 0..chunks-1:  MatmulChunk(c)                 // 仅计算
  mm.End; status=0xA1; return

mode=SERIAL(1):                                          // bulk 零重叠基线
  for c in 0..chunks-1:
    MatmulChunk(c); Clean(C_c)                           // 算完全部
    Notify(邮箱槽c, C_c, chunkBytes/4)                    // 统一发描述符
  for c in 0..chunks-1:
    Poll(flagArrival+c) → 超时: status=0xEE; return
    Accumulate(c); Clean(C_c)                            // 统一收+累加
  mm.End; status=0xB1; return

mode=FUSED(0):                                           // 计算通信融合
  MatmulChunk(0); Clean(C_0); Notify(槽0)                // 流水填充(首 chunk 通信暴露)
  for c in 1..chunks-1:
    Poll(flagArrival+c-1) → 超时: status=0xEE; return    // 对端 c-1 提前一拍已发, 命中≈SDMA 时延
    Accumulate(c-1); Clean(C_{c-1})                      // 消费上一拍数据
    MatmulChunk(c); Clean(C_c); Notify(槽c)              // 通信 c 与下一轮计算重叠
  Poll(flagArrival+chunks-1); Accumulate(chunks-1); Clean(C_{last})
  mm.End; status=0xC1; return
```

### 6.3 基本操作（复用/扩展 ex08 原语）

- `MatmulChunk(c)`：`SetTensorA(A_half + c*chunkRows*K)`，`SetTensorB(B_half)`，
  `SetTensorC(C + c*chunkRows*N)`，`SetTail(chunkRows, N)`，`while(mm.Iterate()){}`（见 §8）
- `Clean`：`ExCleanRegion`（ENTIRE_DATA_CACHE，matmul copy-out 后 SDMA/D2H 可见性）
- `Notify`：`smem_shm_overlap_notify(槽c, C_c 地址, chunkBytes/4)`
- `Poll`：`smem_shm_overlap_poll_flag`（dcci 自旋，返回 0 即超时）
- `Accumulate(c)`：GM→UB→向量 `Add`→GM（16KB 分块，`TQue<VECIN/VECOUT>` 双缓冲）。
  **实现修正**：AICore 禁止 half 标量运算（编译期报错），ex08 INC 式标量读改写不可用，
  累加走 `DataCopy(MTE2) → Add(向量) → DataCopy(MTE3)` 规范路径

## 7. host 主流程（伪代码与计时口径）

```
MPI_Init(np=2) → aclInit → smem_init → smem_shm_init(STORE_URL, 2, rank)
smem_shm_create(64MB, SDMA) → overlap_get_workspace（未就绪即 FAIL 退出）
host 同种子 LCG 生成完整 A[2048,4096]/B[4096,2048] ∈ U[-1,1]（FP16 可表示值）
CPU FP64 算 C_ref = A×B（单次，~10s 量级，可选 OpenMP 加速）
H2D: A_half/B_half → 池内本 rank K 半
SyncPeer

RunOneRound(mode, chunks, chunkRows, runId):               // 单 run 编排
    SyncPeer()                                            // 计时窗外，防跨 run 覆写 hazard
    ResetWorkspace(ws); 清 status
    launch ex09Kernel(mode, chunks, chunkRows, ..., flagArrival=ownFlagBase+runId*64*8)
    if mode != PURE:
        overlap_launch(shm, dst=peerSeg+RECV_OFF, flag=peerFlagBase+runId*64*8, chunks)
        overlap_wait(shm)                                 // AICPU 编排完成
    sync(aiCoreStream)                                    // kernel 完成 = 全部收+累加完成
    return t = NowMs()-t0                                 // 计时窗口: launch→双同步

warmup: fused@1MB 档（runId=0，不计入统计）
for g in {1MB, 512KB, 256KB, 128KB}:                      // 档位从大到小
    for m in {FUSED, SERIAL, PURE}:
        for i in 0..2:
            t[i] = RunOneRound(m, g, runId++)
            if i==0 && m!=PURE: Verify()                  // D2H 读 C 全量比对 C_ref
        avg[m][g] = mean(t[0..2])
r0 打印梯度表格 + 最佳划分；双端销毁干净退出
```

- 校验（`Verify`）：D2H 读 C 8MB，逐元素 `|C−C_ref| ≤ 0.1 + 0.01*|C_ref|`，统计 mismatch；
  pure 模式 C 仅为部和（无 all-reduce），不校验（其 matmul 路径已由 fused 校验覆盖）
- 输出表（r0）：

```
[梯度] chunk=1024KB(256行×8)   Tfused=xx.xxms Tserial=xx.xxms Tpure=xx.xxms 加速比=x.xx 掩盖度=x.xx
[梯度] chunk= 512KB(128行×16)  ...
...
[最佳] chunk=xxxKB  加速比 y.yy  掩盖度(Tpure/Tfused) z.zz
```

最佳划分判据：`Tserial/Tfused` 最大（并列时取 `Tpure/Tfused` 更接近 1 者）。

## 8. matmul 实现细节

- API：`#include "lib/matmul_intf.h"`（CANN 9.1 `adv_api/matmul/matmul.h` 已确认存在）
- 单 `Matmul<half, half, half>` 对象全程复用，逐 chunk 换 `SetTensorA/C` 指针（B 固定）
- 调用序列（实现修正：本版 CANN 无 `SetTensorC`，C 输出经 `IterateAll(gm)` 逐 fragment
  `GetTensorC` 直写 GM；dav-2002 上 `IterateAll` 入口先做 ENTIRE dcci——天然化解风险 R2）：

```cpp
mm.Init(&cubeTiling, &pipe);                  // tiling 见下
mm.SetOrgShape(chunkRows, N, K);              // 2048 维度常量
mm.SetTensorA(aGm);  mm.SetTensorB(bGm);      // 逐 chunk 重绑(SetTensorA 会 Scheduler Reset, 官方支持复用)
mm.SetTail(chunkRows, N, K);                  // 本 chunk 实际形状
mm.IterateAll(cGm);                           // 计算并 copy-out 到 GM
... 全部 chunk 完成后 mm.End();
```

- tiling（`TCubeTiling`，kernel 内构造，host 不依赖其布局）：
  - `singleCoreM = chunkRows, singleCoreN = 2048, singleCoreK = 2048`
  - 起步值 `baseM = chunkRows（32/64/128/256）, baseN = 128, baseK = 64`，实机标定后固化
- 累加器：cube 默认 FP32 累加 → FP16 输出（`MatmulType<half>` 输出转换在 copy-out 完成）
- C 输出后必须 `ExCleanRegion` 再 notify（cube copy-out 数据对 SDMA 的可见性，见风险 R2）

## 9. 数值校验方案

- 生成：确定性 LCG（如 `seed^(i*131+j*37)` 类散列），值域 U[-1,1]，**取 FP16 可精确表示值**
  （如量化到 2^-11 网格），双端同种子 → 无需跨 rank 交换即可各自重建完整 A/B
- 参考：CPU FP64 全量 `C_ref[2048,2048]`（一次），容差 `atol=0.1, rtol=1e-2`
  - 误差预算：C 元素 σ≈21（K=4096，U[-1,1]），FP16 ulp@21≈0.016；部和落盘 + FP16 互加
    共两次舍入 ≈ 0.03~0.05 << atol=0.1，裕量充分
- 校验点：每档 fused 首轮、每档 serial 首轮（共 8 次，D2H 在计时窗口外）

## 10. 验收标准

1. **功能**：8 个校验点全部 mismatch=0；双端 kernel 状态码走满（0xC1/0xB1/0xA1），
   `overlap_get_result` mainRet=0、headAfter 前进；双端打印 `=== EX09 rN PASS ===`
2. **性能**：输出 4 档梯度完整表格与最佳划分；理想 fused 稳态 `Tfused ≈ Tpure + ΣAccumulate + 首chunk通信`
   （通信被计算掩盖，不再线性叠加）
3. **干净退出**：destroy/uninit/MPI_Finalize 无残留（对齐 ex08 验收第 3 条）

## 11. 并发正确性分析

| 场景 | 分析 | 结论 |
|------|------|------|
| fused 死锁 | notify 链不依赖任何 poll（先算后发）；poll c-1 时对端 c-1 已提前一拍 notify | 无循环等待 ✓ |
| 双向并发 | 双 rank 各自 launch AICPU 编排 kernel，ex08 双向回程已验证同模式 | ✓ |
| run 内写冲突 | 本端写 C/mailbox；对端 SDMA 写本端 recv/flag；recv 只被本端 kernel 读（poll 后） | 无冲突 ✓ |
| 跨 run 覆写 | run N+1 本端 SDMA 可能写对端 recv[c]，vs 对端 run N 未完成的 Accumulate(c) | **MPI 栅栏隔离**（每 run 前后，计时窗外）✓ |
| C 数据可见性 | matmul→clean→notify（SDMA 读）；accumulate→clean→D2H（host 读） | 每写点后按消费者 clean ✓ |

## 12. 风险与开放问题

| # | 风险 | 缓解 |
|---|------|------|
| R1 | dav-2002 上 `baseM/baseN/baseK` 合法值与 310P3 cube 规格未标定 | 起步保守值（§8），实机冒烟先行；必要时退到 `baseM=32` 全档统一 |
| R2 | matmul copy-out 后 dcci clean 的配合（cube 写 GM 的 cache 行为）与 ex08 vector 写不同 | **已缓解**：dav-2002 的 `IterateAll` 入口自带 ENTIRE dcci（CANN 9.1 实现确认），仍保留 notify 前 `ExCleanRegion` 双保险 |
| R3 | B 以 ND [K,N] 直摆的 LoadData 效率/合法性 | 若不合法，host 生成 B_half 时改按 [N,K] 摆放 + `isTransposeB=true` |
| R4 | AscendC Matmul 与 dcci/邮箱原语混编（cube+vector 同 kernel） | bisheng 理论支持，冒烟验证；极端情况拆两个 kernel 串联 |
| R5 | chunks=64（128KB 档）占满邮箱 64 槽 | 槽 0..63 恰好 1024B，与哨兵(+1024)不冲突，已核算 |
| R6 | CPU FP64 参考 ~10s 一次性开销 | 可选 OpenMP（`-fopenmp`）；不影响设备侧计时 |
| R7 | CANN 9.1 beta1 头文件与 bisheng `--npu-arch=dav-2002` 兼容性 | 编译冒烟；zbal 已有 AscendC kernel 先例 |

## 13. 与 08 的关系 / 复用清单

- **复用**：`Step/NowMs/SyncPeer/ResetWorkspace` host 骨架、`ExStatusSet/ExCleanRegion` 设备原语、
  `smem_shm_overlap_{notify,poll_flag}` 设备原语、`overlap_{launch,wait,get_result}` host API、
  MPI 双 rank 拉起、`MF_310P_SHM_OVERLAP=1` 环境与 kernel 部署链
- **新增**：`MatmulChunk/Accumulate` 设备操作、三模式状态机、梯度扫描与统计 host 逻辑、
  A/B 生成与 CPU 参考校验、64MB 段布局
- **不迁移**：ex08Kernel 及其 pattern 校验阶段（08 目录原样保留）
- README 结构镜像 08（场景/目标/使用能力/规模建议/必要条件/验收标准/运行命令），实现阶段产出
