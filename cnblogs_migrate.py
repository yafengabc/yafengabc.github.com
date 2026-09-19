# -*- coding: utf-8 -*-
"""把 cnblogs 博客（yafengabc）的 30 篇文章搬移到 Hugo 的 content/cnblogs/ 作为归档。
- 从 sitemap 取链接（已存 _cnblogs_links.txt）
- 抓取每篇：标题/日期/分类/标签/正文
- 正文 HTML -> Markdown（markdownify + 自定义代码块/图片处理）
- 输出 content/cnblogs/<id>.md
"""
import re, os, sys, time, ssl, html
import urllib.request
from bs4 import BeautifulSoup
from markdownify import markdownify as md

BASE = "https://www.cnblogs.com/yafengabc"
OUTDIR = "content/cnblogs"
LINKFILE = "_cnblogs_links.txt"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

def fetch(url, tries=4):
    last = ""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40, context=ctx) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception as e:
            last = str(e); time.sleep(2)
    print("  [FAIL] fetch", url, last)
    return ""

def extract_codeblocks(soup):
    """把正文里所有 <pre> 代码块抽成纯文本，用占位符替换（避免 markdownify 破坏换行）。"""
    blocks = []
    for idx, pre in enumerate(soup.find_all("pre")):
        cls = " ".join(pre.get("class", []))
        lang = ""
        m = re.search(r"language-([a-zA-Z0-9+#]+)", cls) or re.search(r"brush:([a-zA-Z0-9+#]+)", cls)
        if m:
            lang = m.group(1)
        code = pre.get_text()          # 保留 <pre> 原始换行，BeautifulSoup 自动解实体
        code = code.rstrip("\n")
        ph = f"ZZCODEBLOCK{idx}ZZ"
        blocks.append((ph, f"\n```{lang}\n{code}\n```\n"))
        pre.replace_with(soup.new_string(ph))
    return blocks

def normalize_img(soup):
    for im in soup.find_all("img"):
        src = im.get("data-src") or im.get("src") or im.get("data-original")
        if src and src.startswith("//"):
            src = "https:" + src
        if src:
            im["src"] = src
            im.attrs = {"src": src, "alt": im.get("alt", "") or ""}

def get_text_clean(s):
    return re.sub(r"\s+", " ", s).strip()

def convert():
    os.makedirs(OUTDIR, exist_ok=True)
    links = [l.strip() for l in open(LINKFILE, encoding="utf-8") if l.strip()]
    print(f"共 {len(links)} 篇待处理")
    ok = 0
    for url in links:
        pid = re.search(r"/p/(\d+)\.html", url).group(1)
        h = fetch(url)
        if not h:
            continue
        soup = BeautifulSoup(h, "html.parser")
        # 标题
        t = soup.find("title")
        title = t.get_text(" ", strip=True) if t else pid
        title = re.sub(r"\s*-\s*yafeng\s*-\s*博客园\s*$", "", title).strip()
        # 日期
        dm = re.search(r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})", h)
        if dm:
            date = f"{dm.group(1)}T{dm.group(2)}:00+08:00"
        else:
            date = "2019-01-01T00:00:00+08:00"
        # 分类
        cats = []
        catdiv = soup.find(id="BlogPostCategory")
        if catdiv:
            cats = [a.get_text(strip=True) for a in catdiv.find_all("a")]
        if not cats:
            cats = ["归档"]
        # 标签
        tags = []
        tagdiv = soup.find(id="EntryTag")
        if tagdiv:
            tags = [a.get_text(strip=True) for a in tagdiv.find_all("a")]
        # 正文
        body = soup.find(id="cnblogs_post_body")
        if not body:
            body = soup.find(id="post_detail")
        if not body:
            print("  [WARN] 无正文", url); continue
        blocks = extract_codeblocks(body)
        normalize_img(body)
        md_text = md(str(body), heading_style="ATX", bullets="-", code_language_callback=lambda c: c or "")
        # 把占位符还原成代码块
        for ph, block in blocks:
            md_text = md_text.replace(ph, block)
        # 清理多余空行
        md_text = re.sub(r"\n{3,}", "\n\n", md_text).strip()
        # 去尾部可能的 "posted @ ..." 版权（通常在 postDesc，不在 body）
        fm = {
            "title": title,
            "date": date,
            "categories": cats,
            "tags": tags,
            "original": url,
            "draft": False,
        }
        fm_str = "---\n"
        fm_str += f'title: "{title.replace(chr(34), chr(39))}"\n'
        fm_str += f"date: {date}\n"
        fm_str += f"categories: {cats}\n"
        fm_str += f"tags: {tags}\n"
        fm_str += f'original: "{url}"\n'
        fm_str += "draft: false\n---\n\n"
        out = os.path.join(OUTDIR, f"p{pid}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(fm_str + md_text + "\n")
        ok += 1
        print(f"  [{ok}] p{pid} | {date[:10]} | {title[:30]}")
        time.sleep(0.4)
    print(f"完成，成功 {ok}/{len(links)} 篇 -> {OUTDIR}/")

if __name__ == "__main__":
    convert()
