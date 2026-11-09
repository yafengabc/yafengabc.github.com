---
title: "goc32：TinyOS 用户程序的 C 编译器"
menuTitle: "goc32"
date: 2026-11-09T10:00:00+08:00
draft: false
weight: 5
tags: ["goc32", "TinyOS", "C 编译器", "RV32", "ESP32-C3", "ARM"]
categories: ["编程教程", "goc32", "TinyOS"]
description: "goc32 是一个不链接 libLLVM 的 TinyOS 用户态 C 编译器，一条命令把 C 源码变成 LZ4TOS 用户程序。本小节包含使用教程和用户程序 API 参考。"
---

**goc32** 是 [TinyOS](https://github.com/yafengabc/TinyOS) 的用户态 C 编译器，也是 [goc](https://github.com/yafengabc/goc) 前端在 32 位嵌入式方向上的延续。

它刻意不走 libLLVM 路线：

```text
C 源码
  -> goc 前端（纯 Go 的 C 前端）
  -> gocl 生成的 LLVM 风格 IR 文本
  -> native32（纯 Go 的 RV32IM / Thumb 发射器）
  -> clang 只负责汇编
  -> ld.lld 只负责链接
  -> LZ4TOS 用户程序封装
```

没有 cgo，也没有 `libLLVM.dll`。clang 和 ld.lld 只出现在最后两步，和“用 clang 做代码生成”是两件事。

## 本小节文章

| 文章 | 内容 |
| --- | --- |
| [使用教程](/goc/goc32/usage/) | 安装、命令行、目标平台、GPIO/文件示例、放进 TinyOS 运行、体积现状、限制与排错 |
| [API 参考](/goc/goc32/api/) | `user/lib.h` 里的系统调用、文件、时间、内存、GPIO/ADC、屏幕等用户态 API |

## 它适合做什么

- 给 TinyOS 写 **用户态程序**：`hello`、`ls`、`cat`、`sh`、GPIO 小工具、文本界面程序。
- 目标板：ESP32-C3、QEMU RV32、Cortex-M3、RP2040（Cortex-M0+）、RP2350（Cortex-M33）。
- 关心“没有 LLVM 依赖”和“体积可控”的场景。

## 它不做什么

- 不编译内核；内核仍然由 clang 构建。
- 不支持浮点、`long double`、atomics、TLS、内联汇编。
- 不追求运行速度优先；后端仍以正确性和体积为先。

源码与最新数据在 [yafengabc/goc32](https://github.com/yafengabc/goc32)。

