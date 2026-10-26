# 310P3 CANN9.1 SetAccess 逻辑/物理 DeviceID 修复｜原始源码恢复档案

> **来源**：历史服务器本地但未推送的提交 `5802f55026c16e153e783d8ef26a29f813d63dd9`（2026-10-23，`fix/310p-setaccess-physical-devid-20261023`）。2026-10-27 R0 在服务器独立工作树找回 Patch 和字节一致的原修复 Host SO。**本次 GitHub 提交SHA必然不同于旧本地 SHA**；3个源码文件均依据原 Patch 应用并用 **Git Blob SHA** 与 Patch index 的原提交目标对象逐一比对，结果全部一致。没有新增 CANN/NPU 实测，不能将远端归档视作新验证。
>
> 现场取证源：[R0 原始Patch](https://github.com/10wvw01/vllm-ascend/blob/work/310p-mm-ar-large-m-a1-20261026/csrc/_310P/memfabric_mm_ar_v2/evidence/a1_r0_reentry_20261027/memfabric_fix_5802f550_setaccess_physical.patch) / [R0 源码溯源](https://github.com/10wvw01/vllm-ascend/blob/work/310p-mm-ar-large-m-a1-20261026/csrc/_310P/memfabric_mm_ar_v2/evidence/a1_r0_reentry_20261027/source_provenance.json) / [R0 验收](https://github.com/10wvw01/vllm-ascend/blob/dev/310p-memfabric-mm-ar/csrc/_310P/memfabric_mm_ar_v2/docs/A1_BATCH_R0_ACCEPTANCE_20261027.md)。

## 背景、修复对象和验证边界

在历史310P3 / CANN **9.1.0** 非identity设备可见映射（如`ASCEND_RT_VISIBLE_DEVICES=2,3`、`6,7`）下，设备进程使用逻辑ID0/1；历史实测`aclrtMallocPhysical`使用逻辑设备号，而`aclrtMemSetAccess`的`aclrtMemAccessDesc.location.id`需要物理设备号。旧库对两API都传逻辑号，非零起点映射下Meta区授权曾触发`507899 DRV_INTERNAL_ERROR`。

原提交只添加`HybmPhysicalDeviceIdOf`环境解析辅助函数、转换三个`AclrtMemSetAccess`的`location.id`：`hybm_gva.cpp` Meta授权1处、`hybm_acl_vmm_segment.cpp` Own/Imported段授权2处。**不修改`aclrtMallocPhysical`逻辑ID、通信协议、IB/SDMA、KVER11或AICPU Kernel。**

此处严格恢复**历史原始语义**：可见列表unset/空、解析失败或列表不足时函数退回identity；合法列表按索引选出物理ID。新的[另一仓Draft替代实现](https://github.com/10wvw01/memfabric_hybrid/pull/1)采用不同的非法输入fail-closed规则，**并不与历史修复源码或SO等价**；本仓以已经过历史实机验证的原源码为准，未来任何改变该语义的版本都必须重新审查和实机验证。

## 字节级代码身份校验（Git Blob）

| 原Patch文件 | 原始基底 Git Blob | 目标 Git Blob SHA1（2026-10-27本仓复核） | 结果 |
|---|---|---|---|
| `src/hybm/csrc/common/hybm_define.h` | `2a8aa6189eb8ae0028f9eda36b46905ebe9ad70a` | `8ec9e3f15fa04b2385d927029b9fe8b8a85ffa8b` | **原目标对象一致** |
| `src/hybm/csrc/driver/hybm_gva.cpp` | `eaf752d931ca1839d22d02935f1aaa6d046f3365` | `1221fb719c34110be0e57b27c27b7ff14ffeccbc` | **原目标对象一致** |
| `src/hybm/csrc/mm/hybm_acl_vmm_segment.cpp` | `fa16a644d1bcc5705780760f363d2a4c80bc816a` | `8ffcb281747ba8ac503c57d78b21e4d0020bc2d9` | **原目标对象一致** |

这三个 Git Blob 全哈希的前缀分别与原始Patch的 `index 2a8aa618..8ec9e3f1`、`eaf752d9..1221fb71`、`fa16a644..8ffcb281` 完全一致，且恢复后从远端再次读取得到对应全哈希。**并非仅凭函数名相似判为等价。**

## 已有历史产物和本次部署策略

- 2026-10-23 的历史修复Host SO SHA256 为`1dc46d7dcbe583a5dbb78b4c65a0b5e22f9f352976c53ed31b761fa744f91a30`，服务器本地两个归档副本字节一致；历史S3-R/S3-R2用进程级装载验证过非零映射、KFC首调与双Rank25次短请求。这是**历史二进制**，不能因为本次GitHub源码恢复就推断重新编译出的SO也会字节相同。
- R0检查时`/usr/local/memfabric_hybrid/1.2.0/aarch64-linux/lib64/libmf_hybm_core.so`及Python`site-packages/memfabric_hybrid/lib/libmf_hybm_core.so`仍为**未修复**`88d392850733d7f166ce28615a8fdbf164b604979df9d3fc8cd0c10c64fd7bc1`；不得不经另行审批直接覆盖共享服务SO。
- 选择与其它任务隔离的进程/装载路径，必须证明**实际dlopen/动态依赖装入**历史正确Host SO，并单独核对真正的V2 D4 Device SO（`2a19390c…`）和Python插件（`8eb65de9…`）。仅靠`LD_LIBRARY_PATH`或只校验候选路径的SHA都不构成实际装载证明。
- 310P3单卡双Die选卡要G0复验；KVER11/KFC不允许预部署，设备池和首次Smoke必须独立获得用户授权。新CANN版本、不同ABI、新构建产物SHA则重新评审，不沿用旧S3-R2自动GO。

## 维护约束

**以本仓源代码作为后续修复事实来源**，保留原服务器`5802f550`与这次GitHub新提交不同的身份记录；审查和构建优先复用相同源码/历史正确产物。任何更新不能无审查改变三处授权边界或用单卡0/1通过掩盖2/3失败。非此修复的历史KVER与KFC变更已经存在于基线`131ee5ff`，无需再提交第二份。
