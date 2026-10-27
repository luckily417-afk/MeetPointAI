"""修复复制后 venv 里 console-script 启动器（*.exe）内嵌的旧绝对路径。

Windows 上 pip 生成的 `Scripts\\*.exe` 会把解释器绝对路径写进启动器。
把项目目录整体复制到新位置后，这些启动器仍指向旧目录：
`pip.exe --version` 会报告旧路径，安装包也会装到旧环境里。
本脚本用 pip 自带的 distlib 按当前解释器重新生成全部 console_scripts。
"""

from __future__ import annotations

import sys
import sysconfig
from importlib.metadata import distributions
from pathlib import Path


def main() -> int:
    venv = Path(sys.prefix)
    site = Path(sysconfig.get_paths()["purelib"])
    python_exe = venv / "Scripts" / "python.exe"
    if not python_exe.is_file():
        print(f"[x] 不是 venv 解释器或找不到 {python_exe}", file=sys.stderr)
        return 2

    from pip._vendor.distlib.scripts import ScriptMaker

    specs: list[str] = []
    seen: set[str] = set()
    for dist in distributions():
        try:
            entries = dist.entry_points
        except Exception:  # noqa: BLE001
            continue
        for entry in entries:
            if entry.group != "console_scripts":
                continue
            name = entry.name.strip()
            if not name or name in seen:
                continue
            seen.add(name)
            specs.append(f"{name} = {entry.value}")

    sm = ScriptMaker(source_dir=str(site), target_dir=str(venv / "Scripts"))
    sm.executable = str(python_exe)
    sm.clobber = True
    made: list[str] = []
    for spec in specs:
        try:
            made.extend(Path(p).name for p in sm.make(spec))
        except Exception as exc:  # noqa: BLE001
            print(f"[!] {spec}: {type(exc).__name__}: {exc}")

    print(f"python : {python_exe}")
    print(f"site   : {site}")
    print(f"重写 {len(made)} 个启动器：{', '.join(sorted(made)[:12])}{' …' if len(made) > 12 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
