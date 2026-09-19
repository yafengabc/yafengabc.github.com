---
title: "Go vs Nim 性能实测（二）：大数运算与 GMP 四端"
menuTitle: "Go vs Nim（二）大数与 GMP"
date: 2026-09-19T12:30:00+08:00
draft: false
weight: 20
tags: ["Go", "Nim", "大数运算", "GMP", "benchmark"]
categories: ["编程开发", "性能测试"]
description: "Go math/big 四项全胜、Nim 生态库落后最多 210 倍、自研 Karatsuba/Toom-3 库可提升一个数量级；GMP 四端 FFI 开销≈0；百万位 π 里 math/big 输在除法与开方。"
---

> **系列文章**
> [（一）6 项常规算法对决](/programming-misc/go-vs-nim-algorithms/) ·
> **（二）大数运算与 GMP 四端**（本篇） ·
> [（三）6 种 GC 模式全景对比](/programming-misc/go-vs-nim-gc/)

[第一篇](/programming-misc/go-vs-nim-algorithms/)的结论是：常规负载下 Go 与 Nim 基本打平（6 项合计差 3.5%）。但大数运算是另一个世界——差距会从百分之几拉到**数量级**。

本篇覆盖三轮测试：**4 项大数运算（三实现对比）→ GMP 四端 FFI → 百万位 π 根因拆解**。环境、校验和方法与第一篇相同，不再重复。

## 一、大数运算：Go math/big vs Nim bigints vs Nim 自研库

这里对比三个实现：Go 标准库 `math/big`、Nim 生态库 `bigints`，以及**用 Nim 自研的深度优化大数库**（2 的幂 limb + Karatsuba/Toom-3 乘法 + Knuth 长除法）：

| 基准 | 说明 | Go (ms) | bigints (ms) | 自研库 (ms) | 更快 |
| --- | --- | ---: | ---: | ---: | --- |
| fact10k | 10000!（结果约 3.6 万位） | 8.0 | 14.4 | 55.8 | Go |
| fibBig100k | fib(100000)（约 2.1 万位） | 28.5 | 102.0 | 105.3 | Go |
| mulBig500k | 两个 40~50 万位大数相乘 | 1.0 | 210.3 | 37.4 | Go |
| modpow2048 | 2048 位模数模幂（RSA 风格） | <0.5 | 24.6 | 0.5 | Go |

![大数运算三实现对比](charts/bench-bigint.svg "大数运算：Go math/big vs Nim bigints vs Nim 自研库（线性轴）")

结论有三层：

1. **Go `math/big` 四项全胜**，自研库相对它慢 3.7~37 倍（模幂差距更大）。Go 的 math/big 经过多年打磨：原生 limb 原语、更细的算法阈值、更少的分配，这些都不是一个周末能追上的。
2. **但自研库 vs 生态库 `bigints` 是数量级碾压**：`mulBig500k` 快 5.6×（210→37 ms）、`modpow2048` 快 48×（24.6→0.5 ms）、`fibBig100k` 持平；只有 `fact10k` 慢 3.9×——阶乘是「小整数 × 大数」的链式乘法，瓶颈在逐次分配与朴素乘法常数，bigints 对此有更省的分支。
3. **把乘法从 O(n²) 提升到 Karatsuba/Toom-3、除法用 Knuth 长除法，可以在同一生态内带来数量级提升**（模幂 48×）。这印证了一个老观点：对超大数，**算法复杂度 > 编译器 > 语言本身**。

自研库的设计要点（`src/nim_bigint.nim`）：小端序 `seq[uint32]`、base 2³² 的 2 幂 limb；乘法三档分派（朴素 O(n²) → Karatsuba ≥24 limbs → Toom-3 ≥192 limbs）；除法用 Knuth Algorithm D（64 位试商 + 乘减 + 下溢纠正）；另有 `modpow` 平方-乘、`mod1e6` 校验和、十进制转换等。正确性通过 400 组随机小规模 + 16 组大乘（覆盖 Karatsuba/Toom-3）+ 20 组长除法 + 特殊形态，全部对照 `bigints` 通过。

连同常规 6 项，10 项合计：**Go 887 ms vs Nim(bigints) 1171 ms vs Nim(自研大数 + bigints 常规) 1019 ms**。常规打平，大数拉开差距。

## 二、GMP 四端对比（把生态库换成 GMP 之后）

生态库拉胯，那直接上 GMP 呢？补充一轮四端对比：Go `math/big`、Go 经 cgo 调 GMP、Nim 手写 dynlib FFI 绑 GMP（Nim 2.2 已移除 std/gmp）、C 原生链接 GMP（零 FFI 开销参照线）。负载加大到百毫秒级以降低噪声：

| 基准 | Go math/big | Go+GMP (cgo) | Nim+GMP (FFI) | C+GMP (原生) | 说明 |
| --- | ---: | ---: | ---: | ---: | --- |
| fact100k（100000!，约 45.7 万位） | 731.69 | 751.27 | 682.91 | **678.51** | C ≈ FFI；cgo 慢约 11% |
| fibBig500k（fib(500000)） | 498.86 | 515.44 | **440.76** | 447.17 | math/big 慢约 13% |
| mulBig50M（(2⁵⁰⁰⁰万-1)×(2⁴⁰⁰⁰万-1)） | **159.91** | 298.60 | 296.08 | 305.73 | math/big 快约 1.9× |
| modpow8192（3^(2³⁰⁰⁰) mod 2⁸¹⁹²-159） | 57.42 | 36.42 | **36.12** | 36.31 | GMP 快约 58% |
| pi1000000（100 万位 π，Chudnovsky+BS） | 8104.29 | 1034.78 | 740.49 | **715.50** | GMP 快 7.8~11.3× |

![GMP 四端大数对比](charts/bench-gmp.svg "GMP 四端大数对比（线性轴，不含 π）")

四个结论，越往后越反直觉：

1. **Nim FFI 开销 ≈ 0~3.5%**。手写 dynlib 绑定 + 裸调用，每次跨界调用约 10~25 ns，单次大负载场景（fact/fib/modpow）与 C 几乎等价。**"FFI 慢"这个刻板印象，在 Nim 上不成立。**
2. **cgo 在"跨界小调用密集"场景最贵**：π 的 binary-splitting 树有约 100 万次跨界小调用，cgo 慢 45%；链式大调用（fact/fib）慢 11~15%。单次大负载调用 cgo 与 C 等价（modpow 只差 0.3%）。
3. **纯乘法反而是 math/big 快 1.9×**：Go 1.22+ 的 math/big 内置 FFT（阈值低），本机 MSYS2 的 GMP 构建较旧、FFT 调度偏保守。补充探针也印证：200 万位乘法 math/big 2.76 vs GMP 6.35 ms、1000 万位 22.86 vs 38.93 ms（快 1.7~2.2×）。
4. **但 RSA/模幂 GMP 的 powm（Montgomery + 滑动窗口）优势显著**：8192 位下快约 58%（4096 位时只有 19%，差距随规模放大）。而 π 这种「除法/开方密集」的负载，GMP 直接快 7.8~11.3×。

## 三、百万位 π 的根因拆解（为什么 math/big 大输）

π 的差距不是整体性的，而是**结构性**的。把 π 计算（Chudnovsky + binary splitting）切成 4 段分别计时：

| 阶段 | Go math/big | C+GMP | 倍率 |
| --- | ---: | ---: | ---: |
| bs 乘法树 | 1593.0 | 504.5 | 3.2× |
| 开方（10^(2e6)×10005） | **5705.1** | 62.5 | **91×** |
| 最终乘法（Q×426880×√C） | 255.5 | 32.9 | 7.8× |
| 最终除法（num/T） | 411.2 | 78.0 | 5.3× |
| **总计** | **7964.9** | 677.9 | **11.7×** |

![π 百万位阶段拆解](charts/bench-pi-stages.svg "π 百万位阶段拆解：Go math/big vs C+GMP（线性轴）")

根因非常明确：

- **开方段占 Go 总耗时的 72%**。Go 的 `Int.Sqrt` 是朴素牛顿迭代 `z ← ⌊(z + ⌊x/z⌋)/2⌋`——**每一轮都是一次 O(n²) 的 Knuth 大除法**。初值 `2^⌈(n+1)/2⌉` 与 √x 差约 20%（radicand 的 bitlen 恰为偶数 6643870），平方收敛到 1 ulp 需要 log₂(bitlen) ≈ 22 轮（实测 (z₁²-x) 的位长每轮减半）。而 GMP 的 `mpz_sqrt` 内部用 `mpn_sqrtrem` 近似算法，主循环是乘加、无大除法，只花 62.5 ms。
- 最终除法 5.3×：math/big 除法没有 subquadratic 算法，GMP 用 DBL/牛顿。
- bs 乘法树 3.2×：大量中规模乘法调度 + Go 节点分配触发 GC。
- 反过来说，**单次 100 万位乘法 Go 反而快（16.3 vs 20.8 ms）——math/big 输在除法与开方，不在乘法**。

## 小结

| 场景 | 推荐 | 理由 |
| --- | --- | --- |
| 大数乘法/模幂（自己写） | **Go math/big** | 深度优化碾压生态库；Nim 需自研算法级优化才能接近 |
| 大数模幂（可引 GMP） | Nim+GMP FFI / C | FFI 开销≈0，GMP powm 快 58% |
| 除法/开方密集（π 类） | **GMP** | 比 math/big 快 7.8~11.3×，开方段 91× |
| 高性能 FFI 调用 | Nim dynlib | cgo 在百万次小调用场景慢 45% |

---

**下一篇**：[（三）6 种 GC 模式全景对比](/programming-misc/go-vs-nim-gc/)——同一份 Nim 代码在 6 种 GC 模式下分别编译，与 Go GC 对照 7 项负载：没有绝对赢家。
