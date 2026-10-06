---
title: "开发笔记：gocl 通用化与 Linux ELF 填坑"
menuTitle: "gocl 通用化与 Linux ELF 踩坑"
date: 2026-10-07T03:00:00+08:00
draft: false
weight: 5
tags: ["goc", "gocl", "LLVM", "codegen", "syscall", "Linux", "bugfix", "开发笔记"]
categories: ["编程开发", "goc", "开发笔记"]
description: "记录 gocl 从「连 goclib 都编不过」到「ls 在 Windows PE 与 Linux ELF 输出逐字节一致」这一轮的 13 个坑：__goc__ 宏未注册、陈旧构建产物遮蔽、链接期 syscall 桩第 4 参须走 r10、__goclib_brk identity 宏导致 malloc 段错误、整数提升缺失、switch break 跳错层、member 数组不衰变、exprType 库函数类型缺失、printf 单字符折成 fputc 链接失败，以及跨平台一致性与验证基础设施的坑。"
---

> 这是一篇**开发笔记**，不是教程。记录的是 gocl（LLVM IR 后端）这一轮从「连 goclib 都编不过」到「`ls` 在 Windows PE 与 Linux ELF 输出逐字节一致」之间踩过的所有坑。教程正文里不写这些。

goc 有两台独立编译器（同一前端）：`goc` 走自研 goa 汇编器直接出机器码；`gocl` 走 LLVM IR 生成，再交给 gocld 链接器吃 COFF/ELF 对象。本文的坑**全部来自 gocl 这一侧**——有的是 gocl 独有的 codegen 缺陷，有的是 goclib 在 gocl 路径下才暴露的跨平台不一致，有的是链接期机制本身。按时间线从编译期一路排到运行期。

---

## 一、编译期：goclib 在 gocl 下都编不过

### 坑 1：`__goc__` 宏未注册，gocl 编译 goclib 报 `__builtin_va_list`

**现象**：`gocl` 一吃 goclib 就崩在 `goclib/goclib.h: line 51: expected type specifier, got "__builtin_va_list"`。

**根因**：goclib 的头文件用 `#ifdef __goc__` 二分宿主。`stdarg.h` 在 goc 下走 codegen 内建 `__builtin_va_list`，在宿主编译器下走真正的 `__builtin_va_list`。问题是 `__goc__` 这个宏原先只在 `expandAt`（宏**展开期**）特判，**没有被注册进 `p.macros`**，于是 `#ifdef __goc__` 恒为 false，goclib 永远走"宿主"分支，而 gocl 明明是 goc 自家后端。

**修复**：在 `src/common/preprocess.go` 的 `preprocess()` 里真正把 `p.macros["__goc__"]` 注册进去。注册后 `#ifdef __goc__` 对两个后端都为真。

> 迷惑点：报错的文件名（`goclib.h`）和真正出错的行所在文件（`stdarg.h:51`）不是同一个。看起来像 goclib 的回归，其实是宏机制断了。

### 坑 2：陈旧的 `src/bin/gocl.exe` 遮蔽最新构建，假失败满天飞

**现象**：`go test ./src/gocl/...` 全线报坑 1 那条 `__builtin_va_list` 错误，但手编 goclib 明明能过。

**根因**：gocl 端到端测试的 `compilerPath()` 从测试 cwd 逐级**向上**找 `bin/gocl.exe`。`src/bin/` 比仓库根的 `bin/` 更近，于是先命中一个 10-06 09:09 的**陈旧** `src/bin/gocl.exe`（早于坑 1 那次 `__goc__` 注册修复）。构建产物过期，但报错看起来像代码回归。

**修复**：删掉 `src/bin/gocl.exe`（`*.exe` 已被 `.gitignore` 忽略，正规输出是 `bin/`），测试改用 `GOC_TEST_GOCL=D:/Projects/goc/bin/gocl.exe` 显式指定后全绿。

> 排查技巧：`git stash` 掉全部未提交改动后仍然失败 —— 这一步排除了"我改坏了"，是关键信号，说明是环境/产物问题不是代码问题。

---

## 二、链接期：Linux syscall 桩的生成机制

### 坑 3：链接期自动生成 syscall 桩，但第 4 参数走了 rcx 而非 r10

**现象**：`gocl -target linux` 编出的 ELF，凡是 ≥4 参数的 Linux syscall 一起失败，`errno=8`（EFAULT）。**这里记录的是最终修复版暴露的症状**——更早一轮（提交 `95ed623`，只改 goc 调用方）暴露的是另一组症状（`mmap`/`futex`/`wait4`/`clone` 返回 -9 EBADF），那一轮把r10 特例加在调用方，后来发现位置不对、才挪进桩里。两轮症状不要混看。

**机制（先讲清楚 gocl 怎么处理 syscall）**：gocl 在链接期（`cmd/gocl/main.go` 的 `externalImports`）扫描 LLVM 对象里每个未定义符号，只要 `goa.IsLinuxSyscall(name)` 认识（表里含 `stat`、`brk`=12、`getdents64`=217等），就自动发射一条桩。所以 `__goclib_stat` 能跑，因为它就是这种外部符号，由链接时生成的桩提供实现。

> 桩的实际形态（`src/goa/asm.go:704-719`，容易被想象错）：不是裸的 `mov rax,N; syscall; ret`，而是
> `mov rax,N; mov r10,rcx; call __goc_syscall; ret`。
> `syscall` 指令被收敛到单一入口 `__goc_syscall`（由更早的 `fbc0b42`「gocld: become a real linker」引入），目的是让 Windows 测试用的 gocrun loader 能把整条桩重写成 Win32 后端翻译器。所以第 4 参的 `r10` 搬运**就发生在桩内部**，调用方完全无感——这正是修复要达到的效果。

**根因**：Linux **syscall** ABI 第 4 参数必须是 `r10`（`syscall` 指令会破坏 `rcx`/`r11`），而普通**函数**调用 ABI 第 4 参是 `rcx`。gocl 走标准 SysV，第 4 参落 `rcx`，内核却在 `r10` 读垃圾。`dfacc05` 的 commit body 点名的是 **select(2)、setsockopt(2)、recvfrom(2)**，失败表现为 **EFAULT**。

**修复**：把 `mov r10, rcx` 下沉到 goa 生成的**桩**里（约定属于桩，不属于调用方）。`src/goa/asm.go:688-691` 在 `mov rax,N` 之后发射 `{kind: K_REG, reg: 10}, {kind: K_REG, reg: 1}`（助记符就是 `mov r10, rcx`）；`src/goc/codegen.go:267-269` 的 `callArgRegs` 相应变成无条件 `return c.argRegs()`，Linux 侧返回 `{rdi,rsi,rdx,rcx,r8,r9}`，syscall 名特例删除。任何后端自动正确。提交 `dfacc05`（改了 `src/goa/asm.go`、`src/goc/codegen.go`、`src/gocl/cmd/gocl/main.go` 三处——但main.go 那处是坑 10 的 DLLNames，与本坑无关）。

> 经验：几个**毫不相干**的 syscall 同时坏，先数**参数个数**。这是这条坑的线索——它们唯一的共同点就是参数个数。

---

## 三、运行期：Linux ELF 一跑就段错误（头号坑）

### 坑 4：`__goclib_brk` 被写成 identity 宏，malloc 直接写地址 0

**现象**：`gocl -target linux` 编出的 `ls`（以及任意用到堆的程序）在 WSL alpine 真机**一跑即 segfault**。二分定位：

```
m1 printf         OK
m2 getenv         OK
m4 stat           OK        ← __goclib_stat 能跑（外部桩提供）
m3 opendir/readdir SEGV
m6 复刻 opendir 体  → 崩在 malloc(1328)
m7/m8 单独 malloc(16) 也崩   ← 确认 malloc 在 gocl Linux 路径上根本坏
m5 open/close     OK
m9 直接 brk()/syscall() 链接报 undefined symbol(s): syscall
```

**根因链**：`malloc` → `__goclib_heap_alloc` → `__goclib_brk` → `syscall(12,…)`。`__goclib_stat` 能跑是因为它是**直接别名存根**（IR 里 `call i64 @__goclib_stat` 为外部符号，由链接期生成的桩提供）；而 `brk`/`getdents64` 走的是 `syscall()` 宏形式。但真正坏的不是桩缺失——`brk`(12)/`getdents64`(217) 都在 goa 的 `linuxSyscalls` 表里，链接期本该生成桩。坏的在 `syscall.h` 的 **goc 路线**（`#ifdef __goc__`，两后端都预定义 `__goc__`）把 `__goclib_brk` 错写成了 **identity 宏**：

```c
#define __goclib_brk(addr)  ((void *)(addr))   // 错：恒等，从不调用 brk 桩
```

于是 `os.c` 的 bump 分配器 `heap_cur = __goclib_brk(0)` 恒得 `NULL`，`raw = NULL`，`*((long*)raw) = size` 直接写地址 0 → 段错误。`brk` 桩虽在表里却**从未被调用**。

**修复**：让宏真正调用 `brk` 桩，与 host 路线 `syscall(12,…)` 语义对齐：

```c
#define __goclib_brk(addr)  brk(addr)
```

这一改**同时修好了 goc 自身 Linux 后端的 malloc**——此前同样坏，只是没在真机暴露（goc 自家 Linux 后端也走这条宏）。

> 这个坑是 `ls` 跨平台验证（`plain/-a/-F/-1` 与 Windows 逐字节一致）的**唯一剩余阻塞**。修掉后，goc/gocl 在 Windows、goc/gocl 在 Linux 四种组合全部跑通且名字列一致。

---

## 四、codegen 质量：编得过，但算错 / 编不过

这几个坑是「让 gocl 编出**正确**的 `ls`」时逐个暴露的——`ls` 用了 `qsort`+`strcmp`、`strftime`、`readdir`(结构体成员访问)、`stat` 比较，正好撞上 gocl 的四处缺陷。

### 坑 5：整数提升缺失，`strcmp` 返回 255 而非 -1，qsort comparator 全乱

**现象**：`ls` 在 gocl 下排序错乱（`sub b.txt a.txt` 顺序颠倒）。单独抽 `strcmp` 测：`'a' - 'b'` 在 gocl 下得 `255`。

**根因**：C 规定 `_Bool`/char/short（含 unsigned）操作数在运算前要先**整数提升**为 `int`。`src/gocl/operator.go` 的 `arithCommon` 只做了 usual arithmetic conversion，**漏掉了前置的整数提升**。无符号窄类型（如 `unsigned char`）在窄宽度上算完再零扩展，符号丢失。IR 长这样：

```ir
%t = sub i8 %a, %b
%r = zext i8 %t to i32     ; 'a'-'b' = 255，不是 -1
```

`qsort` 的 comparator 用 `return strcmp(a,b)`，每个 `<0` 恒假，排序全乱。

**修复**：在 `arithCommon` 开头调用 `promoteInt`，窄整型先提升到 `int`：

```go
func promoteInt(t *frontend.Type) *frontend.Type {
    if t == nil { return nil }
    if t.Kind == frontend.KInt && t.Width < 4 { return frontend.IntType() }
    return t
}
```

### 坑 6：switch 内 `break` 跳错层，strftime 多格式串只输出首段

**现象**：`strftime("%Y-%m-%d", …)` 在 gocl 下只输出 `2025`，后面的 `-mm`/`dd` 没了；但 `%Y` 单独、`%F`（`%Y-%m-%d` 的等价单 case 写法）正常。

**根因**：`src/gocl/statement.go` 的 `doSwitch` 建了出口标签 `doneL` 却**没把它压入 `breakTo` 栈**。于是 `switch` 内部的 `break` 跳到了外层 `while` 的出口（无外层时什么都不跳）。复现模式：`%Y` 对、`%Y-` 错、`%Y%m` 错、`%F` 对（单 case 内写完）。

**修复**：在 `doSwitch` 中把 `doneL` 压入 `breakTo` 栈，结束处弹出。

### 坑 7：member() 数组不指针衰变，`e->d_name` 当数组传参 → 编译失败

**现象**：任何把结构体里的 `char[]` 成员当参数传的程序，gocl 编译失败：`'%t28' defined with type '[256 x i8]' but expected 'ptr'`。

**根因**：`src/gocl/call.go` 的 `member()` 无条件 `load()`。数组成员（如 `e->d_name`，类型是 `[256 x i8]`）被原样 load 成 `[256 x i8]` 值，而不是像 C 规定的那样**衰变成指向首元素的指针**。传参时 callee 期望 `ptr`，拿到数组值，类型不符。

**修复**：在 `member()` 里对数组类型做 decay（仿 `ident()` 的处理），`src/gocl/call.go:421-423`：

```go
if ty != nil && ty.Kind == frontend.KArr {
    // 结构体数组成员衰变成指向首元素的指针，与 C 的数组到指针转换一致。
    // 注意要用 frontend.PtrType(ty.Elem)：frontend.Type 里字段叫 Elem，
    // 没有 Base 这个字段。
    return val{op: p, ty: frontend.PtrType(ty.Elem)}
}
```

> 写这段时踩过一个更小的坑：照着 `ident()` 的写法手抄成 `&frontend.Type{Kind: frontend.KPtr, Base: ty.Base}`，而 `frontend/types.go:50` 的字段叫 `Elem *Type`，根本没有 `Base`——编译不过。构造指针类型一律走 `types.go:78` 的 `frontend.PtrType(elem)`。

### 坑 8：exprType 对库函数调用返回类型缺失，`__goclib_stat` i64 vs i32 不符

**现象**：用到 `stat()` 返回值的比较，gocl 链接/IR 报 `%t11 defined with type i64 but expected i32`。

**根因**：`src/gocl/types.go` 的 `exprType` 在 `frontend.Call` 分支只查 `funcDef` 与 `fnPtrVar`，对**库/外部调用返回 nil（默认 int）**。`__goclib_stat` 返回 `long`(i64)，但调用点的比较 `icmp eq i32 %t11` 用了 i32，而 IR 里 `call i64 @__goclib_stat` 是 i64 → 类型冲突。

**修复**：`exprType` 的 `frontend.Call` 分支补上 `libFunc` 查找：

```go
case *frontend.Call:
    if fd, ok := tr.funcDef(n.Name); ok { return fd.Ret }
    if _, ft, ok := tr.fnPtrVar(n.Name); ok { return ft.Ret }
    if fd, ok := tr.libFunc(n.Name); ok { return fd.Ret }  // 新增
    return nil
```

> 这四个 codegen 坑（5–8）都落在 gocl 的 LLVM IR 生成层，修完后 `go test ./...` 全绿（含 `gocl` 与 `gocl/cmd/gocl`）。

---

## 五、printf 特化与链接

### 坑 9：无 `%` 的单字符 printf 被特化成 fwrite，LLVM 折叠成 fputc 后链接找不到

**现象**：`printf("\n")` 这类**没有格式符、且长度恰为 1** 的调用，gocl 链接报 `undefined symbol(s): fputc`。

**根因**：`src/common/printfspec.go` 对无 `%` 分支无条件特化为 `fwrite(lit, 1, len, stream)`。当 `len == 1` 时，LLVM 把 `fwrite(p, 1, 1, F)` 优化折叠成 `fputc(*p, F)`——而 gocl 的调用图裁剪只拉入了 `fwrite` 的定义，链接时 `fputc` 成了未定义符号。

**修复**：无 `%` 分支里，当 `len(lit.Bytes) == 1` 直接发 `fputc`，其余仍发 `fwrite`：

```go
if len(lit.Bytes) == 1 {
    first := &frontend.Index{Base: lit, Idx: &frontend.NumLit{Val: 0, Kind: frontend.TInt}}
    return &frontend.Call{Name: "fputc", Args: []frontend.Expr{first, stream}}
}
return &frontend.Call{Name: "fwrite", Args: []frontend.Expr{ lit, num(1), num(int64(len(lit.Bytes))), stream }}
```

> 验证：长度 1 无 `%`（如 `printf("\n")`）必失败、长度 9 正常——修复后二者都过。

---

## 六、跨平台一致性：goclib 在 gocl 路径下暴露的不一致

这些不是 gocl 的 codegen bug，而是 goclib 自身在两后端/两平台下行为不一致，由 gocl 的「双后端 + 跨平台逐字节一致」目标逼出来的。

### 坑 10：gocl Windows 导入表漏掉库内部 DLL 注解

**现象**：用到 socket 的程序在 gocl（Windows PE）下链接报 `undefined symbol(s): WSAStartup, accept, bind, …`——全是用户程序调到的名字，看不出跟导入有关。

**根因**：gocl 原本只从**用户程序**的原型建 name→DLL 映射。但 `ws2_32` 的注解写在**库内部**的 `winsock2.h` 里，用户程序不包含 → 那些名字没被映射到 DLL，链接找不到。

**修复**：改成先读 `common.DLLNames`（`common.Build` 编 goclib 时记录的那张全量表），再补用户程序的。

### 坑 11：宿主编译器下 `syscall(2)` 错误码全错

**现象**：用宿主编译器（gcc/clang）编 goclib 时，走 `syscall(2)` 的包装函数错误码全乱。

**根因**：libc 的 `syscall()` 把错误压成 `-1` 并把号码记进**自己的** `errno`；goclib 按原始 `-errno` 写 → `-1` 变成"取负得 1，查 1"→ default。

**修复**：`syscall.h` 加 `__goclib_raw()`（`__errno_location()`）还原真实错误码，`syscall.h:219-222` 在位，全部走`syscall()` 的宏约 30 个全套上（不是原先估的 21 个——grep `__goclib_raw(` 有 33 处命中，其中 24 个是单行 `#define`，其余为跨行续定义）。

关于 include 守卫，有一点要说清：守卫 `GOC_SYSCALL_H` **不是**这次加的，`git log -S` 指向更早的 `2db9259`（TCP/IP 层那一轮），`stdlib.c` 重复包含是这次用「删掉第二次 include」的方式解决的（`stdlib.c:718` 注释：「`<syscall.h>` 已在文件作用域包含过了，且是同一个翻译单元」），不是靠守卫兜住。

### 坑 12：Linux `struct stat` 缺 `st_mtime` 字段

**现象**：`stat.h` 头注释承诺三字段同名（跨平台），但 Linux 分支的 `struct stat` 只有 `st_mtim_sec`，没有 `st_mtime`，与 Windows 分支不一致。

**修复**：**不是加字段**，而是在 Linux 分支的 `struct stat` 之后加一条宏别名（`stat.h:69`）：

```c
#define st_mtime st_mtim_sec
```

这个区别不是较真。Linux 的 `struct stat` 布局钉在内核 x86-64 的字节偏移上——stat syscall 直接按内核那份布局填这个结构，重排或插入字段会静默读到错误的字节（`stat.h:9-11` 的注释专门说明这点）。所以只能在结构体外面做别名，碰不到里面的偏移。

### 坑 13：Windows `readdir` 返回 `.` 和 `..`

**现象**：头注释承诺两平台 `readdir` 都不报 `.`/`..`，但 Windows 分支实际返回了。

**修复**：Windows 分支 `readdir` 在返回前加跳过逻辑（与 Linux 分支一致的 `for(;;)` 循环 + `.`/`..` 过滤）。`ls -a` 的设计因此明确为"揭示名字以 `.` 开头的文件，但 `.`/`..` 本身仍不列"。

---

## 七、验证基础设施的坑（不修代码，但阻塞开发）

这几条不是 gocl 的 bug，却是「验证 gocl 对不对」时反复撞上的墙，单列出来省得下次再栽。

- **WSL 路径转换**：Git Bash 调 `wsl` 必须加 `MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'`，否则 `/mnt/d/...` 被转成 `C:/...` 报 `"can't open"`。
- **`/mnt/d` 挂载瞬时抖动**：文件明明存在却报 `No such file`，重跑一次即好——别急着改代码。
- **gocl.exe 是 Windows 程序**：直接喂 `/mnt/d/...` 路径会报 `"the system cannot find the path specified"`。所以 `gocl -target linux` 必须**两段式**：Windows 侧编（`tools/portability/lin_build.sh`）+ WSL 里跑（`lin_run.sh`）。
- **别把编译器当程序跑**：验证 Windows 时误把 `bin/goc.exe`（**编译器**）当 `ls` 二进制执行，报 `"read <dir>: Incorrect function"`——那是编译器在把目录当源文件读，不是回归。真正的 `ls` 是 `apps/coreutils/bin/ls_goc.exe` 之类编译产物。

---

## 收尾：这一轮学到的方法

1. **一个 bug 的名字不等于它的范围。** 看到 `-2 → 4294967294` 就断言"有符号除法丢了符号性"是误诊（见坑 1 的姊妹篇：变参窄整型）。真正定位靠**二分**：变量除法对、字面量除法错；不经变参对、经变参错。先收窄范围再下结论。
2. **先排除环境，再怀疑代码。** `git stash` 全部改动后仍失败 = 不是我改的；陈旧构建产物遮蔽 = 不是代码回归。这两步能在 30 秒内把"代码 bug"和"环境坏"分开。
3. **用独立第三方实现当裁判。** 怀疑 gocl 生成的 IR 不对时，把同一份 `.ll` 丢给 clang 编，对照反汇编。clang 没这个问题 → 是 gocl 的读法越界。手里有等价实现时，别只跟自己的另一个版本比。
4. **几个不相干的调用同时坏，先数参数个数 / 数共性。** 坑 3 的线索就是"它们唯一的共同点是参数个数 ≥4"。
5. **identity 宏是最阴的坑。** 编译能过、链接能过、运行才炸，而且炸在完全不相关的 `malloc`。凡是有"为了宿主兼容而写的恒等/透传宏"，都要问一句：它真的透传了吗，还是把关键调用吞了？

---

## 涉及的主要提交

| 提交 | 内容 |
| --- | --- |
| `dc0f8e6` | `feat(goclib): 让 goclib 成为可被 gcc/clang 编译的通用 C 库`（含坑 1 的 `__goc__` 注册） |
| `dfacc05` | `fix: Linux 第 4 参数的 r10 约定下沉到 syscall 桩`（坑 3，涉及 goc/goa/gocl 三处） |
| `c9e2b58` | `feat(apps/coreutils): ls 双后端构建 + 跨平台逐字节一致；修 gocl Linux 崩溃`（坑 4 的 brk 修复 + 坑 5–9 的 gocl codegen 修复 + 坑 12/13 的 goclib 一致性修复，rebase 在 `28a8d72` 之上） |

验证结论：`ls` 在 goc/gocl 双后端、Windows PE 与 Linux ELF 四种组合下，`plain/-a/-F/-1` 输出逐字节一致，`-l` 名字列一致（大小/日期两平台不同，属设计内）。
