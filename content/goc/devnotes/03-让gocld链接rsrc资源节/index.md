---
title: "开发笔记：让 gocld 链接 .rsrc 资源节"
menuTitle: "让 gocld 链接 .rsrc 资源节"
date: 2026-10-06T20:40:00+08:00
draft: false
weight: 3
tags: ["goc", "gocld", "链接器", "PE", "资源", ".rsrc", "开发笔记"]
categories: ["编程开发", "goc", "开发笔记"]
description: "让 gocld 能读入、合并并写出 PE 的 .rsrc 资源节。本文记录这次实现里踩到的字节级坑：OffsetToData 是文件偏移而不是 RVA 造成的循环依赖、IMAGE_RESOURCE_DIRECTORY_ENTRY 两个字段共用最高位作标志、rd32 的符号扩展把 0x80000000 变成负数、名字字符串是节内绝对偏移而非紧跟 entry、以及名字池的 NUL 终止符。"
---

> 这是一篇**开发笔记**，不是教程。记录的是让链接器认得 PE 资源节时踩到的坑——一个格式里每一个字段都另有含义、而读错不会报错的地方。教程正文里不写这些。

## 起因

`.rsrc` 之前是被**静默丢弃**的。

`coffSectionMap` 里没有 `.rsrc`，所以它走「未知节」的默认路径：被当成一个普通的只读节搬进合并后的镜像。可是 `BuildPE` 只认 `.text` / `.rdata` / `.data` / `.tls` / `.bss` / `.pdata` / `.xdata`，`.rsrc` 不在其中，于是没有任何东西把它写进 exe。

结果是一个合法的 PE，头部完好、能运行，只是资源目录是 0。没有警告，没有错误——`windres` 生成的图标就这么没了。

## `.rsrc` 不是数据，是一棵树

`.rsrc` 节的字节不是「一堆资源」，而是一棵 `IMAGE_RESOURCE_DIRECTORY` 树：

```
IMAGE_RESOURCE_DIRECTORY            (16 字节: Characteristics/TimeDateStamp/Major/Minor/计数×2)
  ├── entry: Name, DataToDirectory   (8 字节 × N)
  └── 子目录 / IMAGE_RESOURCE_DATA_ENTRY (16 字节: OffsetToData/Size/CodePage/Reserved)
```

三层 key 是 **type → name或id → language**。三个 `IMAGE_RESOURCE_DIRECTORY_ENTRY` 各 8 字节。payload 是不透明的字节，链接器只搬不看。

## 坑 1：`OffsetToData` 是文件偏移，于是有了循环依赖

`IMAGE_RESOURCE_DATA_ENTRY` 的 `OffsetToData` **既不是 RVA，也不是节内相对偏移**——它是「相对整个 exe 文件起点的字节偏移」。

这条约束制造了一个环：

```
每个 leaf 的 OffsetToData  →  需要 .rsrc 在文件里的位置
.rsrc 的文件位置          →  需要前面所有节的尺寸加总
.rsrc 的尺寸              →  需要这棵树
```

看起来无解。**破法是承认「尺寸与位置无关」**：这棵树的布局只取决于树本身，跟它放在文件哪儿没关系。于是拆成两趟：

```go
func (r *rsrc) size() int {          // fileOff 传 0，只布局、只算长度
	l := newRsrcLayout(0)
	l.place(&r.root)
	return l.next
}

func (r *rsrc) emit(fileOff int) []byte {   // 位置定了，才真正写字节
	l := newRsrcLayout(fileOff)
	l.place(&r.root)
	buf := make([]byte, l.next)
	l.writeDir(buf, &r.root)
	return buf
}
```

「大小与位置无关」这条性质就是破环的依据。测试里专门有一条钉住它：

```go
func TestRsrcSizeIndependentOfPosition(t *testing.T) {
	// emit 在不同位置都必须是 size() 那个长度
	if got := len(r.emit(0x4000)); got != r.size() { ... }
}
```

## 坑 2：entry 的两个字段共用最高位，而且是**位 31**

第二个字段（`DataToDirectory`）的最高位表示「这是子目录」，这不是什么秘密。但**第一个字段（`Name`）的最高位也表示另一件事**——「这不是整数 id，是指向字符串池的偏移」。

我一开始记成了「高 16 位全 1」（`0xFFFF`）。两次都错：

```
nameField & 0xFFFF != 0       →  整数 id 3 也命中 → 把 id 当名字读 → 越界
nameField & 0xFFFF0000 == …    →  真字符串 0x80000080 不命中 → 把名字当 id 读
```

真实定义是位域：

```c
struct {
    DWORD NameOffset : 31;
    DWORD NameIsString : 1;      // ← 位 31，不是高 16 位
};
```

windres 输出里能直接看到证据：`80 00 00 80` —— 高位是 1，低 31 位是 `0x80`，正是节内字符串池的位置。

修好之后这条被单测钉住（`TestParseRsrcNamedNameUsesHighBit`），因为它是「读错不报错」的那类：判断错了不会崩，只会拿到一个看起来合理的数。

## 坑 3：`rd32` 符号扩展，`0x80000000` 变成了负数

修好坑 2 立刻又炸：合法树报 `truncated`。

```go
func rd32(b []byte, off int) int {
	return int(int32(binary.LittleEndian.Uint32(b[off:])))   // ← 符号扩展
}
```

`int32(0x80000020)` 是 `-2147483648`，`&^ rscIsDir` 之后还是个巨大的负数，`off < 0` 判定越界。

`rd32` 对持有**有符号量**的字段是对的，对「最高位是标志」的字段是错的。加了 `rdU32`：

```go
// rdU32 reads a dword as an unsigned value. rd32 sign-extends, which is right
// for a field that holds a signed quantity and wrong for one whose top bit is
// a flag: the resource tree uses bit 31 of both an entry's Name field and its
// Data field as a discriminator, and sign extension turns 0x80000020 into a
// negative number that every offset test then rejects.
func rdU32(b []byte, off int) int {
	return int(binary.LittleEndian.Uint32(b[off:]) & 0xFFFFFFFF)
}
```

`coff.go` 里其他调用方都没动——`rd32` 对它们依然是对的。

## 坑 4：计数是 WORD，读成 DWORD 会越界

`IMAGE_RESOURCE_DIRECTORY` 的尾部是四个 WORD：`MajorVersion(2) MinorVersion(2) NumberOfNamedEntries(2) NumberOfIdEntries(2)`。

我按 DWORD 读计数。这**不报错**——两个 WORD 拼成一个数，比真实计数大一截，越界检查触发，报出来的是 `truncated section`。一棵完全合法的树被判成截断。

而且错误信息具有误导性：真正的问题是「计数读宽了」，报出来的是「节不够长」。这跟坑 2 的症状一模一样，掩盖了真正的成因。

## 坑 5：名字字符串在节内绝对偏移，不在 entry 后面

entry 是固定的 8 字节，**不管名字是什么**。字符串池是另一个数组，entry 里存的是指向它的偏移。

我以为名字紧跟在 entry 之后，把 entry 的下一个位置当作名字位置传进去。结果读到的是下一个 entry 的 Name 字段当长度——一次错位，后面全崩。

更值得记的是这里为什么容易错：entry 里**两个字段都是 4 字节**，第一个是名字 key，第二个是数据/子目录偏移，它们**长得一模一样**。看字节根本分不出哪个是哪个，只能靠字段顺序和标志位。真实的 entry 是 `09040000 98000000`（lang=1033, leaf@0x98）和 `80000080 68000080`（名字@0x80, 子目录@0x68）——两个字段都是「高 1 位 + 偏移」。

## 坑 6：名字池每项少算了 NUL 终止符

`place` 里给名字池记账时：

```go
l.next += 2 + utf16Len(d.named[i].name)*2        // ← 少了 2 字节
```

名字是「长度 + UTF-16 码元 + NUL」。少算的 2 字节让**第一个字符串名之后的所有偏移都偏前一个 word**。

症状很有欺骗性：树结构完全正确（所有 key 都能读出来），但 payload 读出来全是零。因为 payload 的起点落在了自己的数据头里。短树（没有字符串名）完全正常，所以只有带名字的资源会坏。

同一个错误在 `place` 和 `writeDir` 里各犯了一次——这正是把偏移记账集中到 `rsrcLayout` 的理由：三个数字（目录表偏移、leaf 偏移、文件偏移）分散在三处算的时候，一定会漂。

## 坑 7：`OffsetToData` 指向 payload，不是指向 payload 的描述头

写 leaf 时：

```go
putU32at(buf, d+0, uint32(l.fileOff+d))    // ← d 是 DATA_ENTRY 自己的偏移
```

`d` 是这个 16 字节头的位置，payload 在 `d + 16`。这个写法的后果特别恶劣：

- 资源**存在**，size 正确
- loader 拿 `IMAGE_RESOURCE_DATA_ENTRY` 的**头四个字节**当作图标的头四个字节
- 于是图标是垃圾，但没有任何东西会报错

一个只有偏移差 16 字节的 bug，看起来像「图标画错了」，不像「链接器算错了」。

## 补齐短树：为什么不能放在 parse 阶段

`windres` 会给三层，但手工构造的对象常把 payload 直接挂在类型下（`RT_MANIFEST` 就是这样）。loader 查 `RT_MANIFEST` 走的是 `类型/1/language` 路径，所以短树虽然合法，却**查不到**。

`parseRsrc` 的注释里写着「缺失层会被补齐」，但最初代码没实现——注释是假的。后来实现了，位置选在 `size()` 和 `emit()`，**不是** parse：

```go
// Doing it here would be wrong: filling invents an id, and a merge that matched
// an invented id against a real one would either miss a genuine conflict or
// invent a false one.
```

这不是洁癖。有个测试抓的就是这个：`TestMergeRsrcDirectoryAgainstLeaf` 构造了一边是 `type 3 → id 1 → leaf`、另一边是 `type 3 → leaf` 的两棵树，期望报「同 key 一边是目录一边是资源」。如果 parse 阶段就补齐，第二棵会变成 `type 3 → id 1 → lang → leaf`，两棵树在第二层撞上，形状冲突被**降级成**内容冲突，甚至干脆消失。真实的冲突被掩盖成了「没报错」。

补齐规则本身也踩了一下：中间层的 id 我一开始写成了 `rsrcDefaultLang + i - 1`，即拿语言 id 冒充资源编号。正确的值是 **1**——`RT_MANIFEST` 按 `类型/1/语言` 查找，1 是资源自己的编号，不是编出来的。

## 合并语义

多对象合并按 key 递归。三种情况：

- 同 key + **同内容** → 保留一份。这是常态：两份 `.rc` 编出来的同一份资源，两个对象各带一份
- 同 key + **不同内容** → 重复定义，**报错**。静默留一份就是「程序带着错的图标发布」
- 同 key，一边是目录一边是叶子 → **无解**，报错

冲突是**收集完一起报**而不是遇到第一个就停。一次 merge 要访问很多 key，第一个撞上的通常不是最有信息量的那个：

```
rsrc: resource defined twice with different contents: RT_ICON/1/1033 (48 vs 20 bytes)
```

## 验证

`windres` 真实产物 + `objdump` 交叉验证：

```bash
windres app.rc -O coff -o app_res.o
objdump -h app_res.o # .rsrc 0x100 bytes
```

输出的 exe 用**独立的 Python 脚本**解析（不能用 gocld 自己的代码读自己写的文件——自证没有意义）：

```
rsrc fileoff=0x600 rawsz=0x200
/3(RT_ICON) <dir>
  /3/1 <dir>
    /3/1/1033 size=0x30 payload=2800000001000000020000000100200000…
/14(RT_GROUP_ICON) <dir>
  /14/'IDI_ICON' <dir>
    /14/'IDI_ICON'/1033 size=0x14 payload=00000100010001010000010020003000…
```

数据目录：

```
Entry 2  RVA=0x3000  Size=0xfc      ← 都非零
```

`Entry 2` 的 **Size 是 0** 也踩过一次：数据目录是在 `emit` **之前**写的，那时 `sections[rsrcIndex].data` 还是 nil。RVA 对、size 为 0 的数据目录是 loader 会直接走过去的——资源不是坏，是**不存在**。所以那两行 `putU32at` 现在写在 `emit` 之后。

payload 与原 `.o` 逐字节比对一致。

## 单测怎么写才有用

资源树的测试不能用「同一个 helper 写进去再读出来」的方式——那样只证明了 helper 自洽，而这个格式的失败模式恰恰是「写和读以同样的方式错」。

所以测试里 `buildTree` 是**按规范手工排字节**的另一套实现，测试读的路径用第三套（`readTree`）走。三份实现互相印证，才能当证据用。

一个具体的例子：`buildTree` 里我一度在 `alignTo(8)` **之前**写下 `OffsetToData`：

```go
leafAt[n] = len(buf)
buf = append(buf, make([]byte, rscData)...)
putU32at(buf, leafAt[n]+0, uint32(leafAt[n]+rscData))   // ← 对齐还没发生
alignTo(8)                                               // ← 对齐在这
buf = append(buf, n.leaf...)
```

`0x6c % 8 = 4`，对齐把 payload 推到 `0x70`，而 `OffsetToData` 记的是 `0x6c`。测试抓到了。

## 结果

```
rsrc   ok  src/gocld (16 个测试)
全量   go test ./...  ok
真实 windres 对象端到端  PASS（payload 逐字节一致）
```

## 还没做的

- **不解析 `.rc` 源文件**。资源由 `windres`（或 `llvm-rc`、MSVC 的 `rc.exe`）生成 `.o`，gocld 只读 `.o`
- **不做 ELF 侧**。ELF 压根没有资源节（`.rsrc` 是 PE 独有的），所以 `Image.Rsrc` 在 ELF 路径上始终为 nil
- 不做资源 ID 的语义校验（比如 type 3 的 payload 到底是不是合法 ICONDIR）——payload 对链接器是不透明的，看它不是链接器该管的事