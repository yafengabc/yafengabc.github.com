---
title: "第 4 章：项目状态与 roadmap"
menuTitle: "第 4 章 项目状态"
date: 2026-10-06T12:50:00+08:00
draft: false
weight: 5
tags: ["goc", "roadmap", "项目状态", "C23", "LLVM", "验证"]
categories: ["编程开发", "goc"]
description: "goc 实测状态：Windows 腿全绿，C23 实用子集全部落地，LLVM 后端可用但仍是实验性。本文列出能做什么、不能做什么、正在进行什么，以及一个「26 个失败其实全是环境缺依赖」的例子。"
---

前面四章讲的是"怎么用"。这一章讲**"现在到什么程度了"**——包括我实测跑出来的数字，和一个值得单独拿出来说的教训。

所有数字核对于 2026-10-06，goc 提交 `f200cd1`，版本串 `dev (021274f, dirty)`。

## 一句话状态

| 维度 | 状态 |
| --- | --- |
| 默认后端（自研 x86-64） | ✅ **Windows 腿全绿**，回归 465 pass |
| Linux 后端 | ⚠️ 代码在，但本机无法验证（缺 Unicorn/QEMU，见后文） |
| LLVM 后端（gocl） | 🔧 实验性第二后端，产物更小，建议先验证再用 |
| 语言标准 | ✅ C99 主体 + C11 类型系统 + **C23 实用子集**全部落地 |
| 验证纪律 | ✅ **罕见地严格**（这才是 goc 最大的工程亮点） |
| 最新发布 | `v0.1.1`（2026-09-30） |

## 已经做到的

### 语言特性

**C99 全量**：`//` 注释、变参、可变参宏、数组指示符 `[i]=`、指定初始化器 `.field=`、混合嵌套、`long long`、`_Bool`、`restrict`、复合字面量 `(T){...}`、十六进制浮点 `0x1.8p3`。

**C11 类型系统现代化**：`_Thread_local`（goa **原生 TLS**，TLS 目录 + `gs:[0x58]`，不是软模拟）、`_Static_assert`、`_Alignas`/`_Alignof`、`_Noreturn`、`_Generic`、`_Atomic` + `<stdatomic.h>`（`++`/`--` 发 `lock xadd`，其余走 `lock cmpxchg` 重试循环）、`<threads.h>`（Windows/Linux 双平台）、`<uchar.h>`。

**C23 实用子集**（13 项 MVP 优先集全完成，还超额做了几个）：`bool`/`true`/`false`、`typeof`/`typeof_unqual`、`nullptr`/`nullptr_t`、`constexpr`、`auto` 类型推导、`enum E : int` 底层类型、空 `{}` 初始化、`[[...]]` 属性（含 deprecated/nodiscard 警告）、`u8` 前缀、二进制字面量 + 数字分隔符、`#elifdef`/`#elifndef`/`#warning`、`#embed`、`__has_include`、`__has_c_attribute`、`__VA_OPT__`、`stdckdint.h`、`strdup`/`strndup`。

**`<stdbit.h>`**（C23 7.18）：14 个 `stdc_*` 泛型宏 × 5 个宽度族共 **70 个函数**，含 0/全 1 边界与"最高位下标 +1"规则。这个纯库工作顺带修了三个前端类型缺陷（整型提升 / 一般算术转换 / 字面量后缀定型）。

### 特别值得一提：`_BitInt(N)`

这是**唯一能把 goc 从"玩具编译器"推向"真能算东西"的特性**。

goclib 里实现了完整的大数运行时：schoolbook + **Karatsuba** 乘法、**Knuth Algorithm D** 除法、十进制字符串转换，按需分配 scratch。

实测成绩：**用 Chudnovsky 二分公式算 π 到 10 万位，100,011 位逐位对拍 Python 大整数通过，耗时 15.5 秒。**

作为对照，博客里那篇 [Go vs Nim 大数运算](/programming-misc/go-vs-nim-bigint/) 里的数据：Go `math/big` 算 100 万位 π 要 8104 ms。自研的 goclib 在 15.5 秒内出10 万位——虽然位数少 10 倍，但这是**一个不依赖任何外部库的纯 C 实现**，而且能通过逐位对拍验证正确性。

值模型是 `ceil(N/64)` 个 64 位小端字，按地址传值。有个坑：**回绕负数跨宽度必须经同宽 signed 类型符号扩展**（`typedef signed _BitInt(N)`），否则模 2ᴺ 同余会被破坏。

### 平台

- **Windows PE32+**：`-mwindows` 支持 GUI subsystem（双击无黑框）
- **Linux ELF64**：静态，无一条动态链接，只用 `write`/`read`/`brk`/`exit_group`
- 8 个 Windows API 头文件：`windows.h` `windef.h` `winbase.h` `winuser.h` `wingdi.h`
- 原生 GUI 示例：MessageBox、注册窗口类 + `GetMessage` 消息循环 + `WM_PAINT`

### 内联汇编

`__asm { ... }` 块，块内的裸 C 变量名会被绑定成对应的内存操作数：参数/局部变量 → `[rbp±off]`，全局/static → `[rip+G_x]`。自己写括号的 `[x]` 保持原样，不会套成 `[[rbp-8]]`。

## 还没做到的

诚实清单：

| 缺口 | 实际影响 |
| --- | --- |
| **没有独立链接阶段** | 不能消费 `.o`，不能链接多个目标文件（但可以一次编多个 `.c`） |
| `printf` **宽度一概忽略** | `%5d` → `42`，`%02x` → `7`。与标准 C 的明确差异，代码里有意为之 |
| `printf` **单次超512 字节截断** | `sprintf` 跟真货一样不做边界检查 |
| 没有 `%e %a %n` | 科学计数法输出不可用 |
| `%g` 是简化版 | 按小数位计数，不按有效数字、不切科学计数法 |
| 库里没有 `scanf`、文件 I/O、`math.h`、`time.h` | 纯计算与IO 场景受限（`wchar` 族后来补上了） |
| `long double` 降级为 `double` | 标 `__goc_long_double_is_double` 宏 |
| 没有 VLA | 变长数组不行 |

## 正在进行：LLVM 后端

`gocl` 是第二条后端，架构已定案（2026-10-04）：

```
C 源码 ──[front end]──►Program
       │
       ▼ -fllvm
  genLLVMAll：用户函数 + goclib 全部函数 + 全局变量 统一降为单个 LLVM IR 模块
       │
       ▼ libLLVM 23.1.2（纯 Go 无 cgo 绑定，syscall.LazyProc）
  COFF 对象（与 .text/.data 同段布局，重定位走 goa 的 Fixup 模型）
       │
       ▼ goa 链接
  .exe (Win)
```

设计原则是**一个符号只由一个生成器拥有**——避免"按函数分给两个生成器"带来的符号表分歧。这条原则的来源很实在：全局名、未定义符号归属、变参格式串改写，每一个都曾导致链接错误或**静默错误答案**。

已实现：管线打通、全量 IR 生成、符号所有权闭环（`claimed` 传给 goa 只合成入口桩）、COFF 链接端到端、变参降到 LLVM intrinsic。

**但 P0 缺口没修完**，主要是"IR 宽度一致性"——个别 goclib 函数 IR 生成报类型错误（`i32` 用在 `i64` 运算），集中在整型常量、窄类型提升、指针运算。

还有一个已知限制：**Linux ELF 对象还没做**，`compileIR` 对 linux 目标直接报错，所以 gocl 目前只能出 Windows PE。

### 用gocl 之前先做一件事：跟默认后端对一遍

gocl 是第二后端，产物更小，但要用它得先确认它**算得对**。现成的办法是拿它和默认后端逐字节对比输出：

```bash
./bin/goc.exe  run src/examples/stress.c > ref.txt   # 默认后端当基准
./bin/gocl.exe -o s.exe src/examples/stress.c && ./s.exe > got.txt
diff ref.txt got.txt        # 必须为空
```

`stress.c` 覆盖了整型/浮点/结构体/varargs 各类混合运算，是个不错的冒烟用例。gocl 还支持把 IR dump 出来，怀疑某个表达式的生成结果时可以直接看：

```bash
./bin/gocl.exe -dump-ir -o s.exe hello.c
```

输出是标准 LLVM IR，可以用 clang 编译同一份 IR 做参照，对比 codegen 结果。

> gocl 目前是实验性后端（roadmap 里 P0 还有缺口），默认后端完全不受影响。

顺带一个体积观察：**gocl 的产物比自研后端小 27%~42%**，且 `-O0` 档就等于自研后端 `-O1` 的水平——LLVM 的 SSA + GVN 是免费的优化。详见第 3 章。

## 一个值得单独讲的教训：26个"失败"

我在 `src/goa` 跑 `go test ./...`，失败了：

```
--- FAIL: TestATTJumpTableExecutes (1.54s)
    att_jumptable_test.go:100: peun.py failed: exit status 1
    ModuleNotFoundError: No module named 'unicorn'
```

跑 `gocregress`，`pass=465 fail=25`。**26 个失败。**

第一反应是"项目有 26 个 bug"。但把失败项列出来之后，规律非常整齐：

```
8 个 Linux 用例 × 3 个优化档 + 1 个 goa 单测
失败用例：bitint  c23_string  fileio  goclib  goclib2  goclib_test  headers  libmisc
```

然后去查 unicorn：

```bash
$ python -c "import unicorn"
ModuleNotFoundError: No module named 'unicorn'
$ /d/msys64/ucrt64/bin/python.exe -c "import unicorn"
ModuleNotFoundError: No module named 'unicorn'
```

**26 个失败全部源自本机缺一个 Python 模块，与代码无关。** Windows 腿全绿；Linux 腿需要 Unicorn（QEMU 的 TCG 翻译核心）执行 ELF 来验证，而本机两个 Python 都没装。

goc 的 README 里写明了它的立场：

> 找不到能 `import unicorn` 的 Python 时，脚本会**大声跳过** Linux 腿而不是假装通过。

以及那句更狠的：

> 手写解释器最多证明 codegen 和自己一致，证明不了程序真的对。

这个设计在我这次实测里**救了我**——如果它像很多项目那样"环境缺失就静默跳过"，我会看到 `pass=491 fail=0` 然后错误地宣布"goc 全部测试通过"。而现在它给的是 26 个红色失败，逼着我去查清楚为什么。

顺带说，README 里还记着一件事：Ubuntu 那一腿**以前从来没真正跑过**。job 的构建步骤里混进了 `tools/msgboxcheck`——它是驱动真实Windows 对话框的程序，只存在于Windows，于是 Linux 上 `go build` 直接"build constraints exclude all Go files"失败，整个 job 在到达 e2e 之前就结束了。

>所以过去所有"Linux 目标经过真内核验证"的说法，其实没有任何一次 CI 跑来验证过。

修好后第一次跑就是 42/42 全绿，而且产物字节数与本机 Unicorn 下**逐个相同**（`fp` 两边都是 14392、`phase1` 都是 4030）——**代码生成是确定的，无关宿主**。

这类"验证本身没被验证"的 bug 是最危险的：它让整个测试体系形同虚设，而且**看不出来**。

## 验证体系

goc 的测试设计值得单独说，因为这是它最扎实的部分。

### 三层分工明确

| 工具 | 职责 |
| --- | --- |
| `tools/elfcheck -structure-only` | **结构**校验：magic / class / 类型 / 机器 / entry 是否落在可执行段内 / `p_vaddr ≡ p_offset (mod p_align)` / 节表与 `.shstrtab` 自洽 |
| `tools/ucrun.py`（Unicorn/QEMU） | **裁定程序输出是否正确** |
| CI 的 Ubuntu job | **真实内核**上直接 exec ELF |

`elfcheck` 只做结构校验，因为"这一层与指令语义正交，并且是我们自己的断言"。而"谁裁定程序输出对不对"的答案是 Unicorn——不是手写解释器：

> 解释器就算跑对了，证明的也只是"和我们对 ISA 的理解一致"。

README 记的那个例子是这套纪律的价值：

> 历史上 `phase1.c` 有一段把栈指针塞进 `int` 的未定义行为，手写解释器对未映射地址一律返回 0，于是"输出逐字节正确、退出码 0"地掩盖了它；换 Unicorn 跑第一次就段错误在 `0xffffffffffe78`（Linux 栈地址超过 2³¹，32 位截断后符号扩展成了负地址）。

**宽容的验证器会系统性掩盖 bug。** 这条经验值得任何做底层项目的人抄。

### 优化护栏：同一份 golden

这是我认为设计上最漂亮的一点。

```
-O0  输出与历史管线逐字节相同   ← 所以 golden 永远成立
-O1  起才开始跑优化 pass
```

`-O1`/`-Os` 腿拿**的是和 `-O0` 同一份 golden**：

> 任何 pass 改变可观察输出都在这里炸

对比很多项目的做法——"优化后跑测试，失败就更新期望值"——那会让优化 bug 悄悄变成新基线。goc 的做法是：**优化只能"不许改变可观察输出"**，期望值永远来自 `-O0` 那条未优化的路径。

`-O0` 字节不变性是硬性护栏，新加的 C23 特性都是语法/类型层改动，不碰优化管线，所以这条底座一直成立。

### 规模化

- **85 个 C 示例**、**81 份 golden**
- 回归基线 `gocregress pass=475`（81 example × 6 腿：Win/Linux × O0/O1/Os）
- 本机全量 `run_tests.sh` → `pass=253 fail=0`
- 两个 CI job 是**分工**不是重复：Windows runner 跑 Windows 目标 + goa 套件 + 三模块单测；Ubuntu runner 在真内核上跑全部 ELF

## Roadmap 判断

按 `docs/` 里的几份路线图，剩余工作大致是：

**短期（收尾性质）**
- `_Atomic` 的 `fetch_*` 族仍缺（已有 `lock xadd`/`cmpxchg` 基础设施）
- TLS 测试钩子：`tls_basic` 还没 golden，regress 里 SKIP

**中期（LLVM 后端）**
- P0：IR 宽度一致性（整型常量、窄类型提升、指针运算的类型推导）
- P0：Linux ELF 对象（现在 `compileIR` 对 linux 直接报错）
- P1：变参运行期验证、浮点/字符串初值回归
- P2：位域的 IR 降级（现在按名排除含位域访问的函数）

**长期（锦上添花）**
- `long double` 软降级
- 完整内存序
- 跨块 copy-prop + 寄存器级分配

从提交历史看，最近两周的节奏很清楚：**先补 C23 特性 → 再做 LLVM 后端 → goclib 从"够用"扩成"通用 C 库"**（最近一个提交 `dc0f8e6` 就是"让 goclib 成为可被 gcc/clang 编译的通用 C 库"）。

## 建议怎么用

**适合**：想写C 程序但不想被工具链绑架的人；研究编译器/代码生成；嵌入式与体积敏感场景；Win32 底层实验；离线工具（丢一个 exe 到任何 Win10 上就能跑）。

**不适合**：生产项目（缺文件 I/O、math.h、scanf，printf 有限制）；Linux 开发（本机验证链路要额外装 unicorn）；想要 gcc 兼容性。

如果你是第一次试：

```bash
git clone https://github.com/yafengabc/goc.git && cd goc && bash build.sh
./bin/goc.exe run src/examples/hello.c
./bin/gocl.exe -o x.exe src/examples/stress.c     # 体验第二后端
python -m pip install unicorn                     # 想跑 Linux 腿就装这个
bash run_tests.sh
```

## 小结

goc 的技术亮点不在"又一个编译器"，而在**三条硬指标 + 一套验证纪律**：

1. 依赖表只有kernel32/user32——每字节可解释
2. `print` 内建 2048 字节，比 gcc 小 94.7%
3. C23 实用子集全落地，`_BitInt(N)` 能算 10 万位 π

而最值得学的其实是第 4 条：

4. **验证器不宽容、不静默、不自我美化**——缺依赖就大声失败，验证链本身也要被验证

第 4 条才是能撑住前三条的东西。

---

> 本章数字核对于 2026-10-06，goc 提交 `f200cd1`（版本串 `021274f`，工作区 dirty）。回归失败数（465/25）**与本机环境有关**，装了 unicorn 后 Linux 腿才能真正验证——见后文与README 的《测试》一节。