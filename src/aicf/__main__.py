from .path_utils import load_project_env
from .secret_store import load_runtime_secrets

# 应用入口点：显式初始化环境，避免模块级副作用
load_project_env(override=False)
load_runtime_secrets()

from .cli import main

raise SystemExit(main())
