---
title: "树莓派高级GPIO库，wiringpi2 for python使用笔记（二）高精度计时、延时函数"
date: 2016-01-03T15:11:00+08:00
categories: ['归档']
tags: [树莓派, wiringPi, 定时器, Python]
original: "https://www.cnblogs.com/yafengabc/p/5096445.html"
draft: false
---

学过单片机的同学应该清楚，我们在编写传感器驱动时，需要用到高精度的定时器、延时等功能，wiringpi提供了一组函数来实现这些功能，这些函数分别是：

micros() #返回当前的微秒数，这个数在调用wiringPiSetup()后被清零并重新计时

millis() #返回当前的毫秒数，同上，这个数在调用wiringPiSetup()后被清零并重新计时

delayMicroseconds() #高精度微秒延时

delay() #毫秒延时。

python相对于C，一个很大的问题就是执行速度慢，所以指令执行速度不可忽视，我们可以用micos函数来检测指令执行时间，用来避免实际使用中遇到的坑，请看以下代码：

```python
import wiringpi2 as gpio

for i in range(5):
    t1=gpio.micros()
    t2=gpio.micros()
    print(t2-t1)
```

连续调用两次micros，然后打印出差值，运行结果如下：

```bash
[root@RasPi ~/testcode]# python testus.py
12
4
4
5
5
```

我们看到第一次的结果明显比以后的结果要大，多了接近10微秒，一般的程序来说，这无关紧要，要是要求更高，可以把代码改成这个样子:

```python
import wiringpi2 as gpio

for i in range(5):
    t1=gpio.micros()
    t1=gpio.micros()
    t2=gpio.micros()
    print(t2-t1)
```

运行结果如下：

```bash
[root@RasPi ~/testcode]# python testus.py
3
3
3
3
2
```

基本一致了再看以下代码：

```python
import wiringpi2 as gpio

for i in range(5):
    t1=gpio.micros()
    t1=gpio.micros()
    gpio.delayMicroseconds(10)
    t2=gpio.micros()
    print(t2-t1)
```

延时10us，结果如下：

```bash
[root@RasPi ~/testcode]# python testus.py
21
21
18
18
18
```

减去两次调用micros()之间的5us左右的延时，实际延时10us会有5us左右的延时。

```python
import wiringpi2 as gpio

for i in range(5):
    t1=gpio.micros()
    t1=gpio.micros()
    for i in range(100):
        pass
    t2=gpio.micros()
    print(t2-t1)
```

结果：

```bash
[root@RasPi ~/testcode]# python testus.py
59
69
66
61
62
```

也就是，普通几条指令，每条延时在1us以下，可以基本忽略，调用函数，则有5-10us左右的延时，在编写程序时，应充分考虑这一点。若在时序里有复杂的代码段，则最好能实际测试一下，看看执行时间对我们的时序有什么影响。
