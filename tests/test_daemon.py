"""Daemon mode tests - Phase 3/4."""

import argparse
import asyncio
import json
import os
import signal
from typing import Any, Literal

import pytest

from opencode_rate_limiter import (
    AccountPoolConfig,
    CleanupConfig,
    Config,
    DaemonConfig,
    DaemonLockError,
    ModelProber,
    ProbeResult,
    RateLimiterDaemon,
    _pid_alive,
    cmd_check,
    generate_launchd_plist,
    generate_systemd_unit,
    generate_task_xml,
    load_daemon_state,
)


def make_daemon(
    tmp_path,
    interval: int = 1,
    models: list[str] | None = None,
    auto_cleanup: bool = True,
    accounts: list[dict[str, Any]] | None = None,
    strategy: Literal["round_robin", "least_used", "health"] = "round_robin",
) -> RateLimiterDaemon:
    state_file = tmp_path / "state" / "state.json"
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text("{}", encoding="utf-8")
    config = Config(
        daemon=DaemonConfig(
            interval_seconds=interval,
            models=models or ["deepseek-v4-flash-free"],
            probe_timeout_seconds=0.5,
            auto_cleanup_on_429=auto_cleanup,
        ),
        cleanup=CleanupConfig(state_files=[str(state_file)]),
        account_pool=AccountPoolConfig(accounts=accounts or [], strategy=strategy),
    )
    daemon = RateLimiterDaemon(config)
    # 隔离测试：状态/锁文件写入临时目录，不触碰真实用户目录
    daemon._state_path = tmp_path / "daemon.json"
    daemon._lock_path = tmp_path / "daemon.lock"
    return daemon


async def wait_until(predicate, timeout: float = 5.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(_poll(), timeout)


class TestDaemonMode:
    """守护进程模式测试"""

    @pytest.mark.asyncio
    async def test_signal_handling_sigterm(self, tmp_path, httpx_mock):
        """测试 SIGTERM 信号处理（优雅停止）"""
        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        daemon = make_daemon(tmp_path)
        task = asyncio.create_task(daemon.run())

        await wait_until(lambda: daemon.status.total_cycles >= 1)
        daemon._request_stop()
        await asyncio.wait_for(task, timeout=3)

        assert daemon.status.running is False
        assert daemon._running is False
        assert daemon._stop_event.is_set()
        assert daemon.status.total_cycles >= 1

    @pytest.mark.asyncio
    async def test_signal_handling_sigint(self, monkeypatch, tmp_path):
        """测试 SIGINT (Ctrl+C) → 优雅停止的信号接线"""
        daemon = make_daemon(tmp_path)

        class FakeLoop:
            def __init__(self) -> None:
                self.registered: dict[int, object] = {}

            def add_signal_handler(self, sig, callback) -> None:
                self.registered[int(sig)] = callback

        fake = FakeLoop()
        monkeypatch.setattr("asyncio.get_running_loop", lambda: fake)
        daemon._install_signal_handlers()

        sigint = getattr(signal, "SIGINT", None)
        assert sigint is not None
        handler = fake.registered.get(int(sigint))
        assert handler is not None
        handler()  # type: ignore[operator]
        assert daemon._stop_event.is_set()
        assert daemon._running is False

    @pytest.mark.asyncio
    async def test_windows_ctrl_c_event(self, monkeypatch, tmp_path):
        """测试 Windows CTRL_C_EVENT → SIGINT 处理（stdlib 信号回退路径）"""
        daemon = make_daemon(tmp_path)
        loop = asyncio.get_running_loop()

        def _raise_unsupported(*args, **kwargs) -> None:
            raise NotImplementedError()

        monkeypatch.setattr(loop, "add_signal_handler", _raise_unsupported)

        prev = {}
        for name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, name, None)
            if sig is not None:
                prev[sig] = signal.getsignal(sig)
        try:
            daemon._install_signal_handlers()
            # SIGINT 与 SIGTERM 都通过 stdlib signal.signal 安装为自定义处理
            for name in ("SIGINT", "SIGTERM"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    installed = signal.getsignal(sig)
                    assert installed is not None
                    assert installed not in (signal.SIG_DFL, signal.SIG_IGN)
                    assert installed != signal.default_int_handler
        finally:
            for sig, old in prev.items():
                signal.signal(sig, old)

    @pytest.mark.asyncio
    async def test_periodic_probe_execution(self, tmp_path):
        """测试周期性探测执行（连续多轮）"""
        daemon = make_daemon(tmp_path, interval=1)

        async def fake_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            return ProbeResult(model=model, status="available", latency_ms=1.0, timestamp="t")

        daemon.prober.probe = fake_probe  # type: ignore[method-assign]
        task = asyncio.create_task(daemon.run())

        await wait_until(lambda: daemon.status.total_cycles >= 2, timeout=8)
        daemon._request_stop()
        await asyncio.wait_for(task, timeout=3)

        assert daemon.status.total_cycles >= 2
        assert daemon.status.last_probe != ""
        assert daemon.status.next_probe != ""
        assert daemon.status.model_results.get("deepseek-v4-flash-free") is not None

    @pytest.mark.asyncio
    async def test_concurrent_model_probing(self, tmp_path):
        """测试并发模型探测（gather 并行而非串行）"""
        import time

        daemon = make_daemon(tmp_path, models=["m1", "m2", "m3"])
        log: list[tuple[str, str]] = []

        async def fake_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            log.append(("start", model))
            await asyncio.sleep(0.2)
            log.append(("end", model))
            return ProbeResult(model=model, status="available", timestamp="t")

        daemon.prober.probe = fake_probe  # type: ignore[method-assign]

        start_wall = time.monotonic()
        await daemon._probe_cycle()
        elapsed = time.monotonic() - start_wall

        assert len(log) == 6
        assert {m for _, m in log} == {"m1", "m2", "m3"}
        # 并行：总耗时 ≈ 一次 sleep (0.2s)；串行则需 3×0.2s
        # 边界放宽到 0.35s 以容忍共享客户端构建与 CI 负载抖动
        assert elapsed < 0.35
        assert set(daemon.status.model_results) == {"m1", "m2", "m3"}

    @pytest.mark.asyncio
    async def test_auto_cleanup_trigger(self, tmp_path, httpx_mock, monkeypatch):
        """测试 429 触发自动清理（清理 = auth 备份 + 缓存维护）"""
        monkeypatch.setattr("opencode_rate_limiter.cleanup.get_opencode_auth_files", lambda: [])
        monkeypatch.setattr(
            "opencode_rate_limiter.cleanup.get_opencode_native_cache_dirs", lambda: []
        )
        daemon = make_daemon(tmp_path, auto_cleanup=True)
        httpx_mock.add_response(
            url=ModelProber.ZEN_ENDPOINT,
            status_code=429,
            headers={"Retry-After": "15"},
        )

        await daemon._probe_cycle()

        assert daemon.status.total_cleanups == 1
        assert daemon.status.last_cleanup != ""
        result = daemon.status.model_results["deepseek-v4-flash-free"]
        assert result.status == "rate_limited"
        assert result.retry_after == 15

    @pytest.mark.asyncio
    async def test_account_rotation_on_429(self, tmp_path, httpx_mock, monkeypatch):
        """测试 429 触发账号轮换"""
        daemon = make_daemon(
            tmp_path,
            auto_cleanup=False,
            accounts=[
                {"name": "primary", "auth_json": "{}"},
                {"name": "backup1", "auth_json": "{}"},
            ],
            strategy="round_robin",
        )
        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=429)

        await daemon._probe_cycle()

        # 指针前进：当前账号应轮换到 backup1
        assert daemon.pool is not None
        current = daemon.pool.get_current()
        assert current is not None
        assert current.name == "backup1"
        assert daemon.pool.health["primary"].consecutive_failures == 1
        # auto_cleanup 关闭时不应触发清理
        assert daemon.status.total_cleanups == 0

    @pytest.mark.asyncio
    async def test_graceful_shutdown_cleanup(self, tmp_path, httpx_mock):
        """测试优雅关闭：停止探测、结束等待任务、状态复位"""
        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        daemon = make_daemon(tmp_path)
        task = asyncio.create_task(daemon.run())

        await wait_until(lambda: daemon.status.total_cycles >= 1)
        await wait_until(lambda: daemon._running is True)
        daemon._request_stop()
        await asyncio.wait_for(task, timeout=3)

        assert task.done()
        assert daemon.status.running is False
        assert daemon.status.total_cycles >= 1


class TestDaemonState:
    """守护进程状态持久化测试"""

    @pytest.mark.asyncio
    async def test_state_file_persisted(self, tmp_path, httpx_mock):
        """测试探测周期后状态文件被写入"""
        httpx_mock.add_response(url=ModelProber.ZEN_ENDPOINT, status_code=200, json={})
        daemon = make_daemon(tmp_path)
        await daemon._probe_cycle()

        state_path = tmp_path / "daemon.json"
        assert state_path.exists()
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["running"] is False
        assert data["pid"] == os.getpid()
        assert data["last_probe"] != ""
        assert data["next_probe"] != ""
        assert data["total_cycles"] == 1
        assert "deepseek-v4-flash-free" in data["models"]

    @pytest.mark.asyncio
    async def test_check_reads_daemon_state(self, monkeypatch, capsys, tmp_path):
        """测试 check 命令读取并输出守护进程状态"""
        state_path = tmp_path / "daemon.json"
        state_path.write_text(
            json.dumps(
                {
                    "running": True,
                    "pid": 42,
                    "last_probe": "2026-09-10T12:00:00Z",
                    "next_probe": "2026-09-10T12:01:00Z",
                    "total_cycles": 5,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "opencode_rate_limiter.daemon.get_daemon_state_path", lambda: state_path
        )

        rc = await cmd_check(Config(), argparse.Namespace(json=False))
        out = capsys.readouterr().out

        assert rc == 0
        assert "Health Check" in out
        assert "pid 42" in out
        assert "cycles=5" in out

    def test_load_daemon_state_missing(self, monkeypatch, tmp_path):
        """测试状态文件不存在时返回 None"""
        monkeypatch.setattr(
            "opencode_rate_limiter.daemon.get_daemon_state_path", lambda: tmp_path / "nope.json"
        )
        assert load_daemon_state() is None


class TestSystemdIntegration:
    """systemd 集成测试"""

    def test_service_file_generation(self, monkeypatch):
        """测试 systemd 服务文件生成"""
        monkeypatch.setattr(
            "opencode_rate_limiter.service._resolve_binary",
            lambda: "/usr/bin/opencode-rate-limiter",
        )
        out = generate_systemd_unit()
        assert "[Unit]" in out
        assert "Description=OpenCode Rate Limiter Daemon" in out
        assert "After=network-online.target" in out

    def test_service_file_content(self, monkeypatch):
        """测试服务文件内容正确性"""
        monkeypatch.setattr(
            "opencode_rate_limiter.service._resolve_binary",
            lambda: "/usr/bin/opencode-rate-limiter",
        )
        out = generate_systemd_unit()
        assert "ExecStart=/usr/bin/opencode-rate-limiter daemon" in out
        assert "Restart=on-failure" in out
        assert "RestartSec=10" in out
        assert "Environment=OPENCODE_RATE_LIMITER_CONFIG=" in out
        assert "[Install]" in out
        assert "WantedBy=default.target" in out


class TestLaunchdIntegration:
    """launchd 集成测试 (macOS)"""

    def test_plist_generation(self, monkeypatch):
        """测试 launchd plist 生成"""
        monkeypatch.setattr(
            "opencode_rate_limiter.service._resolve_binary",
            lambda: "/opt/homebrew/bin/opencode-rate-limiter",
        )
        out = generate_launchd_plist()
        assert '<?xml version="1.0" encoding="UTF-8"?>' in out
        assert "<key>Label</key>" in out
        assert "<string>com.opencode.ratelimiter</string>" in out
        assert "/opt/homebrew/bin/opencode-rate-limiter" in out
        assert "<key>RunAtLoad</key>" in out
        assert "OPENCODE_RATE_LIMITER_CONFIG" in out


class TestTaskSchedulerIntegration:
    """Windows Task Scheduler 集成测试"""

    def test_task_xml_generation(self, monkeypatch):
        """测试任务 XML 生成"""
        monkeypatch.setattr(
            "opencode_rate_limiter.service._resolve_binary",
            lambda: "C:\\\\opencode-rate-limiter.exe",
        )
        out = generate_task_xml()
        assert '<Task version="1.4"' in out
        assert "<LogonTrigger>" in out
        assert "<Command>" in out
        assert "opencode-rate-limiter.exe" in out
        assert "<Arguments>daemon</Arguments>" in out


class TestDaemonLock:
    """守护进程单实例锁测试"""

    def test_acquire_and_release(self, tmp_path):
        daemon = make_daemon(tmp_path)
        lock = tmp_path / "daemon.lock"
        daemon._lock_path = lock

        daemon._acquire_lock()
        assert lock.exists()
        assert daemon._read_lock_pid(lock) == os.getpid()

        daemon._release_lock()
        assert not lock.exists()

    @pytest.mark.asyncio
    async def test_second_instance_raises(self, tmp_path):
        """第二个 daemon 实例获取同一把锁时抛 DaemonLockError"""
        first = make_daemon(tmp_path)
        first._acquire_lock()
        try:
            second = make_daemon(tmp_path)
            with pytest.raises(DaemonLockError):
                second._acquire_lock()
        finally:
            first._release_lock()

        # 释放后可重新获取
        second._acquire_lock()
        second._release_lock()

    def test_stale_lock_is_taken_over(self, tmp_path):
        """持有死进程 pid 的过期锁会被接管"""
        import subprocess
        import sys

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        proc.terminate()
        proc.wait()
        assert not _pid_alive(proc.pid)

        lock = tmp_path / "daemon.lock"
        lock.write_text(str(proc.pid), encoding="utf-8")

        daemon = make_daemon(tmp_path)
        daemon._lock_path = lock
        daemon._acquire_lock()
        assert daemon._read_lock_pid(lock) == os.getpid()
        daemon._release_lock()

    def test_corrupt_lock_is_taken_over(self, tmp_path):
        lock = tmp_path / "daemon.lock"
        lock.write_text("not-a-pid", encoding="utf-8")

        daemon = make_daemon(tmp_path)
        daemon._lock_path = lock
        daemon._acquire_lock()
        daemon._release_lock()

    def test_pid_alive_current_process(self):
        assert _pid_alive(os.getpid())

    def test_pid_alive_invalid(self):
        assert not _pid_alive(0)
        assert not _pid_alive(-1)


class TestHealthFeedback:
    """探测结果回写账号健康度测试"""

    @pytest.mark.asyncio
    async def test_success_result_marks_health(self, tmp_path):
        daemon = make_daemon(
            tmp_path,
            models=["m1"],
            accounts=[{"name": "a", "auth_json": "{}"}],
            strategy="round_robin",
        )

        async def fake_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            return ProbeResult(model=model, status="available", latency_ms=10.0, timestamp="t")

        daemon.prober.probe = fake_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()

        assert daemon.pool is not None
        assert daemon.pool.health["a"].success_count == 1
        assert daemon.pool.health["a"].total_count == 1

    @pytest.mark.asyncio
    async def test_rate_limited_marks_failure_once(self, tmp_path, monkeypatch):
        """429 仅标记一次失败（由 _handle_rate_limited 负责）"""
        daemon = make_daemon(
            tmp_path,
            models=["m1"],
            auto_cleanup=False,
            accounts=[{"name": "a", "auth_json": "{}"}],
            strategy="round_robin",
        )

        async def fake_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            return ProbeResult(
                model=model,
                status="rate_limited",
                http_status=429,
                retry_after=30,
                timestamp="t",
            )

        daemon.prober.probe = fake_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()

        assert daemon.pool is not None
        assert daemon.pool.health["a"].consecutive_failures == 1
        assert daemon.pool.health["a"].total_count == 1


class TestErrorBackoff:
    """连续全错探测周期的指数退避测试"""

    def test_multiplier_caps_at_8x(self, tmp_path):
        daemon = make_daemon(tmp_path)
        assert daemon._backoff_multiplier() == 1
        daemon._error_streak = 1
        assert daemon._backoff_multiplier() == 2
        daemon._error_streak = 2
        assert daemon._backoff_multiplier() == 4
        daemon._error_streak = 3
        assert daemon._backoff_multiplier() == 8
        daemon._error_streak = 10
        assert daemon._backoff_multiplier() == 8  # capped

    @pytest.mark.asyncio
    async def test_error_streak_increments_and_resets(self, tmp_path):
        daemon = make_daemon(tmp_path, models=["m1"])

        async def error_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            return ProbeResult(model=model, status="error", error="boom", timestamp="t")

        async def ok_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            return ProbeResult(model=model, status="available", latency_ms=5.0, timestamp="t")

        daemon.prober.probe = error_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()
        await daemon._probe_cycle()
        assert daemon._error_streak == 2

        daemon.prober.probe = ok_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()
        assert daemon._error_streak == 0


class TestPoolHealthPersistence:
    """账号健康度持久化与 check 输出测试"""

    @pytest.mark.asyncio
    async def test_health_snapshotted_and_persisted(self, tmp_path):
        daemon = make_daemon(
            tmp_path,
            models=["m1"],
            accounts=[{"name": "a", "auth_json": "{}"}],
            strategy="round_robin",
        )

        async def ok_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            return ProbeResult(model=model, status="available", latency_ms=42.0, timestamp="t")

        daemon.prober.probe = ok_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()

        assert daemon.status.pool_health["a"]["success"] == 1
        assert daemon.status.pool_health["a"]["total"] == 1

        state_path = tmp_path / "daemon.json"
        data = json.loads(state_path.read_text(encoding="utf-8"))
        assert data["pool_health"]["a"]["success"] == 1

    @pytest.mark.asyncio
    async def test_check_merges_pool_health(self, monkeypatch, capsys, tmp_path):
        state_path = tmp_path / "daemon.json"
        state_path.write_text(
            json.dumps(
                {
                    "running": True,
                    "total_cycles": 3,
                    "pool_health": {"a": {"success": 2, "total": 3, "score": 0.71}},
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "opencode_rate_limiter.daemon.get_daemon_state_path", lambda: state_path
        )

        rc = await cmd_check(Config(), argparse.Namespace(json=False))
        out = capsys.readouterr().out

        assert rc == 0
        assert "account health" in out
        assert "score=0.71" in out

    @pytest.mark.asyncio
    async def test_reload_preserves_account_health(self, tmp_path, monkeypatch):
        """SIGHUP 重载后同名账号的健康度（含窗口）被保留"""
        config = Config(
            daemon=DaemonConfig(
                interval_seconds=30,
                models=["m1"],
                probe_timeout_seconds=0.5,
            ),
            account_pool=AccountPoolConfig(
                accounts=[{"name": "a", "auth_json": "{}"}], strategy="health"
            ),
        )
        daemon = RateLimiterDaemon(config)
        daemon._state_path = tmp_path / "daemon.json"
        daemon._lock_path = tmp_path / "daemon.lock"

        async def fail_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            return ProbeResult(model=model, status="error", error="boom", timestamp="t")

        daemon.prober.probe = fail_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()  # mark_result 写入健康数据
        assert daemon.pool is not None
        assert daemon.pool.health["a"].total_count == 1

        # 重载：Config.load 返回同一份配置（真实 SIGHUP 会读新文件）
        def fake_load(cls, path=None):
            return config

        monkeypatch.setattr("opencode_rate_limiter.Config.load", classmethod(fake_load))
        daemon._reload_config()

        assert daemon.pool is not None
        assert daemon.pool.health["a"].total_count == 1  # 健康度未归零


class TestProbeHistory:
    """探测历史环形缓冲测试"""

    @pytest.mark.asyncio
    async def test_history_recorded_and_persisted(self, tmp_path):
        daemon = make_daemon(tmp_path, models=["m1"])

        async def ok_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            return ProbeResult(model=model, status="available", latency_ms=10.0, timestamp="t")

        daemon.prober.probe = ok_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()

        assert len(daemon.status.history) == 1
        entry = daemon.status.history[0]
        assert entry["models"] == {"m1": "available"}
        assert entry["ts"] != ""

        data = json.loads((tmp_path / "daemon.json").read_text(encoding="utf-8"))
        assert data["history"][0]["models"] == {"m1": "available"}

    @pytest.mark.asyncio
    async def test_history_respects_configured_size(self, tmp_path):
        daemon = make_daemon(tmp_path, models=["m1"])
        daemon.config.daemon.history_size = 2
        daemon._rebuild()  # 应用新的窗口大小

        async def ok_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            return ProbeResult(model=model, status="available", latency_ms=1.0, timestamp="t")

        daemon.prober.probe = ok_probe  # type: ignore[method-assign]
        for _ in range(4):
            await daemon._probe_cycle()

        assert daemon.status.history.maxlen == 2
        assert len(daemon.status.history) == 2

    @pytest.mark.asyncio
    async def test_check_displays_history_trend(self, monkeypatch, capsys, tmp_path):
        state_path = tmp_path / "daemon.json"
        state_path.write_text(
            json.dumps(
                {
                    "running": False,
                    "total_cycles": 3,
                    "history": [
                        {"ts": "t1", "models": {"m1": "available", "m2": "rate_limited"}},
                        {"ts": "t2", "models": {"m1": "available", "m2": "available"}},
                        {"ts": "t3", "models": {"m1": "error", "m2": "available"}},
                    ],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "opencode_rate_limiter.daemon.get_daemon_state_path", lambda: state_path
        )

        rc = await cmd_check(Config(), argparse.Namespace(json=False))
        out = capsys.readouterr().out

        assert rc == 0
        assert "probe history    : last 3 probes" in out
        assert "m1: available x2, error x1" in out
        assert "m2: available x2, rate_limited x1" in out


class TestRateLimitCooldown:
    """每模型限流冷却期测试"""

    @pytest.mark.asyncio
    async def test_cooldown_skips_rate_limited_model(self, tmp_path):
        """限流模型在冷却期内不再重复探测"""
        daemon = make_daemon(tmp_path, models=["m1"], auto_cleanup=False)
        calls: list[str] = []

        async def flaky_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            calls.append(model)
            if len(calls) == 1:
                return ProbeResult(
                    model=model,
                    status="rate_limited",
                    http_status=429,
                    retry_after=120,
                    estimated_reset=120,
                    timestamp="t",
                )
            return ProbeResult(model=model, status="available", latency_ms=1.0, timestamp="t")

        daemon.prober.probe = flaky_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()
        await daemon._probe_cycle()
        assert calls == ["m1"]  # 第二轮被冷却跳过

    @pytest.mark.asyncio
    async def test_respect_cooldown_false_probes_every_cycle(self, tmp_path):
        daemon = make_daemon(tmp_path, models=["m1"], auto_cleanup=False)
        daemon.config.daemon.respect_cooldown = False
        calls: list[str] = []

        async def limited_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            calls.append(model)
            return ProbeResult(
                model=model,
                status="rate_limited",
                http_status=429,
                retry_after=120,
                estimated_reset=120,
                timestamp="t",
            )

        daemon.prober.probe = limited_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()
        await daemon._probe_cycle()
        assert calls == ["m1", "m1"]

    @pytest.mark.asyncio
    async def test_cooldown_cleared_on_recovery(self, tmp_path):
        """恢复可用后冷却期解除"""
        daemon = make_daemon(tmp_path, models=["m1"], auto_cleanup=False)
        calls: list[str] = []

        async def probe(model: str, headers: dict[str, str]) -> ProbeResult:
            calls.append(model)
            if len(calls) == 1:
                return ProbeResult(
                    model=model,
                    status="rate_limited",
                    http_status=429,
                    retry_after=1,
                    estimated_reset=1,
                    timestamp="t",
                )
            return ProbeResult(model=model, status="available", latency_ms=1.0, timestamp="t")

        daemon.prober.probe = probe  # type: ignore[method-assign]
        await daemon._probe_cycle()
        # 手动过期冷却，模拟时间流逝
        daemon._cooldowns["m1"] = 0.0
        await daemon._probe_cycle()
        assert calls == ["m1", "m1"]
        assert "m1" not in daemon._cooldowns

    @pytest.mark.asyncio
    async def test_cooldown_persisted(self, tmp_path):
        daemon = make_daemon(tmp_path, models=["m1"], auto_cleanup=False)

        async def limited_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            return ProbeResult(
                model=model,
                status="rate_limited",
                http_status=429,
                retry_after=90,
                estimated_reset=90,
                timestamp="t",
            )

        daemon.prober.probe = limited_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()

        data = json.loads((tmp_path / "daemon.json").read_text(encoding="utf-8"))
        assert 0 < data["cooldowns"]["m1"] <= 90


class TestCleanupDedupPerCycle:
    """同轮多个模型限流只触发一次清理"""

    @pytest.mark.asyncio
    async def test_one_cleanup_for_multiple_rate_limited(self, tmp_path, monkeypatch):
        monkeypatch.setattr("opencode_rate_limiter.cleanup.get_opencode_auth_files", lambda: [])
        monkeypatch.setattr(
            "opencode_rate_limiter.cleanup.get_opencode_native_cache_dirs", lambda: []
        )
        daemon = make_daemon(tmp_path, models=["m1", "m2"], auto_cleanup=True)

        async def limited_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            return ProbeResult(
                model=model,
                status="rate_limited",
                http_status=429,
                retry_after=60,
                estimated_reset=60,
                timestamp="t",
            )

        daemon.prober.probe = limited_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()

        assert daemon.status.total_cleanups == 1


class TestProbeBudget:
    """每日探测预算测试"""

    @pytest.mark.asyncio
    async def test_budget_trims_probe_batch(self, tmp_path):
        daemon = make_daemon(tmp_path, models=["m1", "m2", "m3"], auto_cleanup=False)
        daemon.config.daemon.daily_probe_budget = 2
        probed: list[str] = []

        async def ok_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            probed.append(model)
            return ProbeResult(model=model, status="available", latency_ms=1.0, timestamp="t")

        daemon.prober.probe = ok_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()
        assert len(probed) == 2  # 第 3 个模型被预算裁剪
        assert daemon._probe_count == 2

    @pytest.mark.asyncio
    async def test_budget_exhausted_skips_cycle(self, tmp_path):
        daemon = make_daemon(tmp_path, models=["m1"], auto_cleanup=False)
        daemon.config.daemon.daily_probe_budget = 1
        probed: list[str] = []

        async def ok_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            probed.append(model)
            return ProbeResult(model=model, status="available", latency_ms=1.0, timestamp="t")

        daemon.prober.probe = ok_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()
        await daemon._probe_cycle()  # 预算耗尽，跳过
        assert probed == ["m1"]
        assert daemon.status.total_cycles == 2  # 周期仍计数

    @pytest.mark.asyncio
    async def test_budget_persisted_and_restored(self, tmp_path):
        daemon = make_daemon(tmp_path, models=["m1"], auto_cleanup=False)
        daemon.config.daemon.daily_probe_budget = 5

        async def ok_probe(model: str, headers: dict[str, str]) -> ProbeResult:
            return ProbeResult(model=model, status="available", latency_ms=1.0, timestamp="t")

        daemon.prober.probe = ok_probe  # type: ignore[method-assign]
        await daemon._probe_cycle()
        state = json.loads((tmp_path / "daemon.json").read_text(encoding="utf-8"))
        assert state["probe_usage"]["count"] == 1
        assert state["probe_usage"]["day"] != ""

        # 重启后恢复计数（同一天内不重置）
        daemon2 = make_daemon(tmp_path, models=["m1"], auto_cleanup=False)
        daemon2.config.daemon.daily_probe_budget = 5
        daemon2._load_probe_usage()
        assert daemon2._probe_count == 1
        assert daemon2._probe_day == state["probe_usage"]["day"]

    def test_budget_validation(self):
        with pytest.raises(ValueError, match="daily_probe_budget"):
            DaemonConfig(daily_probe_budget=-1).validate()
