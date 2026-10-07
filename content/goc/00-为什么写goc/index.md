---
title: "第 0 章：为什么再写一个 C 编译器"
menuTitle: "第 0 章 为什么写 goc"
date: 2026-10-06T12:10:00+08:00
draft: false
weight: 1
tags: ["goc", "C 编译器", "Go", "LLVM", "设计取舍"]
categories: ["编程开发", "goc"]
description: "编译器套娃已经很多了，为什么还要用 Go 写一个 C 编译器？goc 的答案是四条取舍：不依赖 gcc、不依赖 libc、不假设 CPU 行为、交付物只有一个文件。本文讲清这个定位怎么决定了整个项目形态。"
---

市面上的"X 语言实现的 Y"已经很多了：Go 实现的编译器、Brainfuck 实现的 C、Python 实现的 Python。为什么还要用 Go 写一个 C 编译器？

因为**大多数编译器套娃是玩具**——能跑 Hello World，能过 OJ，然后卡在 struct 上。而 goc 从第一天起就锚定了一个具体目标：

> **编译出来的 C 程序，产物里既没有 gcc 的痕迹，也没有 libc。**

这一章讲清这个目标怎么决定了 goc 的整个形态。

最后还要加一条不那么容易体现在功能列表里、但日常开发天天遇到的：**编译器本身也得是便携的**。拷一个文件过去就能编 C，不装 LLVM、不配环境变量、不用管 C 库在哪。这是"取舍四"，本章最后讲。

## 先看它有多极端

```
  foo.c ──[goc]──> foo.asm ──[goa]──> foo.exe    Windows PE32+，只导入 kernel32
              └─[goc -target linux]─[goa -f elf]─> foo    Linux ELF64，只用 syscall
```

注意这里的"只导入 kernel32"不是说法。实测一个弹 MessageBox 的程序：

```bash
$ objdump -p msgbox.exe | grep "DLL Name"
	DLL Name: kernel32.dll
	DLL Name: user32.dll
```

**没有 msvcrt。** 没有 CRT 启动代码，没有 `__stdio_common_vfprintf`，没有隐式链接的其它东西。用 MSYS2 的 gcc 编同样程序，依赖表里会多出一长串。

Linux 侧更彻底：静态 ELF，**一条动态链接都没有**，只用 `write` / `read` / `brk` / `exit_group` 四个 syscall。

## 取舍一：不要 gcc

"不要 gcc"这件事本身不难——用 Go 写就天然没有 gcc 依赖。难的是**它连汇编器都不用**。

goc 的 x86-64 代码生成是自研的（`src/goc/codegen.go` 等），汇编器 **goa** 也是自研的纯 Go 程序。中间表示是 goa 自己的 COFF 对象格式，重定位模型自己实现。

这么做换来的是一个完整的、不依赖任何外部工具的闭环：

```bash
bash build.sh    # goc + goa + 两个验证工具，全是 Go，一条命令
```

代价当然是难度。README 里提到的那次调试很能说明问题：

> Windows 上没法 exec ELF，所以本机这一腿交给 QEMU 的 CPU 核心（Unicorn）。历史上 `phase1.c` 有一段把栈指针塞进 `int` 的未定义行为，手写解释器对未映射地址一律返回 0，于是"输出逐字节正确、退出码 0"地掩盖了它；换 Unicorn 跑第一次就段错误在 `0xffffffffffe78`。

这段值得反复读。一个自研的验证器，如果对"非法内存访问"宽容，就会**系统性地掩盖 bug**。Unicorn 是 QEMU 的 TCG 翻译核心做成库，是真指令语义、真标志位、真地址检查。

## 取舍二：不要 libc

这是 goc 最见功力的地方。

`printf` 在 gcc 里不是一个函数，是半个运行时：格式化、缓冲、locale、浮点转换、wchar 支持……链进去就是几十 KB。

goc 的做法是：**把 printf 当成一个真正的库函数，用 C 写出来**。

```
goclib/
├── os.c          5 个平台原语，唯一碰 OS 的文件
├── stdio.c       printf sprintf puts putchar getchar
├── stdlib.c      malloc free calloc atoi abs strtol rand srand exit
├── string.c      18 个字符串/内存函数
└── ctype.c       13 个字符分类函数
```

一共 45 个函数。**只有 `os.c` 里的 5 个原语碰操作系统**：

| 原语 | Windows | Linux |
| --- | --- | --- |
| `__goclib_write(buf,len)` | `GetStdHandle` + `WriteFile` | `write`(fd=1) |
| `__goclib_exit(code)` | `ExitProcess` | `exit_group`(231) |
| `__goclib_heap_alloc(size)` | `GetProcessHeap` + `HeapAlloc` | `brk` bump allocator |
| `__goclib_heap_free(p)` | `HeapFree` | 空操作 |
| `__goclib_read(buf,len)` | `GetStdHandle` + `ReadFile` | `read`(fd=0) |

关键在于**按需发射**：goc 启动时把整个库当普通 C 程序编译，函数体按需发射——程序实际调用到的函数（及其传递闭包）才进产物。

所以只用 `putchar` 的程序，不会背上 `printf` 的 512 字节输出缓冲。这个"调用闭包"分析是纯编译期的，没有链接器参与。

平台差异怎么处理？跟普通 C 库用 `#ifdef` 隔离平台代码是一个思路，跨平台的部分只写一遍：

```c
/* os.c 内部 */
#if defined(_WIN32)
    /* 走 kernel32 */
#elif defined(__linux__)
    /* 走 syscall */
#endif
```

goc 启动时注入 `_WIN32`/`_WIN64` 或 `__linux__`/`__linux`，所以库源码自己不需要在命令行上被告知目标平台。

## 取舍三：不要"编译器魔法"

`print` 这个内建最能说明 goc 的风格。它**不是** `printf` 的别名，而是编译期做静态分派：

| 写法 | 走的路径 |
| --- | --- |
| `print()` | 只换行 |
| `print("hi")` | `str_print`：一个 `write` 调用 + 换行 |
| `print(42)` | `int_print`：整数转文本 + `write` |
| `print(42L)` | `long_print` |
| `print(a, b)` 多参 / 浮点 | 回退 `printf` 按格式串发射 |

编译期看每个实参的**静态类型**，直接选最薄的那条发射路径。用户自己声明了 `print` 函数的话，永远以用户的为准。

这个设计的收益很直接：体积。下文的实测章节里，`print("Hello, world!")` 产出 **2048 字节**，而 gcc 写同样一句话要 38989 字节——差19 倍。

顺带一提，数组也能直接打印：

```c
int a[3] = {11, 12, 13};
print(a);   // [11, 12, 13]
```

实现上goclib 只有一份共享骨架 `__goclib_array_print(a, n, elem_size, conv)`，按元素字节步进、经函数指针回调逐个转文本。**新增一种数组类型 = 一个转换器 + 一个包装。**

## 取舍四：交付物就是一个文件

前三条取舍最终落到用户手上的东西，是这个：

| 编译器 | 交付物 | 依赖 |
| --- | --- | --- |
| `goc.exe` | **4.65 MB，单文件** | 只导入 `kernel32.dll` |
| `gocl.exe` + `libLLVM.dll` | **3.96 MB + 13.88 MB，两个文件** | 同上 |

拷过去就能编。不需要装 LLVM、不需要配 `PATH`、不需要 `goclib/` 源码目录、不需要 worry 少了某个 `.h` 导致 `#include` 找不到。

这一点在2026-10-07 之前是**不成立的**：goclib 当时是运行时从磁盘读的，Release 的 zip 必须整目录解压，只拷 `goc.exe` 出来立刻报 `cannot find the goclib C library`。现在库源码通过 `//go:embed goclib/*` 编译进了二进制，磁盘查找整个从热路径上消失（`src/libembed.go`）。

这不只是"少拷一个目录"的问题。**编译器的正确性不该依赖使用者的目录布局**——少拷一个文件得到的是一个报 cryptic 错误的编译器，而不是"少一个功能"。把库嵌进去之后，"这个 exe 能不能独立工作"变成二进制自身的性质，不再是使用者的责任。

`gocl` 多一个 `libLLVM.dll` 是没法再省的：它通过 `syscall.LazyProc` 直接调 LLVM 的 C API，动态库就是接口本身。它能做到的是**只依赖这一个外部**——LLVM 被裁剪成只保留 X86 后端，`libstdc++`/`winpthread` 静态链入，13 个 `api-ms-win-crt-*` 转发折叠成单个 `ucrtbase.dll`，最终依赖收敛到系统自带的那几个：

```bash
$ objdump -p libLLVM.dll | grep "DLL Name"
	DLL Name: ADVAPI32.dll     # 系统
	DLL Name: KERNEL32.dll     # 系统
	DLL Name: ntdll.dll        # 系统
	DLL Name: ole32.dll        # 系统
	DLL Name: SHELL32.dll      # 系统
	DLL Name: ucrtbase.dll     # 系统（Win10/11 自带）
	DLL Name: WS2_32.dll       # 系统
```

原始构建产物是 42.9 MB、依赖 20 个 DLL（含 `libstdc++-6.dll`、`libwinpthread-1.dll`）。这个"静态重链 + UCRT 折叠"是纯链接期操作，不用重编译 LLVM 任何一行 `.o`——抽出 ninja 的链接命令行，追加 `-static-libstdc++` 和一段按顺序静态链入的 `libstdc++/libwinpthread`，再在库列表前插一个 `-lucrtbase`。细节记在 [开发笔记 04](/goc/devnotes/04-gocl-LLVM后端踩过的坑/)。

顺带一个交叉编译的事实：**Windows 上的 gocl 就是 Linux 交叉编译器**，不需要 WSL 也不需要 Linux 机器：

```bash
$ gocl.exe -target linux hello.c -o hello    # PATH 完全清空
$ file hello
hello: ELF 64-bit LSB executable, x86-64, statically linked, not stripped
```

上一节的架构图里那个 `gocl -target linux` 分支，跑在 Windows 上。

## 架构：八个 Go 模块

```
src/frontend/     C 前端（零依赖）
src/common/       预处理器 + C 库 + 链接（两后端共享）
src/goc/          自研 x86-64 后端 codegen
src/gocld/        链接器（PE32+ / ELF64 镜像）
src/gocl/         LLVM 后端（driver.go 是被 import 的驱动）
src/              自包含入口：goc.go(tag goc) / gocl.go(tag gocl) + libembed.go
src/goa/          汇编器
tools/            验证工具
```

有个容易被忽略的工程细节：**goa 已经编译进 goc 二进制了**。所以 Release zip 里的独立 `goa.exe` 只是给手写汇编用的，`goc` 编C 不需要它旁边有 goa。

入口这一层的组织方式在 2026-10-07 变过一次。此前是两个独立的 `package main`（`src/cmd/goc/` 和 `src/gocl/cmd/gocl/`），各自重复了一整套参数解析；现在是 `src/` 下的两个文件靠 build tag 区分（`//go:build goc` / `//go:build gocl`），`goclib/` 由同目录的 `src/libembed.go` embed 进两个二进制。gocl 的驱动逻辑从 `package main` 搬到了 `src/gocl/driver.go` 的 `package gocl`——因为 `package main` 不可被 import，没法在一个文件里复用。

（上面这节的另一条已过期：goc 曾经在运行时从磁盘读 `goclib/`，查找顺序是 `GOCLIB_PATH` → exe 旁边 → 上级目录 → cwd。当时确实必须整目录解压。现在库已embed，见"取舍四"。`common.FindRoot` 那套查找逻辑还在 `src/common/source.go` 里，但内置版入口不再走它——留它是给将来可能的磁盘库模式用的。）

## 语言子集：诚实地说清边界

goc 支持的：

- 标量类型全套，`float`/`double` 走 SSE2 标量指令
- `struct`（嵌套、按值传参、按值返回）、`union`、`enum`、多维数组、指针、函数指针、`typedef`
- **位域**：MSVC 布局规则，跨存储单元分配、`:0` 强制开新单元
- **方法（UFCS）**：`x.f(args)` 在成员 `f` 不存在时按方法解析，定义 `T_f` 即可。**纯编译期重写，无 vtable、无运行时元数据**
- **变参**：`va_list` / `va_start` / `va_arg` / `va_end`，可以自己写 printf 风格的函数
- **内联汇编**：`__asm { ... }` 块，块内的裸 C 变量名会被绑定成对应的内存操作数
- **C23 实用子集**：已落地 `bool`/`true`/`false`、`typeof`、`nullptr`、`constexpr`、`_Static_assert`、`_Alignas`/`_Alignof`、`[[...]]` 属性、`#embed`、`__has_include`、`__VA_OPT__`、`enum E : int`、`u8` 前缀、二进制字面量、数字分隔符、`#elifdef`/`#elifndef`/`#warning`、`stdckdint.h`
- `_Thread_local`：**goa 原生 TLS**（TLS 目录 + `gs:[0x58]`），不是软模拟
- `_Atomic`（C11）：标量原子，`++`/`--` 发 `lock xadd`，其余走 `lock cmpxchg` 重试循环
- `_BitInt(N)`：goclib 大整数运行时（schoolbook + Karatsuba 乘、Knuth D除、十进制转换）

还没到的：

- **没有独立的链接阶段**——不能消费 `.o`，也不能链接多个目标文件（但可以一次编译多个 `.c`，各自独立翻译单元）
- 没有 `scanf`、文件 I/O、`math.h`、`time.h`（`wchar` 族后来补上了）
- `printf` **宽度一概忽略**：`%5d` 打 `42`、`%02x` 打 `7`，这是与标准 C 明确的差异，代码里有意为之
- 没有 `%e` `%a` `%n`，`%g` 是简化版（按小数位计数，不切科学计数法）
- 没有 VLA、`_Generic` 之外的部分 C99+ 边角

这份清单本身就说明了 goc 的定位：**不追求 100% 标准合规**，而是补齐"现代 C 写法"最常用的那批特性。

## 归类：它更像什么

goc 严格来说不是"编译器"，因为它没有链接阶段。更准确的说法是**单遍编译器 + 自带运行时**：

- 输入：一批 `.c` 文件（各自独立翻译单元）
- 输出：一个完整的可执行文件
- 内部：前端 → 代码生成（自研 x86-64 或 LLVM）→ goa 链接

这也是为什么 `-o` 的语义照抄 gcc：**路径不存在时它表示输出文件名而不是目录**。测试脚本必须先 `mkdir -p bin/goc-out`，少了这一步第一个例子会写出一个叫 `bin/goc-out` 的文件，后面所有例子都 `Not a directory`。README 里专门记了这个坑，因为它真的踩过。

## 小结

四条取舍串起来是goc 的风格：

| 取舍 | 代价 | 收益 |
| --- | --- | --- |
| 不要 gcc（连汇编器也不用） | 自研 x86-64 codegen + 汇编器，难度高 | 闭环、产物干净、无外部工具 |
| 不要 libc | 得自己写 printf 全家，且克制范围 | 只导入系统 DLL，体积可控 |
| 不要编译器魔法 | 得克制着不"顺便支持" | 行为可预测，体积可解释 |
| 交付物只有一个文件 | goclib 要embed 进二进制；gocl 还要再链一遍 LLVM | 拷过去就能编，不看README |

代价换来的是：**产物里每一字节都能解释来源**。这在玩具项目里是奢侈品，在编译器项目里是必需品——因为你得靠这个解释为什么产物是这个大小。

最后一条取舍还有个附加好处：它逼着"这个 exe 能不能独立工作"变成一个**可以自动化测试的性质**。把 exe 拷到一个空目录、把 `PATH` 清空、跑一个用到 `math.h`/`string.h`/`stdlib.h` 的程序——能过就是能过。目录布局这种隐式契约，一旦允许存在，就迟早会在某次拷贝、某个 CI 缓存、某台新机器上变成 bug。

下一章：[5 分钟跑起来](/goc/01-五分钟上手/)。

---

> 本章数字与源码细节核对于 2026-10-07，对应 goc 提交 `cc83ef8`。"取舍四"一节的体积与依赖表为当日实测；"取舍四"之前的内容核对于 2026-10-06、提交 `f200cd1`。goc 迭代很快，读到时若已更新，以仓库 README 为准。