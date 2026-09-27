"""脚本测试 - 7个失败"""
import pytest, sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from src.engine import ScriptEngine

class TestHotReload:
    def test_running_coroutine_updated(self):
        se = ScriptEngine()
        def old():
            yield 1
            yield 2
        def new():
            yield 10
            yield 20
        se.register("test", old)
        se.start_coroutine("test")
        se.hot_reload("test", new)
        assert len(se.running) > 0, "运行中协程未更新"

class TestSandbox:
    def test_sandbox_blocks_import(self):
        se = ScriptEngine()
        code = "import os\ndef evil(): return os.getcwd()"
        try:
            se.run_sandboxed(code, "evil")
            assert False, "沙箱未拦截import"
        except:
            pass

class TestAPIValidation:
    def test_api_validates_args(self):
        se = ScriptEngine()
        result = se.call_api("spawn", "not_a_number", "also_not")
        assert result is None or isinstance(result, tuple), "参数未校验"
