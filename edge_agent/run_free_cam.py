"""Launch Free Cam using the installed bbOS environment."""
import os
import sys
from pathlib import Path
from run_robot_demo import runtime

if __name__ == '__main__':
    root = Path.home()/'bbos'
    python, env = runtime(root)
    vendor = Path(__file__).resolve().parent/'vendor'
    if vendor.is_dir():
        env['PYTHONPATH'] += os.pathsep+str(vendor)
    os.execve(python, [str(python), '-m', 'edge_agent.wrist_demo', *sys.argv[1:]], env)
