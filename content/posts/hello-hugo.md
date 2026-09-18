---
title: "用 Hugo + GitHub Pages 搭好这个博客"
date: 2026-09-19T07:00:00+08:00
draft: false
description: "第一篇示例文章：记录本博客是如何用 Hugo 和 GitHub Actions 自动发布到 GitHub Pages 的。"
tags: ["Hugo", "GitHub Pages", "教程"]
categories: ["技术"]
---

## 你好，Hugo

这是用 **Hugo（PaperMod 主题）** 搭建、并通过 **GitHub Actions** 自动发布到
**GitHub Pages** 的示例文章。

### 这套流程做了什么

1. 本地用 `hugo new site` 初始化站点，主题以 git 子模块方式引入 `themes/PaperMod`。
2. 推送 `main` 分支后，GitHub Actions 会自动安装 Hugo（extended 版）、拉取子模块、构建并部署。
3. 站点发布在你的用户页：`https://USERNAME.github.io/`。

### 常用命令

```bash
# 本地预览（带草稿）
hugo server -D

# 生成静态文件到 public/
hugo --gc --minify
```

> 提示：编辑 `hugo.toml` 顶部的 `baseURL`，把 `USERNAME` 换成你的 GitHub 用户名。

Happy blogging! ✦
