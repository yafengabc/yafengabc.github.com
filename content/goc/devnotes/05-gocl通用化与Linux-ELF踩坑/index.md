---
title: "开发笔记：gocl 通用化与 Linux ELF 填坑"
menuTitle: "gocl 通用化与 Linux ELF 踩坑"
date: 2026-10-07T03:00:00+08:00
draft: false
weight: 5
tags: ["goc", "gocl", "LLVM", "codegen", "syscall", "Linux", "bugfix", "开发笔记"]
categories: ["编程开发", "goc", "开发笔记"]
description: "记录 gocl 从「连 goclib 都编不过」到「ls 在 Windows PE 与 Linux ELF 输出逐字节一致」这一轮的 13 个坑：__goc__ 宏未注册、陈旧构建产物遮蔽、链接期 syscall 桩第 4 参须走 r10、__goclib_brk identity 宏导致 malloc 段错误、整数提升缺失、switch break 跳错层、member 数组不衰变、exprType 库函数类型缺失、printf 单字符折成 fputc 链接失败，以及跨平台一致性与验证基础设施的坑。"
---

> 这是一篇**开发笔记**，不是教程。记录的是 gocl（LLVM IR 后端）这一轮从「连 goclib 都编不过」到「`ls` 在 Windows PE 与 Linux ELF 输出逐字节一致」之间踩过的所有坑。教程正文里不写这些。

goc 有两台独立编译器（同一前端）：`goc` 走自研 goa 汇编器直接出机器码；`gocl` 走 LLVM IR 生成，再交给 gocld 链接器吃 COFF/ELF 对象。本文的坑**全部来自 gocl 这一侧**——有的是 gocl 独有的 codegen 缺陷，有的是 goclib 在 gocl 路径下才暴露的跨平台不一致，有的是链接期机制本身。按时间线从编译期一路排到运行期。

---

## 一、编译期：goclib 在 gocl 下都编不过

### 坑 1：`__goc__` 宏未注册，gocl 编译 goclib 报 `__builtin_va_list`

**现象**：`gocl` 一吃 goclib 就崩，报错是

```
goclib/goclib.h: line 51: expected type specifier, got "__builtin_va_list"
```

注意这条消息的三层信息：报的是 `goclib.h`，行号是 **51**，token 是 `__builtin_va_list`。前两层都具有误导性——真正出错的位置既不在这个文件，也不是这个行号。

**为什么会错 / 为什么难查**：goclib 的头文件用 `#ifdef __goc__` 二分宿主，决定 `va_list` 到底定义成什么。问题出在 `__goc__` 这个宏的处理方式上：它原先**只在 `expandAt`（宏展开期）的 switch 里特判**，也就是"展开一个 token 时遇到 `__goc__` 就当它是 1"。但 `#ifdef` 问的不是"展开它是什么"，而是"宏表里有没有它"。两条路完全独立。于是 `#ifdef __goc__` 恒为 false，goclib 永远走"我是被 gcc/clang 编的"那条分支，去碰 `__builtin_va_list`——而 gocl 是 goc 自家后端，gocl 侧压根没有 `__builtin_va_list` 这个类型。前端的类型检查器读到一个没见过的类型名，只能报"期望一个类型说明符"。**它报得没错，只是报的对象是症状不是病因。**

难查的地方在于这条错误的文件名和行号都指向 goclib 自己，看起来像"goclib 刚改坏了"。实际上 goclib 一行都没错——它按自己声明的契约（`#ifdef __goc__` 分叉）忠实地走了另一条路。**触发它的是编译器的宏表少了一项。**

**根因（附文件:行号）**：`src/common/preprocess.go:137-149` 的注释就是当时的根因分析，逐句记着这件事：

- `preprocess.go:137-143` 先讲清 `__goc__` 的语义——它是"本文件正被 goc 编译"的钩子，goclib 的头靠它区分两个宿主；顺带指出真实编译器各有自己的同类宏（`__GNUC__`、`_MSC_VER`、`__clang__`），而它们**都不定义** `__goc__`，所以在那些编译器上这个测试为假是正确的。
- `preprocess.go:145-149` 是关键，也是修复的直接依据：

  > It has to live in p.macros, not only in expandAt's switch: a header asks with `#ifdef __goc__', and that consults the macro table. Handling it solely as an expansion-time special case made every such test read false and silently compiled goclib down the host path — defining va_list as `__builtin_va_list`, which goc does not have a type for.

**修复**：把宏真正注册进宏表。`src/common/preprocess.go:150`：

```go
p.macros["__goc__"] = &Macro{Name: "__goc__", Body: []frontend.Token{tokNum(1, 0)}}
```

注册后 `#ifdef __goc__` 对 goc 和 gocl 两个后端都为真，`va_list` 走 goc 自家的变参内建路线。同批提交的 `preprocess.go:131-136` 给 `__linux__`/`_WIN32` 等做的是同样的事，可见这批平台宏是统一补齐的。引入于 `dc0f8e6`（"让 goclib 成为可被 gcc/clang 编译的通用 C 库"）——那个提交的目标就是让一份 goclib 同时被两种宿主接受，而这份头文件里恰好已经写好了用于二分的钩子，只是编译器侧一直没把它接上。

**验证与教训**：`#ifdef` 和"展开"是**两条独立的查询路径**，任何预定义宏都必须同时满足两者——只在展开期特判，等于没定义。

> **可迁移的教训**：报错里"报的位置"和"出错的位置"这件事，在两层意义上都会错一层：**文件名会错**（报 `goclib.h`，事出 `p.macros`），**行号也会错**（报 goclib.h:51，病因在 preprocess.go:150）。下次看到一条"某个头文件某行"的类型错误，先问"这个头文件在这个位置真的有权决定这件事吗"，再顺着它 include 进来的东西往上追。**头文件里的错误信息，指向的往往是使用者而不是作者。**

### 坑 2：陈旧的 `src/bin/gocl.exe` 遮蔽最新构建，假失败满天飞

**现象**：`go test ./src/gocl/...` 全线报坑 1 那条 `__builtin_va_list` 错误，一条接一条，看起来像修复完全没生效。但**手编 goclib 明明能过**——同一份源码、同一个修复，手编过、测试不过。

**为什么会错 / 为什么难查**：这是本轮最容易被误判成"代码没修好"的一次。它的杀伤力在于**报错与病因的层次完全不同**：坑 1 已经修好并落在磁盘上了，但测试根本没跑到那份代码。而且它不是报"编译错误"，是报一条**语义上完全合理**的旧错误——`__builtin_va_list` 那个年代它是真的。你会本能地回头去看修复代码是不是写错了。

**根因（附文件:行号）**：gocl 端到端测试的 `compilerPath()`（`src/gocl/cmd/gocl/main_test.go:27-50`）这样定位编译器：

- `main_test.go:29-31`——**先看环境变量** `GOC_TEST_GOCL`，给了就直接用，什么都不查；
- `main_test.go:36-39`——否则从测试的 cwd（`os.Getwd()`）出发；
- `main_test.go:40-49`——**逐级向上**找 `filepath.Join(dir, "bin", name)`，最多 **6 层**，命中即返回。

"逐级向上 + 先命中先返回"这个策略本身是对的（让测试在任何包目录下都能跑），问题出在名字：它找的是 `bin/gocl.exe`。而 gocl 是从 `src/` 下构建的，于是仓库里天然存在**两个都叫 `bin/gocl.exe` 的候选**——`src/bin/gocl.exe`（goa/编译器子包的输出目录）和仓库根的 `bin/gocl.exe`（正式输出）。测试从 `src/gocl/cmd/gocl/` 出发逐级上溯，**先撞上 `src/bin/`**，拿到一个 10-06 09:09 的陈旧产物——它早于坑 1 那次 `__goc__` 注册修复。于是测试忠实地跑了一份修复前的编译器，报出了修复前的错误。

**修复**：删掉 `src/bin/gocl.exe`（`*.exe` 已被 `.gitignore` 忽略，正规输出是仓库根的 `bin/`），并在测试时用 `GOC_TEST_GOCL=D:/Projects/goc/bin/gocl.exe` 显式指定要走哪一份。这正是 `main_test.go:29` 那个环境变量存在的意义——它是这个搜索策略唯一的逃生舱。

**验证与教训**：关键的一步排查是 **`git stash` 掉全部未提交改动后仍然失败**。修复合入工作区却报旧错，说明测试吃的不是这份代码。这一步 30 秒就把"代码 bug"和"环境坏"分开了。

> **可迁移的教训**：产物放进 `bin/` 这种"哪个目录下都合理"的名字，等于编译器给自己埋雷——**越近的候选越容易被命中，而"近"恰好是构建输出最容易出现的层级**。凡是"逐级向上搜索 X"这类测试/脚本辅助函数，都必须同时提供（a）一个能显式覆盖的开关（`GOC_TEST_GOCL`），和（b）一个能让人一眼看出"找到的是哪一份"的诊断输出。否则这类失败会永久伪装成代码回归。

---

## 二、链接期：Linux syscall 桩的生成机制

### 坑 3：链接期自动生成 syscall 桩，但第 4 参数走了 rcx 而非 r10

**现象**：**最终修复版**暴露的症状是——`gocl -target linux` 编出的 ELF，凡是 ≥4 参数的 Linux syscall 一起失败，`errno=8`（EFAULT）。`dfacc05` 的 commit body 点名的是 `select(2)`、`setsockopt(2)`、`recvfrom(2)` 三个。

这个坑**经历了两轮修复**，两轮症状完全不同，不要混看：

| 轮次 | 提交 | 做法 | 暴露的症状 |
| --- | --- | --- | --- |
| 第一轮 | `95ed623` | 把 r10 特例加在**调用方**（引入 `callArgRegs(name, indirect)`） | `mmap`/`futex`/`wait4`/`clone` 返回 **-9（EBADF）**，崩 `thrd_create` |
| 第二轮 | `dfacc05` | 特例从调用方**撤掉、挪进桩里** | `select`/`setsockopt`/`recvfrom` 失败，**EFAULT**（errno=8） |

第一轮的 commit body 说得很清楚：`MAP_ANONYMOUS` 丢了导致 `mmap` 返回 -EBADF——因为 `mmap` 的第 4 个参数**恰好就是 flags**。它同时顺手改了 `threads.c` 的错误检查（原始 syscall 失败是负 errno，不是 -1）。**两轮症状指向不同的函数集合，正是"位置错了"的信号**：如果第一轮的位置是对的，第二轮就不该换出三个新的坏函数。

**为什么会错 / 为什么难查**：根因是**两套 ABI 对同一个"第 4 参数"的答案不同**，而源码里看不出差别。

- Linux **syscall** ABI：第 4 参数必须走 `r10`。因为 `syscall` 指令会破坏 `rcx` 和 `r11`，内核是从 `r10` 读第 4 参的。
- 普通**函数**调用 ABI（System V）：第 4 参数走 `rcx`。

gocl 走标准 SysV，第 4 参落在 `rcx`，内核在 `r10` 读垃圾 → `EFAULT`。难查的原因很直接：**从调用点看，两者都"传了 4 个参数"，源码上没有任何可疑之处**。而线索又极弱——`select`、`setsockopt`、`recvfrom` 三个函数毫无关系，唯一的共同点只有"参数个数 ≥4"。这种"不相干的东西一起坏"的现象，最好的线索就是**找共性**，而共性恰好藏在最容易被跳过的维度（参数个数）里。

**根因（附文件:行号）**：先把机制讲清楚，否则后面的修复看不懂。

**机制（gocl 怎么处理 syscall）**：gocl 在链接期（`src/gocl/cmd/gocl/main.go:281` 的 `externalImports`）扫描 LLVM 对象里每个未定义符号，只要 `goa.IsLinuxSyscall(name)` 认识（`src/goa/asm.go:391-394`，查 `linuxSyscalls` 表，`src/goa/asm.go:309` 起；表里含 `brk`=12、`getdents64`=217、`__goclib_stat`=4 等），就自动发射一条桩。**所以 `__goclib_stat` 能跑，是因为它就是这种外部符号**，由链接期生成的桩提供实现。

桩的实际形态（`src/goa/asm.go:643-648` 的注释 + `emitSyscallStubs`）容易被想象错——**不是**裸的 `mov rax,N; syscall; ret`，而是：

```asm
mov rax, N
mov r10, rcx        ; ← 就是这一行，第四参数的搬运
call __goc_syscall
ret
```

`syscall` 指令被收敛到单一入口 `__goc_syscall`（`src/goa/asm.go:714` 定义：`syscall; ret`，后面两个 NOP 留给 gocrun 打 5 字节 `jmp rel32` 补丁）。这个收敛由更早的 `fbc0b42`「gocld: become a real linker」引入，目的是让 Windows 测试用的 gocrun loader 能把**整条桩**重写成 Win32 后端翻译器——**目的正是让"调用方完全不需要知道自己在调 syscall"**。

**修复**：`src/goa/asm.go:688-691`，在 `mov rax,N` 之后立刻发射：

```go
if err := a.encode("mov", []Operand{
    {kind: K_REG, reg: 10}, // r10
    {kind: K_REG, reg: 1},  // rcx
}, "mov r10, rcx"); err != nil {
```

`src/goa/asm.go:672-687` 的注释把为什么放这儿讲透了：这条搬运在桩内部，于是"一个 syscall 桩的行为和任何别的函数一样"，调用方不必知道对面是 syscall，**任何后端都自动正确**。gocrun 的 Win32 翻译器读的正是同一组寄存器（`a1..a6 = rdi, rsi, rdx, r10, r8, r9`，见 `src/goa/asm.go:701-702`），所以真内核和 Windows 测试harness 对 `r10` 的看法一致。

调用方一侧同步撤销特例：`src/goc/codegen.go:267-269` 的 `callArgRegs` 变成无条件 `return c.argRegs()`。`src/goc/codegen.go:255-266` 保留了"这里曾经返回过 `rdi,rsi,rdx,r10,r8,r9`"的历史说明——注释里那句 "That move is inside the stub, and used not to be" 就是两轮修复的现场记录。

**验证与教训**：提交 `dfacc05` 改了 `src/goa/asm.go`、`src/goc/codegen.go`、`src/gocl/cmd/gocl/main.go` 三处——但 `main.go` 那处其实是**坑 10** 的 DLLNames，与本坑无关，别把两件事混成一件。验证结论：Windows PE 双后端 12/12、Linux ELF 16/16，goa/goc/gocld/gocl 单测全绿。

> **可迁移的教训**：**约定属于被调用方，不属于调用方。** "这个 callee 其实是 syscall"是 callee 的实现细节，让每个 call site 都知道它，等于把一份 ABI 约定复制到全代码库——**新后端一进来就全错，而且错法统一（同一类参数位置全坏），看着像新后端的基本 ABI 坏了**。下沉到桩里之后，正确性只写一遍。另外：**几个毫不相干的调用同时坏，先数共同特征**（这里是参数个数），这是最省时间的线索。

---

## 三、运行期：Linux ELF 一跑就段错误（头号坑）

### 坑 4：`__goclib_brk` 被写成 identity 宏，malloc 直接写地址 0

**现象**：`gocl -target linux` 编出的 `ls`（以及**任意用到堆的程序**）在 WSL alpine 真机**一跑即 segfault**。不是输出错、不是崩溃在某个函数里——是刚进 `main` 附近就死。

**二分定位过程**（这个序列本身比结论更有价值，所以完整保留）：

```
m1 printf                OK        ← 输出通路本身没问题
m2 getenv                OK        ← 环境变量/字符处理没问题
m4 stat                  OK        ← __goclib_stat 能跑（外部桩提供）
m5 open/close            OK        ← 文件描述符层面的裸 syscall 没问题
m3 opendir/readdir       SEGV     ← 第一个崩的用例
m6 复刻 opendir 函数体   → 崩在 malloc(1328)   ← sizeof(DIR)，把崩溃收敛到 malloc
m7/m8 单独 malloc(16)    SEGV     ← 连 16 字节的 malloc 都崩：问题在 malloc 本身
m9 直接 brk()/syscall()  链接报 undefined symbol(s): syscall
```

**为什么会错 / 为什么难查——两个独立的误导层**：

第一层是**崩溃位置与病因位置差了两次间接**。崩在 `malloc`，而 `malloc` 只是受害者：`malloc` → `__goclib_heap_alloc` → `__goclib_brk` → 什么都没调。段错误发生在"往地址 0 写"的那一刻，而错误发生在几层之外的宏展开。

第二层是**为什么 `stat` 能跑而 `brk` 不能**——这个问题不搞清楚就没法收窄。答案是两者走的是**不同的机制**：

- `__goclib_stat` 是**直接别名存根**。IR 里就是 `call i64 @__goclib_stat`，一个外部符号，由链接期 goa 生成的桩提供。它压根不经过任何宏。
- `brk` / `getdents64` 走的是 **`syscall()` 宏形式**（在 goc 路线和 host 路线里都不一样），中间隔着一层 `__goclib_brk` 宏。

所以"桩明明在表里（`brk`=12、`getdents64`=217 都在 `src/goa/asm.go:309` 起的 `linuxSyscalls` 表中），链接期也生成了桩，为什么还是坏的"——答案是**桩从来没被调用过**。

**根因（附文件:行号）**：`src/goclib/syscall.h` 的 **goc 路线**（`#ifdef __goc__`，两个后端都预定义）把 `__goclib_brk` 写成了 **identity 宏**：

```c
#define __goclib_brk(addr)   ((void *)(addr))   /* dc0f8e6:86 */
```

**为什么当初会写成 identity —— 这里有一段完整的错误推理链，值得原样摊开。** `git log -S"__goclib_brk" -- src/goclib/syscall.h` 只给出两个提交：`dc0f8e6` 引入、`c9e2b58` 修复。看 `dc0f8e6` 里的原文（当时的 `syscall.h:85-87`）：

```c
/* goa's brk stub takes and returns a pointer, which is what is declared above,
 * so on this host the mapping is the identity. */
#define __goclib_brk(addr)                   ((void *)(addr))
```

这句话就是**错误假设的来源**。它做了一次"类型推理"：桩的签名确实是 `void *brk(void *addr)`（`src/goclib/syscall.h:117`），返回指针、收一个指针——**看起来完全像个普通函数**。既然参数和返回值都是指针，那么"把 `__goclib_brk(addr)` 写成 `brk(addr)`"和"写成 `((void*)(addr))`"在**值**上是一样的——**如果**调用真的发生了。

但 identity 宏**不是调用**。`((void *)(addr))` 里没有任何函数调用，`brk` 桩从头到尾没被碰过。这就是**"桩生成"与"宏展开"两条完全独立的路径**：桩在链接期由 `emitSyscallStubs` 按 `linuxSyscalls` 表生成，宏在前端的 `expandAt` 里展开，两者唯一的交集是**你写了一个看起来像函数调用的宏**。链接器完全看不出问题——符号在、桩在、`brk` 是被"引用"了的（宏体里没有，但 extern 声明在）。**没有任何一个工具会在这里报错。**

**后果**：`src/goclib/os.c:114-131` 的 bump 分配器：

```c
void *__goclib_heap_alloc(long size) {        /* os.c:114 */
    ...
    if (heap_cur == 0) {
        heap_cur = (char *)__goclib_brk((void *)0);   /* os.c:121 恒得 NULL */
    }
    next = heap_cur + need;
    if ((char *)__goclib_brk((void *)next) != next) {  /* os.c:124 恒"成功" */
        return 0;
    }
    raw = heap_cur;                            /* os.c:127 == NULL */
    heap_cur = next;
    *((long *)raw) = size;                     /* os.c:129 写地址 0 → 段错误 */
    return raw + HEAP_HDR;
}
```

identity 宏让 `__goclib_brk(0)` 恒返回 `NULL`，`os.c:129` 那句 `*((long *)raw) = size` 就直接往地址 0 写。`os.c:124` 那个失败检查还**永远返回"成功"**（identity 宏回显入参，`next == next`），所以连"分配失败"这条退路都没有。

**修复**：`src/goclib/syscall.h:130`，让宏真正调用桩，与 host 路线的 `syscall(12, …)` 语义对齐：

```c
#define __goclib_brk(addr)   brk(addr)
```

`src/goclib/syscall.h:124-129` 换掉了那句错误假设，写明了理由：**goa 的 `brk` 桩是真正的 `mov rax,12; syscall; ret`，它会推进内核 break 并返回新的 break**（失败时返回旧 break）。分配器需要这个返回值——`__goclib_brk((void*)0)` 必须能**查询**当前 break，`__goclib_brk(next)` 必须能**真的移动**它。

这一改**同时修好了 goc 自身 Linux 后端的 malloc**：因为 goc 自家 Linux 后端走的也是这条宏，此前同样坏，只是没在真机上暴露（goc 的 Linux 路径当时没被真机验证覆盖）。**一个"修 gocl 的 bug"的动作，顺手修掉了 goc 的同一个 bug**——这也是"同一份 goclib 要同时被三种编译器接受"这件事的必然结果。

**验证与教训**：`src/goclib/syscall.h:164-170` 顺带记了另一个相关事实：实测 musl 1.2.6 上，编译器内建的 `brk(0)` 返回 `(void*)-1`，而同一进程里 `syscall(SYS_brk, 0)` 返回真实 break——**host 路线必须走 `syscall()`，不是"因为形式上更像函数"就能换过去**。修掉后，goc/gocl × Windows/Linux 四种组合全部跑通，`plain/-a/-F/-1` 与 Windows 逐字节一致，`-l` 名字列一致。

> **可迁移的教训**：**identity/透传宏是最阴的一类坑**——编译能过、链接能过、只在运行期炸，而且炸在一个语义上完全无辜的调用上（这里是 `malloc`）。它的危险来自一个很隐蔽的推理跳步："参数和返回值类型对得上，所以不调用也一样"。**类型兼容 ≠ 语义等价**，恒等函数的正确性前提是"真的调过它"。凡是看到 `#define X(...)  ((T*)(...))` 这种**看起来像函数调用的宏**，一律问一句：它真的透传了吗，还是把关键调用吞了？**并且要记住桩生成与宏展开是两条独立路径，链接期完全看不出问题。**

---

## 四、codegen 质量：编得过，但算错 / 编不过

这几个坑是「让 gocl 编出**正确**的 `ls`」时逐个暴露的——`ls` 用了 `qsort`+`strcmp`（坑 5）、`strftime`（坑 6）、`readdir` 的结构体成员访问（坑 7）、`stat` 返回值比较（坑 8），正好撞上 gocl 的四处缺陷。

### 坑 5：整数提升缺失，`strcmp` 返回 255 而非 -1，qsort comparator 全乱

**现象**：`ls` 在 gocl 下排序错乱（`sub b.txt a.txt` 顺序颠倒）。单独抽出 `strcmp` 测：`'a' - 'b'` 在 gocl 下得 **255**。

**为什么会错 / 为什么难查**：C 规定 `_Bool`/`char`/`short`（**含 unsigned 版本**）作为运算符操作数时，在运算之前要先做**整数提升**（integer promotion）到 `int`。gocl 的 `arithCommon` 只做了 usual arithmetic conversion（算术转换），**漏掉了前置的整数提升**。缺了前置那一步，窄类型在**自己的宽度上**算完，再零扩展到 `int`：符号在扩展那一刻就永久丢了。

难查的地方在于**症状距离病因极远**，而且是一个"看起来像随机化"的症状：

1. `char`/`short` 的**有符号**窄类型实际上"侥幸没事"——`src/gocl/operator.go:275-279` 的注释点了这件事：i8 的减法如果回绕后再**符号扩展**，结果恰好等于先提升到 int 再算。所以**同一个缺陷对有符号窄类型隐藏了很多年**，一碰到无符号就暴露。
2. 暴露出来的是一个**函数返回一个错误的正数**。`strcmp` 内部对 `unsigned char` 做减法，`qsort` 的 comparator 是 `return strcmp(a,b)`（`apps/coreutils/ls.c:94`），然后 `qsort` 内部判 `cmp(a,b) < 0`。每个 `<0` **恒假**——排序不是"排错"，而是**根本没有任何一次比较认为该交换**，输出看起来就像被打乱了。

IR 长这样：

```ir
%t = sub i8 %a, %b
%r = zext i8 %t to i32     ; 'a'-'b' = 255，不是 -1
```

**关键洞察：这个 bug 在 goc 后端不可见。** goa 那边做了提升（它在 codegen 阶段就处理了窄整型），所以同一个 goclib 源在 goc 下完全正确。**这正是"新后端特有的漏项"**——同一份源码、同一份前端，两套后端对同一条 C 规则实现与否不同。这也解释了为什么"两后端行为不一致"在这种 bug 上会表现为**"看起来像排序随机化"**这种充满随机性的症状，而不是"某个常量算错了"。

**根因（附文件:行号）**：`src/gocl/operator.go:282-284`，`arithCommon` 的**首两行**就是修复：

```go
func arithCommon(a, b *frontend.Type) *frontend.Type {   /* operator.go:282 */
	a = promoteInt(a)                                  /* operator.go:283 */
	b = promoteInt(b)                                  /* operator.go:284 */
```

实现是 `src/gocl/operator.go:324-332`：

```go
func promoteInt(t *frontend.Type) *frontend.Type {   /* operator.go:324 */
	if t == nil {
		return nil
	}
	if t.Kind == frontend.KInt && t.Width < 4 {
		return frontend.IntType()
	}
	return t
}
```

注意它的边界（`operator.go:319-323` 的注释说明了）：只提升**秩低于 int** 的整型（宽度 < 4），指针、浮点、位精确整数（`_BitInt`）、`nil` 一律不动。提升目标恒为**有符号 int**，因为目标类型能表示源类型的每个值。

**验证与教训**：`apps/coreutils/ls.c:213` 的 `qsort(ents, nents, sizeof(struct ent), cmp_ent)` 现在排出来的名字列与 goc 后端、Windows 与 Linux 四方一致。

> **可迁移的教训**：**同一份源码在两个后端下行为不同，未必是"这个后端有问题"——更常见的是"两个后端实现完备度不同"。** 判断新后端是否可信的最快方法，是**拿一条已经过验证的旧后端做对照**，而不是自己跟自己对答。"看起来随机"的症状（排序乱、偶发失败）尤其要往"某个恒真/恒假的条件判断"上想，而不是往"数据被污染了"上想。

### 坑 6：switch 内 `break` 跳错层，strftime 多格式串只输出首段

**现象**：`strftime("%Y-%m-%d", …)` 在 gocl 下只输出 `2025`——`%Y` 对，后面的 `-%m`/`-%d` 全没了。但 `%Y` 单独正常，`%F`（等价于 `%Y-%m-%d` 的单 case 写法）也正常。

**为什么会错 / 为什么难查**：`src/gocl/statement.go` 的 `doSwitch` 建了出口标签 `doneL`，却**没把它压入 `breakTo` 栈**。于是 `switch` 内部的 `break` 沿 `breakTo` 栈一路弹到**外层 `while`** 的出口（没有外层循环时什么都不跳，直接落到下一个 case）。复现模式非常精确：

| 写法 | 结果 | 为什么 |
| --- | --- | --- |
| `%Y` | 对 | 第一个 case 里的 `break` 才刚执行，无所谓 |
| `%Y-` | 错 | `break` 跳到外层 `while` 出口，整个格式串循环终止 |
| `%Y%m` | 错 | 同上 |
| `%F` | 对 | 单个 case 内一次写完，不经第二次循环 |

**`%F` 正常是这条坑最迷惑的地方**——它让人以为"格式串里多个转换符"没问题，实际只说明"单 case 写完时不需要第二次迭代"。`src/gocl/statement.go:311-317` 的注释把这个案例完整记下来了（"strftime is where it showed: its format loop is a while around a switch"）。

难查的第二个原因：**静默、无警告**。跳错出口不产生任何 LLVM 报错，只是提前退出循环、少输出一半字符串。

**根因（附文件:行号）**：`doSwitch`（`src/gocl/statement.go:241`）在 `statement.go:255` 建了 `doneL := e.newLabel()`，但直到生成 arm 之前都没告诉 `breakTo` 这个栈。

**修复**：压栈 / 弹栈两行，位置很关键——

- `src/gocl/statement.go:319`：`e.breakTo = append(e.breakTo, doneL)`，必须在**遍历所有 case 之前**（否则 case 体里的 `break` 看到的还是外层出口）；
- `src/gocl/statement.go:332`：`e.breakTo = e.breakTo[:len(e.breakTo)-1]`，在 `blockLabel(doneL)` 之前弹栈。

同文件里 `doWhile`/`doFor` 走的是完全相同的模式（`statement.go:159`/`:162`、`statement.go:179`/`:182`），可以对照。`continue` 是**故意不压栈**的（`statement.go:317-318`）：C 里 `continue` 属于循环而非 `switch`，`switch` 不该遮蔽它。

**验证与教训**：`ls.c:137` 的 `strftime(out, outsz, "%Y-%m-%d %H:%M", &tm)` 现在输出完整，四个组合下时间列一致。

> **可迁移的教训**：**"跳表"（breakTo/continueTo 这类目标栈）是控制流翻译里最容易被忘掉的耦合点**——新增一种跳转语句时，除了"把它 emit 出来"，还要问"我的栈顶对不对"。一个栈顶不对的 `break` 症状是**静默的语义错误**（少执行一段代码）而不是崩溃，因为"跳到某个合法标签"永远是合法的 IR。**编译期能接受的错，比编译期报错的错危险一个数量级。**

### 坑 7：member() 数组不指针衰变，`e->d_name` 当数组传参 → 编译失败

**现象**：任何把结构体里的 `char[]` 成员当参数传的程序，gocl 编译失败：

```
'%t28' defined with type '[256 x i8]' but expected 'ptr'
```

**为什么会错 / 为什么难查**：C 规定数组在**大多数语境**下会**衰变**（decay）成指向首元素的指针，`->d_name` 的结果虽然写得像数组，类型上却是 `char *`。gocl 的 `member()` 却**无条件 `load()`**——把 `e->d_name`（类型 `[256 x i8]`）原样 load 出一个 `[256 x i8]` 的**值**。传参时 callee 期望 `ptr`，拿到的是一个 256 字节的数组值，类型直接冲突。

难查在于**报错信息完全不提"数组"两个字**：`'%t28'` 是个匿名临时值，LLVM 只知道你说"这里有个 `[256 x i8]`，这里要个 `ptr`"。而 `[256 x i8]` 从哪来的、为什么会出现在这里，报错里一概没有。读者得先知道"`e->d_name` 应该是指针"才能反推出来。

**根因（附文件:行号）**：`src/gocl/call.go:408-425` 的 `member()` 末尾无条件 `return e.load(p, ty)`，数组类型没有特殊处理。

**修复**：`src/gocl/call.go:420-423`，在 `load` 之前拦掉数组类型：

```go
	if ty != nil && ty.Kind == frontend.KArr {
		return val{op: p, ty: frontend.PtrType(ty.Elem)}
	}
	return e.load(p, ty)
```

`src/gocl/call.go:411-419` 的注释把三件事说清了：这是**与 `ident()` 一样的数组到指针转换**；`stat(e->d_name, &st)` 传的是那个地址，不是它指向的 256 字节；而"下标访问 member 仍然读单个元素"也自然成立——因为 `Index` 问的是 base 的地址，同一个东西。

> **这里有个写代码时踩到的小坑，值得留着**：照着 `ident()` 的写法手抄成 `&frontend.Type{Kind: frontend.KPtr, Base: ty.Base}`，**编译不过**——`src/frontend/types.go:50` 里那个字段叫 **`Elem *Type`**（`Elem *Type // KPtr / KArr: element type`），**`frontend.Type` 结构体里根本没有 `Base` 这个字段**。构造指针类型一律走 `src/frontend/types.go:78` 的 `frontend.PtrType(elem)`。

**验证与教训**：`dir.c` 的 `readdir` 把 `d_name` 传给 `printf`/`strlen` 全部正常，`ls` 的四平台输出一致。

> **可迁移的教训**：**数组到指针转换是 C 里最"隐形"的一条规则**——它不写在表达式语法里，而是嵌在类型系统里，凡是"把数组用在需要指针的地方"的地方都要触发一次，凡是"数组出现在值位置"的地方都不触发。**补这条规则时不能只改一处**：变量（`ident`）、结构体成员（`member`）、函数返回的数组、解引用结果，各自是独立的代码路径。本条那个 `Base`/`Elem` 的教训则是通用的——**新增/复用结构体字段时别凭记忆手抄字面量，`grep` 一下结构体定义**。

### 坑 8：exprType 对库函数调用返回类型缺失，`__goclib_stat` i64 vs i32 不符

**现象**：用到 `stat()` 返回值的比较，gocl 报 `%t11 defined with type i64 but expected i32`。

**为什么会错 / 为什么难查**：这一条和坑 7 长得很像（都是 LLVM 类型不符），但病因方向**相反**——坑 7 是**该退化的没退化**，这条是**该查到的没查到**。

`exprType` 是 gocl 的类型推理入口：一个表达式该是什么类型。LLVM 是强类型的，同一个 SSA 值在定义处和用处必须**完全一致**的类型，否则报 `%t11 defined with type i64 but expected i32`。这个报错的形态是"两个数字不一致"，读起来像"某个常量算错了宽度"，完全看不出是"类型推断少查了一张表"。

**根因（附文件:行号）**：`src/gocl/types.go:165` 的 `exprType`，在 `*frontend.Call` 分支里只查了两处——`funcDef`（`types.go:213`）与 `fnPtrVar`（`types.go:293`）。两者都没命中时**返回 `nil`**，而 `nil` 在下游被当成"默认 `int`"（i32）。

而 `__goclib_stat` 是**库函数**，它的声明来自 goclib 的构建，不在 `funcDefs` 里。`src/gocl/types.go:38-42` 的注释解释了 `funcDefs` 覆盖不到的原因："apart from funcDefs, which also holds the C runtime's"——库函数有一套自己的表。于是 `__goclib_stat` 返回 `long`（i64），但调用点的比较（`stat(...) == 0`）用了 `icmp eq i32 %t11`，而 IR 里实际是 `call i64 @__goclib_stat`。i64 vs i32，冲突。

**修复**：`src/gocl/types.go:296-297`，在 `funcDef`/`fnPtrVar` 之后补 `libFunc` 查找（`libFunc` 定义在 `src/gocl/types.go:94`，同时覆盖 `lib.Funcs` 与 `lib.Protos`）：

```go
		if fd, ok := tr.libFunc(n.Name); ok {
			return fd.Ret
		}
		return nil
```

`src/gocl/types.go:288-291` 的注释点出了最后一档 fallback 的意义："Only a call with no known declaration falls back to int, which is the common case."——也就是说，查不到就退 i32 本身是**有意为之**的正常路径，错的只是**少查了一张表**。顺带一提，`libFunc` 里连 `Protos` 那半（`types.go:100-108`）也是踩过坑才加的：Win32 入口点由头文件声明、由 DLL 定义，只在 `Protos` 里，`GetStdHandle` 的 HANDLE 被截成 32 位曾让所有打印崩在 write 上。

**验证与教训**：坑 5–8 这四个 codegen 坑修完后 `go test ./...` 全绿（含 `gocl` 与 `gocl/cmd/gocl`）。

> **可迁移的教训**：**任何"查表决定类型"的实现，都得把表列全——少列一行的表现不是"查不到"，而是"静默退到默认值"。** 默认值（这里退 i32）对大部分代码是对的，所以缺失不会在多数地方暴露，只在"返回类型恰好比默认宽/窄"的那一两个调用点炸。加上 `libFunc` 之后仍保留 `nil` → int 的 fallback 是对的：那是**有意的**默认，不是这个 bug 的成因。**区分"有意的兜底"和"漏掉的分支"，靠的是给兜底写清注释**——`types.go:288-291` 那句 "which is the common case" 就是这种注释，它让后来者知道下一张表该加在哪里。

---

## 五、printf 特化与链接

### 坑 9：无 `%` 的单字符 printf 被特化成 fwrite，LLVM 折叠成 fputc 后链接找不到

**现象**：`printf("\n")` 这类**没有格式符、且长度恰为 1** 的调用，gocl 链接报 `undefined symbol(s): fputc`。注意：长度 9 的同类调用正常。

**为什么会错 / 为什么难查**：这条的诡异之处在于**你写的和你最终链接的不是同一个函数**，中间夹着一次 LLVM 优化。完整链条：

1. `src/common/printfspec.go:52` 的 `SpecializePrintfCall` 发现格式串**不含 `%`**（`printfspec.go:71`），于是判定它是"纯 echo"，特化成 `fwrite(lit, 1, len, stream)`。这个特化是**编译期决策**（`printfspec.go:46-49` 的注释解释了为什么必须编译期：调用图裁剪依赖它，运行期探测会让 vfmt 保持可达，就什么都裁不掉了）。
2. 当 `len == 1` 时，`fwrite(p, 1, 1, F)` 是 LLVM 的 libcall 识别会**主动折叠**的规范形态 → 变成 `fputc(*p, F)`。
3. gocl 的调用图裁剪只拉入了 `fwrite` 的定义（它按前端的调用名收集），**优化发生在 IR 生成之后、链接之前**，裁剪逻辑根本不知道。
4. 链接时 `fputc` 成为未定义符号。

**报错指向 `fputc`，而源码里一个 `fputc` 字都没有。** 这是最迷惑的部分：你去 grep `printfspec.go` 只会看到 `fwrite`。而且长度 9 正常、长度 1 失败，边界条件精确到"恰好 1 字节"，看起来像某种对齐/尺寸的边界问题，不会让人想到"优化器改写了函数名"。

**根因（附文件:行号）**：`src/common/printfspec.go:85-91` 记录了整件事（注释直接点名 gocl 后端）：

> A single-byte echo would specialise to `fwrite(ptr, 1, 1, stream)`. LLVM's libcall simplification rewrites exactly that shape into `fputc(*ptr, stream)`, but the gocl back end had only emitted `fwrite`'s definition, so the link then failed with an undefined fputc.

**修复**：`src/common/printfspec.go:92-99`，无 `%` 分支里，长度 1 直接发 `fputc`，其余仍发 `fwrite`：

```go
		if len(lit.Bytes) == 1 {
			first := &frontend.Index{Base: lit, Idx: &frontend.NumLit{Val: 0, Kind: frontend.TInt}}
			return &frontend.Call{Name: "fputc", Args: []frontend.Expr{first, stream}}
		}
		return &frontend.Call{Name: "fwrite", Args: []frontend.Expr{ ... }}
```

`printfspec.go:89-91` 的思路值得学：**把决定前移到优化器之前**——既然优化器必然会这么折，就直接发折后的形态，"裁剪逻辑和 emitter 都不需要知道优化器会做什么"。

**验证与教训**：`printf("\n")`（长度 1，无 `%`）与长度 9 的同类调用现在都过。

> **可迁移的教训**：**后端优化会改变你写下的函数调用形态**（`fwrite(p,1,1,F)` → `fputc(*p,F)` 只是最温和的一例，还能有 inlining、尾调用、memcpy 合并、intrinsic 识别）。**任何"按名字裁剪调用图 / 按名字收集符号 / 按名字做链接期决策"的逻辑，都必须和优化器对账**。具体做法：要么在优化**之前**做完决定（`printfspec.go:92` 的选择），要么在优化**之后**重扫一遍 IR 重新收集（更稳但更贵），要么干脆别裁。裁剪优化换来的体积/加载收益，通常不值得为它承担一类只在链接期出现、且报错信息完全指错方向的故障。

---

## 六、跨平台一致性：goclib 在 gocl 路径下暴露的不一致

这些不是 gocl 的 codegen bug，而是 goclib 自身在两后端/两平台下行为不一致，由 gocl 的「双后端 + 跨平台逐字节一致」目标逼出来的。**它们本来就会存在，只是只有"要求两个平台输出一致"这个目标才会把它们照出来。**

### 坑 10：gocl Windows 导入表漏掉库内部 DLL 注解

**现象**：用到 socket 的程序在 gocl（Windows PE）下链接报 `undefined symbol(s): WSAStartup, accept, bind, …`——**全是用户程序调到的名字**，看不出跟导入有关。

**为什么会错 / 为什么难查**：报错的符号集合有一个明显的"错位"特征——它们是**用户程序直接调用**的函数，而 Windows PE 的导入表关心的是**库内部**调用了哪些 DLL 函数。用户调用 `accept()`，goclib 内部去调 `WSAStartup()`，这两件事在符号表里长得一样，但在导入决策里属于完全不同的两个集合。报错只列前者，于是"链接器说缺 `accept`"看起来像是 `accept` 真的没定义——其实它定义得好好的（goclib 的 socket 包装），只是没人告诉链接器"这个名字来自 ws2_32"。

更迷惑的是：`WSAStartup` 是**纯 Windows API，没有任何平台会缺它**。看到它和 `accept`/`bind` 一起报出来，第一反应会是"goclib 的 socket 层写错了"。

**根因（附文件:行号）**：`ws2_32` 的注解写在**库内部**的 `goclib/winsock2.h` 里——goclib 用自己的 inline-DLL 语法（`, ws2_32`）标在原型后面。但 gocl 原本只从**用户程序**的 `prog.Prototypes` 建 name→DLL 映射（`src/gocl/cmd/gocl/main.go:335-338`）。用户程序 `#include <socket.h>`，看到的是 goclib 自己的包装名，**永远不包含 `winsock2.h`**。所以那些注解只对库构建可见。

`src/gocl/cmd/gocl/main.go:320-328` 的注释把整个错位讲清楚了，包括最关键的一句：报错"lists exactly the calls the program made … rather than as anything to do with imports"。

**修复**：`src/gocl/cmd/gocl/main.go:329-338`，**先**读 `common.DLLNames`（`common.Build` 编 goclib 时记录的全量表，也就是 goc 自己后端用的同一张），**再**补用户程序的：

```go
	for name, dll := range common.DLLNames {   /* main.go:332 */
		dllOf[name] = dll
	}
	for _, f := range prog.Prototypes {
		if f.DLL != "" {
			dllOf[f.Name] = f.DLL
		}
	}
```

`main.go:329-331` 说明这张表的来源（"common.DLLNames is that library-side view"），`main.go:341-345` 还留了一条兜底：`UndefinedSymbols` 解析失败时不许伪装成缺导入，而是退回"声明所有已知 DLL 原型"（即修复前的行为）保证链接能过。

**验证与教训**：改的是同一个 `dfacc05`（与坑 3 同提交，但属两件事）。socket 用例在 Windows PE 下全部链接通过。

> **可迁移的教训**：**当 A 依赖 B，而 B 的依赖信息只有 B 自己看得见时，A 的依赖就是隐式的。** DLL 注解、编译选项、内联函数、静态注册表——凡是"写在被依赖方内部、消费者看不见"的信息，都得有一条**把库侧视角完整传给消费者**的通道（这里就是 `common.DLLNames`）。配套的诊断教训：**当一批"不可能缺"的符号一起报未定义时，先怀疑"符号名到来源的映射表不全"，而不是"符号真的不存在"**——报错列出的集合往往不是出问题的那一层。

### 坑 11：宿主编译器下 `syscall(2)` 错误码全错

**现象**：用宿主编译器（gcc/clang）编 goclib 时，走 `syscall(2)` 的包装函数**错误码全乱**——所有 socket 错误都报 `EIO`，包括一个真实原因是 `ECONNREFUSED` 的。

**为什么会错 / 为什么难查**：这是**两条路线返回值形状不同**造成的。goa 的桩（gocl 路线）交回**原始内核值**：失败是一个小的负数，比如 `-111` 表示"连接被拒绝"。而 libc 的 `syscall(2)` 不这样——musl 和 glibc 都把任何错误**压缩成 `-1`**，把真实号码记进**自己的 `errno`**。

goclib 的 Linux 臂是**照着原始值写的**（取负、查表）。于是 `-1` 到达那里变成"取负得 1，查表 1"——**1 不是一个错误码**，直接落到 default。难查在于：所有错误都退化成同一个 `EIO`，看起来像"错误码表建错了"，而实际上是"喂进去的值已经不是错误码了"。而**两个后端表现不同**这件事又会把人引向"gocl 的问题"——实际坏的是**宿主编译器那条路线**，gocl 路线一直是对的。

**根因（附文件:行号）**：`src/goclib/syscall.h:200-213` 的注释是完整解释：goa 的桩交原始值，libc 的 `syscall(2)` 把错误压成 -1 并记进自己的 errno，而 goclib 的 Linux 臂按原始 `-errno` 写 → "`-1` arrives there as 'negate 1, look up 1', which is not a code and falls through to the default. Measured: every socket error under gcc reported EIO, including one that was really ECONNREFUSED."

**修复**：`src/goclib/syscall.h:219-222`，加一个把号码放回去的辅助函数：

```c
extern int *__errno_location(void);

static inline long __goclib_raw(long r) {
    if (r < 0) return -(long)(*__errno_location());
    return r;
}
```

`src/goclib/syscall.h:211-213` 解释了为什么用 `static inline` 而不是普通 `static`：这个头被十几个翻译单元包含，其中只有一个真调 socket，普通 `static` 在另外十一个里就是未使用函数。`src/goclib/syscall.h:210-212` 还说明了 `__errno_location()` 这个名字在 `-nostdinc` 下是唯一出路。

全部走 `syscall()` 的宏**整套**都套上了（不是原先估的 21 个）：`grep -c "__goclib_raw(" src/goclib/syscall.h` 有 **33** 处命中，其中 **25** 处是单行 `#define`，其余为跨行续定义（`syscall.h:225-229`、`:275-288` 那几段 `clone`/`futex`/`sendto`/`recvfrom`/setsockopt/getsockopt/select）。

**关于 include 守卫，有一点要说清**：守卫 `GOC_SYSCALL_H`（`syscall.h:1-2`）**不是**这次加的。`git log -S"GOC_SYSCALL_H"` 只指向更早的 **`2db9259`**（TCP/IP 层那一轮，diff 里 `+#ifndef GOC_SYSCALL_H` / `+#endif /* GOC_SYSCALL_H */`）。`stdlib.c` 重复包含是这次用**「删掉第二次 include」**的方式解决的（`src/goclib/stdlib.c:718-722` 注释：「`<syscall.h>` 已在文件作用域包含过了，且是同一个翻译单元」，并明确说"which is why this header carries a guard now and why this is not relying on it"）——**不是靠守卫兜住**。

**验证与教训**：socket 错误码在 gcc 与 clang 下全部正确（`ECONNREFUSED` 不再退化成 `EIO`）。

> **可迁移的教训**：**同一个语义量在不同底层上可能有不同的"编码"**——一个用负 errno 原文，一个用 -1 + `errno` 旁路。**跨路线复用同一个数据形状时，必须在边界上归一化**，而不是指望每个消费者都懂两种编码。归一化的位置要在**产生值的最低层**（这里就是那 33 个宏共用的 `__goclib_raw`）——**在消费者里各自修，就是 33 个地方各修一次，且下一次加新 syscall 的人一定会漏**。附带一条：`grep -c` 出来的数字和直觉差得远时（21 vs 33），**先确认自己的计数口径**（单行 `#define` / 含续行 / 含定义处），别急着写进文档。

### 坑 12：Linux `struct stat` 缺 `st_mtime` 字段

**现象**：`stat.h` 头注释承诺三字段同名（`st_size`、`st_mode`、`st_mtime`，两平台都叫这个名字），但 Linux 分支的 `struct stat` 只有 `st_mtim_sec`，没有 `st_mtime`。于是按文档写的代码在 Windows 上编过、在 Linux 上报 "no such member"。

**为什么会错 / 为什么难查**：`src/goclib/stat.h:8-11` 的头注释明确承诺了"only the three fields a portable program can rely on are named the same on both platforms: st_size, st_mode and st_mtime"。**Windows 分支确实照做了**（`stat.h:35` 就是 `long st_mtime;`），Linux 分支用了内核的名字 `st_mtim_sec`（`stat.h:54`，offset 88）。这不是笔误，是**不能改**——

**根因（附文件:行号）**：`src/goclib/stat.h:9-11` 的注释说明了不能改的原因：**Linux 的 `struct stat` 布局钉在内核 x86-64 的字节偏移上**（共 144 字节），因为 stat syscall 直接按内核那份布局填这个结构。`src/goclib/syscall.h:187-189` 补充了另一半理由："stat.h's struct stat is the kernel's x86-64 layout, the one stat(2) fills in directly, so asking the kernel keeps goclib reading the bytes it was written against."

注意这里的关键：**重排或插入字段不会报错，只会静默读到错误的字节**——比如把 `st_size` 读成 `st_mtim`。两个分支因此处在**不对称**的位置上：Windows 的布局是 goclib 自己定的（从 `GetFileAttributesExA` 填，见 `stat.h:11-12`），想怎么命名就怎么命名，`stat.h:35` 写 `st_mtime` 毫无障碍；Linux 的布局是**别人的**，名字和偏移都改不了。头注释却对两者承诺了同一个名字——**承诺是对称的，能力是不对称的**，缺口只能落在 Linux 这一侧。

**修复**：**不是加字段**——加一个同名的 `long st_mtime;` 会占 8 字节、把所有后续字段的偏移推错，正是上面说的灾难。而是在 Linux 分支的 `struct stat` **之后**加一条宏别名，`src/goclib/stat.h:69`：

```c
#define st_mtime st_mtim_sec
```

`stat.h:63-68` 的注释解释了这条别名的价值：没有它，"code written against the documented three-field contract compiles on Windows and fails on Linux with 'no such member', which is the opposite of what a portable header is for"。

**验证与教训**：`stat(e->d_name, &st)` 后的 `st.st_mtime` 在两平台都取到正确值，`ls -l` 的时间列正常。

> **可迁移的教训**：**当一个公共 API 的名字必须统一、而底层实现的布局不可动时，正确的做法是在布局之外做名字的映射，不是往布局里插字段。** "补一个字段"在结构体上是**有代价的**——它会让所有后续偏移失效，而在"布局由外部约定钉死"的场景（比如内核 ABI、磁盘格式、网络报文）下，这个代价是不可检测的错误数据。凡是遇到"名字不一致 + 布局不能动"，先问一句"能不能在结构体外面做别名/包装函数"，那通常才是对的位置。

### 坑 13：Windows `readdir` 返回 `.` 和 `..`

**现象**：头注释（`src/goclib/dir.c:15`）承诺两平台 `readdir` 都不报 `.`/`..`，但 Windows 分支实际返回了它们。

**为什么会错 / 为什么难查**：`FindFirstFileA` 用 `"*"` 作 pattern 时**真的会**把 `.` 和 `..` 交回来（它们是 NTFS 上的真实目录项），而 Linux 的 `getdents64` 那条路也返回它们，只是 Linux 分支的 reader 做了过滤。于是同一个 `ls -a` 在两个平台上名字数不同——**而这种差异在"逐字节比对"里会表现为"多两行"，看起来像分页/缓冲区处理有问题，而不是过滤逻辑不对称。**

难查的另一半：`.` 和 `..` 长得**极像**普通文件（它们没有隐藏位标记可以一概过滤——`.gitignore` 这种名字以 `.` 开头的普通文件应该被 `-a` 列出来）。所以过滤不能写成"跳过所有以 `.` 开头的名字"，必须**精确匹配两个名字**。

**修复**：Windows 分支 `readdir` 在返回前加跳过逻辑，`src/goclib/dir.c:95-111`——与 Linux 分支一致的结构：包一层 `for (;;)`，命中就 `continue`。`dir.c:105-109`：

```c
		if (d->ent.d_name[0] == '.') {
			if (d->ent.d_name[1] == '\0') continue;          /* "." */
			if (d->ent.d_name[1] == '.' &&
				d->ent.d_name[2] == '\0') continue;          /* ".." */
		}
```

`dir.c:104` 有一处**顺序上的关键细节**，注释也点了："Advance before the test, or a skipped entry is re-read forever."——`FindNextFileA` 的推进必须在过滤**之前**，否则跳过的项会被反复读回，`readdir` 死循环。

`dir.c:90-94` 的注释记着这条修复的动机："The promise used to be kept on one side only -- FindFirstFileA with a "*" pattern does hand back both entries, so a program that listed a directory saw two extra names on Windows and none of them on Linux, which is the exact difference the filter exists to remove."

**验证与教训**：`ls -a` 的设计因此明确为"**揭示名字以 `.` 开头的文件，但 `.`/`..` 本身仍不列**"——`ls.c` 的 `-a` 分支按名字首字符判定，不用过滤后的存在与否来推断。

> **可迁移的教训**：**"两个平台行为一致"这种承诺，必须在每个平台上都显式实现，不能假定它"本来就是那样"。** Linux 分支做了过滤不代表 Windows 分支会做——差异往往来自**两条路径的底层机制不同**（`getdents64` vs `FindFirstFileA`），而不是有人漏写。推广这类修复时有个通用套路：**把一个平台已有的正确逻辑，搬到另一个平台，再用跨平台一致性测试锁住它**。同时注意配套的两个细节——过滤条件要**精确**（不能连坐 `.gitignore`），以及**状态推进必须在过滤之前**（否则跳过即死循环）。

---

## 七、验证基础设施的坑（不修代码，但阻塞开发）

这几条不是 gocl 的 bug，却是「验证 gocl 对不对」时反复撞上的墙，单列出来省得下次再栽。

- **WSL 路径转换**：Git Bash 调 `wsl` 必须加 `MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'`，否则 `/mnt/d/...` 被转成 `C:/...` 报 `"can't open"`。
- **`/mnt/d` 挂载瞬时抖动**：文件明明存在却报 `No such file`，重跑一次即好——别急着改代码。
- **gocl.exe 是 Windows 程序**：直接喂 `/mnt/d/...` 路径会报 `"the system cannot find the path specified"`，因为那些是 Linux 路径，gocl.exe 拿它们去问 Windows 文件系统。`tools/portability/lin_build.sh` 的头注释把这个写成了规范：**"So the compile happens here and only the run happens in the alpine container"**。所以 `gocl -target linux` 必须**两段式**：Windows 侧编（`tools/portability/lin_build.sh`，编出 `tmp/portability-out/*`）+ WSL 里跑（`tools/portability/lin_run.sh`，`DST=/mnt/d/Projects/goc/tmp/portability-out`）。
- **为什么两段式必须是脚本文件而不是内联命令**：`lin_run.sh` 的头注释记着原因——Git Bash 交给 WSL 的 shell 会把 `"$t"` 交给 Windows 路径转换，内联循环里每个 case 都变成 `./: Permission denied`，**"读起来像链接失败，而它不是"**。`lin_run.sh` 还额外用 `run_ok` 处理"输出含内核选择的端口、无法比对固定字符串"的自报告用例（socket 那两个），判据是**退出码 0 + 末行 `OK`**。
- **别把编译器当程序跑**：验证 Windows 时误把 `bin/goc.exe`（**编译器**）当 `ls` 二进制执行，报 `"read <dir>: Incorrect function"`——那是编译器在把目录当源文件读，不是回归。真正的 `ls` 是 `apps/coreutils/bin/ls_gocl.exe` / `ls_goc.exe` 这类编译产物（`apps/coreutils/build.sh:35-36`；Linux ELF 版在 `build.sh linux` 分支产出 `ls_goc`）。
- **`write(2)` 用例不能列进 Windows 组**：`win_regress.sh` 的头注释专门记了这条——直接调 `write(2)` 的用例在 Windows 上必然失败，列进去"看起来像坏了"，实际上那个平台没有这个 syscall。它们只在 `lin_build.sh` 那一组里。

---

## 收尾：这一轮学到的方法

这一轮踩的坑分属三个完全不同的层（编译器宏机制 / 链接期符号机制 / codegen 语义），但把它们串起来看，真正可迁移的东西收敛成几条。其中 (a)(b)(c) 是这一轮新增的，也是最有复用价值的。

**(a) 二分定位比读代码快。** 坑 4 那条隔离序列是全文最可复用的方法：

```
m1 printf OK  →  m2 getenv OK  →  m4 stat OK  →  m5 open/close OK
m3 opendir SEGV  →  m6 复刻函数体（崩在 malloc(1328)）
m7/m8 单独 malloc(16) SEGV   →  确认 malloc 本身坏
m9 直接 brk() →  undefined symbol: syscall
```

它的价值不在于"这次找到了 brk"，而在于**序列本身是有设计的**：
- **先测最宽的通路**（printf、getenv、open/close）——它们 OK，说明输出、环境、fd 都正常，把"整个 Linux 目标坏了"这个假设否掉；
- **按调用链从上往下切**（opendir 崩 → 复刻 opendir 体 → 崩点收敛到 malloc）——每一步只把搜索空间缩小一档，且每一步的产物都是"下一步的输入"（复刻出的 opendir 体是可以直接编译的小程序）；
- **在可疑层上做两个极端探针**（`malloc(1328)` vs `malloc(16)`）：从"1328 是不是太大了"这种实现细节怀疑，收缩到"任何 malloc 都坏"；
- **最后确认机制**（m9 直调 brk → 报 undefined symbol: `syscall`）——这一条证伪了"桩缺失"这个方向，把矛头指回宏。

读完一整个 `syscall.h` 可能要十分钟，而这条序列每一步都是"编一个 10 行的程序看结果"。**代价对比越悬殊，越应该在遇到"现象和病因隔了好几层"时优先选后者。**

**(b) "报告的位置"和"出错的位置"在两层意义上都会错。** 这是坑 1 和坑 4 共同的教训，值得分开说：

- **文件名会错**：坑 1 报 `goclib/goclib.h`，病因在 `src/common/preprocess.go:150`——编译器自己的宏表少了一项。
- **行号会错**：同一条错误报的是 `goclib.h:51`，而 `:51` 那个位置上根本没有任何可疑的东西（真的 `__builtin_va_list` 定义在 `stdarg.h`）。更极端的是坑 9：报 `undefined symbol: fputc`，而源码里**一个 `fputc` 字都没有**——报错指向的是优化器**改写之后**的名字。

所以这条经验不是"报错信息不可靠"（那太消极了），而是**"报错信息是关于某个中间产物的陈述，不是关于病因的陈述"**。可操作的做法有两个：顺着**它 include/引用进来的东西**往上追（坑 1 追到 `stdarg.h`），以及**报错里出现的名字如果在你写的代码里搜不到，先怀疑"谁改写了它"**而不是先怀疑"我漏写了它"（坑 9）。

**(c) 同一份 goclib 要同时被三种编译器接受，任何一条平台分支写错都只在那一侧暴露。** goclib 的头文件同时被 `goc`（goa 后端）、`gocl`（LLVM 后端）、`gcc`/`clang`（宿主）编译。这个目标不是"多支持一个平台"的附加要求，而是**一整套 bug 暴露机制**，本文 13 个坑里有一半来自它：

- 坑 1：`#ifdef __goc__` 恒假，是**跨编译器**分支写错；
- 坑 11：`syscall()` 返回值编码差异，是**跨编译器**路线不一致；
- 坑 12/13：字段名与 `readdir` 过滤，是**跨平台**分支不对称。

关键在于**错误的可见性是不对称的**：一条平台分支写错，只会在**那一侧**暴露，而且往往要等到目标平台上真跑一次才看得见（坑 4 顺手修好 goc 后端那个同样坏掉的 bug，就是这个道理——它此前一直坏着，只是没人跑）。**推论是：不能让"某一条分支没被测到"变成"某个 bug 潜伏"。** 这条也是本文几个"顺手修好"和"此前同样坏，只是没暴露"的解释——**看起来独立的 bug 往往是同一个 bug 在不同侧面的显影**。

**(d) 一个 bug 的名字不等于它的范围。** 看到 `-2 → 4294967294` 就断言"有符号除法丢了符号性"是误诊（见坑 1 的姊妹篇：变参窄整型）。真正定位靠**二分**：变量除法对、字面量除法错；不经变参对、经变参错。先收窄范围再下结论。

**(e) 先排除环境，再怀疑代码。** `git stash` 全部改动后仍失败 = 不是我改的；陈旧构建产物遮蔽 = 不是代码回归。这两步能在 30 秒内把"代码 bug"和"环境坏"分开——而它们长得**一模一样**（都是"我明明修好了却报旧错"）。坑 2 就是被这一步 30 秒定位掉的。

**(f) 用独立第三方实现当裁判。** 怀疑 gocl 生成的 IR 不对时，把同一份 `.ll` 丢给 clang 编，对照反汇编。clang 没这个问题 → 是 gocl 的读法越界。手里有等价实现时，别只跟自己的另一个版本比——两个后端可能**共享同一个错误假设**（坑 4 的 `__goc__` 注释就是一个共享的错误假设）。

**(g) 几个不相干的调用同时坏，先数共同特征。** 坑 3 的线索就是"它们唯一的共同点是参数个数 ≥4"。而这种线索恰恰是最容易被跳过的维度——因为源码里那几个函数的参数表看起来毫不相干。

**(h) 后端优化会改变你写下的调用形态，任何"按名字裁剪"的逻辑都要和优化器对账。** 见坑 9 的教训段。

**(i) identity 宏是最阴的坑。** 编译能过、链接能过、运行才炸，而且炸在完全不相关的 `malloc`。凡是有"为了宿主兼容而写的恒等/透传宏"，都要问一句：它真的透传了吗，还是把关键调用吞了？

---

## 涉及的主要提交

| 提交 | 内容 | 关联坑号 |
| --- | --- | --- |
| `dc0f8e6` | `feat(goclib): 让 goclib 成为可被 gcc/clang 编译的通用 C 库`。引入 `p.macros["__goc__"]`（`preprocess.go:150`，原根因分析留在 `:137-149`）；**同时**引入了坑 4 的错误 identity 宏（当时的 `syscall.h:85-87`，注释原文 "so on this host the mapping is the identity"） | 坑 1（修）、坑 4（**引**，潜伏到 `c9e2b58`） |
| `95ed623` | `fix: Linux syscall calls pass 4th arg in r10, not rcx`。**第一轮**：把 r10 特例加在调用方，引入 `callArgRegs(name, indirect)`；暴露 `mmap`/`futex`/`wait4`/`clone` 返回 -9（EBADF），`MAP_ANONYMOUS` 丢失；顺手改 `threads.c` 错误检查（负 errno 而非 -1） | 坑 3（**第一轮**） |
| `dfacc05` | `fix: Linux 第 4 参数的 r10 约定下沉到 syscall 桩`。**第二轮**：特例从调用方撤进桩（`goa/asm.go:688-691` 的 `mov r10, rcx`，`codegen.go:267-269` 退化为无条件 `argRegs()`）；暴露 `select`/`setsockopt`/`recvfrom` 的 EFAULT（errno=8）。同提交顺带修坑 10 的 `DLLNames`（`gocl/cmd/gocl/main.go:329-338`，**与坑 3 无关**） | 坑 3（**第二轮**）、坑 10 |
| `2db9259` | `feat(goclib): TCP/IP 层 —— 可被 goc/gocl/gcc/clang 编译的 BSD socket`。**引入 include 守卫 `GOC_SYSCALL_H`**（不是坑 11 那次加的） | 坑 11（守卫来源） |
| `fbc0b42` | `gocld: become a real linker -- goc -c emits relocatable objects`。引入 `__goc_syscall` 单一入口，使 gocrun 能在 Windows 测试时把整条桩重写成 Win32 翻译器 | 坑 3（机制前提） |
| `c9e2b58` | `feat(apps/coreutils): ls 双后端构建 + 跨平台逐字节一致；修 gocl Linux 崩溃`。goclib：`__goclib_brk` identity → `brk(addr)`（`syscall.h:130`，**同时修好 goc 与 gocl 两后端的 Linux malloc**）、`stat.h:69` 的 `st_mtime` 宏别名、`dir.c:95-111` 的 Windows `readdir` 过滤。gocl：`operator.go:283-284` 的 `promoteInt`、`statement.go:319`/`:332` 的 `breakTo` 压/弹栈、`call.go:420-423` 的 `member()` 数组衰变、`types.go:296-297` 的 `libFunc` 查找。common：`printfspec.go:92-99` 的 `fputc`。rebase 在 `28a8d72`（TLS 支持）之上 | 坑 4–9、坑 12、坑 13 |

验证结论：`ls` 在 goc/gocl 双后端、Windows PE 与 Linux ELF 四种组合下，`plain/-a/-F/-1` 输出逐字节一致，`-l` 名字列一致（大小/日期两平台不同，属设计内）。