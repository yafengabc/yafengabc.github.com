---
title: "Archlinux在Btrfs分区上的安装（bios篇）"
date: 2016-01-31T09:38:00+08:00
categories: ['归档']
tags: [ArchLinux, Btrfs, 安装教程, BIOS]
original: "https://www.cnblogs.com/yafengabc/p/5172862.html"
draft: false
---

其实本文所有的内容在Archwiki上都可以找到，并且更新更全面（只是比较零散），我所做的只是对安装流程做一个小小的总结，每一步我都会稍微解释一下，但不会说的特别详细，毕竟这只是一篇安装引导文，而不是Wiki。

首先显然是下载最新的archlinux安装镜像：

1，用浏览器打开archlinux源，比如mirrors.163.com,mirrors.ustc.edu.cn(这里以163为例)：（url）[http://mirrors.163.com/archlinux/](http://mirrors.163.com/archlinux/ "http://mirrors.163.com/archlinux/")

![image](images/760932-20160131093643833-1400831203.png)

可以看到，有个iso目录，这就是安装镜像所在的地址了。打开后里边是这个样子的：

![image](images/760932-20160131093645896-1700844850.png)

其中latest目录下，是包含官方最新的archlive镜像，而archboot目录下，则是另一个版本的archboot镜像（以前的archlinux官方镜像，包含一个类似FreeBSD的图形化安装脚本哦，感兴趣的童鞋可以试试，感觉还是比较好用哦）。不多说了，还是进latest下载安装镜像(直接扔地址：)[http://mirrors.163.com/archlinux/iso/latest/archlinux-2016.01.01-dual.iso](http://mirrors.163.com/archlinux/iso/latest/archlinux-2016.01.01-dual.iso "http://mirrors.163.com/archlinux/iso/latest/archlinux-2016.01.01-dual.iso")（建还是自己进去下哦，说不定你看到本文时，2016年2月甚至2017年的镜像已经出了，建议下载最新的）

600多兆，时间比较漫长，我就先八一下怎么做安装USB，要是你用linux系统，直接dd进U盘就行了（命令我不多说了我觉得linux用户应该都会，不会的google baidu一下也会了，另一个方法就是男人（man）一下dd（搞基？），咱还是策反windows下的众linux小白为主![大声笑](images/760932-20160131093646224-813361256.png)）。

考虑到网上众基们用ultra iso做启动盘的比较多，我就顺应民意用一下这个软件：

用ultra iso打开刚才下载好的镜像文件，选择启动->写入硬盘镜像

![image](images/760932-20160131093647380-1793847884.png)

并在接下来的窗口选择RAW写入：

![image](images/760932-20160131093649646-1015608921.png)

等一会儿，就写完了（要是启动失败，请移步互联网，找更靠谱的方法![转动眼睛](images/760932-20160131093650833-1641661817.png)，（因为这不是重点））

假设在座各位已经搞定了启动方法，下边就是安装了（我用的vbox虚拟机）

![image](images/760932-20160131093652271-126880494.png)

嗯，现在的电脑都支持x86\_64(amd64),只要电脑不太差，选这个就OK了，内存小，可以选i686可以省内存哦（上下箭头选择，回车继续，不用我教吧）。

![image](images/760932-20160131093657474-2080480386.png)

嗯嗯，看到一个命令行界面输一个lsblk看看有没认到硬盘：

![image](images/760932-20160131093658958-94858240.png)

那个sda就是硬盘了。分区cfdisk /dev/sda,在接下来的界面选dos（也可能没这个界面）

![image](images/760932-20160131093659771-1381291656.png)

然后new一个分区（这里我是把所有空间都给我们的btrfs分区了，各位看官按需分区，按需分区嗯）并加上boot标志（以防有些sb主板只认有boot标志的硬盘）：

![image](images/760932-20160131093700802-967820211.png)

然后选“Write”写入，选“Quit”退出。注意上边那个Start，一定要是2048或以上（比如4096 8192……），否则btrfs无法作为启动分区，什么你的是64？那……换个别的工具分区吧……

再lsblk，我们看到了sda1，分区搞定。

![image](images/760932-20160131093701614-1704148243.png)

下一步就是格式化了，好激动，千万不要格式化错了分区哦（看官：你妹的在虚拟机下激动个P，就一个分区……）

```
mkfs.btrfs /dev/sdaX
```

![image](images/760932-20160131093702349-276161332.png)

看到这个提示，说明格式化成功了。接下来，建立子卷：

![image](images/760932-20160131093706286-1265734282.png)

我建立了rootfs子卷作为archlinux的/，建立了homefs作为/home,接下来就是挂载：

![image](images/760932-20160131093710224-1352424419.png)

解释一下：先cd ..跳出/mnt目录然后umount（卸载）掉sda1(不cd出去会umount失败)，然后把rootfs子卷挂载到/mnt,然后建立/mnt/home目录，挂载homefs到/mnt/home，最后用mount命令查看一下挂载是不是成功了。

至于挂载参数，我作为例子，只用了一个compress=lzo，也就是用lzo模式压缩卷，lzo是一种先进的压缩实时算法，能减少磁盘占用，提升硬盘性能哦，当然如果系统装在SD卡等特别慢的设备上，我推荐zlib算法，牺牲部分CPU性能换硬盘速度，因为zlib压缩率高，比如把原来100M的文件压缩成了50M，读写显然就只用原来一半的时间，很好理解。

其他挂载参数，抄一下wiki上的：

![image](images/760932-20160131093714114-485250920.png)

依照你是SSD还是HDD，各取所需了吧。另外，稍微提一下，btrfs现在不支持各个子卷用不同的参数挂载，所以只有第一个子卷挂载时需要上边的罗哩罗嗦的一堆参数，比如上边我挂载home时，就只加了个subvol参数，指定子卷，其他会默认跟rootfs的一样。

挂载好了，下一步就是安装了，先编辑一下

![image](images/760932-20160131093715177-1471226432.png)

把你最快的源放在最前边，比如我是用的163为例子：

![image](images/760932-20160131093715880-1382289567.png)

然后就是安装基本系统：

![image](images/760932-20160131093716896-1710129004.png)

我安装了base btrfs-progs grub三项，要是需要wifi-menu连无线，还需要架上wpa\_actiond dialog，嗯，安装个很快，几分钟就装完了。

好了，下一步生成fstab与grub启动项：

![image](images/760932-20160131093717661-477286832.png)

嗯，命令我都用红框框出来啦，一定按顺序，别敲错了。

第一步用genfstab –U /mnt来查看生成的fstab项，如果没问题，第二步就是用>>符号把这些写到/mnt下的/etc/fstab（为什么是mnt下的呢？）。

然后用chroot后用grub-mkconfig生成grub菜单。并导入grub.cfg

最后一步，安装grub到mbr：

![image](images/760932-20160131093718521-1204220702.png)

嗯，注意最后一行，有No error reported，就说明安装成功了。

接下来重启就OK了

![image](images/760932-20160131093719333-522286299.png)

先exit退出chroot环境，然后sync一下（不做其实也无所谓），然后reboot

![image](images/760932-20160131093720271-741992375.png)

启动成功，用root登录，密码为空

![image](images/760932-20160131093721130-1164858305.png)

嗯，现在系统768M，好大![热烈的笑脸](images/760932-20160131093721443-580446820.png)
