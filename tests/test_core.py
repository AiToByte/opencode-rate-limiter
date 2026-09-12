"""Core module tests for opencode-rate-limiter Phase 1."""

import argparse
import datetime
import json
import logging
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opencode_rate_limiter import (
    AccountPoolConfig,
    CleanupConfig,
    Config,
    DaemonConfig,
    HeadersConfig,
    HumanFormatter,
    JSONFormatter,
    __version__,
    build_parser,
    cmd_deep,
    cmd_generate_config,
    cmd_quick,
    cmd_rotate,
    get_opencode_auth_files,
    get_opencode_config_dirs,
    get_opencode_native_cache_dirs,
    get_opencode_native_state_files,
    level_from_args,
    setup_logging,
)


class TestConfig:
    """Config class tests - Phase 1"""

    def test_load_defaults(self):
        """Default config loads correctly"""
        cfg = Config.load(None)
        assert cfg.daemon.interval_seconds == 900
        assert cfg.daemon.probe_timeout_seconds == 10.0
        assert cfg.daemon.auto_cleanup_on_429 is True
        assert "deepseek-v4-flash-free" in cfg.daemon.models
        assert len(cfg.daemon.models) == 8
        assert cfg.account_pool.strategy == "health"
        assert cfg.headers.user_agent == "opencode/{version}"
        assert cfg.cleanup.preserve_config is True

    def test_load_custom_toml(self, sample_toml_config: Path):
        """Custom TOML config overrides defaults"""
        cfg = Config.load(sample_toml_config)
        assert cfg.daemon.interval_seconds == 60
        assert cfg.daemon.probe_timeout_seconds == 15.0
        assert cfg.daemon.models == ["deepseek-v4-flash-free", "nemotron-3-ultra-free"]
        assert cfg.account_pool.strategy == "health"
        assert len(cfg.account_pool.accounts) == 2
        assert cfg.account_pool.accounts[0]["name"] == "primary"
        assert cfg.account_pool.accounts[1]["name"] == "backup"
        assert cfg.headers.user_agent == "opencode/{version}"
        assert cfg.cleanup.cache_dirs == ["~/.opencode/cache"]

    def test_load_minimal_toml(self, minimal_toml_config: Path):
        """Minimal TOML fills in defaults"""
        cfg = Config.load(minimal_toml_config)
        assert cfg.daemon.interval_seconds == 120
        # models not in file, uses default DaemonConfig() list
        assert "deepseek-v4-flash-free" in cfg.daemon.models
        assert cfg.daemon.probe_timeout_seconds == 10.0  # Default
        assert cfg.account_pool.strategy == "health"  # Default
        assert cfg.cleanup.preserve_config is True  # Default

    def test_load_invalid_toml_raises(self, invalid_toml_config: Path):
        """Invalid TOML raises ValueError"""
        with pytest.raises(ValueError, match="Invalid TOML"):
            Config.load(invalid_toml_config)

    def test_load_nonexistent_file_returns_defaults(self, tmp_path: Path):
        """Non-existent config file returns defaults"""
        cfg = Config.load(tmp_path / "nonexistent.toml")
        assert cfg.daemon.interval_seconds == 900

    def test_env_overrides_daemon_interval(self, monkeypatch, sample_toml_config: Path):
        """Environment variable overrides config file"""
        monkeypatch.setenv("OPENCODE_RATE_LIMITER_DAEMON__INTERVAL_SECONDS", "120")
        cfg = Config.load(sample_toml_config)
        assert cfg.daemon.interval_seconds == 120

    def test_env_overrides_models(self, monkeypatch):
        """Environment variable overrides default models"""
        monkeypatch.setenv("OPENCODE_RATE_LIMITER_DAEMON__MODELS", "model-a,model-b")
        cfg = Config.load(None)
        assert cfg.daemon.models == ["model-a", "model-b"]

    def test_env_overrides_bool(self, monkeypatch):
        """Environment variable overrides boolean"""
        monkeypatch.setenv("OPENCODE_RATE_LIMITER_DAEMON__AUTO_CLEANUP_ON_429", "false")
        cfg = Config.load(None)
        assert cfg.daemon.auto_cleanup_on_429 is False

    def test_env_overrides_strategy(self, monkeypatch):
        """Environment variable overrides account pool strategy"""
        monkeypatch.setenv("OPENCODE_RATE_LIMITER_ACCOUNT_POOL__STRATEGY", "round_robin")
        cfg = Config.load(None)
        assert cfg.account_pool.strategy == "round_robin"

    def test_config_file_precedence(self, monkeypatch, tmp_path: Path):
        """Config file values override defaults, env overrides file"""
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[daemon]
interval_seconds = 60
""")
        monkeypatch.setenv("OPENCODE_RATE_LIMITER_DAEMON__INTERVAL_SECONDS", "90")
        cfg = Config.load(config_file)
        assert cfg.daemon.interval_seconds == 90  # Env wins


class TestConfigValidation:
    """Config validation tests"""

    def test_validate_daemon_interval_too_low(self):
        """Interval < 5 raises ValueError"""
        cfg = Config()
        cfg.daemon.interval_seconds = 3
        with pytest.raises(ValueError, match="interval_seconds must be >= 5"):
            cfg.validate()

    def test_validate_probe_timeout_zero(self):
        """Probe timeout = 0 raises ValueError"""
        cfg = Config()
        cfg.daemon.probe_timeout_seconds = 0.0
        with pytest.raises(ValueError, match="probe_timeout_seconds must be > 0"):
            cfg.validate()

    def test_validate_probe_timeout_negative(self):
        """Probe timeout < 0 raises ValueError"""
        cfg = Config()
        cfg.daemon.probe_timeout_seconds = -1.0
        with pytest.raises(ValueError, match="probe_timeout_seconds must be > 0"):
            cfg.validate()

    def test_validate_models_empty(self):
        """Empty models list raises ValueError"""
        cfg = Config()
        cfg.daemon.models = []
        with pytest.raises(ValueError, match="models list cannot be empty"):
            cfg.validate()

    def test_validate_strategy_invalid(self):
        """Invalid strategy raises ValueError"""
        cfg = Config()
        cfg.account_pool.strategy = "invalid"  # type: ignore[assignment]
        with pytest.raises(ValueError, match="strategy must be one of"):
            cfg.validate()

    def test_validate_preserve_config_false(self):
        """preserve_config = false raises ValueError"""
        cfg = Config()
        cfg.cleanup.preserve_config = False
        with pytest.raises(ValueError, match="preserve_config must be true"):
            cfg.validate()

    def test_validate_account_missing_name(self):
        """Account without 'name' raises ValueError"""
        cfg = Config()
        cfg.account_pool.accounts = [{"auth_path": "/tmp/test.json"}]
        with pytest.raises(ValueError, match="missing required 'name' field"):
            cfg.validate()

    def test_validate_account_missing_auth_source(self):
        """Account without auth source raises ValueError"""
        cfg = Config()
        cfg.account_pool.accounts = [{"name": "test"}]
        with pytest.raises(ValueError, match="missing auth source"):
            cfg.validate()

    def test_validate_account_valid(self):
        """Valid account passes validation"""
        cfg = Config()
        cfg.account_pool.accounts = [{"name": "test", "auth_path": "/tmp/test.json"}]
        cfg.validate()  # Should not raise


class TestConfigMerge:
    """Config merge tests"""

    def test_merge_daemon(self):
        """Daemon config merges correctly"""
        base = Config()
        override = {"daemon": {"interval_seconds": 120}}
        merged = Config._merge(base, override)
        assert merged.daemon.interval_seconds == 120
        assert merged.daemon.probe_timeout_seconds == 10.0  # Unchanged

    def test_merge_headers(self):
        """Headers config merges correctly"""
        base = Config()
        override = {"headers": {"user_agent": "custom/{version}"}}
        merged = Config._merge(base, override)
        assert merged.headers.user_agent == "custom/{version}"
        assert merged.headers.x_opencode_client == "opencode-cli"  # Unchanged

    def test_merge_account_pool(self):
        """Account pool config merges correctly"""
        base = Config()
        override = {"account_pool": {"strategy": "round_robin"}}
        merged = Config._merge(base, override)
        assert merged.account_pool.strategy == "round_robin"
        assert merged.account_pool.accounts == []  # Unchanged

    def test_merge_cleanup(self):
        """Cleanup config merges correctly"""
        base = Config()
        override = {"cleanup": {"cache_dirs": ["/tmp/cache"]}}
        merged = Config._merge(base, override)
        assert merged.cleanup.cache_dirs == ["/tmp/cache"]
        assert merged.cleanup.preserve_config is True  # Unchanged


class TestConfigPathExpansion:
    """Path expansion tests"""

    def test_expand_home(self, tmp_path: Path, monkeypatch):
        """~ expands to home directory"""
        test_home = tmp_path / "fakehome"
        test_home.mkdir()
        monkeypatch.setenv("HOME", str(test_home))
        # Also set USERPROFILE for Windows
        monkeypatch.setenv("USERPROFILE", str(test_home))
        cfg = Config()
        cfg.cleanup.cache_dirs = ["~/.opencode/cache"]
        cfg._expand_paths()
        assert cfg._expanded_cache_dirs[0] == test_home / ".opencode" / "cache"

    def test_expand_env_var(self, tmp_path: Path, monkeypatch):
        """$VAR expands to environment variable"""
        monkeypatch.setenv("MY_DIR", str(tmp_path / "mydir"))
        cfg = Config()
        cfg.cleanup.cache_dirs = ["$MY_DIR"]
        cfg._expand_paths()
        assert cfg._expanded_cache_dirs[0] == tmp_path / "mydir"

    def test_get_cache_dirs_dedup(self, tmp_path: Path, monkeypatch):
        """get_cache_dirs deduplicates and includes native paths"""
        test_home = tmp_path / "fakehome"
        test_home.mkdir()
        monkeypatch.setenv("HOME", str(test_home))
        monkeypatch.setenv("USERPROFILE", str(test_home))
        cfg = Config()
        cfg.cleanup.cache_dirs = [str(test_home / ".opencode" / "cache")]
        dirs = cfg.get_cache_dirs()
        # Should have unique paths
        assert len(dirs) == len(set(dirs))


class TestDaemonConfig:
    """DaemonConfig tests"""

    def test_default_values(self):
        cfg = DaemonConfig()
        assert cfg.interval_seconds == 900
        assert cfg.probe_timeout_seconds == 10.0
        assert cfg.auto_cleanup_on_429 is True
        assert len(cfg.models) == 8

    def test_validate_passes(self):
        cfg = DaemonConfig()
        cfg.validate()  # Should not raise


class TestAccountPoolConfig:
    """AccountPoolConfig tests"""

    def test_default_strategy(self):
        cfg = AccountPoolConfig()
        assert cfg.strategy == "health"

    def test_valid_strategies(self):
        for strategy in ["round_robin", "least_used", "health"]:
            cfg = AccountPoolConfig(strategy=strategy)  # type: ignore[arg-type]
            cfg.validate()


class TestHeadersConfig:
    """HeadersConfig tests"""

    def test_default_headers(self):
        cfg = HeadersConfig()
        assert cfg.user_agent == "opencode/{version}"
        assert cfg.x_opencode_client == "opencode-cli"
        assert cfg.x_opencode_version == "{version}"


class TestCleanupConfig:
    """CleanupConfig tests"""

    def test_default_preserve_config(self):
        cfg = CleanupConfig()
        assert cfg.preserve_config is True

    def test_validate_preserves_config(self):
        cfg = CleanupConfig()
        cfg.validate()  # Should not raise


class TestPathResolution:
    """Path resolution tests"""

    def test_get_opencode_config_dirs(self):
        """Config dirs are found"""
        dirs = get_opencode_config_dirs()
        assert len(dirs) >= 1
        # All should be Path objects
        for d in dirs:
            assert isinstance(d, Path)

    def test_get_opencode_native_cache_dirs(self):
        """Cache dirs include native paths"""
        dirs = get_opencode_native_cache_dirs()
        assert len(dirs) >= 1
        # All should end with 'cache'
        for d in dirs:
            assert d.name == "cache"

    def test_get_opencode_native_state_files(self):
        """State files include native paths"""
        files = get_opencode_native_state_files()
        assert len(files) >= 1
        # All should be state.json
        for f in files:
            assert f.name == "state.json"

    def test_get_opencode_auth_files(self):
        """Auth files include native paths"""
        files = get_opencode_auth_files()
        assert len(files) >= 1
        # All should be auth.json
        for f in files:
            assert f.name == "auth.json"


class TestLogging:
    """Logging system tests"""

    def test_json_formatter(self, capsys):
        """JSON formatter produces valid JSON"""
        import logging

        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logger = logging.getLogger("test_json")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        logger.info("Test message")
        logger.removeHandler(handler)

        captured = capsys.readouterr()
        # Should be valid JSON
        data = json.loads(captured.err)
        assert data["level"] == "INFO"
        assert data["logger"] == "test_json"
        assert data["message"] == "Test message"
        # 时间戳为真 UTC（Z 后缀且可解析回 UTC 时刻）
        ts = datetime.datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        assert ts.tzinfo is not None
        assert abs(ts.timestamp() - time.time()) < 60

    def test_json_formatter_extra_fields(self, capsys):
        """extra 字段透传进 JSON 且不含 stdlib 内部键"""
        import logging

        handler = logging.StreamHandler()
        handler.setFormatter(JSONFormatter())
        logger = logging.getLogger("test_json_extra")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        logger.info("with extra", extra={"model": "m1", "status": "available"})
        logger.removeHandler(handler)

        data = json.loads(capsys.readouterr().err)
        assert data["model"] == "m1"
        assert data["status"] == "available"
        assert "created" not in data
        assert "thread" not in data

    def test_human_formatter(self, capsys):
        """Human formatter produces readable output"""
        import logging

        handler = logging.StreamHandler()
        handler.setFormatter(HumanFormatter())
        logger = logging.getLogger("test_human")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        logger.info("Test message")
        logger.removeHandler(handler)

        captured = capsys.readouterr()
        assert "INFO" in captured.err
        assert "test_human" in captured.err
        assert "Test message" in captured.err

    def test_setup_logging_json(self):
        """setup_logging with JSON mode"""
        setup_logging(logging.WARNING, json_output=True)
        assert any(
            isinstance(h.formatter, JSONFormatter) for h in logging.root.handlers if h.formatter
        )

    def test_setup_logging_human(self):
        """setup_logging with human mode"""
        setup_logging(logging.WARNING, json_output=False)
        assert any(
            isinstance(h.formatter, HumanFormatter) for h in logging.root.handlers if h.formatter
        )

    def test_level_from_args_quiet(self):
        """Quiet mode = ERROR"""
        args = MagicMock()
        args.quiet = True
        args.verbose = 0
        assert level_from_args(args) == logging.ERROR

    def test_level_from_args_verbose_1(self):
        """-v = INFO"""
        args = MagicMock()
        args.quiet = False
        args.verbose = 1
        assert level_from_args(args) == logging.INFO

    def test_level_from_args_verbose_2(self):
        """-vv = DEBUG"""
        args = MagicMock()
        args.quiet = False
        args.verbose = 2
        assert level_from_args(args) == logging.DEBUG

    def test_level_from_args_default(self):
        """Default = WARNING"""
        args = MagicMock()
        args.quiet = False
        args.verbose = 0
        assert level_from_args(args) == logging.WARNING


class TestCLI:
    """CLI argument parser tests"""

    def test_help_output(self):
        """--help produces output"""
        parser = build_parser()
        # Should not raise
        assert parser.prog == "opencode-rate-limiter"

    def test_version_output(self):
        """Version string is valid"""
        assert __version__ == "0.2.0"

    def test_parser_has_all_subcommands(self):
        """All subcommands registered"""
        parser = build_parser()
        # Parse with minimal args to check subcommand dest
        args = parser.parse_args(["quick"])
        assert args.command == "quick"

    def test_parser_probe_model(self):
        """Probe subcommand accepts model"""
        parser = build_parser()
        args = parser.parse_args(["probe", "deepseek-v4-flash-free"])
        assert args.command == "probe"
        assert args.model == "deepseek-v4-flash-free"

    def test_parser_probe_default_all(self):
        """Probe defaults to all"""
        parser = build_parser()
        args = parser.parse_args(["probe"])
        assert args.model == "all"

    def test_parser_headers_export(self):
        """Headers --export flag"""
        parser = build_parser()
        args = parser.parse_args(["headers", "--export"])
        assert args.export is True

    def test_parser_rotate_strategy(self):
        """Rotate --strategy flag"""
        parser = build_parser()
        args = parser.parse_args(["rotate", "--strategy", "round_robin"])
        assert args.strategy == "round_robin"

    def test_parser_daemon_interval(self):
        """Daemon --interval flag"""
        parser = build_parser()
        args = parser.parse_args(["daemon", "--interval", "60"])
        assert args.interval == 60


class TestConfigSave:
    """Config save tests"""

    def test_save_creates_file(self, tmp_path: Path):
        """Save creates TOML file"""
        config_file = tmp_path / "config.toml"
        cfg = Config()
        cfg.save(config_file)
        assert config_file.exists()
        content = config_file.read_text()
        assert "interval_seconds" in content

    def test_save_preserves_values(self, tmp_path: Path):
        """Saved config preserves custom values"""
        config_file = tmp_path / "config.toml"
        cfg = Config()
        cfg.daemon.interval_seconds = 120
        cfg.save(config_file)

        # Reload and verify
        loaded = Config.load(config_file)
        assert loaded.daemon.interval_seconds == 120

    def test_save_creates_parent_dirs(self, tmp_path: Path):
        """Save creates parent directories"""
        config_file = tmp_path / "subdir" / "config.toml"
        cfg = Config()
        cfg.save(config_file)
        assert config_file.exists()


class TestConfigRoundTrip:
    """Config round-trip tests (load -> save -> load)"""

    def test_round_trip_defaults(self, tmp_path: Path):
        """Default config round-trips correctly"""
        config_file = tmp_path / "config.toml"
        original = Config()
        original.save(config_file)

        loaded = Config.load(config_file)
        assert loaded.daemon.interval_seconds == original.daemon.interval_seconds
        assert loaded.daemon.models == original.daemon.models
        assert loaded.account_pool.strategy == original.account_pool.strategy
        assert loaded.headers.user_agent == original.headers.user_agent
        assert loaded.cleanup.preserve_config == original.cleanup.preserve_config

    def test_round_trip_custom(self, tmp_path: Path):
        """Custom config round-trips correctly"""
        config_file = tmp_path / "config.toml"
        original = Config()
        original.daemon.interval_seconds = 120
        original.daemon.models = ["model-a", "model-b"]
        original.account_pool.strategy = "round_robin"
        original.account_pool.accounts = [{"name": "test", "auth_path": "/tmp/test.json"}]
        original.headers.user_agent = "custom/{version}"
        original.cleanup.cache_dirs = ["/tmp/cache"]
        original.save(config_file)

        loaded = Config.load(config_file)
        assert loaded.daemon.interval_seconds == 120
        assert loaded.daemon.models == ["model-a", "model-b"]
        assert loaded.account_pool.strategy == "round_robin"
        assert loaded.account_pool.accounts == [{"name": "test", "auth_path": "/tmp/test.json"}]
        assert loaded.headers.user_agent == "custom/{version}"
        assert loaded.cleanup.cache_dirs == ["/tmp/cache"]


# =============================================================================
# Iteration tests: env config path / quick-deep split / generate-config / rotate
# =============================================================================


class TestEnvConfigPath:
    def test_load_reads_env_config_path(self, tmp_path: Path, monkeypatch):
        """OPENCODE_RATE_LIMITER_CONFIG points to the config file when --config absent"""
        config_file = tmp_path / "custom.toml"
        config_file.write_text("[daemon]\ninterval_seconds = 90\n", encoding="utf-8")
        monkeypatch.setenv("OPENCODE_RATE_LIMITER_CONFIG", str(config_file))

        config = Config.load()
        assert config.daemon.interval_seconds == 90

    def test_cli_config_beats_env(self, tmp_path: Path, monkeypatch):
        """--config wins over OPENCODE_RATE_LIMITER_CONFIG"""
        env_file = tmp_path / "env.toml"
        env_file.write_text("[daemon]\ninterval_seconds = 90\n", encoding="utf-8")
        cli_file = tmp_path / "cli.toml"
        cli_file.write_text("[daemon]\ninterval_seconds = 77\n", encoding="utf-8")
        monkeypatch.setenv("OPENCODE_RATE_LIMITER_CONFIG", str(env_file))

        config = Config.load(cli_file)
        assert config.daemon.interval_seconds == 77

    def test_env_path_supports_tilde_and_vars(self, tmp_path: Path, monkeypatch):
        """~ and env vars in OPENCODE_RATE_LIMITER_CONFIG are expanded"""
        config_file = tmp_path / "custom.toml"
        config_file.write_text("[daemon]\ninterval_seconds = 66\n", encoding="utf-8")
        monkeypatch.setenv("TEST_QUEST_DIR", str(tmp_path))
        monkeypatch.setenv("OPENCODE_RATE_LIMITER_CONFIG", "$TEST_QUEST_DIR/custom.toml")

        config = Config.load()
        assert config.daemon.interval_seconds == 66


class TestQuickDeepSplit:
    def _setup_targets(self, tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
        state_file = tmp_path / "state.json"
        state_file.write_text("{}", encoding="utf-8")
        auth_file = tmp_path / "auth.json"
        auth_file.write_text('{"access_token": "tok"}', encoding="utf-8")
        cache_dir = tmp_path / "cache"
        cache_dir.mkdir()
        (cache_dir / "entry.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(
            "opencode_rate_limiter.cleanup.get_opencode_auth_files", lambda: [auth_file]
        )
        monkeypatch.setattr(
            "opencode_rate_limiter.cleanup.get_opencode_native_cache_dirs", lambda: [cache_dir]
        )
        return state_file, auth_file, cache_dir

    @pytest.mark.asyncio
    async def test_quick_backs_up_auth_only(self, tmp_path: Path, monkeypatch, capsys):
        """quick: 仅备份 auth.json，不碰缓存、不清 token、不删任何状态文件"""
        state_file, auth_file, cache_dir = self._setup_targets(tmp_path, monkeypatch)
        args = argparse.Namespace(json=True, dry_run=False)

        rc = await cmd_quick(Config(), args)
        assert rc == 0
        # 内容原样保留（不再做 token 手术）
        assert json.loads(auth_file.read_text(encoding="utf-8"))["access_token"] == "tok"
        assert auth_file.with_suffix(".json.bak").exists()
        assert state_file.exists()  # 虚构目标不再删除
        assert (cache_dir / "entry.json").exists()
        # JSON 输出无 relogin_hint（token 未被清除）
        out = capsys.readouterr().out
        assert "relogin_hint" not in out

    @pytest.mark.asyncio
    async def test_deep_purges_cache(self, tmp_path: Path, monkeypatch, capsys):
        """deep: quick 全部 + 清缓存"""
        state_file, auth_file, cache_dir = self._setup_targets(tmp_path, monkeypatch)
        args = argparse.Namespace(json=True, dry_run=False)

        rc = await cmd_deep(Config(), args)
        assert rc == 0
        assert not (cache_dir / "entry.json").exists()
        assert auth_file.with_suffix(".json.bak").exists()
        assert state_file.exists()

    @pytest.mark.asyncio
    async def test_quick_dry_run_touches_nothing(self, tmp_path: Path, monkeypatch):
        _state_file, auth_file, cache_dir = self._setup_targets(tmp_path, monkeypatch)
        args = argparse.Namespace(json=True, dry_run=True)

        rc = await cmd_quick(Config(), args)
        assert rc == 0
        assert json.loads(auth_file.read_text(encoding="utf-8"))["access_token"] == "tok"
        assert not auth_file.with_suffix(".json.bak").exists()
        assert (cache_dir / "entry.json").exists()


class TestGenerateConfig:
    @pytest.mark.asyncio
    async def test_generate_config_creates_file(self, tmp_path: Path, capsys):
        target = tmp_path / "out" / "config.toml"
        args = argparse.Namespace(config=target, force=False, json=True)

        rc = await cmd_generate_config(Config(), args)
        assert rc == 0
        assert target.exists()
        loaded = Config.load(target)
        assert loaded.daemon.interval_seconds == Config().daemon.interval_seconds

    @pytest.mark.asyncio
    async def test_generate_config_refuses_overwrite(self, tmp_path: Path, capsys):
        target = tmp_path / "config.toml"
        target.write_text("[daemon]\ninterval_seconds = 99\n", encoding="utf-8")
        args = argparse.Namespace(config=target, force=False, json=True)

        rc = await cmd_generate_config(Config(), args)
        assert rc == 1
        assert Config.load(target).daemon.interval_seconds == 99

    @pytest.mark.asyncio
    async def test_generate_config_force_overwrites(self, tmp_path: Path):
        target = tmp_path / "config.toml"
        target.write_text("[daemon]\ninterval_seconds = 99\n", encoding="utf-8")
        args = argparse.Namespace(config=target, force=True, json=True)

        rc = await cmd_generate_config(Config(), args)
        assert rc == 0
        assert Config.load(target).daemon.interval_seconds == Config().daemon.interval_seconds

    @pytest.mark.asyncio
    async def test_generate_config_defaults_to_platform_path(self, monkeypatch, tmp_path: Path):
        monkeypatch.setattr(
            "opencode_rate_limiter.cli._default_config_path_str",
            lambda: str(tmp_path / "cfg" / "config.toml"),
        )
        args = argparse.Namespace(config=None, force=False, json=True)

        rc = await cmd_generate_config(Config(), args)
        assert rc == 0
        assert (tmp_path / "cfg" / "config.toml").exists()


class TestRotateDryRun:
    def _make_config(self) -> Config:
        return Config(
            account_pool=AccountPoolConfig(
                accounts=[
                    {"name": "primary", "auth_json": '{"access_token": "tok-a"}'},
                    {"name": "backup", "auth_json": "{}"},
                ],
                strategy="round_robin",
            )
        )

    @pytest.mark.asyncio
    async def test_rotate_reports_dry_run_and_token(self, capsys):
        args = argparse.Namespace(strategy="round_robin", json=True, dry_run=True, apply=False)

        rc = await cmd_rotate(self._make_config(), args)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["rotated_to"] == "primary"
        assert data["dry_run"] is True
        assert data["auth_token_resolved"] is True

    def _patch_auth_target(self, monkeypatch, tmp_path: Path) -> Path:
        auth_target = tmp_path / "opencode" / "auth.json"
        auth_target.parent.mkdir(parents=True, exist_ok=True)
        auth_target.write_text('{"access_token": "old-token"}', encoding="utf-8")
        monkeypatch.setattr(
            "opencode_rate_limiter.cli.get_opencode_auth_files", lambda: [auth_target]
        )
        return auth_target

    @pytest.mark.asyncio
    async def test_rotate_apply_writes_auth_with_backup(self, tmp_path: Path, monkeypatch, capsys):
        """--apply 把选中账号的 auth JSON 写入目标并先备份"""
        auth_target = self._patch_auth_target(monkeypatch, tmp_path)
        args = argparse.Namespace(strategy="round_robin", json=True, dry_run=False, apply=True)

        rc = await cmd_rotate(self._make_config(), args)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["applied"] == "applied"
        assert data["auth_target"] == str(auth_target)
        # 新内容来自选中账号的 auth_json；旧内容已备份
        assert json.loads(auth_target.read_text(encoding="utf-8"))["access_token"] == "tok-a"
        backup = auth_target.with_suffix(".json.bak")
        assert backup.exists()
        assert json.loads(backup.read_text(encoding="utf-8"))["access_token"] == "old-token"

    @pytest.mark.asyncio
    async def test_rotate_apply_dry_run_touches_nothing(self, tmp_path: Path, monkeypatch, capsys):
        auth_target = self._patch_auth_target(monkeypatch, tmp_path)
        args = argparse.Namespace(strategy="round_robin", json=True, dry_run=True, apply=True)

        rc = await cmd_rotate(self._make_config(), args)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["applied"] == "dry_run"
        assert data["auth_target"] == str(auth_target)
        assert json.loads(auth_target.read_text(encoding="utf-8"))["access_token"] == "old-token"
        assert not auth_target.with_suffix(".json.bak").exists()

    @pytest.mark.asyncio
    async def test_rotate_apply_unresolvable_auth_fails(self, tmp_path: Path, monkeypatch):
        self._patch_auth_target(monkeypatch, tmp_path)
        args = argparse.Namespace(strategy="round_robin", json=True, dry_run=False, apply=True)
        config = Config(
            account_pool=AccountPoolConfig(
                accounts=[{"name": "a", "auth_json": "not-json"}], strategy="round_robin"
            )
        )

        rc = await cmd_rotate(config, args)
        assert rc == 1

    @pytest.mark.asyncio
    async def test_rotate_missing_token_reported(self, capsys):
        args = argparse.Namespace(strategy="round_robin", json=True, dry_run=False, apply=False)
        config = Config(
            account_pool=AccountPoolConfig(
                accounts=[{"name": "a", "auth_json": "{}"}], strategy="round_robin"
            )
        )

        rc = await cmd_rotate(config, args)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["auth_token_resolved"] is False

    @pytest.mark.asyncio
    async def test_rotate_dry_run_human_output(self, capsys):
        args = argparse.Namespace(strategy="health", json=False, dry_run=True, apply=False)

        rc = await cmd_rotate(self._make_config(), args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "dry run" in out


# =============================================================================
# Iteration round 2: OPENCODE_VERSION / [prober] config / health in check
# =============================================================================


class TestVersionOverride:
    def test_opencode_version_env_override(self, monkeypatch):
        from opencode_rate_limiter import get_opencode_version

        monkeypatch.setenv("OPENCODE_VERSION", "9.9.9-test")
        assert get_opencode_version() == "9.9.9-test"

    def test_opencode_version_empty_env_falls_back(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_VERSION", "")
        # 空值视为未设置：走真实检测（which 找不到 opencode 时返回 unknown）
        monkeypatch.setattr("shutil.which", lambda _: None)
        from opencode_rate_limiter import get_opencode_version

        assert get_opencode_version() == "unknown"


class TestProberConfigSection:
    def test_load_prober_section(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        config_file.write_text(
            '[prober]\nendpoint = "http://localhost:8080/v1"\n'
            'ping_message = "hi"\nmax_tokens = 2\n'
            '[prober.extra_headers]\nX-Trace = "abc"\n',
            encoding="utf-8",
        )
        config = Config.load(config_file)
        assert config.prober.endpoint == "http://localhost:8080/v1"
        assert config.prober.ping_message == "hi"
        assert config.prober.max_tokens == 2
        assert config.prober.extra_headers == {"X-Trace": "abc"}

    def test_env_overrides_prober(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_RATE_LIMITER_PROBER__ENDPOINT", "http://envhost:1/v1")
        config = Config.load()
        assert config.prober.endpoint == "http://envhost:1/v1"

    def test_invalid_prober_endpoint_rejected(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('[prober]\nendpoint = "not-a-url"\n', encoding="utf-8")
        with pytest.raises(ValueError, match="endpoint"):
            Config.load(config_file)

    def test_prober_round_trips_through_save(self, tmp_path: Path):
        config_file = tmp_path / "config.toml"
        original = Config()
        original.prober.endpoint = "http://localhost:1234/v1"
        original.prober.proxy = "http://127.0.0.1:7890"
        original.save(config_file)

        loaded = Config.load(config_file)
        assert loaded.prober.endpoint == "http://localhost:1234/v1"
        assert loaded.prober.proxy == "http://127.0.0.1:7890"
