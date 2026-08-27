---
name: repo-view
description: >-
  在浏览器里可视化浏览一个源码目录：左树右文、语法高亮、图片/视频预览、文件名筛选。
  支持 Session Diff：顶栏按「工作区 / 整支相对 main / 各 commit」切换，只看该条目改动与 diff。
  支持快捷专区：--focus 常看目录钉成侧栏 chip，一键切换子树，默认仍是完整 repo 树。
  用户说「看一下这个仓库/目录长什么样」「打开 repo-view」「可视化目录」「Session Diff」时加载。
---

# repo-view：本地只读源码浏览器

零依赖（Python 3 标准库），只读，仅绑 127.0.0.1。

## 启动

```bash
# 克隆即用：
python3 serve.py <目录> [端口=8770] [--focus <相对路径>]...
# 作为 skill 安装在 ~/.zcode/skills/（或 ~/.agents/skills/ 等）后：
/usr/bin/python3 ~/.zcode/skills/repo-view/serve.py <目录> [端口=8770] [--focus <相对路径>]...
# 后台起服务后 open http://127.0.0.1:<端口>
# 直接进 Session Diff：
open 'http://127.0.0.1:8770/?mode=session'
# 例：服务整个 repo，同时把常看目录钉成快捷专区：
/usr/bin/python3 ~/.zcode/skills/repo-view/serve.py ~/Projects/myapp 8770 --focus server/prompts
# 例：浏览全是软链的 skill 库（~/.zcode/skills 里链到 .cursor/.agents 的 skill）：
/usr/bin/python3 ~/.zcode/skills/repo-view/serve.py ~/.zcode/skills 8774 --follow-symlinks
```

- 每次点击文件都是**现读磁盘**，看到的永远是当前内容（无缓存）。
- 文本 >1MB 截断显示并提示；二进制（非图片/视频）不预览。
- 忽略 node_modules/.git/dist/build 等重目录；文件数 >20000 拒绝。
- 快捷键：`/` 聚焦搜索，`Esc` 清空筛选。

## Session Diff 查看器

顶栏：

1. **Session 标题**：当前 git 分支 · vs base（main/master…）
2. **全库 | Session Diff** 模式切换
3. **可点条目（chips）**：
   - **工作区（未提交）**：`git status` 改动，diff vs HEAD
   - **整支相对 base**：`merge-base(base)...HEAD` 全部提交改动
   - **每个 commit**：本分支相对 base 的单次提交（新→旧）

Session 模式下左侧树**只含该条目改动文件**；点文件默认打开 **diff**（可再切工作区全文）。

API：`/api/session` · `/api/session/files?entry=` · `/api/session/diff?entry=&path=`

## 快捷专区（quick zones）

侧栏顶部一排 chip：仓库名 chip（= 全库树）+ 钉住的目录 chip。**默认视图始终是完整 repo 树**，点目录 chip 只是把树根切到该子树，点仓库名 chip 切回。

三种钉法：

1. **启动种子 `--focus <相对路径>`**（可重复）：agent 起服务时带上，每次启动自动回来；页面里移除只对当次生效。
2. **URL 种子 `?focus=a,b`**：不改启动命令也能分享「带钉」的链接。
3. **手动钉**：浏览时 hover 任意文件夹，行尾 `＋` 钉进 chip；chip 上的 `✕` 移除。按「仓库绝对路径」存 localStorage，各仓库互不干扰。

深链：`?zone=<相对路径>` 打开直达某专区（不写入 pin）。

快捷专区只在全库模式生效；Session Diff 模式下 chip 行自动隐藏。

## 跟随软链（--follow-symlinks）

默认建树跳过软链。加 `--follow-symlinks` 后目录软链进树、内容可读（realpath 去重防环，断链略过）。适合根目录本身就是软链集合的场景，如 `~/.zcode/skills`。此模式下路径检查改为逻辑段校验：禁 `..`、禁绝对路径，只能顺着树里存在的名字走。

## 已知边界

- 只读浏览，不能编辑保存。
- 语法高亮走 cdnjs highlight.js，离线降级纯文本。
- 「当前 Session」按 **git 分支 + 工作区** 理解（ZCode session id 未注入时不显示 sess_*）。
- 换目录 = 换端口再起实例。
