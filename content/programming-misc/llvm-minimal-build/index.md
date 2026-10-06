---
title: "用 MinGW64 裁剪构建 libLLVM-23.dll：109MB 压到 10.9MB 的完整实录"
menuTitle: "LLVM 裁剪构建实录"
date: 2026-10-06T22:17:48+08:00
draft: false
weight: 50
tags: ["LLVM", "MinGW", "MSYS2", "PE/COFF", "链接器", "体积优化"]
categories: ["编程开发", "编译工具链"]
description: "在 MSYS2 MinGW64 下把 LLVM 23.1.2 裁剪成一个只保留 X86 后端、读 IR→优化→输出 ELF/COFF/ASM 的 libLLVM-23.dll：静态运行时、仅依赖 7 个系统 DLL、只导出 1093 个 C 符号，体积从 109.8MB 一路压到 10.9MB。记录 PE 导出上限、-Os 被覆盖、whole-archive 抵消 gc、组件级裁剪等全部踩坑。"
aliases: ["/llvm-minimal-build/", "/programming-misc/llvm-minimal-build/"]
---

> **TL;DR**：LLVM 23.1.2 + MSYS2 MinGW64（GCC 16.2.0），只保留 X86/x64 后端与 IR 读入、优化管线、ELF/COFF/ASM 输出。最终产物 **libLLVM-23.dll 10.9MB**：静态链接 libgcc/libstdc++（运行时不依赖它们），导入表只剩 7 个系统 DLL + msvcrt，导出表只有 1093 个 C 符号（`LLVM*`/`llvm_*`），全链路冒烟测试 PASS。

---

## 目标

需要一个"最小可用的 LLVM 动态库"，能力边界是：

- **输入**：LLVM IR（文本/bitcode）
- **处理**：优化管线（`default<O2>` 这类 pipeline）
- **输出**：X86/x64 后端的 **ELF / COFF 目标文件**和 **汇编文本**
- 不需要：前端（clang 不内置）、JIT、调试信息生成（CodeView/DWARF 因 AsmPrinter 硬依赖去不掉，但 PDB 等已裁）、反汇编、JIT、LTO、链接器

硬约束：

- 只要一个 DLL，exe 工具不要（`LLVM_BUILD_TOOLS=OFF`）
- DLL 自身静态链接运行时：**不允许依赖 libstdc++-6.dll / libgcc_s_seh-1.dll / winpthread-1.dll 及任何第三方**
- 导出面收敛：完整 C++ API 有 7 万多个导出符号，会撞 PE 的 65535 上限，只导出 C API（1093 个）就够了

## 环境

- 源码：LLVM 23.1.2，TUNA 镜像 `llvm-project-23.1.2.src.tar.xz`（170.9 MiB，GitHub 直连不通）
- 工具链：MSYS2 `mingw64` 仓库 —— gcc 16.2.0、GNU ld 2.47、cmake、ninja、upx 5.2.1
- 构建目录：独立 build 目录，脚本幂等可复跑

## CMake 配置骨架

```bash
cmake -S llvm -B build-mingw64 -G Ninja \
  -DCMAKE_BUILD_TYPE=MinSizeRel \
  -DCMAKE_C_FLAGS="-static-libgcc" \
  -DCMAKE_CXX_FLAGS="-static-libgcc -static-libstdc++" \
  -DCMAKE_EXE_LINKER_FLAGS="-static" \
  -DCMAKE_SHARED_LINKER_FLAGS="-static" \
  -DCMAKE_MODULE_LINKER_FLAGS="-static" \
  -DLLVM_TARGETS_TO_BUILD="X86" \
  -DLLVM_DEFAULT_TARGET_TRIPLE="x86_64-w64-windows-gnu" \
  -DLLVM_TARGET_ARCH=X86 \
  -DLLVM_BUILD_LLVM_DYLIB=ON \
  -DLLVM_LINK_LLVM_DYLIB=OFF \
  -DLLVM_BUILD_LLVM_C_DYLIB=OFF \
  -DLLVM_BUILD_TOOLS=OFF \
  -DLLVM_ENABLE_ZLIB=OFF -DLLVM_ENABLE_ZSTD=OFF \
  -DLLVM_ENABLE_TERMINFO=OFF -DLLVM_ENABLE_LIBXML2=OFF \
  -DLLVM_ENABLE_LIBEDIT=OFF -DLLVM_ENABLE_CURL=OFF \
  -DLLVM_ENABLE_HTTPLIB=OFF -DLLVM_ENABLE_FFI=OFF \
  -DLLVM_ENABLE_LIBPFM=OFF -DLLVM_ENABLE_PLUGINS=OFF \
  -DLLVM_ENABLE_PIC=OFF -DLLVM_ENABLE_ASSERTIONS=OFF -DLLVM_ENABLE_LTO=OFF
```

外部库全关是关键：zlib/zstd/libxml2/libffi/libcurl 一旦漏关，DLL 导入表里就会出现它们，白名单校验直接挂。

## 坑 1：PE 导出上限 65535 —— 只导出 C 符号

完整 LLVM C++ API 有 **73,526 个导出符号**。PE 的 Export Ordinal Table 是 16 位索引数组，上限 65535：

- GNU ld：`ld.exe: error: export ordinal too large: 73526`
- lld 23 有硬检查：`too many exported symbols (got 73525, max 65535)`

尝试过的路线：给 lld 打补丁放宽检查（重链时 tblgen 缺失，废弃）、`LLVM_BUILD_LLVM_DYLIB_VIS` 隐藏可见性（AddLLVM.cmake 的条件要求编译器是 Clang，MinGW+GCC 下无效，且 LLVM C++ 公开 API 没有任何可见性标注）。

**最终方案**：给 llvm-shlib 的 MinGW 分支换成 version script，只导出 C 符号：

```cmake
# tools/llvm-shlib/CMakeLists.txt 中 MINGW 分支
target_link_options(LLVM PRIVATE LINKER:--version-script=${CMAKE_CURRENT_SOURCE_DIR}/c-exports.map)
```

```text
/* c-exports.map */
{
  global: LLVM*; llvm_*;
  local: *;
};
```

结果：导出符号从 7 万级收敛到 **1093 个**，全部是 `LLVM*` / `llvm_*` C 接口，无 C++ mangled 名。

## 坑 2：-Os 不生效 —— 用 MinSizeRel 绕开

LLVM 顶层 `CMakeLists.txt` 对 MinGW + GCC 强制做：

```cmake
if( MINGW AND NOT "${CMAKE_CXX_COMPILER_ID}" MATCHES "Clang" )
  llvm_replace_compiler_option(CMAKE_CXX_FLAGS_RELEASE "-O3" "-O2")
endif()
```

`llvm_replace_compiler_option` 的逻辑：有 `-O3` 就替换成 `-O2`，没有就**追加** `-O2`。所以无论你在 `CMAKE_CXX_FLAGS_RELEASE` 里怎么传 `-Os`，最终命令行都是 `-Os ... -O2`，GCC 取最后一个优化选项，`-Os` 被吃掉。

这个分支只动 `CMAKE_CXX_FLAGS_RELEASE`，**不碰 MINSIZEREL**。所以直接 `-DCMAKE_BUILD_TYPE=MinSizeRel`，GCC 默认的 `-Os -DNDEBUG` 原样生效，无需改源码。

## 体积路线：109.8MB → 10.9MB

### 第一刀：组件级裁剪（60 → 39）

llvm-shlib 用 `llvm_map_components_to_libnames` 展开 `LLVM_DYLIB_COMPONENTS`，链接命令是：

```cmake
set(LIB_NAMES -Wl,--whole-archive ${LIB_NAMES} -Wl,--no-whole-archive)
```

这里有一个很有用的性质：**whole-archive 下链接能过 = 所有被引用的符号都齐**。砍掉一个组件如果别的库真的引用它，链接器立刻报 undefined reference。所以"砍组件 → 重链 → 报 U 就回填"是安全且可验证的流程。

砍掉的是（每批都过了链接 + 冒烟）：

- **反汇编**：MCDisassembler（我们只"汇编/输出目标文件"，不反汇编）
- **前端组件**：FrontendHLSL / FrontendOpenMP / FrontendOffloading / FrontendAtomic / FrontendDirective（给 clang 用的：HLSL 着色器、OpenMP offload、GPU offload、C++ 原子等）
- **调试格式**：DebugInfoPDB / DebugInfoMSF / DebugInfoGSYM / DebugInfoBTF（保留 DWARF + CodeView —— AsmPrinter 硬依赖）
- **YAML 序列化**：ObjectYAML（6.5MB 的静态库，obj2yaml/yaml2obj 工具专用）
- **其他非路径库**：Symbolize / TextAPI / SandboxIR / CGData / CFGuard / HipStdPar / Linker / MCParser / IRPrinter / ObjCARCOpts

保留的 39 个都是硬需求，按静态库体积排序的大头：CodeGen(30MB) > Analysis(18MB) > ipo(15.6MB) > X86CodeGen(14.7MB) > Core(13.5MB) > Passes(13.2MB) > Vectorize(13MB) > ScalarOpts(12.7MB)… 其中 **GlobalISel** 去不掉（X86CodeGen 里的 X86LegalizerInfo 等对象引用它）、**DWARF/CodeView** 去不掉（AsmPrinter 编译引 DwarfDebug/CodeViewDebug）。

### 第二刀：gc-sections 的两连败

先说结论：**这条路在 MinGW + whole-archive 的组合下走不通**，记录如下以免后人重复踩。

**尝试 A：`--gc-sections` + whole-archive**。flags 加 `-Wl,--gc-sections`（编译本来就带 `-ffunction-sections`），重链后 DLL 大小纹丝不动 —— whole-archive 把所有对象的节都标记成了 GC 根，裁剪被整体抵消。

**尝试 B：`--start-group/--end-group` 换掉 whole-archive**（按需拉对象，理论上 gc 就能生效）。实测链接"成功"，但 DLL 只有 **20KB 空壳**。原因：start-group 按需拉对象的依据是 **undefined 引用**，而 C API 的 1093 个导出符号全部是"已定义在静态库里、等着被导出"的符号，libllvm.cpp 里没有任何东西引用它们，version script 的 `LLVM*` 通配符**不产生拉取动作**。于是 ld 一个库对象都没拉。

回滚到 whole-archive，体积瘦身最终靠的是**组件级裁剪**（第一刀）。

### 第三刀：strip + UPX

符号表占了约 59MB（未 strip 的 DLL 文件大小 102.8MB，strip 后 40.4MB）。交付物 strip 掉符号表，再用 UPX 压缩：

```bash
strip libLLVM-23.dll
upx --best --lzma libLLVM-23.dll
```

## 最终结果

| 阶段 | 60 组件 -O2 | 60 组件 -Os | 39 组件 -Os |
|---|---|---|---|
| 原始 | 109.8 MB | 102.8 MB | 97.3 MB |
| strip | 60.0 MB | 40.4 MB | 38.3 MB |
| UPX --best --lzma | 23.6 MB | 11.4 MB | **10.9 MB** |

硬验收（objdump 实测）：

- **导入表**：仅 `ADVAPI32.dll` `KERNEL32.dll` `msvcrt.dll` `ntdll.dll` `ole32.dll` `SHELL32.dll` `WS2_32.dll` —— 无 libstdc++/libgcc/winpthread/zlib/libzstd/libxml2/libffi
- **导出表**：1093 个，全 `LLVM*`/`llvm_*`，无 C++ mangled
- **内部完整性**：`objdump -t` 无 `*UND*` 悬空引用（注意：`nm` 读 PE DLL 会误报几千个 U，以 objdump 为准）
- **功能冒烟**：C API 全链路 `LLVMParseIRInContext2` → `LLVMRunPasses "default<O2>"` → `LLVMTargetMachineEmitToMemoryBuffer`，三路输出全部 PASS：
  - `x86_64-pc-windows-msvc` → COFF `.obj`
  - `x86_64-pc-linux-gnu` → ELF `.o`
  - 同上 triple → 汇编文本 `.s`

## 给 C 客户端的三条提醒

写 C 程序链接这个 DLL 时，LLVM 23 的 C API 有几个坑（都是实测段错误换来的）：

1. **用 `LLVMParseIRInContext2`**，不要用废弃的 `LLVMParseIRInContext`（废弃版内部对 MemBuf 做 take-ownership，极易悬垂）
2. **`LLVMRunPasses` 的 PassBuilderOptions 参数必须非 NULL**：23 版实现直接解引用它（`PassOpts->DebugLogging`），传 NULL 必段错误。用 `LLVMCreatePassBuilderOptions()` 创建、`LLVMDisposePassBuilderOptions()` 释放
3. `LLVMGetTargetFromTriple`、`LLVMTargetMachineEmitToMemoryBuffer`（注意带 ModuleRef 参数）、`LLVMGetVersion` 都是 23 版签名，按头文件声明写

## 附：39 组件清单

```text
Core;Support;Demangle;TargetParser;BinaryFormat;BitstreamReader;BitReader;BitWriter;
AsmParser;IRReader;Option;Passes;Analysis;ScalarOpts;InstCombine;AggressiveInstCombine;
ipo;Vectorize;TransformUtils;Instrumentation;Coroutines;ProfileData;Remarks;Target;
CodeGen;CodeGenTypes;SelectionDAG;GlobalISel;AsmPrinter;MC;Object;X86CodeGen;
X86AsmParser;X86Desc;X86Info;DebugInfoDWARF;DebugInfoDWARFLowLevel;DebugInfoCodeView;TableGen
```

> 注：`TableGen` 组件会被 llvm-shlib 的 CMakeLists 用 `list(REMOVE_ITEM ...)` 排除（它只服务内部 tblgen 工具），所以 DLL 里其实没有它。
