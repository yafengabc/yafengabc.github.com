---
title: "让普通用户可以控制树莓派的GPIO(Archlinuxarm)"
date: 2018-04-18T11:52:00+08:00
categories: ["嵌入式"]
tags: [树莓派, GPIO, ArchLinuxARM, udev]
original: "https://www.cnblogs.com/yafengabc/p/8873397.html"
aliases: ["/cnblogs/p8873397/"]
weight: 90
draft: false
---

Raspbian上的Rpi.GPIO库是可以在普通用户下控制树莓派的GPIO的，然而在ArchlinuxARM下，却需要root用户才行，这无疑会引起安全问题，好在RPi.GPIO提供了一个获取权限的脚本：create\_gpio\_user\_permissions.py

然而这个脚本是没法在ArchlinuxARM上直接运行的，因为Archlinux缺少adduser命令。

这个脚本是这样的：

```python
import grp
import subprocess

def ensure_gpiogroup():
    try:
        grp.getgrnam('gpio')
    except KeyError:
        print('GPIO group does not exist - creating...')
        subprocess.call(['groupadd', '-f', '-r', 'gpio'])
        subprocess.call(['adduser', 'pi', 'gpio'])
        # in future, also for groups:
        #   spi
        #   i2c
        add_udev_rules()

def add_udev_rules():
    with open('/etc/udev/rules.d/99-gpio.rules','w') as f:
        f.write("""SUBSYSTEM=="bcm2835-gpiomem", KERNEL=="gpiomem", GROUP="gpio", MODE="0660"
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", ACTION=="add", PROGRAM="/bin/sh -c 'chown root:gpio /sys/class/gpio/export /sys/class/gpio/unexport ; chmod 220 /sys/class/gpio/export /sys/class/gpio/unexport'"
SUBSYSTEM=="gpio", KERNEL=="gpio*", ACTION=="add", PROGRAM="/bin/sh -c 'chown root:gpio /sys%p/active_low /sys%p/direction /sys%p/edge /sys%p/value ; chmod 660 /sys%p/active_low /sys%p/direction /sys%p/edge /sys%p/value'"
""")

if __name__ == '__main__':
    ensure_gpiogroup()
```

反正很简单，手动搞进去就行了

首先，建一个gpio组：

```bash
groupadd -f -r gpio
```

然后把当前用户（我的用户是yafeng）加入到gpio组

```bash
 gpasswd -a yafeng gpio
```

然后建立udev的规则文件：/etc/udev/rules.d/99-gpio.rules

```
SUBSYSTEM=="bcm2835-gpiomem", KERNEL=="gpiomem", GROUP="gpio", MODE="0660"
SUBSYSTEM=="gpio", KERNEL=="gpiochip*", ACTION=="add", PROGRAM="/bin/sh -c 'chown root:gpio /sys/class/gpio/export /sys/class/gpio/unexport ; chmod 220 /sys/class/gpio/export /sys/class/gpio/unexport'"
SUBSYSTEM=="gpio", KERNEL=="gpio*", ACTION=="add", PROGRAM="/bin/sh -c 'chown root:gpio /sys%p/active_low /sys%p/direction /sys%p/edge /sys%p/value ; chmod 660 /sys%p/active_low /sys%p/direction /sys%p/edge /sys%p/value'"
```

然后重启，就可以直接在普通用户下操作GPIO了，经测试，不光Rpi.GPIO库,wiringpi等其他库也可以了
