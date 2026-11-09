---
title: "TinyOS 用户程序 API 参考（goc32）"
menuTitle: "API 参考"
date: 2026-11-09T12:00:00+08:00
draft: false
weight: 2
slug: "api"
tags: ["goc32", "TinyOS", "API", "libc", "GPIO", "文件系统", "进程"]
categories: ["编程参考", "goc32", "TinyOS"]
description: "用 goc32 编写 TinyOS 用户程序时的完整用户态 API：进程、时间、控制台、文件系统、内存、字符串、GPIO/ADC、屏幕与设备文件。"
---

这篇是 **TinyOS 用户程序 API 参考**，以 `user/lib.h` 为准，适用于 goc32 编译的 `user/*.c`。

所有声明都在：

```c
#include "lib.h"
```

它会把 `types.h` 和 `tos.h` 一起带进来，所以 `size_t`、`uint32_t`、`TOS_PIN_*`、`GPIO_OP_*` 等都能直接用。

## 1. 程序入口与镜像笔记

```c
int main(int argc, char **argv);
```

- 用户程序是 PIE，由内核在 exec 时重定位到任务窗口。
- `argv` 来自 shell/`exec()`；`argv[0]` 是命令名。
- 返回值就是退出码；也可以调用 `exit(code)`。

### `TOS_STACK(bytes)`

覆盖程序默认栈大小。放在文件作用域，只在程序里写一次：

```c
TOS_STACK(16 * 1024);
```

- 必须是 2 的幂、至少一页，且不超过板子的任务窗口粒度（C3/RP2040 为 16 KiB，PC/QEMU 为 64 KiB）。
- 内核在 exec 前读取这个 note，用它决定任务窗口大小。

### `TOS_HEAP(bytes)`

声明程序需要的堆：

```c
TOS_HEAP(12 * 1024);
```

- 不写这个 note 时，堆只有任务窗口对齐后剩下的“边角料”，小工具里经常刚好是 0，`malloc()` 会全部返回 NULL。
- 只要程序调用 `malloc`，就应该声明。
- 它是下限，不是上限；窗口向上取整多出来的部分仍然归堆。

## 2. 进程与系统

| 函数 | 说明 |
| --- | --- |
| `int getpid(void)` | 当前任务 pid |
| `void exit(int code)` | 立即退出，`code` 是退出码 |
| `int exec(const char *path, char *const argv[], int argc)` | 启动一个程序，返回新任务 pid（不是替换当前进程） |
| `int wait(int pid)` | 等待任务结束，返回退出码 |
| `int yield(void)` | 主动让出 CPU |
| `int ps(void)` | 打印任务表，返回任务数 |
| `int setprio(int pid, int prio)` | 设置优先级，`0` 最高、`2` 最低；`pid=0` 表示自己 |
| `int getcpu(void)` | 当前运行在哪个 CPU 上 |
| `void reboot(void)` | 重启，不返回 |

`exec` 的路径是文件系统里的路径，例如 `bin/hello`：

```c
char *args[] = { "hello", "one", "two" };
int pid = exec("bin/hello", args, 3);
if (pid >= 0)
    wait(pid);
```

## 3. 时间

| 函数 | 说明 |
| --- | --- |
| `int sleep(int ms)` | 睡眠毫秒，让出 CPU |
| `int uptime(void)` | 开机以来的毫秒数（调度器时钟） |
| `int clockinfo(struct clockinfo *c)` | 填充高精度时钟信息 |
| `uint64_t clock_ns(void)` | 单调纳秒 |
| `uint32_t clock_ms(void)` | 毫秒（低 32 位） |

```c
struct clockinfo {
    uint64_t ns;       /* 单调纳秒 */
    uint32_t us;       /* 微秒（低 32 位） */
    uint32_t ms;       /* 毫秒（低 32 位） */
    uint32_t tick_hz;  /* 调度 tick 频率 */
    uint32_t cpus;     /* CPU 数量 */
};
```

`clock_ns/ms` 来自自由运行的硬件计数器，不是 10 ms 调度 tick；适合测耗时。

## 4. 控制台与字符 I/O

| 函数 | 说明 |
| --- | --- |
| `int write(int fd, const void *buf, int len)` | 写 fd |
| `int write_all(int fd, const void *buf, int len)` | 循环写直到写完；管道满时会短写，输出大块数据用它 |
| `int read(int fd, void *buf, int len)` | 读 fd |
| `int ttyraw(int on)` | 切换控制台原始模式：返回上一个模式，1=raw |
| `int getline(char *buf, int max)` | 从 fd 0 读一行，去掉换行，返回长度 |
| `int hist_count(void)` | shell 历史条数 |
| `int hist_get(int newest_index, char *buf, int cap)` | 取第 N 条历史（1=最近一条），buf 至少 256 字节 |

约定：`0` 标准输入，`1` 标准输出，`2` 标准错误（内核/管道重定向使用）。

`ttyraw(1)` 后 `read(0)` 会原样返回字节（方向键等），不再回显；进程退出时内核会恢复 cooked 模式。

## 5. 文件与目录

| 函数 | 说明 |
| --- | --- |
| `int open(const char *path)` | 以只读方式打开，返回 fd；失败 `<0` |
| `int create(const char *path)` | 创建/截断文件，返回 fd |
| `int close(int fd)` | 关闭 |
| `int unlink(const char *path)` | 删除普通文件 |
| `int read(int fd, void *buf, int len)` | 读，返回字节数，0=EOF |
| `int write(int fd, const void *buf, int len)` | 写 |
| `int stat(const char *path, struct stat *st)` | 文件信息 |
| `int readdir(int index, char *name, int len)` | 按索引读目录项 |
| `int chdir(const char *path)` | 切换当前目录 |
| `int getcwd(char *buf, int len)` | 取当前目录，返回长度或 -1 |
| `int dfinfo(struct dfinfo *d)` | littlefs 使用量 |

```c
struct stat {
    uint32_t size;   /* 文件大小；目录/设备为 0 */
    uint8_t  type;   /* TOS_FTYPE_REG / DIR / CHR */
};

struct dfinfo {
    uint32_t total;  /* littlefs 分区大小 */
    uint32_t used;   /* 已用字节 */
};
```

`open()` 没有 flags 参数；需要新建/清空用 `create()`。目录项用索引遍历：

```c
char name[64];
for (int i = 0; readdir(i, name, sizeof(name)) >= 0; i++)
    puts(name);
```

## 6. 管道与重定向

| 函数 | 说明 |
| --- | --- |
| `int pipe(int fds[2])` | 创建管道，`fds[0]` 读端、`fds[1]` 写端 |
| `int dup2(int oldfd, int newfd)` | 把 oldfd 复制到 newfd（用于重定向 0/1/2） |

典型用法：

```c
int fds[2];
if (pipe(fds) == 0) {
    int pid = exec("bin/hello", args, argc);
    if (pid >= 0) {
        close(fds[1]);
        char buf[128];
        int n;
        while ((n = read(fds[0], buf, sizeof(buf))) > 0)
            write_all(1, buf, n);
        close(fds[0]);
        wait(pid);
    }
}
```

`read()` 在空管道且还有写端时会阻塞；所有写端关闭后返回 0（EOF）。

## 7. 内存

| 函数 | 说明 |
| --- | --- |
| `void *malloc(size_t n)` | 分配；失败返回 NULL |
| `void free(void *p)` | 释放 |
| `void *sbrk(int bytes)` | 直接移动 break，返回旧 break 或 -1 |
| `int meminfo(struct meminfo *m)` | 内核堆统计 |

```c
struct meminfo {
    uint32_t total;        /* 堆 arena 大小 */
    uint32_t used;         /* 当前已分配 */
    uint32_t free;         /* total - used */
    uint32_t peak;         /* 历史峰值 */
    uint32_t ram_total;    /* 可用 RAM */
    uint32_t kernel_used;  /* 内核镜像 + .bss（含堆） */
    uint32_t app_total;    /* 预留给任务窗口的 RAM */
    uint32_t ram_unused;   /* 剩余 */
};
```

再次提醒：调用 `malloc` 的程序要写 `TOS_HEAP(...)`。

## 8. 字符串与格式化

```c
size_t strlen(const char *s);
int    strcmp(const char *a, const char *b);
int    strncmp(const char *a, const char *b, size_t n);
char  *strcpy(char *dst, const char *src);
char  *strncpy(char *dst, const char *src, size_t n);
void  *memcpy(void *dst, const void *src, size_t n);
void  *memmove(void *dst, const void *src, size_t n);
void  *memset(void *dst, int c, size_t n);
int    memcmp(const void *a, const void *b, size_t n);
char  *strchr(const char *s, int c);
char  *strrchr(const char *s, int c);
size_t strspn(const char *s, const char *accept);
size_t strcspn(const char *s, const char *reject);
char  *strstr(const char *hay, const char *needle);
int    atoi(const char *s);
```

输出：

```c
int  printf(const char *fmt, ...);
int  tos_vsnprintf(char *buf, int cap, const char *fmt, __builtin_va_list ap);
void puts(const char *s);
void putchar(int c);
int  putint(int v);
```

`printf`/`tos_vsnprintf` 支持的格式是 `%d %u %x %c %s %%`，可带零/空格宽度；没有 `%p`、`%ld`、浮点格式。需要把文本写进缓冲区时用 `tos_vsnprintf`（没有公开的 `snprintf` 包装）。

## 9. GPIO 与 ADC

```c
int gpio_count(void);
int gpio_mode(int pin, int mode);
int gpio_write(int pin, int value);
int gpio_read(int pin);

int adc_count(void);
int adc_read(int chan);      /* 原始计数，失败 -1 */
int adc_vref_mv(void);       /* 参考电压 mV */
int adc_bits(void);          /* 分辨率位数 */
```

模式常量：

| 常量 | 含义 |
| --- | --- |
| `TOS_PIN_IN` | 输入 |
| `TOS_PIN_OUT` | 输出 |
| `TOS_PIN_IN_PULLUP` | 输入 + 上拉 |
| `TOS_PIN_IN_PULLDOWN` | 输入 + 下拉 |

示例：

```c
int n = gpio_count();
if (n > 0) {
    gpio_mode(2, TOS_PIN_OUT);
    gpio_write(2, 1);
    sleep(200);
    gpio_write(2, 0);
}
```

### 通过 `/dev/gpioN`

同样的事情也可以走设备文件：

```c
int fd = open("/dev/gpio2");
if (fd >= 0) {
    ioctl(fd, GPIO_OP_MODE, TOS_PIN_OUT);
    write(fd, "1", 1);
    char buf[8];
    int n = read(fd, buf, sizeof(buf) - 1);
    if (n > 0) {
        buf[n] = 0;
        printf("level=%s", buf);
    }
    close(fd);
}
```

`GPIO_OP_MODE` / `GPIO_OP_WRITE` / `GPIO_OP_READ` / `GPIO_OP_COUNT` 定义在 `tos.h`。

ADC 通过 `/dev/adcN` 时使用 `ADC_OP_*`。

## 10. 屏幕与帧缓冲

如果板子有 LCD，设备节点是 `/dev/lcd0`（文本/字符模式，`/dev/screen` 是它的别名）和 `/dev/fb0`（RGB565 像素模式）。

字符屏：

| `ioctl(fd, ...)` | 参数 / 返回 |
| --- | --- |
| `LCD_OP_INFO` | 返回 `(cols << 16) \| rows` |
| `LCD_OP_CLEAR` | 参数 RGB565，清屏 |
| `LCD_OP_CURSOR` | `(row << 16) \| col` |
| `LCD_OP_BACKLIGHT` | `0/1`，返回旧状态 |
| `LCD_OP_MIRROR` / `LCD_OP_MIRROR_GET` | 镜像开关/查询 |
| `LCD_OP_INVERT` | 反色 |
| `LCD_OP_FG` / `LCD_OP_BG` | 前景/背景 RGB565 |
| `LCD_OP_PIXELS` | 返回 `(width << 16) \| height` |
| `LCD_OP_DEVS` | 绘制设备表 |

对 `/dev/lcd0` 的 `write()` 按 UTF-8-ish ASCII 文本渲染，处理 `\n`、`\r`、`\b`。

像素屏：

| `ioctl(fd, ...)` | 作用 |
| --- | --- |
| `FB_OP_INFO` | 返回 `(width << 16) \| height` |
| `FB_OP_RECT` | 用 `FB_RECT(x,y,w,h)` 选择下一块写入区域 |
| `FB_OP_SYNC` | 等待 DMA 完成 |
| `FB_OP_CLEAR` | 用 RGB565 填满 |

`FB_RECT(x,y,w,h)` 把四个字节压进一个 int；`FB_RECT_X/Y/W/H` 取出。`write()` 的数据是两个字节一个像素（高字节在前）。

## 11. 设备文件

| 路径 | 说明 |
| --- | --- |
| `/dev/gpioN` | GPIO N |
| `/dev/adcN` | ADC 通道 N |
| `/dev/ledN` | 板载 LED；`write` "0"/"1"，`ioctl(0/1)` 设置/读取 |
| `/dev/lcd0`, `/dev/screen` | 字符屏 |
| `/dev/fb0` | 像素帧缓冲 |
| `/proc/ps` | 任务表 |
| `/proc/dev` | 设备表 |

`/proc/dev` 和 `ls /dev` 可以查看当前板子实际暴露了哪些节点。

## 12. 错误约定

- 系统调用风格：失败返回负值（通常是 `-1`），成功返回 `0`、字节数、fd 或 pid。
- 没有用户可见的 `errno`；需要判断返回值。
- `read()` 返回 `0` 表示 EOF；`read()` 在管道上可能阻塞。
- `malloc()` 失败返回 `NULL`。

## 13. 语言与编译限制（goc32）

- 不支持浮点、`long double`、atomics、TLS、内联汇编。
- 支持整数/指针/数组/结构体/函数/可变参数。
- 后端仍是栈机 + 局部优化；没有全函数寄存器分配。
- 单调用点内联只在单输入文件、RV32/ARMv7-M/v8-M 上启用，且函数不超过 40 条 IR 指令。
- ARM 上的 `user/smp.c` 使用 GCC 寄存器变量和裸 `svc` 内联汇编，暂时不能编译。

## 14. 最小示例索引

| 需求 | 组合 |
| --- | --- |
| 点灯/闪灯 | `gpio_count` + `gpio_mode` + `gpio_write` + `sleep` |
| 读按键 | `gpio_mode(pin, TOS_PIN_IN_PULLUP)` + `gpio_read` |
| 读模拟量 | `adc_count` + `adc_read` + `adc_vref_mv` |
| 复制文件 | `open` + `read` + `write_all` + `close` |
| 列目录 | `readdir` 循环 |
| 运行子程序 | `exec` + `wait` |
| 管道重定向 | `pipe` + `dup2` + `exec` |
| 申请内存 | `TOS_HEAP(...)` + `malloc/free` |
| 画字符屏 | `open("/dev/lcd0")` + `write` + `ioctl` |

完整的可运行例子可以直接看 TinyOS 的 `user/` 目录：`gpio.c`、`cat.c`、`ls.c`、`mem.c`、`wheap.c`、`sh.c`。

