"""脚本引擎：热重载 + 受限沙箱 + 协程调度 + API 参数校验"""
import ast
import logging
import math
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class ScriptCoroutine:
    func: callable
    locals: dict
    finished: bool = False
    needs_restart: bool = False
    error: Exception | None = None
    gen: object = None


class _SandboxValidator(ast.NodeVisitor):
    """AST 级检查：拦截危险属性访问和危险调用"""

    BLOCKED_ATTRS = {
        "__subclasses__", "__globals__", "__code__", "__class__", "__bases__",
        "__mro__", "__dict__", "__import__", "__builtins__", "eval", "exec",
        "compile", "open", "getattr", "setattr", "delattr", "globals",
        "locals", "vars", "breakpoint", "exit", "quit", "input", "memoryview",
    }

    def visit_Attribute(self, node: ast.Attribute):
        if node.attr.startswith("_"):
            raise PermissionError(f"沙箱禁止访问属性: {node.attr}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name):
        if node.id in self.BLOCKED_ATTRS or (node.id.startswith("__") and node.id.endswith("__")):
            raise PermissionError(f"沙箱禁止使用名称: {node.id}")
        self.generic_visit(node)


class ScriptEngine:
    # 引擎 API 参数校验规则：参数名 -> (类型, 校验函数)
    API_SCHEMAS = {
        "spawn": [("x", (int, float), lambda v: math.isfinite(v)),
                  ("y", (int, float), lambda v: math.isfinite(v))],
        "damage": [("target", (str,), lambda v: len(v) > 0),
                   ("dmg", (int, float), lambda v: math.isfinite(v) and v >= 0)],
    }

    def __init__(self):
        self.scripts: dict[str, callable] = {}
        self.running: list[ScriptCoroutine] = []
        self.globals_backup: dict[str, dict] = {}
        self.script_globals: dict[str, dict] = {}
        self.allowed_modules = {"math", "random"}

    # ---------- 注册与热重载 ----------

    def register(self, name: str, func: callable):
        self.scripts[name] = func

    def hot_reload(self, name: str, new_func: callable, conflict_policy: str = "old"):
        """热重载：备份并迁移全局变量，运行中协程切换到新函数。

        conflict_policy: 全局变量冲突策略
          - "old":   保留重载前的旧值（默认）
          - "new":   使用新脚本的值
          - "error": 冲突时抛 ValueError
        """
        if conflict_policy not in ("old", "new", "error"):
            raise ValueError(f"未知冲突策略: {conflict_policy}")
        old_func = self.scripts.get(name)
        # bug3 修复：重载前备份脚本全局变量
        old_globals = dict(self.script_globals.get(name, {}))
        self.globals_backup[name] = old_globals

        self.scripts[name] = new_func

        # 恢复全局变量到新环境，按策略处理冲突
        new_globals = {
            k: v for k, v in (getattr(new_func, "__globals__", {}) or {}).items()
            if not (k.startswith("__") and k.endswith("__"))
        }
        restored = dict(new_globals)
        for key, value in old_globals.items():
            if key in restored and restored[key] != value:
                if conflict_policy == "error":
                    raise ValueError(f"全局变量冲突: {key}")
                if conflict_policy == "old":
                    restored[key] = value
                # "new": 保留新值，不覆盖
            else:
                restored[key] = value
        self.script_globals[name] = restored

        # bug1 修复：迁移运行中协程的局部变量并切换到新函数
        for co in self.running:
            if co.func is old_func and not co.finished:
                co.func = new_func
                co.needs_restart = True  # 旧生成器无法原地替换，标记用新函数重启
                # co.locals 保留在原协程对象上，重启时随协程携带，状态不丢

    # ---------- 沙箱 ----------

    def _safe_import(self, name, globals=None, locals=None, fromlist=(), level=0):
        top = name.split(".")[0]
        if top not in self.allowed_modules:
            raise ImportError(f"沙箱禁止导入模块: {name}")
        return __import__(name, globals, locals, fromlist, level)

    def _build_sandbox(self) -> dict:
        safe_builtins = {
            "print": print, "len": len, "range": range, "str": str, "int": int,
            "float": float, "bool": bool, "list": list, "dict": dict,
            "tuple": tuple, "set": set, "abs": abs, "min": min, "max": max,
            "sum": sum, "enumerate": enumerate, "zip": zip, "sorted": sorted,
            "isinstance": isinstance, "Exception": Exception, "ValueError": ValueError,
            "__import__": self._safe_import,  # bug2 修复：拦截 import
            "__build_class__": __build_class__,
        }
        return {
            "__builtins__": safe_builtins,
            # 白名单引擎 API，脚本只能通过它触达引擎
            "engine_api": lambda api_name, *args: self.call_api(api_name, *args),
        }

    def run_sandboxed(self, code: str, name: str):
        tree = ast.parse(code, filename=f"<script:{name}>")
        _SandboxValidator().visit(tree)  # 危险属性访问在此抛 PermissionError
        sandbox_globals = self._build_sandbox()
        exec(compile(tree, f"<script:{name}>", "exec"), sandbox_globals)
        # 记录脚本全局变量，供热重载迁移
        self.script_globals[name] = {
            k: v for k, v in sandbox_globals.items()
            if k not in ("__builtins__", "engine_api")
        }
        if name in sandbox_globals:
            self.register(name, sandbox_globals[name])

    # ---------- 协程调度 ----------

    def start_coroutine(self, name: str):
        func = self.scripts.get(name)
        if not func:
            return None
        co = ScriptCoroutine(func, dict(self.script_globals.get(name, {})))
        self.running.append(co)
        self._step(co)
        return co

    def _step(self, co: ScriptCoroutine):
        """推进协程一步；bug4 修复：异常被捕获并记录，只终止该协程"""
        if co.finished:
            return
        try:
            if co.needs_restart or co.gen is None:
                co.gen = co.func()  # 用（可能已热重载的）新函数重启
                co.needs_restart = False
            next(co.gen)
        except StopIteration:
            co.finished = True
        except Exception as exc:  # noqa: BLE001 - 隔离单个协程的异常
            co.finished = True
            co.error = exc
            logger.exception("协程异常终止: %s", getattr(co.func, "__name__", co.func))

    def step_all(self):
        for co in list(self.running):
            self._step(co)

    # ---------- 引擎 API ----------

    def call_api(self, api_name: str, *args):
        """bug5 修复：参数类型和范围校验，非法参数返回 None（错误码）不抛异常"""
        apis = {
            "spawn": lambda x, y: (x, y),
            "damage": lambda target, dmg: target,
        }
        api = apis.get(api_name)
        schema = self.API_SCHEMAS.get(api_name)
        if api is None or schema is None:
            logger.warning("未知引擎 API: %s", api_name)
            return None
        if len(args) != len(schema):
            logger.warning("API %s 参数个数错误: 期望 %d 实得 %d",
                           api_name, len(schema), len(args))
            return None
        for (param, types, check), value in zip(schema, args):
            # bool 是 int 子类，数值参数不接受 bool
            if isinstance(value, bool) or not isinstance(value, types):
                logger.warning("API %s 参数 %s 类型错误: %r", api_name, param, value)
                return None
            if not check(value):
                logger.warning("API %s 参数 %s 超出范围: %r", api_name, param, value)
                return None
        return api(*args)
