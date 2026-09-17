# 09_310p_aicore_matmul_allreduce：310P Duo 单卡双 die 2-rank TP matmul all-reduce 融合

## 场景

Atlas 300I Duo（Ascend310P3）单卡双 die（dev0/dev1）2-rank SHM 对称内存池上，演示
**K 维切分 TP=2 的 matmul all-reduce 计算通信融合 kernel**（08 的 TP 化演进）：

- 矩阵 `A=[2048,4096]`、`B=[4096,2048]`、`C=[2048,2048]`，FP16，TP=2
- **K 维切分**：rank r 持 `A_half=A[:, r*2048:]`、`B_half=B[r*2048:, :]`（各 8MB）
- **AICore matmul**（AscendC Matmul 高层 API，单 block）逐 chunk 产部分和 `C_partial[2048,2048]`
- **计算→通信融合**：chunk 算完即 `ENTIRE_DATA_CACHE` clean + GM 邮箱委托 AICPU 编排
  SDMA 互传（对端接收区落位 + 8B 到达标志），**对端 poll 到达后 UB 向量 Add 累加**
  （AICore 禁止 half 标量运算，累加走 `DataCopy→Add→DataCopy` 向量路径）
- **对称双产双收**：两 rank 跑同一 kernel 同一逻辑（无 role 参数），双端各得完整
  `C = A×B` —— 真 TP all-reduce 语义

三模式（同一 `ex09Kernel`，`mode` 参数区分）：

| mode | 名称 | 语义 |
|------|------|------|
| 0 | fused | 逐 chunk 计算→notify→poll/累加**上一拍**，通信藏于计算（仅首 chunk 暴露） |
| 1 | serial | bulk 零重叠：算完全部→统一 notify→逐 chunk poll+累加（无重叠对照） |
| 2 | pure | 仅 matmul 无通信（纯计算基线） |

**梯度扫描**：chunk = 1MB/512KB/256KB/128KB（沿 M 切 256/128/64/32 行，chunks = 8/16/32/64），
每配置 3 次取平均，输出 `Tserial/Tfused`（加速比）与 `Tpure/Tfused`（通信掩盖度），
**测出最佳 chunk 划分**（判据：加速比最大，并列取掩盖度更接近 1 者）。

## 目标

- 验证 SHM overlap 链路承载**真实计算负载**（而非 08 的 pattern 填充）：matmul 结果
  跨 die 互传 + 设备侧 all-reduce 累加
- 验证 AscendC Matmul 高层 API 与协同原语（邮箱/标志/dcci）在单 kernel 内混编
  （cube 计算 + vector 累加 + 通知链共存）
- 量化**计算掩盖通信**：fused 稳态 ≈ matmul 单 chunk 时间（SDMA 被计算吸收），
  并给出 chunk 粒度与掩盖效果的权衡曲线（粒度细则通信启动早、调度开销升）

## 使用能力

`smem_shm_create(SDMA)` → `smem_shm_overlap_get_workspace` → AICore kernel
（`smem_shm_overlap_notify`/`poll_flag` 设备原语 + AscendC `Matmul::IterateAll` +
`DataCopy`/`Add` 向量累加）→ `smem_shm_overlap_launch`（chunks 1~64）→
`smem_shm_overlap_wait` → `smem_shm_destroy`

## 内存布局（每 rank 64MB 段）

```
+0x0000000  A_half  [2048,2048] FP16  8MB  本 rank 的 A K 半
+0x0800000  B_half  [2048,2048] FP16  8MB  本 rank 的 B K 半
+0x1000000  C       [2048,2048] FP16  8MB  部分和 → 累加后 = 完整 C
+0x1800000  recv    [2048,2048] FP16  8MB  对端部分和镜像(SDMA 落地区)
+0x2000000  预留 30MB
+0x3E00000  协同标志保留区 2MB(到达标志按 runId×64 槽分区, 轮次隔离免清)
```

## 规模建议

- world_size=2 固定（同卡 die 对映射 dev0/dev1）
- 单 chunk ≤ 1MB（AICPU SQE 上限）、chunk 数 ≤ 64（邮箱槽数）：本示例 4 档全部顶格合规
- 每 rank 段末尾 2MB 为协同标志保留区（勿存放用户数据）
- 梯度扫描共 37 run（1 预热 + 4×3×3），槽占用 ≤ 2368×8B ≈ 18.4KB << 2MB

## 必要条件

- Atlas 300I Duo（Ascend310P3）+ CANN ≥ 9.1（本示例验证于 cann-9.1.0-beta.1，
  bisheng 随 CANN 提供）
- MPI（双 rank 拉起与栅栏；编译与启动必须同一套，混用会段错误：
  发行版 4.1.2：`/usr/bin/mpirun` + 头/库 `/usr/lib/aarch64-linux-gnu/openmpi/{include,lib}`
  （编译需 `-DOMPI_SKIP_MPICXX`）；自装 4.1.5：`/usr/local/openmpi` 全套）
- 协同编排 kernel 已部署到设备 CP1（部署链：CUSTAICPUKernel mode=1 迁移落盘 + 设备内复制
  到 `/usr/lib64/aicpu_kernels/0/aicpu_kernels_device/`，机制与运维纪律见
  `310p-aicore-aicpu-开发进展.md` 附录 A.4；**每次正式运行前须过验证门**）
- 环境变量：`MF_310P_SHM_OVERLAP=1` + `MF_310P_OVERLAP_JSON=<launch json 路径>`
  （json 由主构建 `mf_overlap_kernel` 目标产出）

## 验收标准

1. **功能**：每档 fused/serial 首轮全量校验 `mismatch=0`
   （双端同种子 U[-1,1]，CPU FP64 参考，容差 atol=0.1 + rtol=1e-2，覆盖 FP16 部分和
   落盘 + 互加两次舍入），双端 kernel 状态走满（fused=0xC1 / serial=0xB1 / pure=0xA1）
2. **性能**：输出 4 档梯度完整表格与**最佳划分**；理想 fused 稳态
   `Tfused ≈ Tpure + ΣAccumulate + 首chunk通信`（通信被计算掩盖，不再线性叠加）
3. **干净退出**：`destroy`/`uninit`/`MPI_Finalize` 无残留

> 风险注记（实机首跑关注）：① tiling 起步值 baseM=chunkRows / baseN=128 / baseK=64、
> depth=1 为保守配置，未做性能调优；② B 以 ND 行主序直摆依赖 dav-2002 LoadData 支持，
> 若异常可改 B 按 `[N,K]` 摆放 + `isTransposeB=true`；③ CPU FP64 参考计算为一次性
> ~10s（可 OpenMP 加速），不计入设备侧计时。

## 运行（C++，实机验证）

```bash
# 1. 仓库根构建 + 示例编译(bisheng host+AICore 统一编译, 含 MPI)
cmake -B build -DXPU_TYPE=NPU -DBUILD_PYTHON=OFF && cmake --build build -j
cd examples/memory_pool/01_basic/09_310p_aicore_matmul_allreduce
#   MPI 取发行版一套(头/库/启动器须同源; 换 /usr/local/openmpi 时改用其路径与启动器)
export ASCEND_HOME_PATH=/usr/local/Ascend/cann-9.1.0
MPI_INC=/usr/lib/aarch64-linux-gnu/openmpi/include
MPI_LIB=/usr/lib/aarch64-linux-gnu/openmpi/lib
REPO=<仓库根目录>
bisheng --npu-arch=dav-2002 -O2 -std=c++17 -w -x asc 09_310p_aicore_matmul_allreduce.asc -x none \
  -o /tmp/ex09 -DOMPI_SKIP_MPICXX -I$MPI_INC -I$ASCEND_HOME_PATH/include \
  -I$REPO/src/smem/include/host -I$REPO/src/smem/include/device \
  -Wl,--start-group $REPO/build/src/smem/csrc/libmf_smem.a $REPO/build/src/hybm/csrc/libmf_hybm_core.a \
  $REPO/build/src/acc_links/csrc/libacc_tcp_net.a -Wl,--end-group \
  -L$MPI_LIB -lmpi -L$ASCEND_HOME_PATH/aarch64-linux/lib64 -lascendcl -lruntime -ldl -pthread -lm
# 可选: 大 K 档(计算规模 x4, 池 128MB): 编译命令追加 -DEX_KVAL=8196
#  (K=8196 经零填充对齐至 8320 实现——原生非对齐 K 在 dav-2002 ND 路径实测丢尾块)

# 2. 部署 kernel 到设备(部署链见开发进展文档附录 A.4)
# 3. mpirun 双 rank 运行(rank 由 MPI 分配)
export MF_310P_SHM_OVERLAP=1 MF_310P_OVERLAP_JSON=<libmf_overlap_v3.json 路径>
export LD_LIBRARY_PATH=$ASCEND_HOME_PATH/aarch64-linux/lib64:$MPI_LIB:$LD_LIBRARY_PATH
mpirun -np 2 /tmp/ex09   # root 用户加 --allow-run-as-root
# 期望输出: 双方 "=== EX09 rN PASS ===" + 各档梯度表 + r0 最佳划分
```

## 与相关示例/实验的关系

- **07**：host 编排（BM API + `HostDataOpAclMemcpy`），数据面全在 host 线程
- **08**：设备发起协同（SHM API + AICore×AICPU），但负载是 pattern 填充 + INC 读改写
- **09**（本示例）：08 的协同链路 + **真实 matmul 计算负载 + TP all-reduce 语义 +
  chunk 粒度梯度扫描**，面向"计算与通信融合"的量化选型
- 设计文档：本目录 `DESIGN.md`（内存布局/槽位分配/三模式状态机/并发正确性分析）
