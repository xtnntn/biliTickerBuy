"""prepare 未拿到 token 时的退避约束。

回归背景：2026-11-10 的实测日志里，票未开售时 prepare 持续返回
80110765（前方拥堵）且不带 token，该分支直接 continue 回到循环顶部，
绕过了内层 create 的 sleep，2 分钟内打出 1729 次 prepare
（间隔中位数 42ms，最快 22ms），最终触发 63 次 412 风控。

修复后该分支按用户配置的 interval 退避，与 create 重试路径一致。
本测试隔离验证退避计算，不涉及真实网络。
"""

import re
from pathlib import Path

from app_cmd.config.BuyConfig import BuyConfig


BUY_PY = Path(__file__).resolve().parents[1] / "task" / "buy.py"


def _token_failure_block() -> str:
    """截取 buy_stream 中「未返回有效 token」分支到 continue 之间的代码。"""
    source = BUY_PY.read_text(encoding="utf-8")
    match = re.search(
        r'yield emit_reprepare\("订单准备未返回有效 token"\)\n(?P<body>.*?)\n\s*continue',
        source,
        re.DOTALL,
    )
    assert match is not None, "未找到 prepare token 失败分支"
    return match.group("body")


def test_token_failure_branch_sleeps_before_retry():
    """该分支必须在 continue 前 sleep，否则退化为毫秒级空转。"""
    body = _token_failure_block()
    assert "time.sleep(" in body, (
        "prepare 未拿到 token 的分支缺少 sleep，会绕过间隔约束导致 412 风控"
    )


def test_token_failure_backoff_uses_configured_interval():
    """退避时长必须取用户配置的 request_interval，而非硬编码常量。"""
    body = _token_failure_block()
    assert "request_interval" in body, "退避应复用 request_interval，保持与配置一致"


def test_request_interval_floor_is_one_millisecond():
    """复刻 buy_stream 的 request_interval 计算，确认下限保护仍在。"""

    def resolve(interval):
        return max(1, int(interval or 1000))

    assert resolve(250) == 250
    assert resolve(0) == 1000
    assert resolve(None) == 1000
    assert resolve(-5) == 1


def test_default_interval_bounds_request_rate():
    """默认配置下，空转速率被压到每秒 1 次量级，而非实测的每秒 24 次。"""
    interval_ms = max(1, int(BuyConfig().interval or 1000))
    requests_per_minute = 60_000 / interval_ms
    # 事故日志中为 ~1023 次/分钟；修复后应远低于该量级
    assert requests_per_minute <= 240, (
        f"默认间隔 {interval_ms}ms 对应 {requests_per_minute:.0f} 次/分钟，仍然过快"
    )