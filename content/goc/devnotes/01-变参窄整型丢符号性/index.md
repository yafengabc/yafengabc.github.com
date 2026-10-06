---
title: "开发笔记：gocl 变参窄整型丢符号性"
menuTitle: "变参窄整型丢符号性"
date: 2026-10-06T14:30:00+08:00
draft: false
weight: 1
tags: ["goc", "gocl", "LLVM", "ABI", "变参", "bugfix", "开发笔记"]
categories: ["编程开发", "goc", "开发笔记"]
description: "gocl（LLVM 后端）把所有经变参传递的 32 位整型的符号性丢掉。本文记录从「误诊为有符号除法」到「定位为变参8 字节槽未填满」的完整排查过程、根因、修复，以及两次误诊里学到的东西。"
---

> 这是一篇**开发笔记**，不是教程。记录的是一次 bug 从"看不懂"到"修掉"的完整过程，包括走错的弯路。教程正文里不写这些。

## 现象

`src/examples/stress.c`，两个后端跑同一份源码：

```
goc  :  div=-2 mod=-2
gocl :  div=4294967294 mod=4294967294
```

`-2` 变成了 `4294967294`（= 2³²- 2）。第一反应当然是"有符号整数除法在 LLVM 后端丢了符号性"。

这个诊断**是错的**。错得不只是细节，而是整个方向。

## 第一次定位：排除算术

先确认除法本身没问题。`a = -7, b = 3`：

```
goc  : -2
gocl : -2        ← 正确
```

变量除法对。再试常量折叠，`-8 / 3`：

```
goc  : -2
gocl : -2        ← 也对
```

算术和常量折叠都排除了。但 `-8` 这个**字面量**作为运算数时是好的，作为 `printf` 的**实参**时就坏。所以问题不在表达式，在**传参路径**。

关键分水岭是这一条：

```c
print(a / b);              // 对
printf("%d", a / b);       // 错
```

前者不走变参，后者走。范围一下从"除法"缩到"变参"。

## 第二次定位：排除 printf

不能假设是 `printf` 的问题——gocl 对 `printf` 做了特化降级（`printf` → `printf_lite` / `printf_lite_with`），但特化本身就是嫌疑对象。

于是自己写一个不依赖 goc 特化的 `myprintf`：

```c
#include <stdarg.h>
static int mycount(int n, ...) {
    va_list ap;
    va_start(ap, n);
    int total = 0;
    for (int i = 0; i < n; i++) total += va_arg(ap, int);
    va_end(ap);
    return total;
}
```

`mycount(3, 10, -20, -30)`：

```
goc  : -40
gocl : 4294967246      ← 还是错
```

**特化排除了。** 这是通用变参 bug：`va_arg(ap, int)` 从 8 字节槽里读出了错的东西。

## 第三次定位：IR 是对的

用 `-dump-ir` 把 IR 倒出来：

```ir
%t8 = sub i32 0, 8
call i32 @__goclib_printf_lite(ptr %t7, i32 %t8)
```

IR 完全正确。`i32` 在 LLVM 里天然是有符号类型，`sub i32 0, 8` 就是 `-8`，没有类型错误、没有算错。

到这一步可以下一个很强的结论：**问题不在 IR 生成，在调用点。**

## 第四次定位：反汇编

把 gocl 产出的 `stress.exe` 反汇编，看那个调用点：

```asm
mov    $0xfffffff8, %edx
```

问题浮出水面了。

`%edx` 是 64 位寄存器，这条指令**只写了低 32 位**。高 32 位（`%rdx` 的上半）保留着栈上的残留垃圾。

再看 goclib 那边怎么读：

```c
// printf_lite_with 的 va_list 形参，前端把 va_list typedef 成 char*
// 所以它按 8 字节 ptr 读这个槽
```

于是读出来的是 `0x????????fffffff8` 而不是 `0x00000000fffffff8`。`-8` 变成 `4294967288`。

## 决定性证据：让 clang 编同一份 IR

到这里还有个说不通的地方：**LLVM 自己生成的代码会不会也这样？** 如果会，那"gocl 写错了"的结论就得推翻。

把刚才那份 `.ll` 丢给 clang（MSYS2 UCRT64 自带clang 22.1.8）：

```bash
clang -target x86_64-pc-windows-msvc -c stress.ll -o stress_ref.o
objdump -d stress_ref.o
```

同一个调用点，clang 生成：

```asm
xor    %edx, %edx
sub    $0x8, %edx
```

clang **显式清零了高 32 位**。

对照自研后端（Go 直接发指令）同处：

```asm
movsxd %rax, %eax        ; 符号扩展到 64 位
```

三条证据指向同一结论：**gocl 的 `va_list` 读法违反了调用约定。**

## ABI 依据

C 7.16.1.1：每个变参占一个 **8 字节槽**。Win64 / SysV 的寄存器保存区（`al` 用的那8 个）本身就是 8 字节槽的数组。

`i32` 变参只填槽的**低半边**，高半边按约定是**未定义**的。clang 清零它，是为了让后续 64 位读取得到确定值（属于超出要求但很常见的安全习惯）。**所以 LLVM 的行为合规，是 callee 的读法越界了。**

问题定性：**gocl 生成的调用点没有把窄整型实参扩展到满8 字节。**

## 修复

`src/gocl/call.go` 新增一个函数：

```go
// widenVarargSlot 把窄整型变参扩展到它完整的 8 字节槽。
// C 7.16.1.1 规定每个变参占一个 8 字节槽；窄整型只写低半边，
// 高半边按约定未定义。goclib 侧按 8 字节 ptr 读槽（va_list 被
// typedef 成 char*），所以必须在这里填满，否则读到栈残留。
func (e *irEmitter) widenVarargSlot(v val) val {
    if v.ty == nil || v.ty.Kind != frontend.KInt {
        return v
    }
    lty := e.ty(v.ty)
    switch lty {
    case "i8", "i16", "i32":
    default:
        return v
    }
    out := e.newTmp()
    opc := "zext"
    if v.ty.Signed {
        opc = "sext"
    }
    e.line("%s = %s %s %s to i64", out, opc, lty, v.op)
    return val{op: out, ty: &frontend.Type{Kind: frontend.KInt, Width: 8, Signed: true}}
}
```

两个调用点都要加：`callExpr`（直接调用）和 `indirectCall`（间接调用）—— 只修一条会漏。

```go
v = e.defaultPromote(v)
v = e.widenVarargSlot(v)   // 新增
```

`signed` 用 `sext`，`unsigned` 用 `zext`，这个选择直接来自被扩展类型的符号性，不额外判断。

## 验证

- `stress.c`、`va_arg` 用例、`mycount` 三项输出与自研后端**逐字节一致**
- 边界覆盖全部通过：`signed char` / `unsigned char` / `short` / `unsigned short` / `int` / `unsigned` / `long` / `unsigned long` / `double`
- gocl 与 goc 在所有边界类型上的输出**完全一致**
- Linux ELF：24347 字节，`elfcheck --structure-only` 通过

## 两次误诊，学到了什么

**误诊一：症状命名。** 看到 `-2 → 4294967294` 就断言"有符号除法丢了符号性"。教训是——**一个 bug 的名字不等于它的范围**。真正暴露 bug 的不是那次除法，而是后面"变量除法对、字面量除法错"、"不经变参对、经变参错"这些**二分**动作。如果一开始就接受了自己的命名，就直接跳到"改除法实现"，会在错的地方改很久。

**误诊二：自己写的最小复现差点又骗我。** 为了排除 `printf` 特化写了 `myprintf`，第一次跑出错的。但那次的错误**可能来自别的地方**（当时还没排除算术），我却准备拿它当"稳定复现"。教训是——最小复现要**逐个排除变量之后**才算数，先用它证伪一件事，再拿它当基准。

还有一条方法上的：**用独立的第三方实现（clang）编同一份中间表示**。这一步同时证伪了"gocl 生成错了"和"我自己的判断"两个假设，是整条链上性价比最高的一次验证。手里有等价实现时，别只跟自己的另一个版本比。

## 附：另一个独立议题（未修）

`src/gocl/cmd/gocl` 的 3 个单测（`TestGoclEndToEnd/string_handling` 等）报：

```
goclib/goclib.h: line 51: expected type specifier, got "__builtin_va_list"
```

`src/goclib/stdarg.h:20` 有 `#ifdef __goc__`，而 `__goc__` 在 `src/common/preprocess.go:150` 无条件注入，所以**从正常 cwd 跑完全不受影响**。`git stash` 验证过：改动前后同样失败，非本次引入。

精确复现条件：**仅当 cwd = `src/gocl/cmd/gocl` 时失败**。`probe()` 有 4 层深度限制，疑似库定位或宏作用域问题。独立议题，尚未定位最终根因。
