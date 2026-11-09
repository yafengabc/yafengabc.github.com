---
title: "TinyOS CI：用 GitHub Actions 直接生成可刷板子的镜像"
menuTitle: "TinyOS CI"
date: 2026-11-09T15:00:00+08:00
draft: false
weight: 10
slug: "tinyos-ci"
tags: ["TinyOS", "GitHub Actions", "CI", "CI/CD", "ESP32-C3", "RP2040", "RP2350", "嵌入式"]
categories: ["嵌入式", "TinyOS", "CI/CD"]
description: "给 TinyOS 加一条 GitHub Actions 流水线：一次构建 ESP32-C3 的 esptool 镜像和 RP2040 / RP2350 的 UF2，main 推送出 artifact，v* 标签自动发 Release。"
---

给 [TinyOS](https://github.com/yafengabc/TinyOS) 加了一条 GitHub Actions 流水线：推送到 `main` 就构建所有真机目标的镜像，推 `v*` 标签则自动发 GitHub Release。

现在新的板子镜像可以直接从 GitHub 下载，不用在本地装一整套交叉工具链。

- 工作流：<https://github.com/yafengabc/TinyOS/blob/main/.github/workflows/images.yml>
- 使用说明：<https://github.com/yafengabc/TinyOS/blob/main/docs/CI.md>
- 第一次成功运行：<https://github.com/yafengabc/TinyOS/actions/runs/42591885072>

## 构建哪些镜像

| `-target` | 产物 | 刷写方式 |
| --- | --- | --- |
| `esp32c3` | `esp32c3.bin` + `ramdisk-esp32c3.bin` | `esptool` 写 `0x0` 和 `0x80000` |
| `rp2040` | `rp2040.uf2` | BOOTSEL 后拖到 `RPI-RP2` |
| `rp2350` | `rp2350.uf2` | BOOTSEL 后拖到 `RP2350` |
| `rp2350smp` | `rp2350smp.uf2` | 同上，双核 |
| `rp2350rv` | `rp2350rv.uf2` | 同上，RISC-V 核 |
| `rp2350rvsmp` | `rp2350rvsmp.uf2` | 同上，RISC-V 双核 |

每个 artifact 里还有一个 `FLASH-<target>.txt`，写清楚该目标怎么刷。

## 为什么值得做成 CI

TinyOS 的构建有一个特点：**同一套内核和用户程序，要按板子生成不同格式的镜像**。

- ESP32-C3 是 XIP flash：内核 boot image 在 `0x0`，littlefs 分区在 `0x80000`，要两个文件。
- RP2040 要加 256 字节的 stage-2 boot，再和 littlefs 一起封成 UF2。
- RP2350 的 ARM / RISC-V、单核 / 双核又是不同的链接脚本和启动路径。

这些组合在本地手工跑很容易漏：少刷一个 ramdisk、拿错核的 UF2、工具链版本不同，都会得到“刷进去没反应”的结果。CI 把它们变成一条固定的矩阵，每次提交都重新生成一遍，产物可下载、可复现。

## 工作流结构

触发条件：

```yaml
on:
  push:
    branches: [main]
    tags: ["v*"]
  workflow_dispatch:
```

- 推送 `main`：构建所有镜像，上传为 Actions artifacts。
- 推送 `v*` 标签：构建后额外把镜像附到 Release。
- 手动触发：调试 workflow 本身时用。

核心是一个矩阵：

```yaml
strategy:
  fail-fast: false
  matrix:
    target:
      - esp32c3
      - rp2040
      - rp2350
      - rp2350smp
      - rp2350rv
      - rp2350rvsmp
```

`fail-fast: false` 很重要：一个板子挂了，其它板子的镜像仍然会构建出来，方便判断问题范围。

## 关键步骤

### 1. 工具链

TinyOS 的构建在 Linux 上只需要 Go + clang + ld.lld：

```yaml
- name: Set up Go
  uses: actions/setup-go@v5
  with:
    go-version: stable
    cache: true

- name: Install LLVM
  run: |
    sudo apt-get update
    sudo apt-get install -y clang lld libc6-dev
```

- `clang` 负责 C 编译和汇编。
- `ld.lld` 由构建工具直接调用；RV32 用 `elf32lriscv`，ARM 用 `armelf`。
- `mklfs` 是构建期的 host 工具，用同一个 clang 编译，所以 `libc6-dev` 也要装。
- 不需要 checkout 子模块：默认构建不使用 `goc32` / `rv32emu-c`，这一条能省不少 checkout 时间。

### 2. 构建

每个矩阵 job 只做一件事：

```yaml
- name: Build ${{ matrix.target }}
  run: go run ./cmd/tos build -target ${{ matrix.target }}
```

`tos build` 会完成：编译内核、编译用户程序、生成 littlefs、链接、按板级格式封装。

### 3. 收集镜像

ESP32-C3 和 RP2 的产物形状不同，要分开处理：

```bash
target="${{ matrix.target }}"
out="dist/$target"
mkdir -p "$out"

if [ "$target" = "esp32c3" ]; then
  cp build/esp32c3.bin "$out/"
  cp build/ramdisk-esp32c3.bin "$out/"
  # 写 FLASH-esp32c3.txt，说明 0x0 / 0x80000
else
  cp "build/$target.uf2" "$out/"
  # 写 FLASH-<target>.txt，说明 BOOTSEL 拷贝
fi

for f in "$out"/*; do
  test -s "$f" || { echo "empty file: $f"; exit 1; }
done
```

这里有意加了一个“文件非空”检查：构建脚本返回 0 但没产出目标文件，是最难排查的一类 CI 失败。

### 4. 上传 artifact

```yaml
- uses: actions/upload-artifact@v4
  with:
  name: tinyos-${{ matrix.target }}
  path: dist/${{ matrix.target }}
  if-no-files-found: error
  retention-days: 30
```

Artifact 名称就是 `tinyos-esp32c3`、`tinyos-rp2040` 这样，下载后直接看到对应板子的文件。

### 5. 标签自动发 Release

Release job 只在标签上运行：

```yaml
release:
  if: startsWith(github.ref, 'refs/tags/')
  needs: build
  permissions:
    contents: write
```

先把所有 artifact 下载下来，再用 `gh` 发布：

```bash
tag="${GITHUB_REF_NAME}"
mapfile -t files < <(find dist -type f | sort)

if gh release view "$tag" >/dev/null 2>&1; then
  gh release upload "$tag" "${files[@]}" --clobber
else
  gh release create "$tag" "${files[@]}" \
    --title "TinyOS $tag" \
    --generate-notes
fi
```

这样 `git push origin v0.1.0` 之后，Release 页面就有六个板子的可刷镜像，长期保存；main 分支的 artifacts 默认保留 30 天。

## 完整 workflow

```yaml
name: Build flashable images

on:
  push:
    branches: [main]
    tags: ["v*"]
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: images-${{ github.ref }}
  cancel-in-progress: true

jobs:
  build:
    name: ${{ matrix.target }}
    runs-on: ubuntu-24.04
    permissions:
      contents: read
    strategy:
      fail-fast: false
      matrix:
        target:
          - esp32c3
          - rp2040
          - rp2350
          - rp2350smp
          - rp2350rv
          - rp2350rvsmp
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-go@v5
        with:
          go-version: stable
          cache: true
      - name: Install LLVM
        run: |
          sudo apt-get update
          sudo apt-get install -y clang lld libc6-dev
      - name: Build ${{ matrix.target }}
        run: go run ./cmd/tos build -target ${{ matrix.target }}
      - name: Collect images
        shell: bash
        run: |
          set -euo pipefail
          target="${{ matrix.target }}"
          out="dist/$target"
          mkdir -p "$out"
          if [ "$target" = "esp32c3" ]; then
            cp build/esp32c3.bin "$out/"
            cp build/ramdisk-esp32c3.bin "$out/"
          else
            cp "build/$target.uf2" "$out/"
          fi
          ls -l "$out"
      - uses: actions/upload-artifact@v4
        with:
          name: tinyos-${{ matrix.target }}
          path: dist/${{ matrix.target }}
          if-no-files-found: error

  release:
    if: startsWith(github.ref, 'refs/tags/')
    needs: build
    runs-on: ubuntu-24.04
    permissions:
      contents: write
    steps:
      - uses: actions/download-artifact@v4
        with:
          path: dist
          pattern: tinyos-*
      - shell: bash
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail
          tag="${GITHUB_REF_NAME}"
          mapfile -t files < <(find dist -type f | sort)
          if gh release view "$tag" >/dev/null 2>&1; then
            gh release upload "$tag" "${files[@]}" --clobber
          else
            gh release create "$tag" "${files[@]}" --title "TinyOS $tag" --generate-notes
          fi
```

实际仓库里的版本多了 `FLASH-<target>.txt` 生成、非空检查和注释，逻辑相同。

## 第一次跑 CI 就抓到一个真问题

第一次 `main` 构建时，`rp2040` 直接失败：

```text
user/smp.c:59:41: error: invalid instruction
  str r12, [r1, #8]
note: instruction requires: thumb2
note: operand must be a register in range [r0, r7]
```

`smp.c` 里为了检查异常返回帧，用了一段内联汇编：

```c
#if defined(__arm__) || defined(__thumb__)
    register int r0 __asm__("r0") = SYS_GETCPU;
    ...
    __asm__ volatile("svc #0\n\t"
                     "str r0, [r1, #0]\n\t"
                     "str r3, [r1, #4]\n\t"
                     "str r12, [r1, #8]\n\t"
                     ...
```

`str r12, [r1, #8]` 是 Thumb-2 才有的编码。Cortex-M3/M33 没问题，但 RP2040 是 Cortex-M0+（ARMv6-M，只有 Thumb-1），所以汇编直接报错。

修法是让 M0+ 走已有的 C fallback：

```c
#if (defined(__arm__) || defined(__thumb__)) && !defined(TOS_ARM_M0P)
```

构建系统在 rp2040 目标上已经定义了 `TOS_ARM_M0P`。改完之后：

- rp2040 能正常编译；
- `smp` 程序仍然保留，在 M0+ 上走 `getcpu()` 的普通路径；
- M3/M33 继续用原来的内联汇编检查异常帧。

这就是 CI 的价值：本地只构建过 arm/esp32c3 时，这个错误不会被发现。

## 下载和刷写

### main 构建

Actions → **Build flashable images** → 对应运行 → 页面底部 **Artifacts**：

```text
tinyos-esp32c3
tinyos-rp2040
tinyos-rp2350
tinyos-rp2350smp
tinyos-rp2350rv
tinyos-rp2350rvsmp
```

### 标签发布

```bash
git tag v0.1.0
git push origin v0.1.0
```

Release 页面会直接附带所有镜像。

### ESP32-C3

```bash
esptool.py --chip esp32c3 write-flash 0x0     esp32c3.bin
esptool.py --chip esp32c3 write-flash 0x80000 ramdisk-esp32c3.bin
```

新语法可以一次写完：

```bash
esptool --chip esp32c3 write-flash \
  0x0 esp32c3.bin \
  0x80000 ramdisk-esp32c3.bin
```

刷之前按住 BOOT、点一下 RESET 进入下载模式。

### RP2040 / RP2350

按住 BOOTSEL 插入 USB，电脑上出现 `RPI-RP2` / `RP2350` 盘，把对应 `.uf2` 拖进去。复制完成板子自动重启。

## 几个容易踩的坑

**1. 不需要子模块**

TinyOS 仓库里有 `goc32` 和 `rv32emu-c` 两个子模块，但默认 `tos build` 不使用它们。CI checkout 不加 `submodules: recursive`，checkout 更快，也不会因为某个子模块 commit 没推上去而失败。

**2. RP2040 的 300 KiB 文件系统**

RP2040 的 littlefs 分区固定为 300 KiB。仓库当前跟踪的用户程序能放下；如果之后把很大的程序（比如正在开发中的 `user/wasm`）提交进去，可能会看到：

```text
lfs.c:691:error: No more free space
```

这不是 CI 的错，是镜像容量到了。需要精简该目标的用户程序，或者调整分区/板级配置。

**3. CI 里的用户程序是 clang 构建的**

TinyOS 默认构建链路用 clang；`goc32` 是一个独立的、无 libLLVM 的用户态 C 编译器实验，没有接进默认镜像构建。两者不要混为一谈。

**4. Node 20 deprecation 警告**

`actions/checkout@v4`、`actions/setup-go@v5`、`actions/upload-artifact@v4` 目前会提示 Node 20 弃用，但运行仍然成功。等这些 action 的新主版本稳定后可以一起升级。

**5. Go 缓存提示**

仓库没有 `go.sum`（没有外部依赖），`setup-go` 的缓存步骤会提示找不到依赖文件，但不影响构建。也可以在 `with` 里加 `cache-dependency-path: go.mod` 消除提示。

## 验证

第一次完整运行结果：

- 六个构建 job 全部 `success`
- Release job 因为推的是 `main` 而不是标签，按预期 skipped
- Artifacts 全部生成

Run：<https://github.com/yafengabc/TinyOS/actions/runs/42591885072>

## 还能加什么

- PR 上只构建一个 `esp32c3` 做冒烟，main/标签再全量。
- 给每个镜像生成 `SHA256SUMS`。
- 把 artifact 直接做成 “latest” rolling release。
- 用 `actionlint` 在 CI 里检查 workflow 本身。
- 把 goc32 也纳入镜像构建（需要先把 goc32 接成 `tos build` 的用户程序编译器）。

对现在的 TinyOS 来说，这组镜像已经覆盖了 ESP32-C3、RP2040 和 RP2350 的 ARM/RISC-V、单核/双核组合；后续加板子时，只需要在矩阵里加一个 target，剩下的收集和发布逻辑可以复用。
