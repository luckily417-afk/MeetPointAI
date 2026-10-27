"""Create a credential-free source archive and a source change report."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import zipfile
from pathlib import Path


SKIP_DIRS = {
    ".venv", "build", "dist", "data", "out", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".taste-skill", ".git",
}
SKIP_FILES = {".env", "api-keys.txt"}
TEXT_SUFFIXES = {".py", ".html", ".css", ".js", ".json", ".md", ".txt", ".ps1", ".cmd", ".spec", ".example", ".gitignore"}


def files(root: Path) -> dict[str, Path]:
    found = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in SKIP_DIRS for part in rel.parts[:-1]):
            continue
        if path.name in SKIP_FILES or path.suffix.lower() in {".log", ".pyc"}:
            continue
        found[rel.as_posix()] = path
    return found


def digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(chunk)
    return sha.hexdigest()


def text_lines(path: Path) -> list[str] | None:
    if path.suffix.lower() not in TEXT_SUFFIXES and path.name != ".gitignore":
        return None
    try:
        return path.read_text(encoding="utf-8-sig").splitlines(keepends=True)
    except (UnicodeError, OSError):
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("original", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    original = files(args.original)
    current = files(args.current)
    added = sorted(current.keys() - original.keys())
    removed = sorted(original.keys() - current.keys())
    modified = sorted(name for name in original.keys() & current.keys() if digest(original[name]) != digest(current[name]))
    unchanged = len(original.keys() & current.keys()) - len(modified)

    lines = [
        "MeetPoint AI 源码与资源变动对比\n",
        f"原版：{args.original}\n",
        f"当前版：{args.current}\n",
        f"新增 {len(added)}，修改 {len(modified)}，删除 {len(removed)}，未变 {unchanged}\n",
        "范围：可分发源码、测试、文档、示例与资源；排除 .env、api-keys.txt、日志、data、虚拟环境、缓存及构建产物。\n",
        "二进制资源只记录 SHA-256；文本修改附完整逐行差异。\n\n",
    ]
    for title, names in (("新增", added), ("修改", modified), ("删除", removed)):
        lines.append(f"== {title} ({len(names)}) ==\n")
        lines.extend(f"{name}\n" for name in names)
        lines.append("\n")
    lines.append("== 修改详情 ==\n")
    for name in modified:
        before = original[name]
        after = current[name]
        lines.append(f"\n--- {name} ---\n")
        old_lines, new_lines = text_lines(before), text_lines(after)
        if old_lines is not None and new_lines is not None:
            diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"原版/{name}", tofile=f"当前版/{name}")
            lines.extend(diff)
            if lines[-1] and not lines[-1].endswith("\n"):
                lines.append("\n")
        else:
            lines.append(f"原版 SHA-256: {digest(before)}\n当前版 SHA-256: {digest(after)}\n")

    args.output.mkdir(parents=True, exist_ok=True)
    report = args.output / "MeetPoint-AI-变动对比-V0.0.0.1.txt"
    report.write_text("".join(lines), encoding="utf-8-sig")

    archive = args.output / "MeetPoint-AI-源码-V0.0.0.1.zip"
    guide = (
        "MeetPoint AI V0.0.0.1 源码包\n\n"
        "系统：Windows 10/11 64 位；Python 3.12 或更新版本。\n"
        "安装依赖：python -m venv .venv，然后 .venv\\Scripts\\python.exe -m pip install -r requirements.txt\n"
        "配置：复制 .env.example 为 .env，填写自己的高德和 DeepSeek API Key。\n"
        "启动 GUI：.venv\\Scripts\\python.exe main.py\n"
        "运行 CLI：.venv\\Scripts\\python.exe meetpoint_debug.py --help\n"
        "运行测试：.venv\\Scripts\\python.exe -m pytest -q\n\n"
        "此包不含本机 API Key、运行数据、虚拟环境和已编译程序。\n"
    )
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zipped:
        prefix = "MeetPoint-AI-0.0.0.1/"
        for name, path in sorted(current.items()):
            if name.startswith("docs/") and path.suffix.lower() in {".png", ".jpg", ".jpeg"}:
                continue  # 展示截图不属于源码，可能含本机界面数据。
            zipped.write(path, prefix + name)
        zipped.writestr(prefix + "源码包使用说明.txt", guide)
    print(f"report={report}")
    print(f"archive={archive}")
    print(f"added={len(added)} modified={len(modified)} removed={len(removed)} unchanged={unchanged}")


if __name__ == "__main__":
    main()
