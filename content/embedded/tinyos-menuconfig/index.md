---
title: "TinyOS 构建系统三件事：menuconfig、可选浮点运行时与链接期内存布局"
menuTitle: "TinyOS 构建改造"
date: 2026-11-10T10:00:00+08:00
draft: false
weight: 11
slug: "tinyos-menuconfig"
tags: ["TinyOS", "ESP32-C3", "menuconfig", "软浮点", "Q16.16", "链接脚本", "嵌入式"]
categories: ["嵌入式", "TinyOS"]
description: "给 TinyOS 加一个类似 Linux 的 menuconfig，比较 32 位软浮点和 Q16.16 定点，并把内核/任务区的分割从写死常量改成链接期自动推导。附实测体积、速度和内存数据。"
---

TinyOS 是一个多平台的小内核：x86、QEMU 的 rv32/ARM、RP2040、RP2350（ARM 与 RISC-V 双核）、ESP32-C3，镜像里带一个 300 KiB 的 littlefs 分区。它没有 Makefile，构建全由一个 Go 工具 `tos` 负责，配置是仓库里的 INI 文件。

最近做了三件互相关联的事：

1. 给 INI 配置加了一个类似 Linux `menuconfig` 的交互界面；
2. 把“浮点运行时”做成可选，重写了一个 32 位软浮点，并和 Q16.16 定点做了体积/速度对比；
3. 把内核与用户任务区的分割从写死常量改成链接期自动推导，让省下来的内核 RAM 真正变成任务窗口。

这篇记录设计取舍和实测数据，也记录踩到的两个坑。

## 1. menuconfig：给 INI 加一层 schema

### 为什么不直接抄 Kconfig

Linux 的 Kconfig 是一套完整的配置语言：`menu`、`choice`、`depends on`、`select`、表达式求值、`.config` 写回。TinyOS 实际只有十几项可配：目标、WiFi/网络、浮点变体、内核堆/栈、进程表大小、驱动目录、用户程序清单。

为十几项选项实现一套 Kconfig 解析器和依赖求解器，大概要三到五天，收益很低。于是走了一条更小的路：**Go 里写死菜单结构，状态就是现有 `Config` 结构体的一个可编辑子集，保存时写回规范 INI**。代价是新增选项要改 Go 代码，好处是零解析器、零依赖表达式，而且加载/保存都能单元测试。

界面用 `golang.org/x/term` 做 raw mode，ANSI 转义画屏。为了保持 `go 1.21` 兼容，`x/term` 固定在上游还要求 1.18 的 `v0.25.0`。

### 菜单长什么样

主界面是一屏列表，底部两行帮助，最后一行按键提示：

```text
 TinyOS configuration  configs/esp32c3-wifi.conf
------------------------------------------------------------
   Target            esp32c3  >
   Kernel            heap 80K, stack 16K  >
   Drivers           4 dirs + wifi  >
   [ ] IPv4 / DHCP / DNS / TCP   (needs the WiFi driver)
   [ ] Soft float: saturating (no NaN/Inf)
   User programs                    >

------------------------------------------------------------
 The built-in network stack on /dev/net: ARP, ICMP, DHCP client,
 DNS, TCP client and the http command.  Enable the WiFi driver first.
 arrows move   space toggle   enter open   esc back   s save   q quit
```

几个具体设计：

- **Target 是二级单选项**。所有目标各占一行，`(*)`/`( )` 标记，选中即返回；每个目标带一行说明（x86 是 PC 镜像，rp2040 是 M0+ 无 FPU，rp2350rv 是 Hazard3）。
- **Kernel 子菜单**里是真正会进 `.bss` 的数字：堆大小、每 CPU 内核栈、进程表槽位、优化级别。堆和栈的默认值来自目标（C3 80 KiB / 16 KiB，RP2040 32 KiB / 16 KiB），菜单只是覆盖。
- **Drivers 子菜单**里 WiFi 也是一个驱动：它除了目录，还要 Espressif 的 net80211/pp/phy/coexist 预编译库和 ROM 链接脚本，所以打开它时保存会自动补上 `[wifi]` 三条路径。gpio/serial/usb/spi 标成 `(required)`（控制台和总线），LCD 驱动可以关掉。
- 改目标时自动关掉不支持的选项：WiFi 只有 esp32c3 有，IPv4 栈依赖 WiFi。
- `menuconfig -print` 只输出规范化后的 INI、不启动界面，方便脚本和 CI。

保存时整个文件按固定顺序重写（build → features → drivers → wifi → user → sources → ld），不认识的键和 feature 原样保留。TUI 本身没法在非交互环境里测，所以把“渲染”和“状态机”拆开了：`render(w,h)` 返回整屏字符串，测试检查 80×24、40×10、120×40 三种尺寸下每行宽度、标题、滚动，以及按键驱动的状态变化和 INI 往返。

实测关掉 RP2350 的 LCD 驱动：内核 `.text` 少 8.5 KiB，`.bss` 少 4 KiB。

## 2. 浮点运行时：把 double 换成 32 位，再和定点比

### 起点：goc 的 softfloat 是 double 实现

goc 项目里有一份 38 KB 的 `softfloat.c`，实现了完整的 binary64 软件浮点。它的 `float` 接口长这样：

```c
float __addsf3(float a, float b) { return (float)__adddf3((double)a, (double)b); }
```

也就是说，在没有 FPU 的目标上，每一个 `float` 加法都先扩成 double、走 64 位路径、再截回 float。对 RV32IMC 和 Cortex-M0+ 来说，这个代价和代码体积都不划算。

### 32 位重写

新的实现只使用 32 位整数：

- 工作尾数 27 位：1 位隐含位 + 23 位小数 + guard/round/sticky 各 1 位，正好放进一个 `uint32_t`；
- 加减对齐时右移并把丢掉的位折进 sticky；
- 乘法是 24×24 → 48 位，用 16 位拆分（Cortex-M0+ 没有 UMULL，不能用 64 位乘法，否则会拉进 `__aeabi_lmul`）；
- 除法是 32 位恢复除法，产生 30 位商再归一化；
- 舍入只在 `sf_finish()` 一处做，避免每个操作各写一遍。

### 坑：LLVM 把位测试折叠回浮点比较

写完第一版，板上直接栈溢出。反汇编发现 `__eqsf2` 在调用自己：

```text
__eqsf2:
    ...
    jal __eqsf2      ; 递归
```

原因是 LLVM 能看穿 `to_bits()`（union 的位读取），把我们写的

```c
if ((u & 0x7F800000) == 0x7F800000 && (u & 0x7FFFFF) != 0)   /* isnan */
```

识别成 `isnan`，再折叠成 `fcmp uno`。在没有 FPU 的目标上，`fcmp` 就是一次函数调用，于是 `__eqsf2` 调回了 `__eqsf2`。`==` 和 `== 0` 的位比较也会被折叠成浮点比较。

修法是给位模式加一个空内联汇编“洗值”，让优化器无法把整数值和浮点值关联起来：

```c
static uint32_t sf_launder(uint32_t u)
{
    __asm__ volatile("" : "+r"(u));
    return u;
}
```

goc 的 double 实现里也记录过同样的坑，这不是 32 位独有的。

### print_float：把打印从运行时里拆出来

原来的 `print_float` 用浮点运算逐位取小数（`rem *= 10`、`__fixsfsi`），结果是“只想打印一个数”也会把整个软浮点链进来。

改成纯整数实现：解出 IEEE 的尾数和指数，算 `v * 10^d = M * 10^d * 2^(e-150)`，48 位乘法加一次 64/32 除法，直接得到整数部分和 d 位小数。舍入用最近偶数，和 printf 对齐。

效果（RV32，软浮点与打印代码统一按 `-Oz` 编译）：

| 项目 | 之前 | 之后 |
|---|---|---|
| 只打印的程序 | 2,181 B | **1,119 B** |
| 其中打印路径 | 1,654 B | **592 B** |
| 打印是否依赖软浮点 | 是 | 否 |

两个数字之间也包含把运行时切到 -Oz 的收益（约 200 B）；打印本身的整数化省得更多。

用约 100 万组随机“值 + 小数位数”和主机 `printf("%.*f")` 对拍，零差异。

### 体积与速度

C3 @160 MHz，50,000 次 `v*1.0001 - 0.0001`：

| 版本 | 代码 | 时间 |
|---|---|---|
| IEEE softfloat，-O2 | 2,628 B | 84 ms |
| IEEE softfloat，-Oz | 2,048 B | 113 ms |
| softfloat-min（无 NaN/Inf/次正规），-Oz | **1,468 B** | 100 ms |
| 显式 Q16.16 API | 678 B（-Oz），打印路径 +62 B | **16 ms** |

`softfloat-min` 用 `TOS_SOFTFLOAT_MIN` 编译：次正规数清零，NaN/Inf 不表示（输入钳到最大有限值，溢出和 x/0 饱和）。省 580 B，代价是不能依赖 IEEE 的边角语义。

### 一个失败实验：float 语法 + Q16.16 后端

中间试过一层“让 `float` 全部映射到 Q16.16”的兼容层：`__addsf3` 等函数把 IEEE 位解成 Q16.16、算完再编码回去。源码确实不用改，但每次运算两次位转换，结果是

- 体积 4.5 KB，比 IEEE 的 4.0 KB 还大；
- 速度 132 ms，比 IEEE 的 84 ms 还慢。

两头不讨好，删掉。最终结论是**显式 API**：要小要快就用 `fix16_*`，要 IEEE 语义就用 `float`。两个运行时都始终链接，`--gc-sections` 决定每个程序真正付出多少——一个不碰数值的程序，镜像大小一个字节都不变（`hello` 始终 527 B）。

## 3. 内存布局：从写死分割到链接期推导

### 原来的问题

每个板子的 `layout.c` 里有一对常量：

```c
const uint32_t PLAT_KERNEL_MAX = 0x403B0000;   /* 内核最多用到这 */
const uint32_t PLAT_APP_BASE   = 0x403B0000;   /* 任务窗口从这开始 */
```

内核的 `.bss`（堆、内核栈、进程表）在下面，任务窗口从固定地址开始。把堆从 80 KiB 调到 8 KiB，内核结束地址确实往下走了，但任务区不会跟着长——省出来的空间只是变成一个洞：

```text
heap 80K / stack 16K:   kernel 191,696 B   tasks 180,224 B   unused 6,720 B
heap  8K / stack  8K:   kernel  42,256 B   tasks 180,224 B   unused 156,160 B
```

`tasks` 一模一样。

### 链接期符号

每个链接脚本在内核段之后加一行：

```ld
__kernel_end = .;
__app_base   = ALIGN(__kernel_end, 0x4000);
ASSERT(__app_base + 0x4000 <= 0x403DC000,
       "kernel leaves no room for a task window")
```

`platform.h` 把它暴露成宏：

```c
extern uint8_t __app_base[];
#define PLAT_APP_BASE ((uint32_t)__app_base)
```

这样不需要运行时初始化，窗口分配器读到的就是链接器算出的地址。分割点自动等于“内核实际结束、按 16 KiB 向上取整”，链接期只检查一件事：至少还能放得下一个窗口。

### 实测

| 构建 | 内核结束 | 任务区 之前 → 现在 |
|---|---|---|
| C3，heap 8K / stack 8K | 0x4038a510 | 176 KiB → **320 KiB** |
| C3，无 WiFi（heap 80K） | 0x4039e510 | 176 KiB → **240 KiB** |
| C3，WiFi | 0x403aecd0 | 176 KiB → 176 KiB（内核确实需要） |
| x86 | ~1 MB | 112 MiB → **127 MiB** |
| rv32 (QEMU) | 0x800a6e50 | 16 MiB → **19.3 MiB** |
| arm (mps2) | 0x20075ee8 | 3.5 MiB（对齐后不变） |

C3 的 `free` 现在直接体现出来：

```text
  kernel heap     8192         0      8192       768
  SRAM         378640       369
  kernel        42256        41
  tasks        327680       320
  unused         8704         8
```

之前链接会失败的 `heap = 128k` + WiFi 现在也能过：内核结束在 `0x403bacd0`，窗口从 `0x403bc000` 开始，还剩 128 KiB 任务区。

### 测试里发现的坑：x86 的 VGA 空洞

第一版在 x86 上把 `__app_base` 直接对齐到 `__kernel_end`，结果所有用户程序都报：

```text
exec: 'bin/ls' is not a valid ELF32 program
```

加临时打印才看到窗口基址是 `0xa0000`，解出来的数据是 `ff ff ff ff`——`0xA0000–0x100000` 是 VGA framebuffer 和 BIOS ROM，不是 RAM。x86 的内核比想象的小，`.bss` 结束在 1 MiB 以下，按 64 KiB 对齐就落进了空洞。

修法是给 x86 一个下限：

```ld
__app_base = ALIGN(MAX(__kernel_end, 0x00100000), 0x10000);
```

MCU 的 SRAM 是连续的，不需要这个下限；x86 有 128 MiB，也不在乎从 1 MiB 还是 16 MiB 开始。

## 4. 几条经验

1. **`-ffunction-sections` + `--gc-sections` 让“始终链接”变得没有成本**。Q16.16、IEEE 软浮点、打印代码都放进公共对象里，用不到的函数会被链接器删掉。先确认这一点，再决定要不要做 switch——大多数 switch 只是把能编过的程序变成链接错误。
2. **先量再改**。“float 映射到定点”听起来省空间，实测更大更慢；“连 double 转 float”听起来该更快，实测慢 2 倍。两个结论都是跑出来的。
3. **链接期符号是免费的**。`__app_base` 不需要启动时代码初始化，也不会有一致性问题；代价是链接脚本要按板子写对地址上限，以及注意平台的内存空洞。
4. **配置项要能测试**。menuconfig 的 TUI 没法自动化，但渲染函数、状态机、INI 往返都可以；这样“选项是否真的生效”至少有一半是单测保证的。

## 怎么试

```bash
# 交互式配置
go run ./cmd/tos menuconfig -config configs/esp32c3-wifi.conf
go run ./cmd/tos menuconfig -print -config configs/esp32c3-wifi.conf

# C3：heap 8K / 内核栈 8K，看任务区变大
cat > /tmp/min.conf <<'EOF'
[build]
target = esp32c3
heap = 8k
kstack = 8k
EOF
go run ./cmd/tos build -config /tmp/min.conf
```

源码和 release 在 <https://github.com/yafengabc/TinyOS>。
