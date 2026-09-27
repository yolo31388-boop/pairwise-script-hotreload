"""脚本热重载 - 带5个bug"""
import types
from dataclasses import dataclass, field

@dataclass
class ScriptCoroutine:
    func: callable
    locals: dict
    finished: bool = False

class ScriptEngine:
    def __init__(self):
        self.scripts: dict[str, callable] = {}
        self.running: list[ScriptCoroutine] = []
        self.globals_backup: dict = {}
        self.allowed_modules = {"math", "random"}  # bug2: 白名单但不拦截import

    def register(self, name: str, func: callable):
        self.scripts[name] = func

    def hot_reload(self, name: str, new_func: callable):
        old_func = self.scripts.get(name)
        self.scripts[name] = new_func
        # bug1: 不更新运行中协程
        for co in self.running:
            if co.func == old_func:
                pass  # 不替换

    def run_sandboxed(self, code: str, name: str):
        # bug2: 不拦截import
        sandbox_globals = {"__builtins__": __builtins__}
        exec(code, sandbox_globals)
        if name in sandbox_globals:
            self.register(name, sandbox_globals[name])

    def start_coroutine(self, name: str):
        func = self.scripts.get(name)
        if func:
            co = ScriptCoroutine(func, {})
            self.running.append(co)
            # bug4: 异常不捕获
            gen = func()
            try:
                next(gen)
            except StopIteration:
                co.finished = True

    def call_api(self, api_name: str, *args):
        # bug5: 不校验参数
        apis = {"spawn": lambda x, y: (x, y), "damage": lambda target, dmg: target}
        return apis.get(api_name, lambda *a: None)(*args)
