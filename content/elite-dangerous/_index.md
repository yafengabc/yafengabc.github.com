---
title: "Elite Dangerous 专栏教程"
weight: 10
menuTitle: "游戏教程 · Elite Dangerous"
categories: ["游戏教程", "Elite Dangerous"]
---

本专栏记录 Elite Dangerous 挂机监控、赏金统计与掉盾提醒的完整流程，从新手入门到自动化运维的全套教程。

---

{{ range sort (where .Site.RegularPages "Section" "posts/elite-dangerous") "Weight" "asc" }}
{{ if not .IsHome }}
- [{{ .Title }}]({{ .RelPermalink }})
{{ end }}
{{ end }}