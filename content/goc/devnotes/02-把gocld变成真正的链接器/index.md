---
title: "开发笔记：把 gocld 变成真正的链接器"
menuTitle: "把 gocld 变成真正的链接器"
date: 2026-10-06T17:10:00+08:00
draft: false
weight: 2
tags: ["goc", "gocld", "链接器", "COFF", "ELF", "x86-64", "开发笔记"]
categories: ["编程开发", "goc", "开发笔记"]
description: "让 goc -c 产出真正可重定位的目标文件（PE 的 COFF 与 Linux 的 ELF64），让 gocld 成为可独立运行的链接器。本文记录这次改造里踩到的字节级坑：字符串表 NUL 约定、Elf64_Sym 漏掉 st_other、section 索引 1-based、sh_link 偏移差 4 字节、deferred 分支跳过重定位生成，以及 goa 与 ELF 两套PC-relative 算术的对齐。"
---

> 这是一篇**开发笔记**，不是教程。记录的是把「编译」拆成「编译 + 链接」时踩到的坑，包括那些读起来完全正常的字节。教程正文里不写这些。

## 起因

goc 一直是「一次编译直接出可执行文件」：`goc a.c b.c` 把每个 `.c` 当独立翻译单元解析，合并声明，然后整体交给 goa 汇编成 exe。**没有 `.o` 这个中间产物**，也就没有链接阶段。

这个设计在只有一个 `.c` 时看不出问题。但一旦有人问「能不能分两次编译」，答案是不能——因为没有可重定位的东西可分。

## 目标

- `goc -c foo.c` 产出真正的 `.o`：PE 目标下是 COFF，Linux 目标下是 ELF64
- `gocld a.o b.o -o app` 独立链接，也接受 `.o` 与 `.c` 混合
- goclib 的重复副本能去重

## 坑 1：`fwrite.Lcmp13` —— 局部标签被提升成了全局符号

goc 的 C 库（goclib）没有独立的库阶段：**每个用到 `printf` 的翻译单元都自带一整份实现**。这在单文件构建下是自洽的，两个单元合并时就会撞名。

第一次撞的是 `fwrite.Lcmp13`。追下去发现，goa 把函数内的 `.L` 局部标签拼成 `函数名.L标签` 并 `defineSym` 成 EXTERNAL 级全局符号，于是 `fwrite` 的一个内部跳转标签参与了全局命名空间。

`.goc_lib` 清单里只列了函数名（`fwrite`），不含这些派生名，所以去重逻辑认不出来。

修法是按 owner 识别：

```go
owner, rest, qualified := strings.Cut(name, ".")
return qualified && strings.HasPrefix(rest, "L") && objLib[owner]
```

这个判断的关键洞察是：`.L` 标签**总是**带函数名前缀，所以 `X.Y` 形式的撞名⟺ `X` 本身就撞了名。既然 `X` 已经走去重或报错路径，`.Y` 那部分只需跟着 owner 认就行。

## 坑 2：Linux 目标写出的是 COFF 字节

`goc -c -target linux u.c` 报成功，链接时却报 `elf: bad magic`。

`AssembleObject` 里硬编码了 `gocld.WriteCOFFObject(img)`——没有按目标分派。gocld 只有 ELF 的**读端**和**可执行文件写端**，唯独没有 ELF **对象**写端。

那就写一个。写完之后踩了下面这一串字节级的坑。

## 坑 3：字符串表没有以 NUL 开头

ELF 的 `.strtab` 和 `.shstrtab` 都有同一个约定：**偏移 0 必须是空名**（null symbol 的 `st_name`、NULL section 的 `sh_name`）。所以两张表都要以 `\x00` 开头。

漏掉这个的后果是全套输出里最难查的一种：

- 第一个真实名字落在偏移 0
- null symbol 的 `st_name` = 0，于是它**指向了那个真实名字**
- 后面每个符号的偏移整体错一位，落在前一个名字的中间

`readelf` 打印出来的 section 名和符号名全是乱码（当时看到的是 `__goclib_exit^J` 这种），而文件本身没有任何一处"非法"。objdump 会报「有 section 超出文件末尾」，但那是另一个 bug。

## 坑 4：`Elf64_Sym` 漏掉 `st_other`

```
st_name(4) st_info(1) st_other(1) st_shndx(2) st_value(8) st_size(8) = 24
```

`st_other` 是中间那1 字节。漏掉它之后，条目**仍然是 24 字节**（`st_size` 正好吸收这个损失），所以没有任何长度检查会失败——但每个符号的 `st_shndx` 和 `st_value` 都整体偏移一字节，指向错误的节。

这类 bug 的共同特征：**丢掉的那一字节被下游某个字段吸收了，于是错误不可见**。

## 坑 5：section 索引是 1-based

`e_shstrndx`、`sh_link`、`sh_info` 全都是「节头表里的下标」，而 0 号是 NULL section（无数据）。所以文件里的节序号 = image 里的节序号 + 1。

我当时在对应的 `put` **之前**就把索引算出来了，于是全体off-by-one：`e_shstrndx` 指向 `.symtab`，而 `.symtab` 的字节是另一张字符串表。于是每个节名都解析成「那个位置上恰好存在的符号名」，或干脆什么都没有——objdump 列出一个七个节全部没有名字的对象。

改成所有 `put` 完成之后在 body 里反查，才是对的。

## 坑 6：`sh_link` / `sh_info` 的偏移差4 字节

`sh_size` 占 +32..+39（8 字节），所以紧随其后的两个 4 字节字段在 **+40 和 +44**，不是直觉上的 +36/+40。

写成 +36/+40 会让这两个字段落进 `sh_size` 的高位和 `sh_addralign` 里，reader 读出来的节长度是 `12884901984`（= 0x300000060）这种一眼荒谬的值。这个反而是最好查的一个。

## 坑 7：deferred 分支里的 `continue` 跳过了重定位生成

跨单元引用的符号，在读第一个对象时还不知道第二个对象会不会定义它。deferred 模式下正确做法是：**记下来（`AddPending`），但不要跳过 fixup 生成**。

我写了 `continue`，于是符号随后被兄弟对象定义了，却已经没有重定位要应用——字段保留占位字节 0。链接干净通过，程序调用的是**下一条指令**。

这是最值得记住的一个：它不产生任何错误，只是安静地算错。而且症状是「结果不对」而不是「链接失败」，极难定位。

## 坑 8：`st_shndx` 写的是布局之前的下标

这个坑最值得单独讲，因为它**通过了所有已有的检查**。

写符号表时，`st_shndx` 用的是「这一节在 `secs` 切片里的下标」。但布局是之后才排的，而排布局时会往 body 里插入 `.rela.<name>` —— 紧在被重定位的那一节**后面**。于是文件里的节序号整体后移：

```
secs 下标（写符号时以为的）      文件里的真实下标
  1 .text                         1 .text
  2 .rdata                        2 .rela.text      ← 插进来了
  3 .data                         3 .rdata          ← 全部 +1
  4 .bss                          4 .data
                                  5 .bss
```

符号带着旧下标，于是 `.rdata` 里的字符串符号声称自己在 `.rela.text` 里。

**为什么检查不出来**：节头全部正确、符号表格式全部正确、每个符号的 `st_value`（节内偏移）也全部正确。错的只有「属于哪个节」这一项，而这一项没有任何单独的校验。

症状是：`goc hello.c -o hello`（单文件直出）输出 `hello, world` 正确；
`goc -c hello.c && goc hello.o -o hello`（经 `.o` 往返）输出 **12 个空格**。

长度对，内容是乱码——因为 `lea [rip+msg]` 解析到了 `.text` 里某个函数的机器码，
程序按字符串长度打印了那段字节。

**为什么单节测试测不出**：没有重定位就不存在 `.rela` 节，谁都不移位，
临时下标恰好等于真实下标。必须构造「`.text` 有 fixup，后面还有 `.rdata`/`.data`/`.bss`」
的 image 才能暴露。所以补测试时特意写成这个形状，并验证它在修复前确实失败：

```
msg: st_shndx names section ".rela.text", want ".rdata"
```

修法是布局定下来之后按 body 的实际位置重映射所有符号的 `st_shndx`——包括
section symbol。section symbol 是靠携带下标来「命名某一节」的，不动它会让
「对本节的 relocation」打到别处。

教训一句话：**ELF 的 section 索引在布局定下来之前是不存在的**。任何先写
`st_shndx` / `sh_link` / `e_shstrndx` 再排布的做法都会错。

## 坑 9：两套 PC-relative 算术

goa 的 `applyFixup`：

```
target + Addend - (base + Off + size + RipAdjust)
```

从字段**之后**的字节算起。ELF 的 `R_X86_64_PC32`：

```
S + A - P
```

`P` 是字段**自身**的地址。两个都要对，唯一的办法是让 addend 吸收差值：

```
A = Addend - size - RipAdjust
```

call 的 `RipAdjust` 是 0（位移字段的末尾就是下一条指令的开头），所以 `A = -4`，恰好是 x86-64 对 call 的惯例。

## 坑 10：`mode` 字符串的历史包袱

`-c` 原来只表示「不自动运行」，和默认路径是同一件事，所以 `buildCfg.mode` 默认值和 `-c` 都是 `"compile"`。改成「`-c` 停在对象」之后，两个语义分叉了，但**名字还共用着**：

```
$ goc -target linux hello.c -o hello.bin
compiled hello.c -> hello.o (19520 bytes)     # 走了 -c 分支，没有可执行文件
```

修法是把默认mode 改成 `link`，`-c` 改成 `object`。凡是历史上共用一个值的地方，以后都必须分开——因为它们迟早会变成两件事。

## 坑 11：验证脚本伪装成被测系统的现状

`run_tests.sh` 里 `-c` 还在按老语义用（`-c -o <dir>` 之后去找无扩展名的可执行文件），
于是 Linux 腿 79 项全报 `cannot load ELF`；Windows 腿则「假绿」——它跑的是上一次
遗留在输出目录里的 exe，看起来全过。

加上 `run_goa` / `run_unit goa` 还在用 `goa/` 路径（模块早就搬进 `src/` 了），
路径错误被报成测试失败；顺序分支读一个从未赋值的 `$rc`，而 `set -u` 让它恰好在
「汇报结果」那一行变成致命错误。

验证工具的缺陷会以被测对象的缺陷的形式出现。脚本和解释器报错时，第一件事
该是确认它说的是不是真的。

## 坑 12：COFF 节头声明的重定位条数比实际写出的多

`relCount`（写进节头的那个数）和实际写出的重定位记录，是两趟独立的遍历，
而这两趟看的不是同一批fixup：没有 COFF 重定位类型的（短跳转）在写出时被
`continue` 丢掉了，计数时却照样算上。

于是节头声称的重定位条数大于文件里实有的条数。读端当然信节头——**所有读端
都信**——于是它按声称的条数走 10 字节一条，越过表尾走进符号表，把符号表的
字节当重定位条目解码。报出来的是：

```
coff: .text relocation 294: symbol index 28462 out of range (282 symbols)
```

`28462` 在一个 282 条的符号表里——这个数字指向不了任何东西。真正的因果离它
十万八千里：某个短跳转的 fixup 被丢了，导致节头多算了一条。

修法是让计数和写出用同一个判定。顺带一句：这类「两趟遍历必须给出同一个答案」
的地方，判定条件应该只有一个来源，否则它们迟早会分叉。

## 坑 13：`.o` 里没有任何关于 DLL 归属的信息

`winbox` / `winreg` / `wintest` / `c11_threads_basic` 四个 GUI / 线程例子
单文件直接编译+链接都对，一旦走 `.o` 就链接失败：

```
undefined symbol(s): GetSystemMetrics, MessageBoxA
```

COFF 对象把每个它不定义的符号留成 undefined，而**文件里没有任何字段说明
这个符号该由哪个 DLL 导出**。这个知识只在编译器手里——它读的是 goclib 头文件
里的 `extern int MessageBoxA(...), user32;`。对象文件把它丢了。

链接器确实有一张 `coffWin32DLLs` 表可以做兜底，但它里面只有 kernel32，而且
它本来是给 LLVM 路径准备的（那条路径只碰得到 kernel32）。往里手抄 306 个符号
（`user32` 107 个、`gdi32` 49 个……）是条错路：表是链接器对**一个程序的导入**
的猜测，而一个仅仅不完整的猜测和程序本身的 bug 长得一模一样。

正确的形状是让对象自己带。COFF 没有元数据节，但符号表里可以塞一个——这和
`.goc_lib` 是同一个手法，那份库符号清单就是这么活过 `.o` 往返的。于是加了一个
`.goc_dll:`静态符号，名字是 `Name=user32` 这样的对，逗号分隔；读端把它读回
`img.Exts`，也就是 `buildIData` 生成 `.idata` 用的那份。

**导入是关于加载器的声明，不是定义**，所以读回来的是 `Exts` 而不是 `img.Syms`：
给一个导入名一个映像地址，会让重定位解析到一个根本没有地址的 IAT 槽位上。

## 坑 14：把「不可表达」当成「可以丢弃」

`jmp short label` 写进对象时，`coffRelocType` 和 `elfRelocFor` 都拒绝它——
COFF 和 ELF 都没有 rel8 重定位类型。两端的注释都写着「拒绝而不是近似」，
听起来很正确。

然后 `asm2` 挂了：

```
FAIL jmp short skips one insn: got 7 want 2
FAIL jrcxz taken on rcx==0: got 101 want 1
```

**丢弃 fixup 不等于丢弃问题。** 被丢掉的位移字节里留着代码生成器写的占位值，
于是 `jmp short` 跳到占位值指的地方：链接通过、加载通过、运行通过，只是走错
了分支。这是「无声」和「只是算错」同时成立的一种错，比缺一条错误信息糟糕得多。

短跳转的位移是**节内相对**的，而它的两端（字段位置、目标位置）在写对象时都
已经确定——链接把节摆到哪里都不影响这个差。所以根本不需要 rel8 重定位：
写端把这个字节算出来写进去，不为它生成重定位。COFF 用 `cloneShortFixed`，
ELF 用 `resolveShorts`，都是先拷一份节数据再改，不动调用方那张 Image
（`Image` 可能还要拿去直接链接，可重定位对象不该消费掉别人交给它的 fixup）。

跨节目标仍然留给链接时报错：那种跳转按定义就超范围，goa 会在链接时报出具体
字节数。

## elfcheck 的两个自身缺陷

验证工具本身也有问题，值得单独记一笔，因为它们都伪装成了「被测对象出错」：

**1. 只按 `p_filesz` 分配内存。** `.bss` 在 `p_memsz` 里但不在 `p_filesz` 里，于是段在零初始化数据开始的地方被截断。`puts` 一碰 stdio 缓冲就报：

```
elfcheck: read of unmapped memory 0x403b78 at 0x400ebd
```

`0x403b78` 明明落在 `.bss` 范围内（0x401970 + 0x2318）。按 `p_memsz` 分配就对了。

**2. 缺 `movslq`。** `REX.W + 0x63`（`movsxd %eax,%rax`，int → long 加宽）解码器里没有。这是 goa 对每个整型加宽都会发的指令，不是什么罕见路径。

这两个都不是 goc 的 bug。**验证工具的缺陷会以被测对象的缺陷的形式出现**，所以它报错时第一件事该是确认它说的是不是真的。

## 还没做的

链接器目前**不做死代码消除**。被去重的库副本，它的字节仍然留在 `.text` 里——符号确实不再被引用了，但代码还在。两个单元都用 `printf` 时 `.text` 是 10505 字节，其中约一半是永不执行的第二份副本。

要做需要在合并前就知道每个函数的字节区间，而 COFF 符号表只有偏移没有大小，得靠「同一节内按偏移排序取相邻差值」来推断。留作后续。

## 结果

```
# PE：两个单元都用 printf，靠 goclib 去重链接成功
compiled a.c -> a.o / compiled b.c -> b.o / compiled pm.c -> pm.o
linked 3 objects -> app.exe (12288 bytes)
from a
from b

# Linux：分离编译 + 链接
compiled u.c -> u.o / compiled h.c -> h.o
linked 2 objects -> app
elfcheck: ran 48 instructions, exit=43

# Linux：单文件直接出可执行
$ goc -target linux hello.c -o hello.bin
elfcheck: hello from linux
elfcheck: ran 1264 instructions, exit=7
```
全量回归（`run_tests.sh`，Windows / Linux 各三档优化 + goa 套件 + 三模块单测）：

```
pass=460 fail=27
```

Windows 三档各 82/82，goa 套件 14/14，`src/goa` 62 个单测全过。

27 个失败全在 Linux 那条腿（三档各 9 个），并且**与对象文件无关**：
`bitint`、`c11_threads_basic`、`c23_string`、`fileio`、`goclib`、`goclib2`、
`goclib_test`、`headers`、`libmisc`。逐个对比过——

```
退出码一致: 9, 不一致: 0
```

`goc -target linux x.c`（不经 `.o`）与 `goc -c` + `goc x.o` 两条路径退出码完全一样。
它们输出正确、收尾时空指针访问（`unmapped access at 0x0`）导致退出码非 0，
是 Linux 目标的既有缺陷，这一轮没碰它。
