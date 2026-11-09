---
title: "TinyOS 刷写教程：ESP32-C3 / RP2040 / RP2350 镜像下载与烧录"
menuTitle: "TinyOS 刷写"
date: 2026-11-09T16:30:00+08:00
draft: false
weight: 10
slug: "tinyos-flash"
aliases: ["/embedded/tinyos-ci/"]
tags: ["TinyOS", "ESP32-C3", "RP2040", "RP2350", "刷机", "esptool", "UF2", "嵌入式"]
categories: ["嵌入式", "TinyOS", "刷机教程"]
description: "TinyOS v0.1.0 预编译镜像的下载与刷写教程：ESP32-C3 用 esptool 写 0x0/0x80000，RP2040/RP2350 用 BOOTSEL 拖 UF2，含串口、选镜像和常见问题。"
---

TinyOS 的预编译镜像已经发布，不用本地装交叉工具链，下载、刷写、开机就能用。

- Release：<https://github.com/yafengabc/TinyOS/releases/tag/v0.1.0>
- 源码：<https://github.com/yafengabc/TinyOS>

这篇只讲**怎么选镜像、怎么刷、怎么连串口、刷坏了怎么排错**。

## 1. 先选对镜像

Release 里有两类产物：ESP32-C3 的两个 `.bin`，以及 RP2040 / RP2350 的 `.uf2`。

| 你的板子 | 下载文件 | 刷写入口 |
| --- | --- | --- |
| ESP32-C3 | `esp32c3.bin` + `ramdisk-esp32c3.bin` | `esptool`，两个偏移 |
| RP2040（Pico / Pico W 类） | `rp2040.uf2` | BOOTSEL 后拖进 `RPI-RP2` |
| RP2350 ARM 单核 | `rp2350.uf2` | BOOTSEL 后拖进 `RP2350` |
| RP2350 ARM 双核 | `rp2350smp.uf2` | 同上 |
| RP2350 RISC-V 单核 | `rp2350rv.uf2` | 同上 |
| RP2350 RISC-V 双核 | `rp2350rvsmp.uf2` | 同上 |

每个目标还带一个 `FLASH-<target>.txt`，就是这份说明的精简版。

> RP2350 的板级支持目前针对 **正点原子 ATK-DNRP2350AM**（1.14 寸 ST7789、CH343P、按键、LED）。
> Pico 2 等其它 RP2350 板子引脚/设备不同，直接刷这个镜像不一定能正常显示或找到串口。
>
> RP2040 目标已经能生成完整 UF2，但真机验证还不如 C3/RP2350 充分；如果刷完没有启动，先按后面的排错检查，再到仓库反馈。

## 2. 通用准备

- 一根**能传数据**的 USB 线（很多“没反应”其实是充电线）。
- ESP32-C3 需要 `esptool`：
  ```bash
  python -m pip install esptool pyserial
  ```
- RP2040 / RP2350 不需要额外工具，电脑会把 bootloader 认成 U 盘。
- 串口终端：Windows 可以用 PuTTY / `python -m serial.tools.miniterm`，Linux 用 `minicom` / `screen`。

## 3. ESP32-C3

### 3.1 下载文件

从 Release 下载：

- `esp32c3.bin`
- `ramdisk-esp32c3.bin`

也可以用命令行：

```bash
gh release download v0.1.0 -R yafengabc/TinyOS \
  -p 'esp32c3.bin' -p 'ramdisk-esp32c3.bin'
```

### 3.2 进入下载模式

- 板子带 CH343/自动下载电路时，`esptool` 一般能自己复位进下载模式。
- 没有自动下载电路的板子：**按住 BOOT，点一下 RESET，再松开 BOOT**。

### 3.3 刷写

两个文件必须写到正确偏移：

```bash
esptool.py --chip esp32c3 -p COM4 write-flash \
  0x0     esp32c3.bin \
  0x80000 ramdisk-esp32c3.bin
```

拆成两条也可以：

```bash
esptool.py --chip esp32c3 -p COM4 write-flash 0x0     esp32c3.bin
esptool.py --chip esp32c3 -p COM4 write-flash 0x80000 ramdisk-esp32c3.bin
```

- `0x0` 是内核 boot image。
- `0x80000` 是 littlefs 文件系统（用户程序都在里面）。
- 只写第一个文件会启动失败；把 ramdisk 写到 `0x0` 会破坏 boot image。

新一点的 esptool 也支持一条命令写多个段：

```bash
esptool --chip esp32c3 -p COM4 write-flash \
  0x0 esp32c3.bin \
  0x80000 ramdisk-esp32c3.bin
```

### 3.4 连串口

ESP32-C3 的控制台是 **UART，115200 8N1**。

```bash
python -m serial.tools.miniterm COM4 115200
```

按一下回车，应该看到：

```text
TinyOS shell (pid 1). Type 'help' for commands.
tos>
```

如果板子是 USB-Serial/JTAG 而不是 CH343，串口设备名会不同（Windows 可能是 `COM5`，Linux 是 `/dev/ttyACM0`）。用设备管理器/`ls /dev/tty*` 确认。

### 3.5 验证

```text
tos> uptime
tos> ls
tos> free
tos> gpio
```

`gpio` 会列出板子暴露的引脚；如果返回 22 个，说明外设框架正常。

## 4. RP2040

### 4.1 进入 BOOTSEL

1. 按住板子上的 **BOOTSEL**。
2. 插入 USB（或按住 BOOTSEL 点一下 RESET）。
3. 电脑上出现名为 **`RPI-RP2`** 的 U 盘。
4. 松开 BOOTSEL。

### 4.2 刷写

把 `rp2040.uf2` 拖进 `RPI-RP2` 盘。复制完成后板子会自动重启。

命令行也可以（Linux/macOS）：

```bash
cp rp2040.uf2 /media/$USER/RPI-RP2/
sync
```

### 4.3 连串口

RP2040 的 TinyOS 控制台是**板载 USB CDC**：刷完后板子重新枚举成一个串口设备，用 115200 打开即可。

```bash
python -m serial.tools.miniterm /dev/ttyACM0 115200
```

Windows 下在设备管理器里看“USB 串行设备”的 COM 号。

### 4.4 验证

按回车看到 `tos>`，然后：

```text
tos> uptime
tos> ls
tos> gpio
```

## 5. RP2350

### 5.1 选对 UF2

RP2350 有两个核（Cortex-M33 和 RISC-V Hazard3）和单/双核两种构建，**不能混刷**：

| 你的意图 | 文件 |
| --- | --- |
| ARM 核，单核 | `rp2350.uf2` |
| ARM 核，双核 | `rp2350smp.uf2` |
| RISC-V 核，单核 | `rp2350rv.uf2` |
| RISC-V 核，双核 | `rp2350rvsmp.uf2` |

刷错核的 UF2，板子可能“什么都没发生”，这是 bootloader 对不匹配镜像的典型反应。

### 5.2 进入 BOOTSEL

1. 按住 **BOOTSEL** 插入 USB（或按住 BOOTSEL 点 RESET）。
2. 出现 **`RP2350`** U 盘。
3. 把对应 `.uf2` 拖进去。

### 5.3 连串口

RP2350 镜像同时输出两路控制台：

- **USB CDC**：板子自己的 USB 口，运行后枚举成 `2e8a:000a` 的串口。
- **UART0**：GPIO0/1，**115200 8N1**，接 USB-TTL 也能看。

USB CDC：

```bash
python -m serial.tools.miniterm /dev/ttyACM0 115200
```

UART0（可选）：

```bash
python -m serial.tools.miniterm COM14 115200
```

如果板子是 ATK-DNRP2350AM，1.14 寸屏会**镜像内核控制台**，不接串口也能看到启动日志和 shell。

### 5.4 自动烧写脚本（可选）

仓库里有 `scripts/rp2350/flash.py`，可以一条命令完成“拷 UF2 + 等重启 + 找 CDC 口 + 抓日志 + smoke”：

```bash
python scripts/rp2350/flash.py --smoke
```

常用选项：

```bash
python scripts/rp2350/flash.py --list            # 列串口/CDC/可移动盘
python scripts/rp2350/flash.py --no-flash --smoke  # 只看串口
python scripts/rp2350/flash.py -p COM14 --no-flash --smoke
```

## 6. 刷完怎么确认正常

不管哪块板子，看到下面这些就说明内核、文件系统和用户程序都跑起来了：

```text
TinyOS shell (pid 1). Type 'help' for commands.
tos> ls
d  dev/
d  proc/
d  bin/
-  readme.txt
```

再试几个命令：

```text
tos> uptime
tos> free
tos> gpio
tos> run hello
```

## 7. 常见问题

| 现象 | 处理 |
| --- | --- |
| ESP32-C3 找不到串口 | 换数据线；装 CH343/USB-JTAG 驱动；确认 COM 号 |
| `esptool` 连不上 | 手动进下载模式：按住 BOOT，点 RESET，松开 BOOT |
| C3 刷完没启动 | 确认 `esp32c3.bin` 在 `0x0`、`ramdisk-esp32c3.bin` 在 `0x80000` |
| C3 串口有乱码 | 波特率必须是 115200；确认没有别的程序占用串口 |
| RP2040 没出现 `RPI-RP2` | 先按住 BOOTSEL 再插 USB；换线；确认板子有正常 bootloader |
| RP2350 没出现 `RP2350` | 同上；有些板子需要按住 BOOTSEL 再点 RESET |
| UF2 拷完没反应 | 可能刷错核/单双核：ARM 用 `rp2350*.uf2`，RISC-V 用 `rp2350rv*.uf2` |
| 有串口但没有 `tos>` | 按回车；确认 115200；RP2350 也可以看板载屏 |
| `lfs`/`No more free space` | 这是**本地重新构建**时文件系统放不下，不是刷写问题；精简程序或调整分区 |

## 8. 镜像里有什么

每个镜像是完整的 TinyOS 系统：

- 内核（调度、系统调用、littlefs、ELF 加载器）
- 用户程序（`sh`、`ls`、`cat`、`gpio`、`adc`、`free`、`uptime` 等）
- littlefs 文件系统

刷完直接进 `tos>`，不需要再传程序。

想放自己的程序，或者修改内核，需要在本地构建：

```bash
go run ./cmd/tos build -target esp32c3
# 或 rp2040 / rp2350 / rp2350smp / rp2350rv / rp2350rvsmp
```

构建产物和 Release 里的文件一致。具体构建/加程序的方法见仓库 README 和 `docs/CI.md`。

## 9. 参考

- Release v0.1.0：<https://github.com/yafengabc/TinyOS/releases/tag/v0.1.0>
- TinyOS 源码：<https://github.com/yafengabc/TinyOS>
- CI 构建说明：<https://github.com/yafengabc/TinyOS/blob/main/docs/CI.md>
- ESP32-C3 刷写脚本：<https://github.com/yafengabc/TinyOS/tree/main/scripts/esp-c3>
- RP2350 刷写脚本：<https://github.com/yafengabc/TinyOS/tree/main/scripts/rp2350>
