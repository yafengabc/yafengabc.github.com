---
title: "第 3 章：体积实测——goc、LLVM 后端、gcc 三方对照"
menuTitle: "第 3 章 体积实测"
date: 2026-10-06T12:40:00+08:00
draft: false
weight: 4
tags: ["goc", "体积优化", "LLVM", "gcc", "benchmark", "实测"]
categories: ["编程开发", "goc"]
description: "全部自己跑出来的数字：Hello World 的 print 版 2048 字节、printf 版 6656 字节，gcc 是 38989 字节。附带发现 LLVM 后端反而更小，以及一套验证第二后端正确性的办法。"
---

前面两章反复出现"小很多""小 83%"这类说法。这一章把数字摊开——**全部是实测**，不是引用。

测量环境：Windows 11 + MSYS2 UCRT64，gcc 16.2.0（`/d/msys64/ucrt64/bin/gcc.exe`），goc 提交 `f200cd1`。gcc 加不加 `-static` 结果一样（因为默认就链到静态的 msys-2.0 dll）。

## Hello World 三方对照

先看最简单的情况。三种写法，同一份"输出 Hello, world!"的意图：

```c
// A. goc print 内建
int main() { print("Hello, world!"); return 0; }

// B. printf（goc 与 gcc 都用这个）
#include <stdio.h>
int main() { printf("Hello, world!\n"); return 0; }

// C. gcc 手写等价 print（用 printf 拼，因为 gcc 没有 print 内建）
#include <stdio.h>
static void print_str(const char *s){ printf("%s\n", s); }
int main(void){ print_str("Hello, world!"); return 0; }
```

实测结果：

| 写法 | 编译器 | 产物体积 |
| --- | --- | ---: |
| A`print("Hello, world!")` | **goc** 自研后端 | **2048 字节** |
| A 同上 | **gocl** LLVM 后端 | **1536 字节** |
| B `printf` | **goc** 自研后端 | **6656 字节** |
| B 同上 | **gocl** LLVM 后端 | **4096 字节** |
| C 手写 print 等价 | gcc -O2 | 38989 字节 |
| B `printf` | gcc -O2 | 38989 字节 |

几个观察：

**1. `print` 内建确实把体积砍到了1/19。** goc 的 2048 字节 vs gcc 的 38989 字节，差 94.7%。而且 **`-O0` 就是这个体积**——不是优化出来的，是架构上就没那么多东西。

**2. 但 `print` 不是免费的。** 换成 `printf`，goc 从 2048涨到 6656（+4608）。这个增量就是 printf 家族的代码——格式化、`%d` 的整数转文本、512 字节输出缓冲。gcc 那边 `printf` 和手写 `print` 体积完全一样（38989），因为 CRT 反正已经在那了，多一个薄包装不占地方。

**3. `puts` 是"看起来轻，实际不轻"的那个。** 实测 goc 的 `puts("Hello, world!")` 是 **7680 字节**，比 printf 还大。原因是 puts 依赖 goclib 里完整的字符串库和 stdout 缓冲初始化，而 `print` 是专门写的最薄路径。**在 goc 上，`print` 比 `puts` 更省**——这跟直觉相反，但符合"内建按需发射"的设计。

![Hello World 三方体积对比](images/size-compare.svg "Hello, world! 三方产物体积对比：goc 2048 / gocl 1536 / gcc 38989 字节")

## 稍复杂的程序：stress.c

`src/examples/stress.c` 是个综合用例——函数调用、多返回值、算术、嵌套循环、比较、位运算都有。

![stress.c 后端对比](images/backend-compare.svg "stress.c：goc 自研后端 vs gocl LLVM 后端 vs gcc，字节越少越好")

| 优化档 | goc（自研） | gocl（LLVM） | gcc -O2 |
| --- | ---: | ---: | ---: |
| `-O0` | 9728 | **5632** | 40784 |
| `-O1` | **7680** | **5632** | — |
| `-Os` | **7680** | **5632** | — |
| `-O2` | 7680 | 5632 | 40784 |

这里有两个值得说的点。

### gocl（LLVM 后端）比自研后端小 27%~42%

这个结果**出乎我的意料**，但完全合理。

LLVM 的强项是全局值编号（GVN）和 SSA 形式的死代码消除——它是先构建完整的控制流图再优化，而不是逐条指令流式改写。goc 的 `-O1` 是在指令流上跑五个 pass（内联、常量传播、窥孔、死存储消除、跨块死存储消除），局部性很强但缺少全局视角。

更明显的是：**gocl 在 `-O0` 档就已经是 5632 字节，等于 goc 在 `-O1`/`-Os` 档的水平**。也就是说 LLVM 那套优化几乎是"免费"的——因为 SSA 形式天然就是优化友好的表示。

goc 的 `-O1` 能把 9728 压到 7680（-21%），说明自研优化器确实在干活，但和 LLVM 还有一个量级上的差距。

### gcc 在这条用例上赢了数字，但赢得没意义

gcc -O2 是 40784 字节，goc 是 7680——小 81%。但这个数字**说明不了 goc 更优**，因为它同时拖了 `msvcrt.dll`，而 goc 一个字节都没拖。

要公平比较，得看**体积 + 依赖**两个维度。这也是 goc README 里那张表的口径。

## 怎么验证 gocl 的输出是对的

gocl 是第二后端，产物更小，但要用它之前得先确认它**算得对**。有个现成的办法：**拿它和自研后端逐字节对比**。

```bash
# 自研后端（基准）
./bin/goc.exe run src/examples/stress.c > out_goc.txt

# gocl（注意必须给 -o，否则不产出文件）
./bin/gocl.exe -o s.exe src/examples/stress.c && ./s.exe > out_gocl.txt

diff out_goc.txt out_gocl.txt
```

为什么这样比对有价值？因为**两个后端的实现路径完全不同**——自研后端是 Go 直接发 x86-64 指令，gocl 是把整个程序降成 LLVM IR 再交给 LLVM 优化。路径不同意味着**没有共享的错误**，两份输出不一致时差异点就是问题所在。

gocl 还支持把 IR dump 出来：

```bash
./bin/gocl.exe -dump-ir -o s.exe hello.c    # 旁边留下 hello.ll
```

这个开关在排查问题时非常有用——它能立刻回答"是 IR 层就错了，还是后面的 codegen 错了"。比如你IR 里写的是 `sub i32 0, 8`（正确），但反汇编出来只有一条写低 32 位的 `mov`，那就说明问题出在调用约定的 8 字节参数槽上，跟 IR 无关。

想更进一步，可以用 clang 编译同一份 IR 做参照——MSYS2 的 UCRT64 里就有 clang：

```bash
clang -target x86_64-pc-windows-msvc -c hello.ll -o hello.o
objdump -d hello.o
```

LLVM 的行为是可预测的，所以"clang 也这么生成"就说明问题不在 IR 内容上，而在 goc 自己的某个假设里。

> gocl 目前是实验性后端（roadmap 里 P0 还有缺口），默认后端完全不受影响。是否切换，建议先用上面的方式自己验一遍。

## 复现方法

想自己验这些数字：

```bash
# goc 自研后端
./bin/goc.exe -c -O1 -o s.exe src/examples/stress.c
ls -l s.exe

# gocl LLVM 后端
./bin/gocl.exe -O1 -o sl.exe src/examples/stress.c
ls -l sl.exe

# gcc 参照（注意要给 stdio.h）
gcc -O2 -o gcc_stress.exe -include stdio.h src/examples/stress.c

# 对比输出（注意 gocl 需要 -o）
./bin/goc.exe run src/examples/stress.c > out_goc.txt
./bin/gocl.exe -o sl_run.exe src/examples/stress.c && ./sl_run.exe > out_gocl.txt
diff out_goc.txt out_gocl.txt
```

## 读数时的一个坑

我一开始测出"`print` 是 6656 字节"，以为自己抓到了 README 的错（README 说 2048）。结果**README 没错**——2048 对应的确实是 `print` 内建，是我把 `printf` 版的产物当成了 `print` 版。

这类实测最容易出错的地方不是数据本身，而是**数据对应的是哪份源码、哪个版本**。所以这篇里每个数字都注明了来源。同样的道理也适用于结论：光看一个数字没法判断对错，得知道它是在什么条件下测的。

## 小结

| 结论 | 依据 |
| --- | --- |
| goc vs gcc 差一个量级 | 依赖表 + 体积双重优势 |
| `print` 内建比 `puts` 更省 | 2048 vs 7680 |
| `print` 内建在 `-O0` 就是 2048 | 不是优化出来的 |
| gocl（LLVM）比自研后端小 27%~42% | stress.c 三档实测 |
| gocl 的 `-O0` 等于自研后端的 `-O1` | 5632 vs 7680 |

最后一条最值得琢磨：**LLVM 的 SSA + GVN 几乎是免费的优化**，而自研后端要跑五个 pass 才达到接近的效果。差的不只是优化能力，更是表示形式——LLVM 从一开始就是优化友好的 SSA，goc 是在指令流上做局部改写。

这背后有个通用道理：**中间表示的设计决定了优化的天花板**。想在现有表示上继续榨性能，不如回头改表示。

下一章[项目状态与roadmap](/goc/04-项目状态/)会把这几天实测到的能/不能全部列出来，包括回归数字和一个"失败其实源自环境缺失"的例子。

---

> 本章所有数字均为实测，核对于 2026-10-06，goc 提交 `f200cd1`。goc 迭代很快，读到时若已更新，以仓库 README 为准。