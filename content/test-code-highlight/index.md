---
title: "代码高亮测试（C / Python / Golang / 等）"
date: 2026-09-19T13:57:00+08:00
draft: false
tags: ["测试", "代码高亮", "C", "Python", "Golang"]
categories: ["测试"]
description: "一篇用于验证 Hugo Relearn 主题代码语法高亮的测试文章，覆盖 C、Python、Golang、JavaScript、Rust、Java、Bash 等多种语言。"
---

这是一篇**测试文章**，用来验证 Hugo + Relearn 主题的代码语法高亮是否正常工作。下面每种语言都放了一段简短但包含关键字、字符串、注释、函数等元素的代码。

如果你能看到不同颜色的关键字、字符串和注释，说明高亮生效了。✅

---

## C 语言

```c
#include <stdio.h>
#include <stdlib.h>

/* 计算斐波那契数列的第 n 项 */
long long fib(int n) {
    if (n < 2) return n;
    long long a = 0, b = 1;
    for (int i = 2; i <= n; i++) {
        long long tmp = a + b;
        a = b;
        b = tmp;
    }
    return b;
}

int main(void) {
    int n = 20;
    printf("fib(%d) = %lld\n", n, fib(n));
    return EXIT_SUCCESS;
}
```

---

## Python

```python
from typing import List
import asyncio

def quicksort(items: List[int]) -> List[int]:
    """经典快排实现"""
    if len(items) <= 1:
        return items
    pivot = items[len(items) // 2]
    left = [x for x in items if x < pivot]
    mid = [x for x in items if x == pivot]
    right = [x for x in items if x > pivot]
    return quicksort(left) + mid + quicksort(right)

async def fetch(name: str, delay: float) -> str:
    await asyncio.sleep(delay)
    return f"task {name} done"

if __name__ == "__main__":
    print(quicksort([5, 2, 9, 1, 7]))
```

---

## Golang

```go
package main

import (
    "fmt"
    "sync"
)

// Worker 从 channel 读取任务并并发处理
func worker(id int, jobs <-chan int, results chan<- int, wg *sync.WaitGroup) {
    defer wg.Done()
    for j := range jobs {
        results <- j * j
    }
}

func main() {
    jobs := make(chan int, 100)
    results := make(chan int, 100)
    var wg sync.WaitGroup

    for w := 1; w <= 3; w++ {
        wg.Add(1)
        go worker(w, jobs, results, &wg)
    }

    for i := 1; i <= 9; i++ {
        jobs <- i
    }
    close(jobs)
    wg.Wait()
    close(results)

    for r := range results {
        fmt.Println(r)
    }
}
```

---

## JavaScript

```javascript
const cache = new Map();

function memoize(fn) {
  return (...args) => {
    const key = JSON.stringify(args);
    if (cache.has(key)) {
      return cache.get(key);
    }
    const value = fn(...args);
    cache.set(key, value);
    return value;
  };
}

const fib = memoize((n) => (n < 2 ? n : fib(n - 1) + fib(n - 2)));
console.log(`fib(30) = ${fib(30)}`);
```

---

## Rust

```rust
use std::thread;

fn main() {
    let mut handles = vec![];
    for i in 0..5 {
        handles.push(thread::spawn(move || {
            println!("thread {i} is running");
            i * i
        }));
    }
    for h in handles {
        println!("result = {}", h.join().unwrap());
    }
}
```

---

## Java

```java
import java.util.stream.IntStream;

public class Main {
    public static void main(String[] args) {
        int sum = IntStream.rangeClosed(1, 100)
                          .filter(n -> n % 2 == 0)
                          .sum();
        System.out.println("1..100 偶数之和 = " + sum);
    }
}
```

---

## Bash

```bash
#!/usr/bin/env bash
set -euo pipefail

for f in *.md; do
  words=$(wc -w < "$f")
  echo "$f: $words words"
done
```

---

## 行内代码

行内代码像 `git commit -m "test"` 或 `fmt.Println("hi")` 也会保持等宽字体。

---

> 测试完毕。确认高亮正常后可以删除这篇文章。
