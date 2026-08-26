# repo-view

本地只读源码浏览器：一个零依赖的 Python 单文件，起一个只绑定 127.0.0.1 的静态服务，在浏览器里可视化浏览任意源码目录——左树右文、语法高亮、图片/视频预览、文件名筛选，并自带 **Session Diff**（只看当前分支/工作区改了什么）。

适合给 coding agent（或人）快速「看一眼这个仓库现在长什么样、这条分支改了哪些文件」。

## 特性

- **零依赖**：Python 3 标准库（`http.server`），无需安装任何包
- **只读**：不写磁盘、不执行仓库代码；仅监听 `127.0.0.1`，不对外
- **全库浏览**：文件树 + 语法高亮（highlight.js CDN，离线自动降级纯文本）、图片/视频直出、二进制不预览
- **Session Diff**：顶栏切换「工作区（未提交）/ 整支相对 base / 单个 commit」，左侧树只列该条目改动的文件，点开默认看 diff
- **工程友好**：自动忽略 `node_modules` / `.git` / `dist` / `build` 等重目录；文本 >1MB 截断；文件数 >20000 拒绝；中文文件名正常显示

## 快速开始

```bash
python3 serve.py <目录> [端口=8770]
# 浏览器打开 http://127.0.0.1:8770
# 直接进 Session Diff：
open 'http://127.0.0.1:8770/?mode=session'
```

## 作为 Agent Skill 安装

把整个目录放进你 agent 的 skills 目录即可，例如：

```bash
git clone https://github.com/gemma1044/repo-view.git ~/.zcode/skills/repo-view
# 或 ~/.agents/skills/ 、~/.claude/skills/ 等，取决于你的 agent 读哪里
```

之后 agent 就能按 `SKILL.md` 的说明自助启动它。

## API 一览

| 路径 | 作用 |
|------|------|
| `/api/tree` | 全库文件树 |
| `/api/status` | `git status` 汇总（path → 新增/修改/删除） |
| `/api/file?path=` | 单文件内容/元信息 |
| `/api/raw?path=` | 原始字节（图片/视频用） |
| `/api/diff?path=` | 工作区单文件 diff vs HEAD |
| `/api/session` | 当前分支 Session 元信息（base 自动探测 main/master/develop/dev） |
| `/api/session/files?entry=` | 某条目（工作区/整支/commit）的改动文件树 |
| `/api/session/diff?entry=&path=` | 某条目下单文件 diff |

## 安全与边界

- 只读浏览，不能编辑保存；没有任何写操作
- 只绑定本机回环地址，不要把它暴露到公网
- 语法高亮依赖 cdnjs 的 highlight.js，离线时降级为纯文本
- 一个实例服务一个目录；换目录就换个端口再起一个

## License

MIT
