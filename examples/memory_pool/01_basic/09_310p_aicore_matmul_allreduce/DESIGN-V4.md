# 09_310p_aicore_matmul_allreduce 设计文档 V4（fused 重构 + M 参数化）

> 状态：**已实现，已实机验证**（双 M 组全档 PASS，实测摘要见 §9，全文见
> EX09-实机验证报告-V4.md）
> 基线：DESIGN.md（原始设计）/ EX09-实机验证报告-V3.md（v3 时序修复 + fused>serial 归因）
> 本文档只覆盖 V4 变更面：fused 模式重构（核心）、M 参数化、梯度跳档；
> 编排 kernel / host API / 部署链 / KVER **零改动**。

## 1. 背景与目标

V3 实机实测（EX09-实机验证报告-V3.md §4）暴露：fused 稳态拍为"消费先行"
（poll(c−1)→accumulate(c−1)→matmul(c)→notify(c)），对称锁步下两 rank 互等，每拍暴露
编排+传输固定时延 δ≈0.11-0.17ms，fused 比 serial 慢 0.11-0.13ms/chunk（加速比 0.79-0.90）。

V4 目标：**消除计算环内的全部等待点**，使传输与计算的重叠成为结构性性质（而非依赖
δ<τ_mm 的时序巧合），并顺带支持 M 维参数化（M=4096 组对比）。

## 2. V4 变更决议汇总

| 维度 | V3 | V4 | 依据 |
|------|----|----|------|
| fused 生产环 | poll→acc→matmul→notify（消费先行） | **matmul→notify（零等待）** | 计算环内无 poll → δ 无从进入关键路径；对 δ 任意值鲁棒 |
| 归约位置 | 内联每拍 | **全部生产结束后统一进行** | 单 block 纪律下累加本就无法与计算重叠，挪尾无净损失；末笔传输延迟被前 N−1 个累加吸收 |
| M 维 | 固定 2048 | **EX_MVAL 编译开关（2048/4096）** | 验证掩盖收益随传输量的缩放规律 |
| A/B 布局 | A/B 同尺寸推导 | **A_BYTES/B_BYTES 分离**（M≠2048 时 A_half≠B_half） | M=4096: A=16MB, B=8MB |
| chunk 数 | 静态表 {8,16,32,64} | **运行时推导 = M/行数**，>64 槽自动跳档 | M=4096 的 128KB 档 = 128 chunk > 64 槽 |
| 编排 kernel / KVER | v3 | **v3 不变** | 数据面（SQE 配方/wire 契约）零改动 |

## 3. fused 设计（V4 核心）

### 3.1 结构

```
生产环（单向同步, 零等待）:
  for c in 0..chunks-1:
    MatmulChunk(c); Clean(C_c); Notify(槽c)      // 算完一块, 通知搬运一块, 立即下一拍

归约环（尾部统一）:
  for c in 0..chunks-1:
    Poll(flagArrival+c); Accumulate(c); Clean(recv_c)   // 此时传输早已完成, poll 必命中
  mm.End; status=0xC1
```

- **单向同步**：同步仅存在于"计算→通知"方向（数据就绪才委托搬运）；"搬运→计算"方向
  无任何回传等待。描述符随算随到，编排器在 CP1 并发派单（每 chunk 处理 0.1-0.17ms
  << 拍长 τ_mm=0.37-0.93ms），永不反压。
- **时间线**（M=2048 1024KB 档）：传输(c) 与 matmul(c+1) 重叠；末笔传输的残余延迟
  （δ≈0.17ms）被归约环前 N−1 个累加（0.39ms）吸收 → **尾步零暴露**。
- **总时间** = N×(τ_mm+τ_acc) + ε ≈ Tpure + Tacc，实测 8.00/8.11ms（预测 7.9-8.0 ✓）。

### 3.2 设计决策与依据

| # | 决策 | 依据 |
|---|------|------|
| D1 | 计算环内不做任何 poll | V3 归因：锁步下 poll(c−1) 紧跟 notify(c−1)，δ 必然暴露；且"对端 poll 多等一个 accumulate"的锁步放大问题在零同步环下**失去发生条件** |
| D2 | 归约全部挪尾 | 累加只能由本 AICore 的 vector 单元做（单 block），内联不是重叠是串行；挪尾后总时间不变、结构更简、且尾部累加吸收末笔传输延迟（比 C1"垫后一拍"方案少付一次 δ） |
| D3 | 对 δ 鲁棒优先于对 δ 隐藏 | C1（垫后一拍）依赖 δ<τ_mm 才隐藏；D1+D2 对 δ 任意值成立（δ 过大只延长尾部归约等待） |
| D4 | notify 内联而非突发（区别于 serial） | 传输与计算的重叠正是"融合"的被测变量：fused−serial 差值 = 真实被掩盖的传输+派单时间 |
| D5 | 累加仍落 recv 原地，C 区只读 | 继承 V2 §5#3 双区设计：C 是本端 SDMA 发送源，原地累加会与在途 SDMA 读竞争 |

### 3.3 波动模型对照（掩盖收益）

wave 模型（Tile=chunk、P=1、W=chunk 数）给出理论 overlap 空间 =
min(T_matmul, T_AllReduce)×(W−1)/W / Tserial：V4 实测值 0.03-0.11，随 chunk 细化下降
（每 chunk 固定编排开销线性增长）——1024KB 档空间最大，与最佳划分结论互证。
扣除结构性暴露的累加 τ_acc 后模型与实测吻合（0.36 vs 0.32ms）；残差 = 每拍 notify
固定开销（ENTIRE clean + 邮箱写 ≈5µs/拍）。详见 V4 报告 §3.0/§3.4。

## 4. 三模式状态机（V4 口径）

```
mode=FUSED(0):                                            // 单向同步生产环 + 尾部归约
  for c in 0..chunks-1:
    MatmulChunk(c); Clean(C_c); Notify(槽c)               // 生产: 零等待
  for c in 0..chunks-1:
    Poll(flagArrival+c) → 超时: status=0xEE; return       // 归约: 传输早已完成
    Accumulate(c); Clean(recv_c)                          // recv 原地: recv = partial + recv
  mm.End; status=0xC1; return

mode=SERIAL(1):                                           // 零重叠基线（与 V3 相同, 未动）
  for c: MatmulChunk(c); Clean(C_c)
  for c: Notify(槽c)
  for c: Poll(flagArrival+c); Accumulate(c); Clean(recv_c)
  mm.End; status=0xB1; return

mode=PURE(2):                                             // 纯计算基线（未动）
  for c: MatmulChunk(c)
  mm.End; status=0xA1; return
```

fused 与 serial 的**唯一差异 = notify 位置**（随算随发 vs 算完突发），即"传输是否与计算
重叠"——恰好是被测变量，Tfused−Tserial = 真实被掩盖的传输+派单时间。

## 5. 内存布局与 M 参数化

```
A_BYTES = M×KPAD×2        B_BYTES = KPAD×N×2        C_BYTES = M×N×2
A_OFF = 0                 B_OFF = A_OFF+A_BYTES     C_OFF = B_OFF+B_BYTES
RECV_OFF = C_OFF+C_BYTES  POOL_NEED = RECV_OFF+C_BYTES+2MB(标志保留区)
```

| 组 | M | KPAD | A | B | C=recv | POOL_NEED | 实际池 |
|----|---|------|---|---|--------|-----------|--------|
| M=2048（默认） | 2048 | 2048 | 8MB | 8MB | 8MB | 34MB | 64MB |
| M=4096（-DEX_MVAL=4096） | 4096 | 2048 | 16MB | 8MB | 16MB | 58MB | 64MB |
| M=2048 + K=8196（-DEX_KVAL=8196） | 2048 | 8320 | 32MB | 32MB | 8MB | 82MB | 128MB |
| M=4096 + K=8196 | 4096 | 8320 | 64MB | 32MB | 16MB | 130MB | **超 128MB 档，当前不支持**（需扩 256MB 档，见 §10） |

梯度扫描：chunk=1MB/512KB/256KB/128KB（沿 M 切 256/128/64/32 行），chunk 数 = M/行数
运行时推导；chunks > 64（邮箱槽数）自动跳档——M=4096 的 128KB 档（128 chunk）SKIP，
跳档不占槽、不参与最佳划分竞选。

## 6. 槽位与轮次（继承 V3，未动）

- 邮箱 ring：槽 c ↔ chunk c，ResetWorkspace 每 run 清零
- 到达标志槽：按 runId 分区免清（runId = 1+g×9+m×3+i；M=4096 跳档轮次不占槽，
  max runId = 27 ≤ 36），槽占用 ≤ 2368×8B ≈ 18.4KB << 2MB 保留区

## 7. 并发正确性分析（V4 序）

| 场景 | 分析 | 结论 |
|------|------|------|
| 死锁/循环等待 | 生产环内零 poll，无等待链；归约环 poll 的数据最迟在归约开始前 δ 已就绪 | 无死锁 ✓（且对 δ 任意值鲁棒） |
| 通知乱序 | notify 严格按 c 递增；编排器/SDMA FIFO 保序；标志槽与 chunk 一一对应 | 到达序 = 生产序 ✓ |
| 生产/归约竞争 | matmul(c) 写 C_c，accumulate(c−1…0) 写 recv；C 区只读（SDMA 源），recv 区仅本端写 | 无写冲突 ✓ |
| ub scratch | 归约环在 matmul 全部 IterateAll（同步）返回后执行，matmul LocalWorkspace 分时复用 | 串行安全 ✓ |
| 跨 run 覆写 | run N+1 的 SDMA 写 recv vs run N 未完成归约：每 run 前后 MPI 栅栏（计时窗外） | 隔离 ✓（继承 V3） |
| 可见性 | matmul→clean→notify（对 SDMA）；accumulate→invalidate 取新→clean→D2H（对 host） | 每写点按消费者 clean ✓（ExPollAccumulate 内） |
| 锁步放大 | 生产环无 poll，"对端 poll 多等一个 accumulate"失去发生条件 | 结构性消除 ✓（V3 实测踩坑项） |

## 8. 实现修改清单（全部在示例 .asc，单文件）

| # | 位置 | 修改 |
|---|------|------|
| 1 | ExRunFused | 状态机重构（§4）：删逐拍 poll/accumulate，改双环结构 |
| 2 | EX_M/偏移宏 | EX_MVAL 参数化；EX_AB_BYTES 拆 EX_A_BYTES/EX_B_BYTES，偏移链/池推导修正 |
| 3 | g_gradChunks | 删静态表，ExGradChunks(g) 运行时推导；ExScanGrad 加 >64 槽跳档守卫 |
| 4 | main | warmup/打印/最佳划分适配（跳档不参赛）；H2D 按 A/B 分尺寸；INFO 增 M |
| 5 | 注释 | 文件头/ExGenInputs/ExRunFused 注释同步 V4 语义 |

不改：ex09Kernel 入参与 wire 契约、ExNotifyChunk/ExPollAccumulate/ExAccumulateVec/
ExMatmulChunk、host API（smem_shm_overlap_*）、编排 kernel、KVER、README 运行命令。
编译：仅重编译示例（`-DEX_MVAL=4096` 为唯一新增开关）；设备侧 v3 部署继续有效。

## 9. 实测验证摘要（双 M 组，双端 PASS，全部 mismatch=0）

| 组/档 | Tfused | Tserial | Tpure | 加速比 | 掩盖度 |
|-------|--------|---------|-------|--------|--------|
| M=2048 1024KB×8 | 8.00 / 8.11 | 8.32 / 8.34 | 7.40 / 7.42 | 1.04 / 1.03 | 0.92 / 0.91 |
| M=2048 512KB×16 | 10.49 / 10.73 | 10.91 / 10.93 | 9.94 / 10.05 | 1.04 / 1.02 | 0.95 / 0.94 |
| M=2048 256KB×32 | 13.65 / 14.23 | 14.24 / 14.29 | 13.13 / 13.50 | 1.04 / 1.00 | 0.96 / 0.95 |
| M=2048 128KB×64 | 24.18 / 24.56 | 24.63 / 24.65 | 23.32 / 24.01 | 1.02 / 1.00 | 0.96 / 0.98 |
| M=4096 1024KB×16 | 16.02 / 15.75 | 16.61 / 16.45 | 14.80 / 14.52 | 1.04 / 1.04 | 0.92 / 0.92 |
| M=4096 512KB×32 | 21.19 / 20.57 | 21.81 / 21.48 | 20.08 / 19.36 | 1.03 / 1.04 | 0.95 / 0.94 |
| M=4096 256KB×64 | 28.04 / 27.24 | 28.58 / 28.14 | 26.88 / 25.68 | 1.02 / 1.03 | 0.96 / 0.94 |

（单位 ms，格式 r0/r1；M=4096 128KB 档跳档。）

- **加速比首次全档 ≥1.00**（1.00-1.04x），fused−serial 由 V3 的 +0.90ms 翻正为
  −0.23~-0.90ms，且随传输量翻倍（M=4096）而放大——被掩盖的正是传输本身
- **预测命中**：V3 报告 C1 预测 Tfused(1024KB)≈8.0-8.1ms，实测 8.00/8.11；Tpure/serial
  基线与 V3 逐位复现（未动项），对照可信
- 掩盖度 0.91-0.98（结构上限 0.94-0.98，已贴顶）；掩盖收益（wave 模型）0.03-0.11

## 10. 约束与下一步

**约束**：单 chunk ≤1MB（AICPU SQE 上限）、chunks ≤64（邮箱槽）、M=4096×K=8196 组合
需 130MB 池（当前池推导只到 128MB 档，需扩 256MB 档方可支持）。

**下一步（按收益/成本排序）**：
1. **doorbell 攒批**（编排器策略，serial/fused 双端同降）：省每 chunk ioctl≈0.1ms，
   Tserial/Tfused 同步逼近 Tpure+Tacc 下限（≈7.9ms）；注意攒批后 fused/serial 差值收窄，
   对比实验需固定口径
2. **多 block 归约/计算**（AICore 侧架构项）：解除单 block 纪律后 τ_acc 可与 cube 重叠，
   P=8 下 wave 模型的 Overlap 空间重新放大
3. M=4096×K=8196 组合（扩池档）与 K 维扫描补测
