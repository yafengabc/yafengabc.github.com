---
title: "goc32 使用教程：从 C 源码到 ESP32-C3 / RV32 / ARM"
menuTitle: "使用教程"
date: 2026-11-09T11:00:00+08:00
draft: false
weight: 1
slug: "usage"
tags: ["goc32", "TinyOS", "C 编译器", "RV32", "ESP32-C3", "ARM", "RP2040", "教程"]
categories: ["编程教程", "goc32", "TinyOS"]
description: "goc32 的完整使用流程：构建、命令行、目标平台、第一个程序、GPIO 与文件示例、放进 TinyOS 文件系统运行、体积现状、限制与排错。"
---

这篇是 **goc32 的使用教程**。目标是让你从零编译出一个能放进 TinyOS 文件系统、在 ESP32-C3（或 RV32/ARM/RP2040）上运行的用户程序。

API 细节放在单独的 [API 参考](/goc/goc32/api/) 里。

## 1. 准备环境

- Go 1.25 或更新版本。
- LLVM 工具链：`clang` 和 `ld.lld`。
  - Windows 下通常在 `C:\msys64\ucrt64\bin` 或 LLVM 安装目录。
  - 确保 `clang --version` 和 `ld.lld --version` 能直接运行。
- 一个 TinyOS 源码树（goc32 需要 `user/lib.c`、`user/crt0_rv32.S`、头文件和 `user.ld`）。

构建 goc32：

```powershell
cd C:\Users\SONG\Documents\Projects\goc32
.\build.ps1
```

Linux/macOS：

```bash
./build.sh
```

产物是 `bin/goc32.exe`（Linux 下是 `bin/goc32`）。也可以直接使用仓库里已经构建好的 `bin/goc32.exe`。

## 2. 第一个程序

在 TinyOS 的 `user/` 下新建 `hello_goc32.c`：

```c
#include "lib.h"

int main(int argc, char **argv)
{
    printf("hello from goc32: argc=%d\n", argc);
    for (int i = 0; i < argc; i++)
        printf("argv[%d] = %s\n", i, argv[i]);
    return 0;
}
```

编译成 ESP32-C3 程序：

```powershell
cd C:\Users\SONG\Documents\Projects\goc32

.\bin\goc32.exe -target esp32c3 `
  -tos C:\Users\SONG\Documents\Projects\TinyOS `
  -o C:\Users\SONG\Documents\Projects\TinyOS\build\hello_goc32.tos `
  C:\Users\SONG\Documents\Projects\TinyOS\user\hello_goc32.c
```

输出类似：

```text
...\hello_goc32.tos: 1720 -> 1180 bytes (LZ4TOS, hint ...)
```

- 第一个数字是链接后的原始 ELF 大小。
- 第二个数字是 LZ4TOS 封装后的大小。
- `hint` 里的 span/stack/heap 是给内核加载器用的窗口提示。

`.tos` 已经是**完整的用户程序**（PIE + LZ4TOS），不是 `.o`，不需要再交给 `ld.lld`。

### `-tos` 什么时候可以省略

goc32 会从输入源文件向上查找 `user/lib.c`。如果源文件就在 TinyOS 树里：

```powershell
.\bin\goc32.exe -target esp32c3 -o build\hello_goc32.tos ..\TinyOS\user\hello_goc32.c
```

通常也能工作。从其它目录编译、或者 TinyOS 不在上级目录时，显式写 `-tos` 最稳。

## 3. 目标平台

| `-target` | ISA / CPU | 典型用途 |
| --- | --- | --- |
| `rv32`（默认） | RV32IMC | QEMU virt、通用 RV32 |
| `esp32c3` | RV32IMC | ESP32-C3，XIP flash |
| `rv32nc` | RV32IM | 无压缩指令的 RV32 |
| `arm` | Thumb-2 / Cortex-M3 | ARMv7-M |
| `rp2040` | Thumb-1 / Cortex-M0+ | ARMv6-M |
| `rp2350` | Thumb / Cortex-M33 | ARMv8-M |

同一个程序换 `-target` 重新编译即可，用户 API 不变。

## 4. 命令行选项

| 选项 | 作用 |
| --- | --- |
| `-target NAME` | 目标平台，默认 `rv32` |
| `-tos DIR` | TinyOS 源码根目录 |
| `-o FILE` | 输出文件（`.tos` / `.s` / `.ll` / `.i`） |
| `-I DIR` | 额外头文件目录，可重复 |
| `-D NAME[=VAL]` | 额外宏定义，可重复 |
| `-S` | 只输出汇编 |
| `-ir` | 只输出 gocl 的 IR 文本 |
| `-E` | 只输出预处理后的 C |
| `-pack` | 把一个已经链接好的 ELF 封装成 LZ4TOS（不编译） |
| `-ll` | 把输入当作 LLVM IR 文本，输出汇编 |
| `-keep` | 保留临时目录（里面有 `.s`、`.o`、`prog.elf`） |
| `-v` | 打印 clang / ld.lld 的完整命令行 |
| `-cc PATH` | 指定 clang |
| `-ld PATH` | 指定 ld.lld |

几个常用调试组合：

```powershell
# 看汇编
.\bin\goc32.exe -target esp32c3 -S -o hello.s ...\user\hello.c

# 看 gocl IR
.\bin\goc32.exe -target esp32c3 -ir -o hello.ll ...\user\hello.c

# 看最终 clang/ld.lld 命令
.\bin\goc32.exe -target esp32c3 -v -o hello.tos ...\user\hello.c

# 保留临时目录，检查链接出来的 ELF
.\bin\goc32.exe -target esp32c3 -keep -o hello.tos ...\user\hello.c
```

> 注意：`-S` 路径为了便于对照，默认**不做**单调用点内联；最终 `.tos` 可能比 `-S` 输出略小。`-ir` 也是内联前的 IR。

## 5. 示例：控制 GPIO

ESP32-C3 板上的 LED 引脚因板子而异，下面用 `LED` 宏，运行时也可以传引脚号。代码：

```c
#include "lib.h"

#define LED 2

int main(int argc, char **argv)
{
    int pin = (argc > 1) ? atoi(argv[1]) : LED;
    int n = gpio_count();

    if (n <= 0) {
        puts("this board exposes no GPIO\n");
        return 1;
    }
    if (pin < 0 || pin >= n) {
        printf("pin %d out of range (0..%d)\n", pin, n - 1);
        return 1;
    }
    if (gpio_mode(pin, TOS_PIN_OUT) < 0) {
        printf("pin %d: cannot configure\n", pin);
        return 1;
    }

    for (int i = 0; i < 10; i++) {
        gpio_write(pin, 1);
        sleep(200);
        gpio_write(pin, 0);
        sleep(200);
    }
    puts("blink done\n");
    return 0;
}
```

用到的 API：

```c
int gpio_count(void);
int gpio_mode(int pin, int mode);   /* TOS_PIN_IN / OUT / IN_PULLUP / IN_PULLDOWN */
int gpio_write(int pin, int value);
int gpio_read(int pin);             /* 0/1，失败 -1 */
int sleep(int ms);                  /* 让出 CPU，不是忙等 */
```

编译：

```powershell
.\bin\goc32.exe -target esp32c3 `
  -tos C:\Users\SONG\Documents\Projects\TinyOS `
  -o C:\Users\SONG\Documents\Projects\TinyOS\build\blink.tos `
  C:\Users\SONG\Documents\Projects\TinyOS\user\blink.c
```

### ESP32-C3 选引脚注意

`gpio_count()` 返回 22（GPIO0..21），但不是每个都能随便驱动：

- GPIO12–17 通常接 SPI flash，动了会直接崩。
- GPIO20/21 是 UART0 控制台。
- GPIO18/19 是 USB D-/D+。
- GPIO8/9 是启动 strapping 引脚。
- 比较安全的普通输出：GPIO2、GPIO3、GPIO4、GPIO5、GPIO6、GPIO7（以你的板子原理图为准）。
- 驱动 LED 记得串限流电阻；板载 RGB LED 往往不是普通 GPIO 电平驱动。

不写程序也可以先用自带的 `gpio` 命令验证引脚：

```text
gpio mode 2 out
gpio 2 1
gpio 2 0
gpio blink 2 10
gpio 2
```

## 6. 示例：读写文件

`cat` 的核心就是一个 `open/read/write_all/close` 循环：

```c
#include "lib.h"

int main(int argc, char **argv)
{
    if (argc < 2) {
        puts("usage: mycat <file>\n");
        return 1;
    }

    int fd = open(argv[1]);
    if (fd < 0) {
        printf("mycat: cannot open %s\n", argv[1]);
        return 1;
    }

    char buf[256];
    int n;
    while ((n = read(fd, buf, sizeof(buf))) > 0) {
        if (write_all(1, buf, n) != n) {
            close(fd);
            return 1;
        }
    }
    close(fd);
    return 0;
}
```

这里用 `write_all` 而不是裸 `write`，是因为管道可能短写；完整 API 见 [API 参考](/goc/goc32/api/)。

## 7. 把程序放进 TinyOS 运行

### 7.1 先构建一次 TinyOS

```powershell
cd C:\Users\SONG\Documents\Projects\TinyOS
go run ./cmd/tos build -target esp32c3
```

这会生成内核和 `build\fs-stage\` 文件系统内容。

### 7.2 用 goc32 的程序替换其中一个文件

假设要加一个 `bin\blink`：

```powershell
copy C:\Users\SONG\Documents\Projects\TinyOS\build\blink.tos C:\Users\SONG\Documents\Projects\TinyOS\build\fs-stage\bin\blink
```

注意是放在 `bin\` 下，并且文件名就是命令名。

### 7.3 重新打包 littlefs 镜像

TinyOS 的 `mklfs` 会把 `fs-stage` 里的 ELF 自动压成 LZ4TOS；已经是 `.tos` 的文件会原样放进镜像。

```powershell
cd C:\Users\SONG\Documents\Projects\TinyOS
$stage = 'build\fs-stage'
$names = Get-ChildItem -Recurse -File $stage |
    ForEach-Object { $_.FullName.Substring((Resolve-Path $stage).Path.Length + 1).Replace('\','/') }

build\mklfs.exe build\ramdisk-esp32c3.bin 307200 $stage @names
```

这里 `307200` 是 esp32c3 的 ramdisk 大小（600 个 512 字节扇区）。

### 7.4 在模拟器里运行

TinyOS 的 ESP32-C3 镜像用 [RV32EMU-C](https://github.com/yafengabc/RV32EMU-C) 模拟器跑。仓库构建目录里如果有 `build\rv32emu-c.exe` 就直接用；没有的话先从 RV32EMU-C 构建它。

```powershell
"run blink 2`nexit`n" | build\rv32emu-c.exe `
  -elf build\kernel-esp32c3.elf `
  -ramdisk build\ramdisk-esp32c3.bin `
  -tickdiv 4
```

在 shell 里也可以直接：

```text
run blink 2
```

### 7.5 烧到真机

按 TinyOS 的 ESP32-C3 烧录流程走（`scripts/esp-c3/` 里的脚本，或 `tos` 生成的镜像/UF2）。关键是让最终镜像包含重新打包后的 `ramdisk-esp32c3.bin`，而不是重新跑一次 `tos build` 把 `fs-stage` 覆盖掉。

## 8. 体积现状

当前版本（37 个 `user/*.c` 程序，RV32IMC；clang 为 `-O2` 基线）：

| 平台 | goc32 原始 ELF | LZ4TOS | 相对 clang |
| --- | ---: | ---: | ---: |
| RV32 / ESP32-C3 | 132,476 | 95,488 | 1.51x / 1.43x |
| ARM Cortex-M3 | 160,808 | 111,146 | — |
| RP2040 | 185,792 | 117,126 | — |

`sh` 单程序：

| 平台 | goc32 | clang |
| --- | ---: | ---: |
| RV32 | 21,380 / 14,571 | 16,054 / 12,589 |
| ARM M3 | 25,832 / 16,751 | — |
| RP2040 | 30,460 / 17,563 | — |

后端仍是“栈机 + 局部优化”：块内寄存器分配、地址折叠、内联（单调用点、≤40 条 IR 指令，仅 RV32 和 ARMv7-M/v8-M）等。和 clang 的主要差距来自没有全函数寄存器分配、没有大规模内联/循环优化。

## 9. 限制与已知问题

- **语言**：不支持浮点、`long double`、atomics、TLS、内联汇编。
- **ARM `smp.c`**：使用 GCC 寄存器变量和裸 `svc` 内联汇编，goc32 目前无法编译；其它用户程序正常。
- **多文件程序**：goc32 可以一次传多个 `.c`，但单调用点内联只在单输入文件时启用，避免误删被兄弟模块引用的函数。
- **`.tos` 不是目标文件**：不能再 `ld.lld` 链接；需要整个程序一起用 goc32 编译。
- **LIBLLVM**：不链接；但需要 clang/ld.lld 作为汇编器和链接器。
- **`-S` 与最终产物**：`-S` 不启用内联，和 `.tos` 的指令序列可能不同。

## 10. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| `cannot find user/lib.c` | 加 `-tos <TinyOS 根目录>` |
| `clang: command not found` | 安装 LLVM，或 `-cc C:\path\clang.exe` |
| `ld.lld: command not found` | 安装 LLVM，或 `-ld C:\path\ld.lld.exe` |
| `undefined symbol: ...` | 该符号不在 lib.c/头文件里；或漏传了某个 `.c` 输入 |
| `gpio: cannot configure pin` | 引脚超出 `gpio_count()`，或该引脚被 flash/USB/UART 占用 |
| `.tos` 放进去运行不了 | 确认放在 `bin/<命令名>`，并用 `mklfs` 重打了 ramdisk |
| 程序体积比预期大 | 用 `-target` 对应平台；`-keep` 看临时 `.s`；关注是否用了大数组/深递归 |

## 11. 参考

- goc32 源码与最新数据：[yafengabc/goc32](https://github.com/yafengabc/goc32)
- TinyOS：[yafengabc/TinyOS](https://github.com/yafengabc/TinyOS)
- RV32EMU-C：<https://github.com/yafengabc/RV32EMU-C>
- 用户态 API：[API 参考](/goc/goc32/api/)

