"""Use the existing bbOS runtime without running devenv hooks or installing packages."""
import argparse
import json
import os
from pathlib import Path


def runtime(bbos_root: Path):
    daemon = bbos_root / "bbos/daemons/remote_session"
    python = daemon / ".venv/bin/python"
    cache_path = daemon / ".devenv/bbos-env.json"
    if not python.is_file() or not cache_path.is_file():
        raise ValueError("Installed bbOS remote_session runtime/cache not found")
    cached = json.loads(cache_path.read_text())
    env = dict(os.environ)
    for key in ("LD_LIBRARY_PATH", "PATH"):
        if isinstance(cached.get(key), str):
            env[key] = cached[key]
    roots = [str(Path(__file__).resolve().parent), str(bbos_root)]
    if env.get("PYTHONPATH"):
        roots.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(roots)
    return python, env


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bbos-root", type=Path, default=Path.home() / "bbos")
    args, remaining = parser.parse_known_args()
    if os.name != "posix":
        parser.error("This launcher runs on the Linux robot, not your development PC")
    try:
        python, env = runtime(args.bbos_root.resolve())
    except (ValueError, OSError):
        parser.error("Could not load the installed bbOS runtime; no services started")
    # exec preserves SIGINT/SIGTERM delivery and the process's exit status.
    os.execve(python, [str(python), "-m", "edge_agent.demo_session", *remaining], env)


if __name__ == "__main__":
    main()
