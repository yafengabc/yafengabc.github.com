---
title: "第 2 章：写第一个 Windows GUI 程序"
menuTitle: "第 2 章 第一个 GUI 程序"
date: 2026-10-06T12:30:00+08:00
draft: false
weight: 3
tags: ["goc", "C 编译器", "Win32", "Windows", "GUI", "教程"]
categories: ["编程开发", "goc"]
description: "用 goc 写一个弹 MessageBox 的 Windows GUI 程序，编译出 37KB 的 exe，只依赖 kernel32 和 user32 两个 DLL——没有 msvcrt。附完整 API 说明与常见错误排查。"
---

上一章的 Hello World 还是控制台程序。这一章弹个真正的窗口，顺带验证一个硬指标：**产物到底依赖哪些 DLL**。

## 目标

写一个程序，弹出消息框显示屏幕尺寸和窗口句柄状态，最终产物**只导入 `kernel32.dll` 和 `user32.dll`**。

## 完整代码

```c
/* msgbox.c -- 第一个 Windows GUI 程序 */
#include <windows.h>
#include <stdio.h>

int main(void) {
    /* 1. 拿到自己的模块句柄。传 NULL 表示"要当前进程" */
    HINSTANCE self = GetModuleHandleA(NULL);

    /* 2. 取屏幕尺寸（Win32 里这些常量直接可用，不需要自己定义） */
    int w = GetSystemMetrics(SM_CXSCREEN);
    int h = GetSystemMetrics(SM_CYSCREEN);

    /* 3. 拼一段文字。goclib 的 sprintf 可用 */
    char buf[128];
    sprintf(buf, "屏幕: %d x %d\n窗口句柄非空: %d", w, h, self != NULL);

    /* 4. 弹框。四个参数：父窗口、 正文、标题、按钮与图标 */
    MessageBoxA(NULL, buf, "goc 第一个 GUI 程序", MB_OK | MB_ICONINFORMATION);

    return 0;
}
```

## 编译

```bash
./bin/goc.exe -c -o msgbox.exe msgbox.c
./bin/goc.exe run msgbox.c
```

`run` 会直接弹框，窗口关掉后进程退出。

## 验证依赖

这是本章的重点。产物跑起来了，但这说明不了什么——**要看它到底拖了哪些 DLL**：

```bash
$ objdump -p msgbox.exe | grep "DLL Name"
	DLL Name: kernel32.dll
	DLL Name: user32.dll
```

**就这两个。** 没有 `msvcrt.dll`。

这是 goc 和 gcc 最直观的分野。MSYS2 的 gcc 编同样代码，依赖表会长这样：

```
	DLL Name: kernel32.dll
	DLL Name: msvcrt.dll     ← 没了这一行
	DLL Name: user32.dll
	DLL Name: KERNEL32.dll
	...
```

gcc 那条链是：`mainCRTStartup` → `__scrt_common_main` → 初始化 CRT（stdin/stdout/stderr 的FILE 结构、命令行、环境块、atexit 表）→ `main`。这些全是 `msvcrt` 提供的。goc 只需要一个 `_start`，三条指令搞定：

```asm
_start:
	mov r12, [rsp]
	lea r13, [rsp+8]
	and rsp, -16
	sub rsp, 48
	call main
	mov rcx, rax
	call __goclib_exit
```

就这么多。`GetModuleHandleA(NULL)` 之所以能拿到句柄，是因为入口桩把 argc/argv 摆好了位置，由kernel32 自己从 PEB 里查——不需要 CRT 帮忙。

## 体积

实测（同一台机器，同一份源码）：

| 编译方式 | 体积 |
| --- | ---: |
| goc `-O0` | 45568 字节 |
| goc `-Os` | 37376 字节 |
| gcc `-O2`（同样代码） | 约 39 KB（静态） |

goc 的 `-Os` 比 `-O0` 小 8KB，主要省在函数内联策略上——`-Os` 关掉了唯一会变大代码的 pass。

## 几个会用到的 API

`windows.h` 里的东西怎么用，看这个结构就清楚了：

### 句柄

Win32 里"对象"都由句柄（handle）表示，本质是个不透明指针：

```c
HANDLE      h;      // 通用
HWND        hwnd;   // 窗口
HINSTANCE   inst;   // 模块（.exe 或 .dll）
HMODULE     mod;    // 同上（类型别名）
```

`GetModuleHandleA(NULL)` 返回当前 exe 的句柄；要拿 `kernel32.dll` 的句柄就传字符串：

```c
HMODULE m = GetModuleHandleA("kernel32.dll");
HMODULE bad = GetModuleHandleA("no_such_module_xyz.dll");  // 返回 0
```

### 输出到控制台

即使是 GUI 程序也常要打日志。**不要用 printf**（虽然能用），直接走 Win32 更省：

```c
HANDLE h = GetStdHandle(STD_OUTPUT_HANDLE);
const char *msg = "hello\n";
DWORD wrote = 0;
BOOL ok = WriteFile(h, msg, (DWORD)strlen(msg), &wrote, 0);
```

注意 `WriteFile` 要传**字节数**而不是字符串长度，所以得 `strlen`。

### 错误处理

```c
SetLastError(0x7777);
DWORD le = GetLastError();     // 0x7777
```

这套 round-trip goc 是通的，`src/examples/wintest.c` 里有对应回归用例。

### 常量

`SM_CXSCREEN`、`MB_OK`、`MB_ICONINFORMATION`、`STD_OUTPUT_HANDLE`、`INVALID_HANDLE_VALUE` 这些都自带，不用自己 `#define`。

有个坑：`MB_OK | MB_ICONINFORMATION` 里的 `|` 是**真的按位或**。早期版本的 goc 不支持 `|` 运算符，`wintest.c` 的注释里留了这条记录：

```c
/* Real bitwise-or: goc supports | since 2026-09-28. */
MessageBoxA(NULL, buf, "goc winbox", MB_OK | MB_ICONINFORMATION);
```

现在支持了。

## 从 MessageBox 到消息循环

弹框只是"一次性 UI"。要做一个真正的窗口，得处理消息循环。`src/examples/winreg.c` 是完整的参考实现，结构大致是这样：

```c
/* 1. 注册窗口类 */
static int wndproc(HWND h, UINT msg, WPARAM wp, LPARAM lp) {
    if (msg == WM_PAINT) {
        /* 拿到绘图上下文，画点东西，结束 */
        return 0;
    }
    return DefWindowProcA(h, msg, wp, lp);
}

int main(void) {
    WNDCLASSA wc;
    wc.lpfnWndProc = wndproc;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "gocwin";
    RegisterClassA(&wc);

    /* 2. 创建窗口 */
    HWND h = CreateWindowExA(0, "gocwin", "标题", WS_OVERLAPPEDWINDOW,
                            CW_USEDEFAULT, CW_USEDEFAULT,
                            800, 600, NULL, NULL, wc.hInstance, NULL);

    /* 3. 消息循环 */
    MSG m;
    while (GetMessageA(&m, NULL, 0, 0)) {
        TranslateMessage(&m);
        DispatchMessageA(&m);
    }
    return 0;
}
```

三个要点：

1. **窗口过程是回调**——`WndProc` 的地址传给 `RegisterClassA`，Windows 在收到消息时反向调用它。所以它得是个 `static` 函数，且签名严格匹配。
2. **消息循环是 `while`**——`GetMessageA` 返回 0 才退出。所以 GUI 程序不是靠 `return` 结束的，是靠这个循环。
3. **`WM_PAINT` 里必须调`BeginPaint`/`EndPaint`**（或 `ValidateRect`），否则 Windows 会反复重发 `WM_PAINT`。

编译成 GUI 程序（不弹黑框）：

```bash
./bin/goc.exe -c -mwindows -o app.exe app.c
```

`-mwindows` 把 PE 的 subsystem 设成 2（Windows GUI），双击运行时**不会有控制台窗口**。这时 `hInstance` 从 `GetModuleHandleA(NULL)` 拿，不依赖 CRT。

> 💡 `wc.lpfnWndProc = wndproc;` 这种"取函数地址"依赖函数指针支持——goc 支持，示例里就是这么写的。

## 常见错误排查

| 现象 | 原因 |
| --- | --- |
| `cannot find the goclib C library` | 只拷了 `goc.exe`，没带 `goclib/` 目录（见第1 章坑 2） |
| `cannot find the goclib C library`（在仓库内） | 从错误的 cwd 运行了；设 `GOCLIB_PATH` 指向仓库的 `goclib/` |
| 弹框中文乱码 | `MessageBoxA` 是 ANSI 版本，中文要写 UTF-8 内容并确认系统代码页；或改用宽字符版`MessageBoxW` |
| 程序一闪而过 | GUI 程序没写消息循环，`main` 返回就退出了 |
| 链接报undefined symbol | Windows 侧 extern 的归属 DLL 写在同名头文件里；确认 `#include <windows.h>` 而不是自己声明原型 |

## 本章产物

一个 37KB 的 exe，双击弹出一个消息框，导入表只有两行。

作为对照，同样逻辑用 gcc 写，体积差不多，但依赖表里会多出一条 `msvcrt.dll`——以及它背后那整套你用不到、但不得不被加载的 CRT 初始化代码。

这就是 goc 定位的缩影：**用一条硬指标（依赖表）把"到底拖了些什么"变成可验证的事实，而不是靠感觉。**

下一章[体积实测](/goc/03-体积实测/)会把 goc、LLVM 后端、gcc 三方放一起比，给出一份完整的数字表。

---

> 本章代码与体积数字核对于 2026-10-06，goc 提交 `f200cd1`。`objdump` 用的是 MSYS2 UCRT64自带版本。