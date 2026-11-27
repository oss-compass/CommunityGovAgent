"""管理入口测试：配置默认值、环境隔离和失败任务恢复。"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
# manage.environment 在调用时导入 receiver，与部署时的导入路径保持一致。
SPEC = importlib.util.spec_from_file_location("webhook_manage", ROOT / "manage.py")
manage = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(manage)
import issue_gov.receiver as receiver


class ManageTests(unittest.TestCase):
    def setUp(self):
        """为每个管理测试创建临时配置目录，并替换管理模块的运行路径。

        Args:
            无。
        Returns:
            None；临时目录和路径替换在测试结束时自动清理。
        """
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        (self.root / "repository.yml").write_text("example/one: []\nexample/two: []\n")
        root_patch = patch("issue_gov.paths.service_root", return_value=self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        (self.root / ".env").write_text(
            "WEBHOOK_SECRET=test-secret\nATOMGIT_TOKEN=test-token\n"
            "LLM_API_KEY=test-model-key\nLLM_BASE_URL=https://models.example\nLLM_MODEL=test-model\n", encoding="utf-8",
        )
        (self.root / ".env.example").write_text((self.root / ".env").read_text(), encoding="utf-8")
        directory = patch.object(manage, "DIRECTORY", self.root)
        directory.start()
        self.addCleanup(directory.stop)
        runtime = patch.object(manage, "RUNTIME_DIRECTORY", self.runtime)
        runtime.start()
        self.addCleanup(runtime.stop)

    def test_init_creates_private_runtime_layout_and_preserves_config(self):
        """验证初始化使用私有目录和配置权限，且重复初始化保留已有配置。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        from dotenv import dotenv_values
        path = self.root / ".env"
        path.unlink()
        (self.root / ".env.example").write_text(
            (ROOT / ".env.example").read_text(), encoding="utf-8",
        )
        # 模拟首次初始化，检查宽松的系统默认权限也不会暴露运行目录。
        (self.root / "repository.yml").unlink()
        self.runtime.rmdir()
        previous_mask = os.umask(0)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                manage.initialize()
            original = path.read_bytes()
            maintainers = self.root / "repository.yml"
            initial = maintainers.read_text()
            self.assertTrue(all(line.startswith("#") for line in initial.splitlines()))
            self.assertIn("# owner/repo:", initial)
            maintainers.write_text("example/one:\n  - alice\n")
            with contextlib.redirect_stdout(io.StringIO()):
                manage.initialize()
        finally:
            os.umask(previous_mask)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(maintainers.read_text(), "example/one:\n  - alice\n")
        self.assertEqual(maintainers.stat().st_mode & 0o777, 0o600)
        self.assertRegex(dotenv_values(path)["WEBHOOK_SECRET"], r"^[0-9a-f]{64}$")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for directory in (self.runtime, *(self.runtime / name for name in ("data", "logs", "run"))):
            self.assertTrue(directory.is_dir())
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for name in ("data", "logs", "run"):
            self.assertFalse((self.root / name).exists())

    def test_init_adds_missing_maintainers_without_changing_existing_env(self):
        """验证初始化补建维护者配置并保留已有环境配置。

        Args:
            无。
        Returns:
            None；核对文件内容及新维护者配置可被读取。
        """
        path = self.root / "repository.yml"
        path.unlink()
        original = (self.root / ".env").read_bytes()
        with contextlib.redirect_stdout(io.StringIO()):
            manage.initialize()
        self.assertTrue(path.is_file())
        self.assertEqual((self.root / ".env").read_bytes(), original)
        with self.assertRaisesRegex(ValueError, "至少填写一个"):
            manage.environment()
        path.write_text("example/one:\n  - alice\n")
        self.assertEqual(receiver.Config.from_env(manage.environment()).maintainers, {"example/one": ("alice",)})

    def test_init_adds_missing_env_without_overwriting_maintainers(self):
        """验证初始化补建环境配置并保留已有维护者名单。

        Args:
            无。
        Returns:
            None；核对新配置存在及维护者文件未被覆盖。
        """
        (self.root / ".env").unlink()
        (self.root / ".env.example").write_text((ROOT / ".env.example").read_text())
        path = self.root / "repository.yml"
        path.write_text("example/one:\n  - alice\n")
        with contextlib.redirect_stdout(io.StringIO()):
            manage.initialize()
        self.assertTrue((self.root / ".env").is_file())
        self.assertEqual(path.read_text(), "example/one:\n  - alice\n")

    def test_init_adds_new_env_keys_without_overwriting_values(self):
        """升级时只补充缺失字段，保留已有值、注释、空值及自定义配置。

        Args:
            无。
        Returns:
            None；核对新增值、特殊字符、重复初始化及敏感信息不被打印。
        """
        path = self.root / ".env"
        original = ("# 用户配置\nexport WEBHOOK_SECRET='existing-secret'\n"
                    "ATOMGIT_TOKEN=existing-token\nREPOSITORIES=example/custom\n"
                    "LLM_API_KEY=\nLLM_MODEL\nCUSTOM='keep me'\n"
                    "MULTILINE='first\nNEW_SETTING=inside quoted text'")
        path.write_text(original, encoding="utf-8")
        (self.root / ".env.example").write_text(
            "WEBHOOK_SECRET=replace-with-a-random-secret\nATOMGIT_TOKEN=changed-default\n"
            "REPOSITORIES=changed/repo\nLLM_API_KEY=changed-key\nLLM_MODEL=changed-model\n"
            "LLM_BASE_URL=\nNEW_SETTING='${CUSTOM} # literal'\n"
            "NEW_MULTILINE='first\nsecond'\n# COMMENTED_OPTION=disabled\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(manage.secrets, "token_hex") as secret:
            manage.initialize()
            updated = path.read_bytes()
            manage.initialize()
        secret.assert_not_called()
        self.assertEqual(path.read_bytes(), updated)
        self.assertTrue(path.read_text().startswith(original + "\n"))
        values = manage.dotenv_values(path, interpolate=False)
        self.assertEqual(values["WEBHOOK_SECRET"], "existing-secret")
        self.assertEqual(values["ATOMGIT_TOKEN"], "existing-token")
        self.assertEqual(values["REPOSITORIES"], "example/custom")
        self.assertEqual(values["LLM_API_KEY"], "")
        self.assertIsNone(values["LLM_MODEL"])
        self.assertEqual(values["CUSTOM"], "keep me")
        self.assertEqual(values["LLM_BASE_URL"], "")
        self.assertEqual(values["NEW_SETTING"], "${CUSTOM} # literal")
        self.assertEqual(values["NEW_MULTILINE"], "first\nsecond")
        self.assertNotIn("COMMENTED_OPTION", values)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertIn("已补充配置项：LLM_BASE_URL, NEW_SETTING, NEW_MULTILINE", output.getvalue())
        for sensitive in ("existing-secret", "existing-token", "${CUSTOM} # literal"):
            self.assertNotIn(sensitive, output.getvalue())

    def test_init_generates_missing_secret_only_once(self):
        """已有配置缺少 Webhook 密码时生成一次，后续初始化保持不变。

        Args:
            无。
        Returns:
            None；验证不会补入示例密码或反复更换已有密码。
        """
        path = self.root / ".env"
        path.write_text("ATOMGIT_TOKEN=existing-token\nREPOSITORIES=example/repo\n")
        (self.root / ".env.example").write_text(
            "WEBHOOK_SECRET=replace-with-a-random-secret\nATOMGIT_TOKEN=placeholder\nREPOSITORIES=example/repo\n")
        with patch.object(manage.secrets, "token_hex", return_value="generated-secret") as secret, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            manage.initialize()
            manage.initialize()
        secret.assert_called_once_with(32)
        self.assertEqual(manage.dotenv_values(path)["WEBHOOK_SECRET"], "generated-secret")
        self.assertNotIn("generated-secret", output.getvalue())

    def test_logs_reads_runtime_files(self):
        """验证日志命令读取 runtime/logs 中的三个日志文件。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        (self.runtime / "logs").mkdir()
        paths = [self.runtime / "logs" / name for name in
                 ("supervisord.log", "webhook.log", "worker.log")]
        for path in paths:
            path.touch()
        with patch.object(sys, "argv", ["manage.py", "logs"]), \
                patch.object(manage.os, "chdir"), patch.object(manage.os, "umask"), \
                patch.object(manage.os, "execvp") as execute:
            manage.main()
        execute.assert_called_once_with("tail", ["tail", "-n", "40", "-f", *map(str, paths)])

    def test_optional_settings_can_be_omitted_and_do_not_leak_from_terminal(self):
        """验证未填写的配置采用默认值，不受终端残留服务配置影响。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        from issue_gov.receiver import Config
        inherited = {"LLM_MODEL": "wrong-model", "LLM_API_KEY": "wrong-key", "WEBHOOK_PORT": "9999",
                     "WEBHOOK_DB": "./outside/events.sqlite3"}
        with patch.dict(os.environ, inherited, clear=True):
            env = manage.environment()
        config = Config.from_env(env)
        self.assertEqual(set(config.repositories), {"example/one", "example/two"})
        self.assertEqual(config.llm_model, "test-model")
        self.assertEqual(config.agent_api_key, "test-model-key")
        self.assertEqual(config.database, "./runtime/data/events.sqlite3")
        self.assertEqual(env["WEBHOOK_PORT"], "7000")

    def test_local_port_survives_configuration_upgrade(self):
        """测试端口来自本地配置，升级补充配置时不会恢复为正式端口。"""
        with (self.root / ".env").open("a") as stream:
            stream.write("WEBHOOK_PORT=7010\n")
        (self.root / ".env.example").write_text((ROOT / ".env.example").read_text())
        with contextlib.redirect_stdout(io.StringIO()):
            manage.initialize()
        with patch.dict(os.environ, {"WEBHOOK_PORT": "7000"}):
            self.assertEqual(manage.environment()["WEBHOOK_PORT"], "7010")

    def test_invalid_port_is_rejected_before_start(self):
        """端口必须有效，防止错误配置进入服务启动命令。"""
        path = self.root / ".env"
        original = path.read_text()
        for port in ("0", "65536", "-1", "abc", "7010 --workers 9", "７０１０"):
            with self.subTest(port=port):
                path.write_text(original + f"WEBHOOK_PORT={port}\n")
                with self.assertRaisesRegex(ValueError, "WEBHOOK_PORT"):
                    manage.environment()

    def test_model_settings_are_required_from_file_even_with_inherited_values(self):
        """缺项、空值和示例值均拒绝启动配置，不能回退到终端中的旧密钥。"""
        path = self.root / ".env"
        original = path.read_text()
        settings = {"LLM_API_KEY": "test-model-key", "LLM_BASE_URL": "https://models.example",
                    "LLM_MODEL": "test-model"}
        for key, value in settings.items():
            for replacement in ("", f"{key}=\n", f"{key}=   \n", f"{key}=replace-with-value\n"):
                with self.subTest(key=key, replacement=replacement), patch.dict(os.environ, settings):
                    path.write_text(original.replace(f"{key}={value}\n", replacement))
                    with self.assertRaisesRegex(ValueError, key):
                        manage.environment()
                    with self.assertRaisesRegex(ValueError, key):
                        receiver.Config.from_file()
        path.write_text(original)

    def test_missing_repository_list_is_rejected(self):
        """验证配置文件缺少仓库列表时拒绝启动，不采用终端中的仓库配置。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        (self.root / "repository.yml").write_text("# 尚未配置仓库\n")
        with patch.dict(os.environ, {"REPOSITORIES": "outside/repo"}, clear=True):
            with self.assertRaisesRegex(ValueError, "至少填写一个"):
                manage.environment()

    def create_tasks(self):
        """创建等待、失败和完成三种状态的测试任务，并将配置指向临时数据库。

        Args:
            无。
        Returns:
            指向测试数据库的 Config 对象。
        """
        from issue_gov.receiver import Config, connect, initialize
        with (self.root / ".env").open("a") as stream:
            stream.write(f"WEBHOOK_DB={self.root / 'events.sqlite3'}\n")
        config = Config.from_env(manage.environment())
        initialize(config)
        with connect(config) as db:
            db.executemany(
                "INSERT INTO events(event_id, repository, issue_number, state, attempts, updated_at, result, last_error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [("one", "example/one", 12, "queued", 0, 1, None, None),
                 ("two", "example/two", 12, "failed", 2, 2, None, "AtomGit 请求失败，HTTP 403"),
                 ("three", "example/one", 13, "completed", 1, 3,
                  json.dumps({"status": "pass", "comment_action": "updated"}), None)],
            )
        return config

    def test_retry_failed_calls_core_function_without_starting_process(self):
        """验证失败任务恢复会重置状态和尝试次数，且不启动新进程。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        from issue_gov.receiver import queue_snapshot
        config = self.create_tasks()
        with patch.object(sys, "argv", ["manage.py", "retry-failed"]), \
                patch.object(manage.os, "chdir"), patch.object(manage.os, "umask"), \
                patch.object(manage.subprocess, "run") as run, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            manage.main()
        run.assert_not_called()
        self.assertEqual(json.loads(output.getvalue()), {"requeued": 1})
        tasks = {row["issue_number"]: row for row in queue_snapshot(config)["recent"]
                 if row["repository"] == "example/two"}
        self.assertEqual(tasks[12]["state"], "queued")
        self.assertEqual(tasks[12]["attempts"], 0)

    def test_status_shows_processes_counts_and_recent_errors_without_writes(self):
        """验证状态输出包含进程、任务统计和处理结果，且不会修改数据库。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        from issue_gov.receiver import queue_snapshot
        config = self.create_tasks()
        before = queue_snapshot(config)
        db_before = Path(config.database).read_bytes()
        result = Mock(returncode=0, stdout="issue-gov-webhook RUNNING pid 1\nissue-gov-worker RUNNING pid 2")
        with patch.object(manage, "running", return_value=True), \
                patch.object(manage, "control", return_value=result), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            manage.status()
        text = output.getvalue()
        for expected in ("RUNNING", "example/one", "example/two", "等待处理 1", "处理失败 1",
                         "已完成 1", "已更新评论", "AtomGit 请求失败，HTTP 403", "#12"):
            self.assertIn(expected, text)
        self.assertEqual(before, queue_snapshot(config))
        self.assertEqual(db_before, Path(config.database).read_bytes())

    def test_superseded_requests_count_as_completed_and_are_not_retried(self):
        """已覆盖请求保留原因、计入已完成，并且不被手动重试重新激活。

        Args:
            无。
        Returns:
            None；验证 status 汇总、历史展示和 retry-failed 的处理范围。
        """
        config = self.create_tasks()
        with receiver.connect(config) as db:
            db.execute("""INSERT INTO events(event_id, repository, issue_number, state, updated_at, last_error)
                          VALUES ('covered', 'example/one', 12, 'superseded', 4, '历史错误')""")
        with patch.object(manage, 'running', return_value=True), \
                patch.object(manage, 'control', return_value=Mock(returncode=0, stdout='RUNNING')), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            manage.status()
        self.assertIn('已完成 2', output.getvalue())
        self.assertIn('已被新请求覆盖', output.getvalue())
        self.assertIn('历史错误', output.getvalue())
        from issue_gov.worker import retry_failed
        self.assertEqual(retry_failed(config), 1)
        with receiver.connect(config) as db:
            self.assertEqual(db.execute("SELECT state FROM events WHERE event_id='covered'").fetchone()[0],
                             'superseded')

    def test_stopped_service_still_shows_saved_tasks(self):
        """验证服务停止后仍显示已保存任务，并返回非零退出码。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        self.create_tasks()
        with patch.object(manage, "running", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result:
                manage.status()
        self.assertEqual(result.exception.code, 1)
        self.assertIn("服务未运行", output.getvalue())
        self.assertIn("AtomGit 请求失败，HTTP 403", output.getvalue())

    def test_status_does_not_create_missing_database(self):
        """验证查询状态时不会创建不存在的数据库或其目录。

        Args:
            无。
        Returns:
            None；通过断言验证上述行为，结果不符时测试失败。
        """
        database = self.root / "missing" / "events.sqlite3"
        with (self.root / ".env").open("a") as stream:
            stream.write(f"WEBHOOK_DB={database}\n")
        with patch.object(manage, "running", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit):
                manage.status()
        self.assertIn("数据库尚未创建", output.getvalue())
        self.assertFalse(database.parent.exists())


    def test_start_waits_for_both_programs_and_passes_configuration(self):
        """验证启动时传入服务环境，并等待接收和处理进程全部就绪才报告成功。

        Args:
            无。
        Returns:
            None；配置传递或就绪判断错误时测试失败。
        """
        env = {"WEBHOOK_SECRET": "test-secret", "PATH": "/test/bin"}
        statuses = [Mock(returncode=0, stdout="webhook RUNNING\nworker STARTING"),
                    Mock(returncode=0, stdout="webhook RUNNING\nworker RUNNING")]
        with patch.object(manage, "running", return_value=False), patch.object(
            manage.subprocess, "run",
        ) as launch, patch.object(manage, "control", side_effect=statuses) as control, patch.object(
            manage.time, "monotonic", side_effect=[0, 1, 2],
        ), patch.object(manage.time, "sleep") as sleep, contextlib.redirect_stdout(io.StringIO()) as output:
            manage.start(env)
        launch.assert_called_once()
        self.assertEqual(launch.call_args.kwargs["env"], env)
        self.assertTrue(launch.call_args.kwargs["check"])
        self.assertEqual(control.call_count, 2)
        sleep.assert_called_once_with(0.5)
        self.assertIn("服务已启动，正在后台运行", output.getvalue())

    def test_start_preserves_existing_service_and_reports_failure(self):
        """验证重复启动不创建新进程，启动失败和就绪超时均明确报错。

        Args:
            无。
        Returns:
            None；重复启动或将失败误报为成功时测试失败。
        """
        with patch.object(manage, "running", return_value=True), patch.object(manage, "status") as status, patch.object(
            manage.subprocess, "run",
        ) as launch, contextlib.redirect_stdout(io.StringIO()):
            manage.start({})
        launch.assert_not_called()
        status.assert_called_once()
        for state, clock in (("worker FATAL", [0, 1]), ("worker STARTING", [0, 1, 21])):
            with self.subTest(state=state), patch.object(manage, "running", return_value=False), patch.object(
                manage.subprocess, "run",
            ), patch.object(manage, "control", return_value=Mock(returncode=1, stdout=state)), patch.object(
                manage.time, "monotonic", side_effect=clock,
            ), patch.object(manage.time, "sleep"), self.assertRaisesRegex(RuntimeError, "未能就绪"):
                manage.start({})
        with patch.object(manage, "running", return_value=False), patch.object(
            manage.subprocess, "run", side_effect=manage.subprocess.CalledProcessError(1, "supervisord"),
        ), patch.object(manage, "control") as control, self.assertRaises(manage.subprocess.CalledProcessError):
            manage.start({})
        control.assert_not_called()

    def test_stop_waits_for_socket_removal_and_handles_failure(self):
        """验证停止等待管理套接字消失，关闭失败及超时均不会误报成功。

        Args:
            无。
        Returns:
            None；停止状态判断错误时测试失败。
        """
        with patch.object(manage, "running", return_value=False), patch.object(manage, "control") as control, contextlib.redirect_stdout(io.StringIO()):
            manage.stop()
        control.assert_not_called()
        for code, exists, clock, message in (
            (0, [True, False], [0, 1, 2], None),
            (1, [], [], "无法停止"),
            (0, [True], [0, 1, 66], "停止服务超时"),
        ):
            with self.subTest(message=message), patch.object(manage, "running", return_value=True), patch.object(
                manage, "control", return_value=Mock(returncode=code),
            ) as control, patch.object(Path, "exists", side_effect=exists), patch.object(
                manage.time, "monotonic", side_effect=clock,
            ), patch.object(manage.time, "sleep"), contextlib.redirect_stdout(io.StringIO()) as output:
                if message:
                    with self.assertRaisesRegex(RuntimeError, message):
                        manage.stop()
                else:
                    manage.stop()
                    self.assertIn("服务已停止", output.getvalue())
                control.assert_called_once_with("shutdown")

    def test_restart_updates_both_processes_after_supplementing_configuration(self):
        """统一重启先补配置并检查，再更新后台和接收端，不停止整个服务。

        Args:
            无。
        Returns:
            None；验证补充项、原值保留和两项服务操作顺序。
        """
        template = self.root / ".env.example"
        template.write_text(template.read_text() + "LLM_MODEL=new-default\n")
        path = self.root / ".env"
        path.write_text(path.read_text().replace("LLM_MODEL=test-model\n", ""))
        calls = Mock()
        with patch.object(sys, "argv", ["manage.py", "restart"]), patch.object(manage.os, "chdir"), patch.object(
                manage.os, "umask"), patch.object(manage, "running", return_value=True), patch.object(
                manage.subprocess, "run", return_value=Mock(returncode=0)) as check, patch.object(
                manage, "control", return_value=Mock(returncode=0)) as control, patch.object(manage, "stop") as stop, patch.object(
                manage, "start") as start, contextlib.redirect_stdout(io.StringIO()):
            calls.attach_mock(check, "check")
            calls.attach_mock(control, "control")
            manage.main()
        self.assertEqual([entry[0] for entry in calls.mock_calls], ["check", "control", "control"])
        self.assertEqual([entry.args for entry in control.call_args_list],
                         [("restart", "issue-gov-worker"), ("signal", "HUP", "issue-gov-webhook")])
        self.assertIn("import issue_gov.worker", check.call_args.args[0][-1])
        self.assertIn("Config.from_file()", check.call_args.args[0][-1])
        self.assertEqual(check.call_args.kwargs["env"]["LLM_MODEL"], "new-default")
        self.assertEqual(receiver.Config.from_file().llm_model, "new-default")
        self.assertEqual(receiver.Config.from_file().secret, "test-secret")
        stop.assert_not_called()
        start.assert_not_called()

    def test_restart_preflight_failure_does_not_touch_running_services(self):
        """配置或新代码检查失败时，保留当前运行中的服务。

        Args:
            无。
        Returns:
            None；验证错误退出且不发送重启或重载命令。
        """
        for failure in ("configuration", "code", "stopped"):
            with self.subTest(failure=failure), patch.object(sys, "argv", ["manage.py", "restart"]), patch.object(
                    manage.os, "chdir"), patch.object(manage.os, "umask"), patch.object(
                    manage, "environment", side_effect=ValueError("配置无效") if failure == "configuration" else None,
                    return_value={}), patch.object(manage, "running", return_value=failure != "stopped"), patch.object(
                    manage.subprocess, "run", return_value=Mock(returncode=1)), patch.object(
                    manage, "control") as control, contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    manage.main()
            self.assertEqual(error.exception.code, 1)
            control.assert_not_called()

    def test_restart_reports_failed_stage_without_claiming_complete_success(self):
        """后台失败时不重载接收端，重载信号失败时说明后台已更新。

        Args:
            无。
        Returns:
            None；验证两处控制失败都以非零状态退出并准确说明已执行的操作。
        """
        for codes, message in (([1], "后台重启失败"), ([0, 1], "后台已重启，但接收端重载信号发送失败")):
            error_output = io.StringIO()
            with self.subTest(codes=codes), patch.object(sys, "argv", ["manage.py", "restart"]), patch.object(
                    manage.os, "chdir"), patch.object(manage.os, "umask"), patch.object(manage, "running", return_value=True), patch.object(
                    manage.subprocess, "run", return_value=Mock(returncode=0)), patch.object(
                    manage, "control", side_effect=[Mock(returncode=code) for code in codes]) as control, contextlib.redirect_stderr(
                    error_output), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    manage.main()
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(control.call_count, len(codes))
            self.assertIn(message, error_output.getvalue())

    def test_new_process_reads_current_file_without_inherited_stale_settings(self):
        """后台重启和接收端重载重新读取文件，删除配置也不会恢复父进程旧值。

        Args:
            无。
        Returns:
            None；验证新值生效、删除值清空及无效配置被拒绝。
        """
        path = self.root / ".env"
        original = path.read_text()
        with patch.dict(os.environ, {"LLM_MODEL": "old-model", "WEBHOOK_SECRET": "old-secret"}):
            path.write_text(original + "LLM_MODEL=new-model\n")
            self.assertEqual(receiver.Config.from_file().llm_model, "new-model")
            path.write_text(original)
            self.assertEqual(receiver.Config.from_file().llm_model, "test-model")
            path.write_text(original.replace("LLM_MODEL=test-model\n", ""))
            with self.assertRaisesRegex(ValueError, "LLM_MODEL"):
                receiver.Config.from_file()
            path.write_text(original.replace("WEBHOOK_SECRET=test-secret\n", ""))
            with self.assertRaisesRegex(ValueError, "WEBHOOK_SECRET"):
                receiver.Config.from_file()
            path.unlink()
            with self.assertRaisesRegex(ValueError, "init"):
                receiver.Config.from_file()


if __name__ == "__main__":
    unittest.main()
