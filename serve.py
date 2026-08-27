#!/usr/bin/env python3
"""repo-view: 本地只读源码浏览器（零依赖，Python 3 标准库）。

用法: python3 serve.py [目录=当前目录] [端口=8770]
浏览器打开 http://127.0.0.1:<端口>
  - 全库浏览：左树右文、语法高亮、图片/视频
  - Session Diff：顶栏切换「工作区 / 各 commit / 整支相对 base」，只看该条目改动与 diff
"""
import os
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", ".cache", "coverage",
    "__pycache__", ".turbo", ".vercel", ".DS_Store", "vendor", "target",
    ".gradle", ".idea", ".vscode", ".pytest_cache", ".mypy_cache",
}
TEXT_EXTS = {
    ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".py", ".pyi",
    ".md", ".mdx", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".css", ".scss", ".less", ".html", ".htm", ".xml",
    ".sh", ".bash", ".zsh", ".rs", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".kt", ".sql", ".txt", ".graphql",
    ".vue", ".svelte", ".env", ".lock", ".mod", ".sum", ".gitignore",
    ".dockerignore", ".editorconfig", ".prettierrc", ".eslintrc",
}
TEXT_BASENAMES = {"Dockerfile", "Makefile", "LICENSE", "README", "CHANGELOG", ".env", ".env.local"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".svg"}
VIDEO_EXTS = {".mp4", ".webm", ".mov"}
MAX_TEXT_BYTES = 1024 * 1024
MAX_FILES = 20000
MAX_DIFF_BYTES = 2 * 1024 * 1024
BASE_CANDIDATES = ("main", "master", "develop", "dev")

ROOT = os.path.realpath(os.getcwd())
LANG_MAP = {
    ".ts": "typescript", ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".mjs": "javascript", ".cjs": "javascript", ".go": "go", ".py": "python",
    ".md": "markdown", ".mdx": "markdown", ".json": "json", ".yaml": "yaml",
    ".yml": "yaml", ".toml": "ini", ".ini": "ini", ".cfg": "ini", ".css": "css",
    ".scss": "scss", ".less": "less", ".html": "xml", ".htm": "xml", ".xml": "xml",
    ".sh": "bash", ".bash": "bash", ".zsh": "bash", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".hpp": "cpp", ".cs": "csharp",
    ".rb": "ruby", ".php": "php", ".swift": "swift", ".kt": "kotlin",
    ".sql": "sql", ".graphql": "graphql", ".vue": "xml", ".svelte": "xml",
}


def build_tree():
    count = [0]

    def walk(dirpath, rel):
        nodes = []
        try:
            entries = sorted(os.scandir(dirpath), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return nodes
        for e in entries:
            if e.name in SKIP_DIRS or e.name.startswith(".DS"):
                continue
            r = f"{rel}/{e.name}" if rel else e.name
            if e.is_symlink():
                continue
            if e.is_dir(follow_symlinks=False):
                nodes.append({"name": e.name, "path": r, "type": "dir", "children": walk(e.path, r)})
            elif e.is_file(follow_symlinks=False):
                count[0] += 1
                if count[0] > MAX_FILES:
                    raise RuntimeError(f"文件数超过 {MAX_FILES}，目录太大")
                nodes.append({"name": e.name, "path": r, "type": "file"})
        return nodes

    return {"name": os.path.basename(ROOT) or ROOT, "path": "", "type": "dir",
            "children": walk(ROOT, "")}


def safe_path(rel):
    p = os.path.realpath(os.path.join(ROOT, rel))
    if p != ROOT and not p.startswith(ROOT + os.sep):
        return None
    return p


def file_info(rel):
    p = safe_path(rel)
    if not p or not os.path.isfile(p):
        return None
    ext = os.path.splitext(rel)[1].lower()
    base = os.path.basename(rel)
    size = os.path.getsize(p)
    if ext in IMAGE_EXTS:
        kind, mime = "image", ("image/svg+xml" if ext == ".svg" else f"image/{ext[1:].replace('jpg','jpeg')}")
    elif ext in VIDEO_EXTS:
        kind, mime = "video", f"video/{'quicktime' if ext == '.mov' else ext[1:]}"
    elif ext in TEXT_EXTS or base in TEXT_BASENAMES or not ext:
        kind, mime = "text", "text/plain"
    else:
        kind, mime = "binary", "application/octet-stream"
    return {"kind": kind, "mime": mime, "size": size, "lang": LANG_MAP.get(ext, ""), "abs": p}


def run_git(args, timeout=30):
    """Run git with cwd=ROOT. Auto-prefix -c core.quotePath=false so CJK paths stay raw."""
    try:
        cmd = list(args)
        if cmd and cmd[0] == "git":
            # insert config after `git`
            cmd = ["git", "-c", "core.quotePath=false"] + cmd[1:]
        r = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=ROOT)
        out = r.stdout.decode("utf-8", "replace")
        err = r.stderr.decode("utf-8", "replace")
        return r.returncode, out, err
    except Exception as e:
        return 1, "", str(e)


def run_git_ok(args, timeout=30):
    code, out, _ = run_git(args, timeout=timeout)
    return out if code == 0 else None


def git_head_branch():
    out = run_git_ok(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    return (out or "").strip() or None


def git_rev_exists(ref):
    code, _, _ = run_git(["git", "rev-parse", "--verify", ref])
    return code == 0


def git_detect_base():
    """相对哪个 base 看本分支：优先 main/master…，再 origin/*，最后空（仅工作区）。"""
    for name in BASE_CANDIDATES:
        if git_rev_exists(name):
            return name
        if git_rev_exists(f"origin/{name}"):
            return f"origin/{name}"
    return None


def git_merge_base(base, head="HEAD"):
    if not base:
        return None
    out = run_git_ok(["git", "merge-base", base, head])
    return (out or "").strip() or None


def status_letter(xy):
    xy = (xy or "  ")[:2]
    if xy == "??":
        return "u"
    if "D" in xy:
        return "d"
    if "A" in xy:
        return "a"
    return "m"


def git_status_map():
    out = run_git_ok(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"])
    if out is None:
        return {}
    m = {}
    for part in out.split("\0"):
        if not part:
            continue
        xy, p = part[:2], part[3:]
        if "->" in p:
            p = p.split("->")[-1].strip()
        m[p] = status_letter(xy)
    return m


def git_diff_worktree(rel, st, abs_path):
    if st == "u":
        code, out, _ = run_git(
            ["git", "diff", "--no-index", "--no-color", "/dev/null", abs_path],
            timeout=20,
        )
        # --no-index returns 1 when files differ; still has useful stdout
        return out or ""
    if st == "d":
        d = run_git_ok(["git", "diff", "HEAD", "--no-color", "--", rel])
        return d if d is not None else ""
    d = run_git_ok(["git", "diff", "HEAD", "--no-color", "--", rel])
    return d if d is not None else ""


def name_status_to_letter(code):
    c = (code or "M")[:1].upper()
    if c == "A":
        return "a"
    if c == "D":
        return "d"
    if c in ("R", "C"):
        return "m"
    return "m"


def unquote_git_path(path):
    """Strip git C-style quotes if present (fallback when quotePath slipped through)."""
    path = (path or "").strip()
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        path = path[1:-1]
        # minimal C-escape: \\ \" \n \t \NNN octal
        out = []
        i = 0
        while i < len(path):
            if path[i] == "\\" and i + 1 < len(path):
                n = path[i + 1]
                if n in '\\"nt':
                    out.append({"\\": "\\", '"': '"', "n": "\n", "t": "\t"}[n])
                    i += 2
                    continue
                if n in "01234567" and i + 3 < len(path):
                    try:
                        out.append(chr(int(path[i + 1:i + 4], 8)))
                        i += 4
                        continue
                    except ValueError:
                        pass
            out.append(path[i])
            i += 1
        return "".join(out)
    return path


def parse_name_status(out):
    """git diff --name-status 行 → [{path, st, status}]"""
    files = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        code = parts[0]
        # R100 old new / C100 old new
        path = unquote_git_path(parts[-1])
        if not path:
            continue
        files.append({
            "path": path,
            "st": name_status_to_letter(code),
            "status": code,
        })
    files.sort(key=lambda x: x["path"].lower())
    return files


def paths_to_tree(files):
    """扁平 path 列表 → 与 build_tree 同形的 dir/file 树（仅含这些文件）。"""
    root = {"name": os.path.basename(ROOT) or ROOT, "path": "", "type": "dir", "children": []}

    def ensure_dir(children, name, path):
        for c in children:
            if c["type"] == "dir" and c["name"] == name:
                return c
        node = {"name": name, "path": path, "type": "dir", "children": []}
        children.append(node)
        children.sort(key=lambda n: (n["type"] != "dir", n["name"].lower()))
        return node

    for f in files:
        rel = f["path"]
        parts = [p for p in rel.split("/") if p]
        if not parts:
            continue
        cur = root["children"]
        acc = []
        for i, part in enumerate(parts):
            acc.append(part)
            ap = "/".join(acc)
            if i == len(parts) - 1:
                cur.append({"name": part, "path": ap, "type": "file", "st": f.get("st")})
                cur.sort(key=lambda n: (n["type"] != "dir", n["name"].lower()))
            else:
                node = ensure_dir(cur, part, ap)
                cur = node["children"]
    return root


def session_meta():
    branch = git_head_branch() or "(detached)"
    base = git_detect_base()
    mb = git_merge_base(base) if base else None
    work = git_status_map()
    commits = []
    if base and mb:
        # 本分支相对 base 的 commits（新→旧）
        log = run_git_ok([
            "git", "log", "--date=short", "--format=%H\t%h\t%ad\t%s", f"{mb}..HEAD",
        ]) or ""
        for line in log.splitlines():
            parts = line.split("\t", 3)
            if len(parts) < 4:
                continue
            full, short, day, subj = parts
            commits.append({
                "id": f"commit:{full}",
                "kind": "commit",
                "sha": full,
                "short": short,
                "date": day,
                "title": subj,
                "label": f"{short} · {subj}",
            })
    entries = []
    entries.append({
        "id": "worktree",
        "kind": "worktree",
        "title": "工作区（未提交）",
        "label": f"工作区（未提交）· {len(work)} 文件",
        "count": len(work),
    })
    if base:
        # 整支：merge-base..HEAD 的文件 + 可再看工作区
        br_files = parse_name_status(
            run_git_ok(["git", "diff", "--name-status", f"{mb}...HEAD"]) or ""
        ) if mb else []
        entries.append({
            "id": "branch",
            "kind": "branch",
            "title": f"整支相对 {base}",
            "label": f"整支 vs {base} · {len(br_files)} 文件",
            "count": len(br_files),
            "base": base,
            "mergeBase": mb,
        })
    for c in commits:
        entries.append(c)

    title = branch
    if base:
        title = f"{branch} · vs {base}"
    return {
        "repo": os.path.basename(ROOT) or ROOT,
        "root": ROOT,
        "branch": branch,
        "base": base,
        "mergeBase": mb,
        "title": title,
        "worktreeCount": len(work),
        "commitCount": len(commits),
        "entries": entries,
        "defaultEntryId": "worktree" if work else ("branch" if base else "worktree"),
    }


def session_files(entry_id):
    meta = session_meta()
    base = meta.get("base")
    mb = meta.get("mergeBase")

    if entry_id == "worktree" or not entry_id:
        stmap = git_status_map()
        files = [{"path": p, "st": s, "status": s} for p, s in sorted(stmap.items())]
        return {
            "entryId": "worktree",
            "kind": "worktree",
            "title": "工作区（未提交）",
            "files": files,
            "tree": paths_to_tree(files),
            "status": stmap,
            "note": "相对 HEAD 的未提交改动（含未跟踪）",
        }

    if entry_id == "branch":
        if not base or not mb:
            return {"entryId": "branch", "kind": "branch", "files": [], "tree": paths_to_tree([]),
                    "status": {}, "note": "找不到 base 分支"}
        files = parse_name_status(run_git_ok(["git", "diff", "--name-status", f"{mb}...HEAD"]) or "")
        stmap = {f["path"]: f["st"] for f in files}
        return {
            "entryId": "branch",
            "kind": "branch",
            "title": f"整支相对 {base}",
            "files": files,
            "tree": paths_to_tree(files),
            "status": stmap,
            "note": f"merge-base({base})...HEAD 的全部提交改动（不含未提交）",
            "base": base,
            "mergeBase": mb,
        }

    if entry_id.startswith("commit:"):
        sha = entry_id.split(":", 1)[1]
        if not git_rev_exists(sha):
            return {"entryId": entry_id, "kind": "commit", "files": [], "tree": paths_to_tree([]),
                    "status": {}, "note": "commit 不存在"}
        files = parse_name_status(run_git_ok(["git", "diff", "--name-status", f"{sha}^!", sha]) or "")
        # ^! 对 root commit 可能失败，退 diff sha~1 sha
        if not files:
            files = parse_name_status(run_git_ok(["git", "diff", "--name-status", f"{sha}~1", sha]) or "")
        stmap = {f["path"]: f["st"] for f in files}
        subj = (run_git_ok(["git", "log", "-1", "--format=%s", sha]) or "").strip()
        short = (run_git_ok(["git", "rev-parse", "--short", sha]) or sha[:7]).strip()
        return {
            "entryId": entry_id,
            "kind": "commit",
            "sha": sha,
            "title": f"{short} · {subj}",
            "files": files,
            "tree": paths_to_tree(files),
            "status": stmap,
            "note": f"单次提交 {short} 引入的改动",
        }

    return {"entryId": entry_id, "kind": "unknown", "files": [], "tree": paths_to_tree([]),
            "status": {}, "note": "未知条目"}


def session_diff(entry_id, rel):
    """返回某条目下单文件 diff 文本。"""
    stmap = {}
    note = ""
    diff = ""

    if entry_id == "worktree" or not entry_id:
        stmap = git_status_map()
        st = stmap.get(rel)
        info = file_info(rel)
        if st == "d":
            diff = run_git_ok(["git", "diff", "HEAD", "--no-color", "--", rel]) or ""
            note = "工作区删除"
        elif st == "u":
            if info:
                diff = git_diff_worktree(rel, "u", info["abs"])
            note = "未跟踪新文件（全量新增）"
        elif st:
            if info:
                diff = git_diff_worktree(rel, st, info["abs"])
            else:
                diff = run_git_ok(["git", "diff", "HEAD", "--no-color", "--", rel]) or ""
            note = "工作区 vs HEAD"
        else:
            note = "该文件不在工作区改动里"
        return {"diff": (diff or "")[:MAX_DIFF_BYTES], "st": st, "note": note, "entryId": "worktree"}

    if entry_id == "branch":
        meta = session_meta()
        mb = meta.get("mergeBase")
        if not mb:
            return {"diff": "", "note": "无 merge-base", "entryId": "branch"}
        diff = run_git_ok(["git", "diff", "--no-color", f"{mb}...HEAD", "--", rel]) or ""
        # 若文件只在工作区，提示
        if not diff and rel in git_status_map():
            note = "此文件只在工作区有改动；请切换到「工作区」条目"
        else:
            note = f"整支 vs {meta.get('base')}（{mb[:7]}...HEAD）"
        # status from name-status
        files = {f["path"]: f["st"] for f in parse_name_status(
            run_git_ok(["git", "diff", "--name-status", f"{mb}...HEAD", "--", rel]) or "")}
        st = files.get(rel)
        return {"diff": diff[:MAX_DIFF_BYTES], "st": st, "note": note, "entryId": "branch"}

    if entry_id.startswith("commit:"):
        sha = entry_id.split(":", 1)[1]
        diff = run_git_ok(["git", "diff", "--no-color", f"{sha}^!", "--", rel]) or ""
        if not diff:
            diff = run_git_ok(["git", "diff", "--no-color", f"{sha}~1", sha, "--", rel]) or ""
        short = (run_git_ok(["git", "rev-parse", "--short", sha]) or sha[:7]).strip()
        note = f"提交 {short}"
        files = {f["path"]: f["st"] for f in parse_name_status(
            run_git_ok(["git", "diff", "--name-status", f"{sha}^!", "--", rel]) or "")}
        st = files.get(rel)
        return {"diff": diff[:MAX_DIFF_BYTES], "st": st, "note": note, "entryId": entry_id, "sha": sha}

    return {"diff": "", "note": "未知条目", "entryId": entry_id}


PAGE = r"""<!DOCTYPE html>
<html lang="zh" translate="yes">
<head>
<meta charset="utf-8">
<title>repo-view · 分支 Diff</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github.min.css">
<style>
  :root { --bg:#ffffff; --panel:#f6f8fa; --line:#d0d7de; --fg:#1f2328; --dim:#656d76;
          --accent:#0969da; --sel:rgba(9,105,218,.10); --ok:#1a7f37; --bad:#cf222e;
          --chip:#eaeef2; --chip-on:#ddf4ff; --chip-bd:#54aeff; }
  * { box-sizing:border-box; margin:0; }
  html,body { height:100%; }
  body { display:flex; flex-direction:column; background:var(--bg); color:var(--fg);
         font:14px/1.5 -apple-system,"SF Pro Text","PingFang SC",sans-serif; }
  header { display:flex; flex-direction:column; gap:0; background:var(--panel);
           border-bottom:1px solid var(--line); flex:none; }
  .hdr-row { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
             padding:8px 14px; }
  .hdr-row.primary { background:var(--panel); }
  .hdr-row.scope { background:#eef2f5; border-top:1px solid var(--line); padding:7px 14px; gap:8px; }
  .hdr-row.commits { background:var(--bg); border-top:1px solid var(--line); padding:8px 14px 10px;
                     flex-direction:column; align-items:stretch; gap:6px; }
  header .repo { font-weight:700; font-size:14px; }
  header .sess-title { font-weight:500; color:var(--dim); font-size:12px; max-width:36vw;
    overflow:hidden; text-overflow:ellipsis; white-space:nowrap; padding:2px 8px;
    background:var(--bg); border:1px solid var(--line); border-radius:6px; }
  header .hint { color:var(--dim); font-size:11px; margin-left:auto; }
  .mode-switch { display:inline-flex; border:1px solid var(--line); border-radius:8px;
                 overflow:hidden; background:var(--bg); }
  .mode-btn { padding:5px 14px; border:0; border-right:1px solid var(--line); background:transparent;
              cursor:pointer; font-size:12px; color:var(--fg); font-weight:500; }
  .mode-btn:last-child { border-right:0; }
  .mode-btn.on { background:var(--fg); color:#fff; font-weight:600; }
  .mode-btn:hover:not(.on) { background:var(--sel); }
  .row-lab { font-size:11px; color:var(--dim); font-weight:600; letter-spacing:.02em;
             text-transform:none; min-width:2.5em; flex:none; }
  .scope-btns { display:flex; gap:6px; flex-wrap:wrap; align-items:center; }
  .scope-btn { padding:5px 12px; border:1px solid var(--line); background:var(--bg); border-radius:8px;
               cursor:pointer; font-size:12px; color:var(--fg); font-weight:500; }
  .scope-btn:hover { border-color:var(--accent); }
  .scope-btn.on { background:var(--chip-on); border-color:var(--chip-bd); color:var(--accent); font-weight:600;
                  box-shadow:0 0 0 1px var(--chip-bd); }
  .scope-btn .n { color:var(--dim); font-weight:400; margin-left:4px; font-size:11px; }
  .scope-btn.on .n { color:var(--accent); opacity:.75; }
  #sess-bar { display:none; flex-direction:column; }
  #sess-bar.show { display:flex; }
  #filters { display:flex; gap:4px; flex-wrap:wrap; align-items:center; }
  #filters .lab { font-size:11px; color:var(--dim); margin-right:4px; font-weight:600; }
  .filter-btn { padding:2px 8px; border:1px solid transparent; background:transparent; border-radius:5px;
                cursor:pointer; font-size:11px; color:var(--dim); }
  .filter-btn:hover { color:var(--fg); background:var(--panel); }
  .filter-btn.on { background:var(--panel); border-color:var(--line); color:var(--fg); font-weight:600; }
  #filterHint { font-size:11px; color:var(--dim); margin-left:4px; }
  #entries { display:flex; gap:5px; flex-wrap:wrap; align-items:center; max-height:72px; overflow:auto; }
  .entry { position:relative; padding:3px 9px; border:1px solid var(--line); background:var(--panel);
           border-radius:6px; cursor:pointer; font-size:12px; color:var(--fg); max-width:280px;
           overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .entry:hover { border-color:var(--accent); background:var(--bg); z-index:5; }
  .entry.on { background:var(--chip-on); border-color:var(--chip-bd); color:var(--accent); font-weight:600; }
  .entry .n { color:var(--dim); font-weight:400; margin-left:4px; }
  /* 截断芯片 hover 立刻显示完整标题（比原生 title 更快更可读） */
  .entry[data-full]:hover::after {
    content: attr(data-full);
    position: absolute; left: 0; top: calc(100% + 6px); z-index: 30;
    min-width: 220px; max-width: min(480px, 70vw);
    padding: 8px 10px; border-radius: 8px;
    background: #1f2328; color: #fff; font-size: 12px; font-weight: 400;
    line-height: 1.45; white-space: normal; word-break: break-word;
    box-shadow: 0 8px 24px rgba(0,0,0,.18);
    pointer-events: none;
  }
  .entry[data-full]:hover::before {
    content: ""; position: absolute; left: 14px; top: calc(100% + 2px); z-index: 31;
    border: 5px solid transparent; border-bottom-color: #1f2328;
    pointer-events: none;
  }
  .entry-note { font-size:11px; color:var(--dim); width:100%; margin-top:2px; }
  .commits-head { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  main { display:flex; flex:1; min-height:0; }
  aside { width:300px; flex:none; display:flex; flex-direction:column; background:var(--panel);
          border-right:1px solid var(--line); min-height:0; }
  #search { margin:10px; padding:6px 10px; background:var(--bg); color:var(--fg);
            border:1px solid var(--line); border-radius:6px; outline:none; font-size:13px; }
  #search:focus { border-color:var(--accent); }
  #tree { overflow:auto; padding:0 6px 16px; flex:1; min-height:0; }
  /* 文件路径是标识符，不参与整页翻译；翻译注入的 <font> 会打爆 nowrap+flex 树形缩进 */
  #tree details { margin-left:10px; display:block; }
  #tree details.top { margin-left:0; }
  #tree summary { cursor:pointer; padding:2px 6px; border-radius:5px; list-style:none;
                  user-select:none; display:block; white-space:nowrap; overflow:hidden;
                  text-overflow:ellipsis; min-width:0; }
  #tree summary::-webkit-details-marker { display:none; }
  #tree summary::before { content:"▸ "; color:var(--dim); }
  #tree details[open] > summary::before { content:"▾ "; }
  #tree .f { display:flex; align-items:center; gap:6px; padding:2px 6px 2px 22px; cursor:pointer;
             border-radius:5px; white-space:nowrap; overflow:hidden; color:var(--fg);
             min-width:0; max-width:100%; }
  #tree .f:hover { background:var(--sel); }
  #tree .f.cur { background:var(--sel); color:var(--accent); }
  #tree .f > .name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; min-width:0; flex:1 1 auto; }
  #tree font { display:inline !important; white-space:inherit; }
  #tree .c-ts,#tree .c-tsx,#tree .c-js,#tree .c-jsx { color:#9a6700; }
  #tree .c-go { color:#0080a8; } #tree .c-py { color:#2b6cb0; }
  #tree .c-md { color:#1a7f37; } #tree .c-json,#tree .c-yaml { color:#bc4c00; }
  #tree .c-img { color:#8250df; } #tree .c-css { color:#6f42c1; }
  .dot { width:7px; height:7px; border-radius:50%; flex:none; }
  .dot.m { background:#cf222e; } .dot.a,.dot.u { background:#1a7f37; } .dot.d { background:#8c959f; }
  #content { flex:1; overflow:auto; padding:0 22px 16px; min-width:0; }
  #viewbar { display:flex; align-items:center; gap:10px; padding:10px 0; position:sticky;
             top:0; background:var(--bg); border-bottom:1px solid var(--line); margin-bottom:12px;
             min-height:41px; z-index:2; }
  #vtitle { font-weight:600; font-size:13px; color:var(--dim); overflow:hidden;
            text-overflow:ellipsis; white-space:nowrap; }
  #btnDiff { margin-left:auto; padding:3px 12px; border:1px solid var(--line); background:var(--panel);
             color:var(--fg); border-radius:6px; cursor:pointer; font-size:12px; flex:none; }
  #btnDiff:hover { border-color:var(--accent); color:var(--accent); }
  /* 不用 pre/code（整页翻译会跳过）。逐行 .code-line 块级节点：翻译改字时不会把 \n 吃成一团。 */
  #content .code-body {
    background:transparent;
    font:12.5px/1.6 "SF Mono",Menlo,Consolas,monospace;
    overflow:auto;
    margin:0;
    white-space:normal;
  }
  #content .code-body.hljs { padding:0; background:transparent; }
  #content .code-line {
    display:block;
    white-space:pre-wrap;
    word-break:break-word;
    min-height:1.6em;
  }
  #content .code-line .hljs-comment,
  #content .code-line .hljs-quote { color:#6a737d; }
  #content .code-line.diff-add { background:rgba(46,160,67,.12); }
  #content .code-line.diff-del { background:rgba(248,81,73,.12); }
  #content .code-line.diff-hunk { color:#656d76; }
  #content img,#content video { max-width:100%; max-height:80vh; border-radius:8px; display:block;
    margin:12px 0; background:repeating-conic-gradient(#f6f8fa 0 25%, #eaeef2 0 50%) 0 0/16px 16px; }
  #statusbar { flex:none; padding:4px 14px; background:var(--panel); border-top:1px solid var(--line);
               color:var(--dim); font-size:12px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  #empty { color:var(--dim); margin-top:40px; text-align:center; }
  .warn { background:#fff8c5; color:#9a6700; padding:6px 10px; border-radius:6px; margin:10px 0; font-size:12px; }
  .sess-empty { color:var(--dim); padding:24px 12px; font-size:13px; line-height:1.6; }
</style>
</head>
<body>
<header>
  <div class="hdr-row primary">
    <span class="repo" id="repoName"></span>
    <span class="sess-title" id="sessTitle" title=""></span>
    <div class="mode-switch" role="group" aria-label="浏览模式">
      <button type="button" class="mode-btn on" id="btnAll" title="浏览整库文件树">全库</button>
      <button type="button" class="mode-btn" id="btnSess" title="只看本分支/工作区改动">分支 Diff</button>
    </div>
    <span class="hint">红点=改过 · 绿点=新增 · 灰点=删除 · / 搜索 · 正文可整页翻译</span>
  </div>
  <div id="sess-bar">
    <div class="hdr-row scope">
      <span class="row-lab">范围</span>
      <div class="scope-btns" id="scopeBtns"></div>
    </div>
    <div class="hdr-row commits">
      <div class="commits-head">
        <span class="row-lab">提交</span>
        <div id="filters">
          <button type="button" class="filter-btn" data-days="1" title="只看今天的 commit">今天</button>
          <button type="button" class="filter-btn" data-days="3" title="近 3 天">近3天</button>
          <button type="button" class="filter-btn" data-days="7" title="近 7 天">近7天</button>
          <button type="button" class="filter-btn" data-days="14" title="近 14 天">近14天</button>
          <button type="button" class="filter-btn" data-days="0" title="本分支相对 main 的全部 commit">全部</button>
          <span id="filterHint"></span>
        </div>
      </div>
      <div id="entries"></div>
      <div class="entry-note" id="entryNote"></div>
    </div>
  </div>
</header>
<main>
  <aside translate="no"><input id="search" placeholder="筛选文件名…" spellcheck="false"><div id="tree" translate="no"></div></aside>
  <div id="content">
    <div id="viewbar" style="display:none"><span id="vtitle" translate="no"></span><button id="btnDiff"></button></div>
    <div id="body"><div id="empty">点击左侧文件查看内容</div></div>
  </div>
</main>
<div id="statusbar">就绪</div>
<script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
<script>
let TREE_ALL = null, TREE = null, STATUS = {}, SESSION = null;
let viewMode = 'all'; // all | session
let entryId = null, entryNote = '';
let curEl = null, curPath = null, curSt = null, mode = 'code';
const $ = s => document.querySelector(s);

function esc(s){ return String(s||'').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
/** 逐行块级节点：整页翻译只改每行文字，不会把整文件并成一段。 */
function renderCodeLines(text, lang){
  const raw = String(text ?? '');
  const lines = raw.split('\n');
  return lines.map((line, i) => {
    let html = esc(line);
    let extra = '';
    if (lang === 'diff'){
      if (line.startsWith('+') && !line.startsWith('+++')) extra = ' diff-add';
      else if (line.startsWith('-') && !line.startsWith('---')) extra = ' diff-del';
      else if (line.startsWith('@@')) extra = ' diff-hunk';
    }
    if (window.hljs && lang){
      try {
        html = hljs.highlight(line, { language: lang, ignoreIllegals: true }).value;
      } catch (e) { /* keep escaped */ }
    }
    if (!line) html = '&nbsp;';
    return `<div class="code-line${extra}" translate="yes">${html}</div>`;
  }).join('');
}
function cls(name){
  const e = name.split('.').pop().toLowerCase();
  if (['ts','tsx','js','jsx'].includes(e)) return 'c-ts';
  if (e==='go') return 'c-go'; if (e==='py') return 'c-py'; if (['md','mdx'].includes(e)) return 'c-md';
  if (['json','yaml','yml'].includes(e)) return 'c-json';
  if (['png','jpg','jpeg','gif','webp','svg','ico'].includes(e)) return 'c-img';
  if (['css','scss','less'].includes(e)) return 'c-css';
  return '';
}
function renderTree(filter){
  const root = $('#tree'); root.innerHTML = '';
  if (!TREE || !TREE.children){
    root.innerHTML = `<div class="sess-empty">没有可显示的文件</div>`;
    return;
  }
  if (viewMode === 'session' && (!TREE.children || TREE.children.length === 0)){
    root.innerHTML = `<div class="sess-empty">此条目没有改动文件。<br/>可点顶栏其他条目，或切回「全库」。</div>`;
    return;
  }
  const walk = (nodes, parent, top) => {
    for (const n of nodes){
      if (n.type === 'dir'){
        const kids = filter ? matchKids(n, filter) : n.children;
        if (filter && kids.length === 0) continue;
        const d = document.createElement('details');
        if (top || filter || viewMode === 'session') d.open = true;
        d.className = top ? 'top' : '';
        d.setAttribute('translate', 'no');
        d.innerHTML = `<summary translate="no">${esc(n.name)}</summary>`;
        walk(kids, d, false); parent.appendChild(d);
      } else {
        if (filter && !n.path.toLowerCase().includes(filter)) continue;
        const a = document.createElement('span');
        const st = n.st || STATUS[n.path];
        a.className = 'f ' + cls(n.name);
        a.setAttribute('translate', 'no');
        a.innerHTML = `<span class="name ${cls(n.name)}" translate="no">${esc(n.name)}</span>` +
                      (st ? `<i class="dot ${st}" title="${st}" translate="no"></i>` : '');
        a.dataset.path = n.path;
        a.onclick = () => openFile(n.path, a);
        parent.appendChild(a);
      }
    }
  };
  walk(TREE.children, root, true);
}
function matchKids(dir, filter){
  const out = [];
  for (const n of dir.children){
    if (n.type === 'file'){ if (n.path.toLowerCase().includes(filter)) out.push(n); }
    else { const kids = matchKids(n, filter); if (kids.length) out.push({ ...n, children: kids }); }
  }
  return out;
}
function markCur(el){ if (curEl) curEl.classList.remove('cur'); curEl = el; if (el) el.classList.add('cur'); }

function setMode(m){
  viewMode = m;
  $('#btnAll').classList.toggle('on', m === 'all');
  $('#btnSess').classList.toggle('on', m === 'session');
  $('#sess-bar').classList.toggle('show', m === 'session');
  curPath = null; curEl = null; mode = 'code';
  $('#viewbar').style.display = 'none';
  $('#body').innerHTML = m === 'session'
    ? `<div id="empty">Session Diff：点顶栏条目，再点左侧改动文件（默认打开 Diff）</div>`
    : `<div id="empty">点击左侧文件查看内容</div>`;
  if (m === 'all'){
    TREE = TREE_ALL; STATUS = STATUS_ALL || {};
    renderTree($('#search').value.trim().toLowerCase());
    $('#statusbar').textContent = '全库浏览';
  } else {
    selectEntry(entryId || (SESSION && SESSION.defaultEntryId) || 'worktree');
  }
}

async function selectEntry(id){
  entryId = id;
  document.querySelectorAll('.scope-btn').forEach(el => {
    el.classList.toggle('on', el.dataset.id === id);
  });
  document.querySelectorAll('.entry').forEach(el => {
    el.classList.toggle('on', el.dataset.id === id);
  });
  $('#statusbar').textContent = '加载条目…';
  try {
    const r = await fetch('/api/session/files?entry=' + encodeURIComponent(id));
    if (!r.ok) throw new Error(await r.text());
    const data = await r.json();
    TREE = data.tree;
    STATUS = data.status || {};
    entryNote = data.note || '';
    const noteEl = $('#entryNote');
    if (noteEl) noteEl.textContent = entryNote + (data.files ? ` · ${data.files.length} 个文件` : '');
    curPath = null; curEl = null; mode = 'code';
    $('#viewbar').style.display = 'none';
    $('#body').innerHTML = `<div id="empty">${esc(data.title || id)}：点左侧文件看 Diff</div>`;
    renderTree($('#search').value.trim().toLowerCase());
    $('#statusbar').textContent = `${data.title || id} · ${entryNote}`;
  } catch (e) {
    $('#tree').innerHTML = `<div class="sess-empty">加载失败：${esc(String(e))}</div>`;
    $('#statusbar').textContent = '条目加载失败';
  }
}

let filterDays = 3; // 默认近 3 天，过期 commit 先藏起来

function parseDay(s){
  if (!s) return 0;
  const m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!m) return 0;
  return new Date(+m[1], +m[2]-1, +m[3]).getTime();
}
function dayStartToday(){
  const n = new Date();
  return new Date(n.getFullYear(), n.getMonth(), n.getDate()).getTime();
}
function commitVisible(e){
  if (!e || e.kind !== 'commit') return false;
  if (!filterDays) return true;
  const t = parseDay(e.date);
  if (!t) return true;
  const oldest = dayStartToday() - (filterDays - 1) * 86400000;
  return t >= oldest;
}
function renderEntries(){
  if (!SESSION) {
    const sb = $('#scopeBtns'); if (sb) sb.innerHTML = '';
    const box = $('#entries'); if (box) box.innerHTML = '';
    return;
  }
  document.querySelectorAll('#filters .filter-btn').forEach(b => {
    b.classList.toggle('on', String(filterDays) === b.dataset.days);
  });
  const all = SESSION.entries || [];
  const scopes = all.filter(e => e.kind === 'worktree' || e.kind === 'branch');
  const commits = all.filter(e => e.kind === 'commit');
  const shownCommits = commits.filter(commitVisible);

  // 第二层：范围（工作区 / 整支）
  const scopeBox = $('#scopeBtns');
  if (scopeBox) {
    scopeBox.innerHTML = scopes.map(e => {
      const cnt = e.count != null ? `<span class="n">${e.count}</span>` : '';
      const title = e.kind === 'worktree' ? '工作区（未提交）' :
                    (e.kind === 'branch' ? `整支 vs ${SESSION.base || 'base'}` : (e.title || e.id));
      return `<button type="button" class="scope-btn" data-id="${esc(e.id)}" title="${esc(e.label || title)}">${esc(title)}${cnt}</button>`;
    }).join('');
    scopeBox.querySelectorAll('.scope-btn').forEach(btn => {
      btn.classList.toggle('on', btn.dataset.id === entryId);
      btn.onclick = () => selectEntry(btn.dataset.id);
    });
  }

  // 第三层：commit 列表（更弱）
  const box = $('#entries');
  if (box) {
    if (!shownCommits.length) {
      box.innerHTML = `<span style="font-size:11px;color:var(--dim)">这个时间范围内没有 commit · 可放宽筛选或点上方「范围」</span>`;
    } else {
      box.innerHTML = shownCommits.map(e => {
        // 芯片：月-日 · 标题；hover 用 data-full 显示完整 commit 标题
        let md = e.date || '';
        const m = String(md).match(/^\d{4}-(\d{2}-\d{2})$/);
        if (m) md = m[1];
        const fullTitle = (e.title || e.short || '').trim();
        const label = `${md} · ${fullTitle}`.trim();
        const full = fullTitle; // hover 全称 = commit 完整标题
        return `<button type="button" class="entry" data-id="${esc(e.id)}" data-full="${esc(full)}" title="${esc(full)}">${esc(label)}</button>`;
      }).join('');
      box.querySelectorAll('.entry').forEach(btn => {
        btn.classList.toggle('on', btn.dataset.id === entryId);
        btn.onclick = () => selectEntry(btn.dataset.id);
      });
    }
  }

  const hint = $('#filterHint');
  if (hint) {
    if (!filterDays) hint.textContent = `全部 ${commits.length} 个 · 新→旧`;
    else hint.textContent = `${shownCommits.length}/${commits.length} · 近${filterDays}天`;
  }
  $('#sessTitle').textContent = SESSION.title || SESSION.branch || '';
  $('#sessTitle').title = `${SESSION.branch || ''} · base ${SESSION.base || '—'} · ${SESSION.commitCount || 0} commits`;
}


async function openFile(path, el){
  markCur(el); mode = 'code'; curPath = path; curSt = STATUS[path] || null;
  const bar = $('#statusbar'); bar.textContent = path + ' · 加载中…';
  try {
    if (viewMode === 'session'){
      // Session 模式：默认直接 Diff（这就是「只看改动」）
      await showDiff(path);
      return;
    }
    const r = await fetch('/api/file?path=' + encodeURIComponent(path));
    if (!r.ok) throw new Error(await r.text());
    const info = await r.json();
    const vb = $('#viewbar');
    if (curSt && curSt !== 'd' && info.kind === 'text'){
      const labels = { m:'已修改', a:'新增', u:'未跟踪' };
      vb.style.display = 'flex';
      $('#vtitle').textContent = `${path} · ${labels[curSt] || curSt}`;
      const b = $('#btnDiff'); b.textContent = '查看 Diff'; b.style.display = '';
      b.onclick = () => mode === 'code' ? showDiff(path) : openFile(path, curEl);
    } else if (info.kind === 'text' || info.kind === 'image' || info.kind === 'video'){
      vb.style.display = 'flex'; $('#vtitle').textContent = path; $('#btnDiff').style.display = 'none';
    } else { vb.style.display = 'none'; }
    renderBody(info, path);
    bar.textContent = `${path} · ${info.size} 字节 · ${info.mime}` +
                      (STATUS[path] ? ' · ● git 有改动' : '');
  } catch (e){ $('#body').innerHTML = `<div id="empty">加载失败：${esc(String(e))}</div>`; bar.textContent = path; }
}
function renderBody(info, path){
  const c = $('#body');
  if (info.kind === 'text'){
    let head = '';
    if (info.truncated) head = `<div class="warn">文件 ${info.size} 字节，超过 1MB，仅显示前 1MB</div>`;
    const langCls = info.lang ? (' language-' + info.lang) : '';
    c.innerHTML = head + `<div class="code-body${langCls}" translate="yes">${renderCodeLines(info.content, info.lang || '')}</div>`;
  } else if (info.kind === 'image'){
    c.innerHTML = `<img src="/api/raw?path=${encodeURIComponent(path)}" alt="${esc(path)}">`;
  } else if (info.kind === 'video'){
    c.innerHTML = `<video src="/api/raw?path=${encodeURIComponent(path)}" controls></video>`;
  } else {
    c.innerHTML = `<div id="empty">二进制文件（${info.mime}，${info.size} 字节）— 暂不支持预览</div>`;
  }
}
async function showDiff(path){
  mode = 'diff';
  const vb = $('#viewbar');
  vb.style.display = 'flex';
  $('#vtitle').textContent = path + (viewMode === 'session' ? ' · Session Diff' : ' · Diff');
  const b = $('#btnDiff');
  if (viewMode === 'session'){
    b.style.display = '';
    b.textContent = '查看工作区文件';
    b.onclick = async () => {
      mode = 'code';
      try {
        const r = await fetch('/api/file?path=' + encodeURIComponent(path));
        if (!r.ok) throw new Error(await r.text());
        const info = await r.json();
        b.textContent = '查看 Diff';
        b.onclick = () => showDiff(path);
        renderBody(info, path);
        $('#statusbar').textContent = path + ' · 工作区文件内容';
      } catch (e) {
        $('#body').innerHTML = `<div id="empty">无法读工作区文件（可能已删除）：${esc(String(e))}</div>`;
      }
    };
  } else {
    b.style.display = '';
    b.textContent = '查看内容';
    b.onclick = () => openFile(path, curEl);
  }
  $('#statusbar').textContent = path + ' · diff 加载中…';
  try {
    let url = '/api/diff?path=' + encodeURIComponent(path);
    if (viewMode === 'session' && entryId){
      url = '/api/session/diff?entry=' + encodeURIComponent(entryId) + '&path=' + encodeURIComponent(path);
    }
    const r = await fetch(url);
    if (!r.ok) throw new Error(await r.text());
    const d = await r.json();
    const c = $('#body');
    if (!d.diff){
      c.innerHTML = `<div id="empty">无差异：${esc(d.note || '无改动')}</div>`;
    } else {
      c.innerHTML = `<div class="code-body language-diff" translate="yes">${renderCodeLines(d.diff, 'diff')}</div>`;
    }
    $('#statusbar').textContent = path + ' · ' + (d.note || 'diff') + (d.st ? ` · ${d.st}` : '');
  } catch (e){ $('#body').innerHTML = `<div id="empty">diff 加载失败：${esc(String(e))}</div>`; }
}

$('#search').addEventListener('input', e => renderTree(e.target.value.trim().toLowerCase()));
document.addEventListener('keydown', e => {
  if (e.key === '/' && document.activeElement !== $('#search')){ e.preventDefault(); $('#search').focus(); }
  if (e.key === 'Escape'){ $('#search').value = ''; renderTree(''); $('#search').blur(); }
});
$('#btnAll').onclick = () => setMode('all');
$('#btnSess').onclick = () => setMode('session');
document.querySelectorAll('#filters .filter-btn').forEach(b => {
  b.onclick = () => {
    filterDays = Number(b.dataset.days || 0);
    try { localStorage.setItem('repo-view-filter-days', String(filterDays)); } catch (_) {}
    renderEntries();
    // 若当前 commit 被藏起，切回工作区列表提示
    if (viewMode === 'session' && entryId && entryId.startsWith('commit:')) {
      const cur = (SESSION.entries || []).find(e => e.id === entryId);
      if (!cur || !commitVisible(cur)) selectEntry('worktree');
    }
  };
});
try {
  const saved = localStorage.getItem('repo-view-filter-days');
  if (saved != null && saved !== '') filterDays = Number(saved);
} catch (_) {}

let STATUS_ALL = {};
Promise.all([
  fetch('/api/tree').then(r => r.json()),
  fetch('/api/status').then(r => r.json()).catch(() => ({})),
  fetch('/api/session').then(r => r.json()).catch(() => null),
]).then(([t, s, sess]) => {
  TREE_ALL = t.tree || t;
  TREE = TREE_ALL;
  STATUS_ALL = s.status || s || {};
  STATUS = STATUS_ALL;
  SESSION = sess;
  $('#repoName').textContent = (TREE && TREE.name) || 'repo';
  if (SESSION){
    renderEntries();
    // 有工作区改动时默认提示可进 Session Diff，但不强切（避免打扰）
    if (SESSION.worktreeCount > 0){
      $('#statusbar').textContent = `就绪 · 工作区 ${SESSION.worktreeCount} 个改动 · 点「Session Diff」只看本分支`;
    }
  }
  renderTree('');
  // URL ?mode=session&entry=...
  const q = new URLSearchParams(location.search);
  if (q.get('mode') === 'session'){
    entryId = q.get('entry') || (SESSION && SESSION.defaultEntryId);
    setMode('session');
  }
});
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            return
        if u.path == "/api/tree":
            try:
                self._json({"tree": build_tree()})
            except RuntimeError as e:
                self._send(500, str(e).encode(), "text/plain; charset=utf-8")
            return
        if u.path == "/api/status":
            self._json({"status": git_status_map()})
            return
        if u.path == "/api/session":
            try:
                self._json(session_meta())
            except Exception as e:
                self._json({"error": str(e), "entries": [], "title": "session error"}, 500)
            return
        if u.path == "/api/session/files":
            entry = (parse_qs(u.query).get("entry") or ["worktree"])[0]
            try:
                self._json(session_files(entry))
            except Exception as e:
                self._json({"error": str(e), "files": [], "tree": paths_to_tree([])}, 500)
            return
        if u.path == "/api/session/diff":
            qs = parse_qs(u.query)
            entry = (qs.get("entry") or ["worktree"])[0]
            rel = (qs.get("path") or [""])[0]
            try:
                self._json(session_diff(entry, rel))
            except Exception as e:
                self._json({"diff": "", "note": str(e)}, 500)
            return
        if u.path == "/api/diff":
            rel = (parse_qs(u.query).get("path") or [""])[0]
            info = file_info(rel)
            st = git_status_map().get(rel)
            if not st:
                self._json({"diff": "", "st": st, "note": "该文件相对 HEAD 无改动"})
                return
            if st == "d":
                diff = run_git_ok(["git", "diff", "HEAD", "--no-color", "--", rel]) or ""
            elif info:
                diff = git_diff_worktree(rel, st, info["abs"])
            else:
                diff = run_git_ok(["git", "diff", "HEAD", "--no-color", "--", rel]) or ""
            note = "未跟踪新文件，以下为全量新增" if st == "u" else "工作区 vs HEAD"
            self._json({"diff": (diff or "")[:MAX_DIFF_BYTES], "st": st, "note": note})
            return
        if u.path == "/api/file":
            rel = (parse_qs(u.query).get("path") or [""])[0]
            info = file_info(rel)
            if not info:
                self._send(404, b"not found", "text/plain")
                return
            if info["kind"] != "text":
                info.pop("abs", None)
                self._json(info)
                return
            with open(info["abs"], "rb") as f:
                raw = f.read(MAX_TEXT_BYTES + 1)
            info["truncated"] = len(raw) > MAX_TEXT_BYTES
            try:
                info["content"] = raw[:MAX_TEXT_BYTES].decode("utf-8")
            except UnicodeDecodeError:
                info.update(kind="binary", mime="application/octet-stream", truncated=False)
            info.pop("abs", None)
            self._json(info)
            return
        if u.path == "/api/raw":
            rel = (parse_qs(u.query).get("path") or [""])[0]
            info = file_info(rel)
            if not info:
                self._send(404, b"not found", "text/plain")
                return
            with open(info["abs"], "rb") as f:
                self._send(200, f.read(), info["mime"])
            return
        self._send(404, b"not found", "text/plain")


def main():
    global ROOT
    if len(sys.argv) > 1:
        ROOT = os.path.realpath(sys.argv[1])
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8770
    if not os.path.isdir(ROOT):
        sys.exit(f"目录不存在: {ROOT}")
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"repo-view: {ROOT} → http://127.0.0.1:{port}")
    print(f"  Session Diff: http://127.0.0.1:{port}/?mode=session")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
