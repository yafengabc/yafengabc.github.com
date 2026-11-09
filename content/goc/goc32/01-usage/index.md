---
title: "goc32 程序上传：通过串口把可执行文件放进 littlefs"
menuTitle: "串口上传"
date: 2026-11-09T17:30:00+08:00
draft: false
weight: 1
slug: "usage"
tags: ["goc32", "TinyOS", "tosput", "串口", "littlefs", "ESP32-C3", "RP2040", "RP2350", "教程"]
categories: ["编程教程", "goc32", "TinyOS"]
description: "用 goc32 编译出 .tos 后，不必重新烧整片镜像：通过控制台串口和板上的 rx，把可执行文件直接写进 littlefs，再从 shell 运行。"
---

以前改一个用户程序，要重新打包 littlefs、重新烧 `0x80000` 的 ramdisk，甚至整片 flash。其实不用：板子上有一个 `bin/rx`，它通过**控制台串口**接收文件并写进 littlefs；宿主的 `tosput` 负责发送。

整个流程是：

```text
goc32 编译 hello.c -> hello.tos
        |
        |  串口（115200，base64 行协议）
        v
板上的 bin/rx /bin/hello  ->  littlefs:/bin/hello
        |
        v
tos> hello
```

内核不用动，镜像不用重烧，只传一个文件。

## 1. 前提

- 板子上已经有 `bin/rx`。官方 v0.1.0 镜像自带；自己构建的镜像只要包含 `user/rx.c` 就有。
- 能连上板子的控制台串口：
  - ESP32-C3：UART，115200 8N1（CH343 或 USB-Serial/JTAG）。
  - RP2040：板载 USB CDC。
  - RP2350：USB CDC（`2e8a:000a`）或 UART0（GPIO0/1，115200）。
- 宿主工具 `tosput`：
  - Windows GUI：`tools\tosput\tosput.exe`
  - Windows CLI：`tools\tosput\tosput-cli.exe`
  - 源码与协议：`tools/tosput/`、`docs/FILE-TRANSFER.md`

`tosput` 不依赖板子的网络，也不改内核；它只做一件事：在串口上执行 `rx <path>`，然后把文件分块发过去。

## 2. 先编译一个程序

在 TinyOS 源码树里写一个最小程序，比如 `user/hello_upload.c`：

```c
#include "lib.h"

int main(int argc, char **argv)
{
    printf("hello from littlefs, argc=%d\n", argc);
    for (int i = 0; i < argc; i++)
        printf("argv[%d]=%s\n", i, argv[i]);
    return 0;
}
```

用 goc32 编译成 ESP32-C3 的 `.tos`：

```powershell
cd C:\Users\SONG\Documents\Projects\goc32

.\bin\goc32.exe -target esp32c3 `
  -tos C:\Users\SONG\Documents\Projects\TinyOS `
  -o C:\Users\SONG\Documents\Projects\TinyOS\build\hello_upload.tos `
  C:\Users\SONG\Documents\Projects\TinyOS\user\hello_upload.c
```

输出类似：

```text
...\hello_upload.tos: 1740 -> 1190 bytes (LZ4TOS, hint ...)
```

`.tos` 是完整的 LZ4TOS 用户程序，直接传即可，不需要先解包或转成 ELF。

> 目标平台要匹配：ESP32-C3 用 `-target esp32c3`，RP2040 用 `-target rp2040`，RP2350 用 `-target rp2350` / `rp2350rv` 等。把 RV32 的程序传到 ARM 板子上，`exec` 会失败。

## 3. 上传到 `/bin/`

### 3.1 命令行

```powershell
cd C:\Users\SONG\Documents\Projects\TinyOS

.\tools\tosput\tosput-cli.exe COM13 `
  .\build\hello_upload.tos `
  /bin/hello_upload
```

参数是：

```text
tosput-cli <串口> <本地文件> <板上的绝对路径>
```

输出大致是：

```text
sending ...\hello_upload.tos -> /bin/hello_upload (1190 bytes, crc32 ...)
  ...
DONE ...
throughput: 6.9 KiB/s
```

`DONE` 后面的 CRC32 是板子对**已经写进 flash 的字节**算的；它和宿主发送前算的一致才算成功。传输失败会报 `FAILED` 或 `NAK`，不会悄悄留下一个坏文件。

### 3.2 GUI

运行：

```powershell
.\tools\tosput\tosput.exe
```

- 选串口（COM 号）。
- 选本地 `.tos` 文件。
- **Remote 一定要填 `/bin/hello_upload`**。
  GUI 的默认 remote 是 `/<文件名>`，即根目录；那样文件会出现在 `/hello_upload`，shell 的 `hello_upload` 命令找不到它。
- 点 Send，等 `DONE`。

### 3.3 为什么要放 `/bin/`

TinyOS 的 shell 执行外部命令时会拼 `bin/<命令名>`。所以：

- `/bin/hello_upload` → `tos> hello_upload` 可以运行。
- `/hello_upload` → 文件在，但 shell 不会把它当作命令。

上传后可以确认：

```text
tos> ls bin
-  hello_upload   1190 bytes
```

## 4. 运行

在 `tos>` 下直接输入命令名：

```text
tos> hello_upload one two
```

或者用 `run`（会等待程序结束并返回退出码）：

```text
tos> run hello_upload one two
```

输出：

```text
hello from littlefs, argc=3
argv[0]=hello_upload
argv[1]=one
argv[2]=two
```

如果提示 `exec: 'bin/hello_upload' not found`，先检查 `ls bin` 和 remote 路径。

## 5. 传别的文件

`rx` 不关心文件类型，二进制/文本都行。remote 是板子上的绝对路径：

```powershell
# 传一个配置/脚本到根目录
.\tools\tosput\tosput-cli.exe COM13 .\demo.sh /demo.sh

# 传一个数据文件
.\tools\tosput\tosput-cli.exe COM13 .\blob.bin /blob.bin
```

对可执行文件，仍然建议放 `/bin/`，这样 shell 能直接按名字运行。

`rx` 用 `create()` 打开目标文件，也就是**存在就截断**。传一半按了 `^C` 或断线，盘上会留一个不完整的文件；重新传一次即可覆盖。

## 6. 协议和速度（简单版）

宿主和 `rx` 说的是很朴素的 ASCII 行协议：

```text
宿主                              板（rx /path）
 "rx /path\n"            ->  shell 启动 rx，ttyraw(1)，create(TRUNC)
                          <-  "READY 1024\n"
 "D <seq> <len> <b64>\n" ->  解码，写入文件
                          <-  "ACK <seq>\n"  或  "NAK <seq> ...\n"
 ...
 "E <crc32hex>\n"        ->  校验
                          <-  "DONE <crc32hex> <total>\n"
```

几个关键点：

- **base64 行帧**：二进制被编码成可打印字符，串口终端/驱动不会把它当作控制字符。
- **停等（stop-and-wait）**：板子在写 flash 的几毫秒里是“聋”的，宿主必须等 `ACK` 再发下一块。这不是保守，是没有硬件流控的 UART 上唯一可靠的流控。
- **块大小 1024 字节**；115200 下实测约 **7 KiB/s**，传一个 1–2 KB 的 `.tos` 基本瞬间完成。
- **CRC32 在板端算**：校验的是落盘内容，不只是线路。
- `READY <chunk>` 里的块大小同时是协议版本。宿主发现版本不匹配会拒绝传输，而不是无限 NAK。

更完整的协议、吞吐和踩坑记录在 TinyOS 的 `docs/FILE-TRANSFER.md`。

## 7. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| `exec: 'bin/rx' not found` | 当前镜像没有 `rx`；用 v0.1.0 官方镜像，或自己构建时保留 `user/rx.c` |
| 一直 `NAK ... r1[...]` / 块大小不匹配 | 板上的 `rx` 是旧版本（内核和文件系统分开烧过）；重新烧完整镜像，或用匹配版本的 `tosput` |
| `open COMx` 失败 | 串口被别的终端占用；关掉串口助手/PuTTY；检查驱动和数据线 |
| 打开串口后板子重启 | C3 的 CH343 自动下载电路会被 DTR/RTS 脉冲复位；等它启动完，`tosput` 会重新等提示符 |
| `DONE` 但 CRC 对不上 / `FAILED` | 线材/供电/干扰，或传输中板子跑了别的程序；重试，必要时降低波特率或换线 |
| 上传成功但 `hello_upload` 找不到 | remote 写成了 `/hello_upload`；改成 `/bin/hello_upload` |
| 上传成功但 `exec` 报错 | `.tos` 的目标平台不对（RV32/ARM/RP2040 混用），或者文件本身不是可加载的 LZ4TOS/ELF |
| 传大文件很慢 | 协议约 7 KiB/s；这是 115200 + base64 + 写 flash 的实际上限，换更高波特率需要接收端一起改 |
| `^C` 后文件半截 | 正常；`rx` 会留下部分文件，重新传一次覆盖即可 |

## 8. 和“重新烧镜像”的区别

| 操作 | 影响 | 适用场景 |
| --- | --- | --- |
| `tosput` 上传 | 只改 littlefs 里的一个文件 | 改用户程序、换脚本/配置 |
| 烧 `ramdisk-*.bin` | 替换整个文件系统 | 一次更新很多程序、恢复出厂文件系统 |
| 烧内核 / UF2 | 替换内核和文件系统 | 改内核、板级支持、启动配置 |

日常改一个用户程序，用串口上传最快，也不会动内核。

## 9. 参考

- 文件传输协议与实测：<https://github.com/yafengabc/TinyOS/blob/main/docs/FILE-TRANSFER.md>
- 接收端源码：<https://github.com/yafengabc/TinyOS/blob/main/user/rx.c>
- 宿主工具：<https://github.com/yafengabc/TinyOS/tree/main/tools/tosput>
- TinyOS v0.1.0 镜像：<https://github.com/yafengabc/TinyOS/releases/tag/v0.1.0>
- 用户程序 API：[API 参考](/goc/goc32/api/)
