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
        monkeypatch.setattr("opencode_rate_limiter.asyncio.get_running_loop", lambda: fake)
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
            await asyncio.sleep(0.05)
            log.append(("end", model))
            return ProbeResult(model=model, status="available", timestamp="t")

        daemon.prober.probe = fake_probe  # type: ignore[method-assign]

        start_wall = time.monotonic()
        await daemon._probe_cycle()
        elapsed = time.monotonic() - start_wall

        assert len(log) == 6
        assert {m for _, m in log} == {"m1", "m2", "m3"}
        # 并行：总耗时 ≈ 一次 sleep (0.05s)；串行则需 3×0.05s
        assert elapsed < 0.12
        assert set(daemon.status.model_results) == {"m1", "m2", "m3"}

    @pytest.mark.asyncio
    async def test_auto_cleanup_trigger(self, tmp_path, httpx_mock, monkeypatch):
        """测试 429 触发自动清理"""
        state_file = tmp_path / "state" / "state.json"
        state_file.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            "opencode_rate_limiter.get_opencode_native_state_files", lambda: [state_file]
        )
        monkeypatch.setattr("opencode_rate_limiter.get_opencode_auth_files", lambda: [])
        monkeypatch.setattr("opencode_rate_limiter.get_opencode_native_cache_dirs", lambda: [])

        state_file.write_text("{}", encoding="utf-8")
        daemon = make_daemon(tmp_path, auto_cleanup=True)
        httpx_mock.add_response(
            url=ModelProber.ZEN_ENDPOINT,
            status_code=429,
            headers={"Retry-After": "15"},
        )

        await daemon._probe_cycle()

        assert not state_file.exists()
        assert daemon.status.total_cleanups == 1
        assert daemon.status.last_cleanup != ""
        result = daemon.status.model_results["deepseek-v4-flash-free"]
        assert result.status == "rate_limited"
        assert result.retry_after == 15

    @pytest.mark.asyncio
    async def test_account_rotation_on_429(self, tmp_path, httpx_mock, monkeypatch):
        """测试 429 触发账号轮换"""
        monkeypatch.setattr("opencode_rate_limiter.get_opencode_auth_files", lambda: [])
        monkeypatch.setattr("opencode_rate_limiter.get_opencode_native_cache_dirs", lambda: [])
        monkeypatch.setattr("opencode_rate_limiter.get_opencode_native_state_files", lambda: [])
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
        monkeypatch.setattr("opencode_rate_limiter.get_daemon_state_path", lambda: state_path)

        rc = await cmd_check(Config(), argparse.Namespace(json=False))
        out = capsys.readouterr().out

        assert rc == 0
        assert '"daemon"' in out
        assert '"pid": 42' in out
        assert '"total_cycles": 5' in out

    def test_load_daemon_state_missing(self, monkeypatch, tmp_path):
        """测试状态文件不存在时返回 None"""
        monkeypatch.setattr(
            "opencode_rate_limiter.get_daemon_state_path", lambda: tmp_path / "nope.json"
        )
        assert load_daemon_state() is None


class TestSystemdIntegration:
    """systemd 集成测试"""

    def test_service_file_generation(self, monkeypatch):
        """测试 systemd 服务文件生成"""
        monkeypatch.setattr(
            "opencode_rate_limiter._resolve_binary",
            lambda: "/usr/bin/opencode-rate-limiter",
        )
        out = generate_systemd_unit()
        assert "[Unit]" in out
        assert "Description=OpenCode Rate Limiter Daemon" in out
        assert "After=network-online.target" in out

    def test_service_file_content(self, monkeypatch):
        """测试服务文件内容正确性"""
        monkeypatch.setattr(
            "opencode_rate_limiter._resolve_binary",
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
            "opencode_rate_limiter._resolve_binary",
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
            "opencode_rate_limiter._resolve_binary",
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
        monkeypatch.setattr("opencode_rate_limiter.get_opencode_auth_files", lambda: [])
        monkeypatch.setattr("opencode_rate_limiter.get_opencode_native_cache_dirs", lambda: [])
        monkeypatch.setattr("opencode_rate_limiter.get_opencode_native_state_files", lambda: [])
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
        monkeypatch.setattr("opencode_rate_limiter.get_daemon_state_path", lambda: state_path)

        rc = await cmd_check(Config(), argparse.Namespace(json=False))
        out = capsys.readouterr().out

        assert rc == 0
        assert '"pool_health"' not in out  # 归位到 account_pool.health
        assert '"health"' in out
        assert '"score": 0.71' in out
