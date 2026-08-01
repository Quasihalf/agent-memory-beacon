import codecs
import json
import os
import pathlib
import plistlib
import stat
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import install_beacon_sync


TASK_NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
HOST_PATH_CLASS = pathlib.WindowsPath if os.name == "nt" else pathlib.PosixPath


def _decode_windows_task_payload(payload):
    return bytes(payload).decode("utf-16")


class InstallBeaconSyncTests(unittest.TestCase):
    def setUp(self):
        self.python = "C:/Program Files/Python/python.exe"
        self.script = "C:/Program Files/Agent Memory Beacon/beacon_sync.py"
        self.config = "C:/Users/demo/Agent Memory Beacon/config.yaml"

    def _windows_task_runner(self, initial_xml):
        state = {"xml": initial_xml}
        calls = []
        created_payloads = []

        def runner(arguments):
            calls.append(list(arguments))
            if "/Query" in arguments:
                if state["xml"] is None:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="ERROR: The system cannot find the file specified.",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=state["xml"],
                    stderr="",
                )
            if "/Create" in arguments:
                xml_path = Path(arguments[arguments.index("/XML") + 1])
                payload = xml_path.read_bytes()
                created_payloads.append(payload)
                state["xml"] = _decode_windows_task_payload(payload)
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            if "/Delete" in arguments:
                state["xml"] = None
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(f"unexpected scheduler command: {arguments}")

        return runner, state, calls, created_payloads

    def test_launchd_plist_is_current_user_periodic_and_ignore_overlap(self):
        payload = install_beacon_sync.build_launchd_plist(
            python_path="/runtime/.venv/bin/python",
            script_path="/runtime/scripts/beacon_sync.py",
            config_path="/runtime/scripts/config.yaml",
            log_dir="/vault/04-Feedback/_logs",
            interval_seconds=60,
        )

        self.assertEqual(
            payload["ProgramArguments"],
            [
                "/runtime/.venv/bin/python",
                "-E",
                "-s",
                "-X",
                "utf8",
                "-B",
                "/runtime/scripts/beacon_sync.py",
                "--config",
                "/runtime/scripts/config.yaml",
                "run",
            ],
        )
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(payload["StartInterval"], 60)
        self.assertFalse(payload["KeepAlive"])
        self.assertEqual(payload["ProcessType"], "Background")
        self.assertFalse(
            any(
                key.upper().startswith("PYTHON")
                for key in payload["EnvironmentVariables"]
            )
        )

    def test_windows_install_rejects_unsupported_atomic_filesystem_api(self):
        sync_cfg = {
            "enabled": True,
            "role": "producer-replica",
        }
        with (
            mock.patch.object(
                install_beacon_sync,
                "load_beacon_sync_config",
                return_value=sync_cfg,
            ),
            mock.patch.object(install_beacon_sync.sys, "platform", "win32"),
            mock.patch.object(install_beacon_sync.os, "name", "nt"),
            mock.patch.object(
                install_beacon_sync,
                "Path",
                HOST_PATH_CLASS,
            ),
            mock.patch.object(
                install_beacon_sync,
                "_assert_supported_windows_atomic_filesystem",
                side_effect=install_beacon_sync.ProtocolError(
                    "Windows build 17763 or newer is required"
                ),
            ),
        ):
            with self.assertRaisesRegex(
                install_beacon_sync.InstallerError,
                "17763",
            ):
                install_beacon_sync.main(["--config", "config.yaml"])

    def test_windows_main_binds_only_manifest_verified_stable_runtime(self):
        sync_cfg = {
            "enabled": True,
            "role": "producer-replica",
        }
        binding = {
            "release_id": "release-a1b2c3d4",
            "python_path": (
                "C:/Users/demo/AppData/Local/AgentMemoryBeacon/"
                "runtime/releases/a1b2c3d4/.venv/Scripts/python.exe"
            ),
            "script_path": (
                "C:/Users/demo/AppData/Local/AgentMemoryBeacon/"
                "runtime/releases/a1b2c3d4/scripts/beacon_sync.py"
            ),
            "config_path": (
                "C:/Users/demo/AppData/Local/AgentMemoryBeacon/"
                "runtime/releases/a1b2c3d4/scripts/config.yaml"
            ),
        }
        with (
            mock.patch.object(
                install_beacon_sync,
                "load_beacon_sync_config",
                return_value=sync_cfg,
            ),
            mock.patch.object(install_beacon_sync.sys, "platform", "win32"),
            mock.patch.object(install_beacon_sync.os, "name", "nt"),
            mock.patch.object(
                install_beacon_sync,
                "Path",
                HOST_PATH_CLASS,
            ),
            mock.patch.object(
                install_beacon_sync,
                "_assert_supported_windows_atomic_filesystem",
            ),
            mock.patch.object(
                install_beacon_sync,
                "prepare_windows_runtime",
                return_value=binding,
            ) as prepare,
            mock.patch.object(
                install_beacon_sync,
                "_current_windows_user",
                return_value="S-1-5-21-100-200-300-1001",
            ),
            mock.patch.object(
                install_beacon_sync,
                "install_windows_components",
                return_value=[],
            ) as install,
        ):
            exit_code = install_beacon_sync.main(
                [
                    "--config",
                    "config.yaml",
                    "--runtime-root",
                    "C:/stable/runtime",
                ]
            )

        self.assertEqual(exit_code, 0)
        prepare.assert_called_once()
        kwargs = install.call_args.kwargs
        self.assertEqual(kwargs["python_path"], binding["python_path"])
        self.assertEqual(kwargs["script_path"], binding["script_path"])
        self.assertEqual(kwargs["config_path"], binding["config_path"])
        self.assertNotEqual(kwargs["python_path"], sys.executable)

    def test_windows_main_does_not_change_task_or_hooks_when_runtime_fails(self):
        sync_cfg = {
            "enabled": True,
            "role": "producer-replica",
        }
        with (
            mock.patch.object(
                install_beacon_sync,
                "load_beacon_sync_config",
                return_value=sync_cfg,
            ),
            mock.patch.object(install_beacon_sync.sys, "platform", "win32"),
            mock.patch.object(install_beacon_sync.os, "name", "nt"),
            mock.patch.object(
                install_beacon_sync,
                "Path",
                HOST_PATH_CLASS,
            ),
            mock.patch.object(
                install_beacon_sync,
                "_assert_supported_windows_atomic_filesystem",
            ),
            mock.patch.object(
                install_beacon_sync,
                "prepare_windows_runtime",
                side_effect=install_beacon_sync.InstallerError(
                    "runtime verification failed"
                ),
            ),
            mock.patch.object(
                install_beacon_sync,
                "install_windows_components",
            ) as install,
        ):
            with self.assertRaisesRegex(
                install_beacon_sync.InstallerError,
                "runtime verification failed",
            ):
                install_beacon_sync.main(["--config", "config.yaml"])

        install.assert_not_called()

    def test_prepare_windows_runtime_publishes_verified_release_and_keeps_old(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            old_release = runtime_root / "releases" / "old-release"
            old_release.mkdir(parents=True)
            (old_release / "sentinel.txt").write_text(
                "old",
                encoding="utf-8",
            )
            install_root = runtime_root / "releases" / ("a" * 16)
            stage = runtime_root / "releases" / ".staging"
            stage.mkdir()
            plan = SimpleNamespace(
                install_root=install_root,
                release_id="a" * 16,
            )
            staged = SimpleNamespace(root=stage)
            cfg = {
                "enabled": True,
                "role": "producer-replica",
                "transcript_paths": [],
                "codex_sessions_path": "",
                "claude_project_path": "",
            }

            def publish(source, destination):
                source.rename(destination)

            with (
                mock.patch.object(
                    install_beacon_sync,
                    "load_beacon_sync_config",
                    return_value=cfg,
                ),
                mock.patch(
                    "install_runtime.build_windows_sync_release_plan",
                    return_value=plan,
                ),
                mock.patch(
                    "install_runtime.stage_runtime",
                    return_value=staged,
                ),
                mock.patch("install_runtime._validate_staged_runtime"),
                mock.patch(
                    "install_runtime._durable_replace",
                    side_effect=publish,
                ) as durable_replace,
                mock.patch(
                    "install_runtime.verify_installed_release",
                    return_value={"release_id": "a" * 16},
                ) as verify,
            ):
                result = install_beacon_sync.prepare_windows_runtime(
                    config_path=root / "config.yaml",
                    runtime_root=runtime_root,
                    source_python=sys.executable,
                )

            self.assertEqual(result["release_id"], "a" * 16)
            self.assertTrue(install_root.is_dir())
            self.assertTrue((old_release / "sentinel.txt").is_file())
            durable_replace.assert_called_once_with(stage, install_root)
            verify.assert_called_once_with(install_root)

    def test_prepare_windows_runtime_accepts_minimal_portable_sync_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.yaml"
            config_path.write_text(
                json.dumps(
                    {
                        "codex_sessions_path": str(root / "codex-sessions"),
                        "beacon_sync": {
                            "enabled": True,
                            "role": "producer-replica",
                            "device_id": "windows-one",
                            "state_dir": str(root / "state"),
                            "outbox_dir": str(root / "outbox"),
                            "received_published_dir": str(root / "received"),
                            "replica_path": str(root / "replica"),
                        },
                    }
                ),
                encoding="utf-8",
            )
            install_root = root / "runtime" / "releases" / ("c" * 16)
            install_root.mkdir(parents=True)
            plan = SimpleNamespace(
                install_root=install_root,
                release_id="c" * 16,
            )

            with (
                mock.patch.object(
                    install_beacon_sync,
                    "load_config",
                    side_effect=AssertionError("portable install used Mac config loader"),
                ),
                mock.patch(
                    "install_runtime.build_windows_sync_release_plan",
                    return_value=plan,
                ) as build_plan,
                mock.patch(
                    "install_runtime.verify_installed_release",
                    return_value={"release_id": "c" * 16},
                ),
            ):
                result = install_beacon_sync.prepare_windows_runtime(
                    config_path=config_path,
                    runtime_root=root / "runtime",
                    source_python=sys.executable,
                )

            self.assertEqual(result["release_id"], "c" * 16)
            runtime_cfg = build_plan.call_args.args[2]
            self.assertEqual(
                runtime_cfg["codex_sessions_path"],
                str(root / "codex-sessions"),
            )
            self.assertEqual(
                runtime_cfg["beacon_sync"]["role"],
                "producer-replica",
            )
            self.assertNotIn("transcript_paths", runtime_cfg["beacon_sync"])

    def test_prepare_windows_runtime_rejects_corrupt_existing_release(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            install_root = root / "runtime" / "releases" / ("b" * 16)
            install_root.mkdir(parents=True)
            plan = SimpleNamespace(
                install_root=install_root,
                release_id="b" * 16,
            )
            cfg = {
                "enabled": True,
                "role": "producer-replica",
                "transcript_paths": [],
                "codex_sessions_path": "",
                "claude_project_path": "",
            }
            with (
                mock.patch.object(
                    install_beacon_sync,
                    "load_beacon_sync_config",
                    return_value=cfg,
                ),
                mock.patch(
                    "install_runtime.build_windows_sync_release_plan",
                    return_value=plan,
                ),
                mock.patch(
                    "install_runtime.verify_installed_release",
                    side_effect=ValueError("runtime release file changed"),
                ),
                mock.patch("install_runtime.stage_runtime") as stage,
                mock.patch("install_runtime._durable_replace") as publish,
            ):
                with self.assertRaisesRegex(ValueError, "file changed"):
                    install_beacon_sync.prepare_windows_runtime(
                        config_path=root / "config.yaml",
                        runtime_root=root / "runtime",
                        source_python=sys.executable,
                    )

            stage.assert_not_called()
            publish.assert_not_called()

    def test_prepare_windows_runtime_removes_only_new_failed_publish(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            releases = runtime_root / "releases"
            old_release = releases / "old-release"
            old_release.mkdir(parents=True)
            (old_release / "sentinel").write_bytes(b"old")
            install_root = releases / ("d" * 16)
            stage = releases / ".stage"
            stage.mkdir()
            (stage / "sentinel").write_bytes(b"new")
            plan = SimpleNamespace(install_root=install_root, release_id="d" * 16)
            staged = SimpleNamespace(
                root=stage,
                release_id="d" * 16,
                manifest_path=stage / "release-manifest.json",
            )
            cfg = {
                "enabled": True,
                "role": "producer-replica",
                "transcript_paths": [],
                "codex_sessions_path": "",
                "claude_project_path": "",
            }

            with (
                mock.patch.object(
                    install_beacon_sync,
                    "load_beacon_sync_config",
                    return_value=cfg,
                ),
                mock.patch(
                    "install_runtime.build_windows_sync_release_plan",
                    return_value=plan,
                ),
                mock.patch("install_runtime.stage_runtime", return_value=staged),
                mock.patch("install_runtime._validate_staged_runtime"),
                mock.patch(
                    "install_runtime._durable_replace",
                    side_effect=lambda source, destination: source.rename(destination),
                ),
                mock.patch(
                    "install_runtime.verify_installed_release",
                    side_effect=ValueError("post-publish verification failed"),
                ),
            ):
                with self.assertRaisesRegex(ValueError, "post-publish"):
                    install_beacon_sync.prepare_windows_runtime(
                        config_path=root / "config.yaml",
                        runtime_root=runtime_root,
                        source_python=sys.executable,
                    )

            self.assertFalse(install_root.exists())
            self.assertEqual((old_release / "sentinel").read_bytes(), b"old")

    def test_prepare_windows_runtime_holds_cross_process_install_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            install_root = runtime_root / "releases" / ("e" * 16)
            install_root.mkdir(parents=True)
            plan = SimpleNamespace(install_root=install_root, release_id="e" * 16)
            cfg = {
                "enabled": True,
                "role": "producer-replica",
                "transcript_paths": [],
                "codex_sessions_path": "",
                "claude_project_path": "",
            }

            with (
                mock.patch.object(
                    install_beacon_sync,
                    "load_beacon_sync_config",
                    return_value=cfg,
                ),
                mock.patch(
                    "install_runtime.build_windows_sync_release_plan",
                    return_value=plan,
                ),
                mock.patch(
                    "install_runtime.verify_installed_release",
                    return_value={"release_id": "e" * 16},
                ),
                mock.patch.object(
                    install_beacon_sync,
                    "portable_file_lock",
                    create=True,
                ) as lock,
            ):
                install_beacon_sync.prepare_windows_runtime(
                    config_path=root / "config.yaml",
                    runtime_root=runtime_root,
                    source_python=sys.executable,
                )

            lock.assert_called_once_with(
                runtime_root / ".install.lock",
                root=runtime_root,
            )

    def test_prepare_windows_runtime_cleans_release_when_durable_rename_raises_after_move(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            releases = runtime_root / "releases"
            install_root = releases / ("f" * 16)
            stage = releases / ".stage"
            stage.mkdir(parents=True)
            (stage / "sentinel").write_bytes(b"new")
            plan = SimpleNamespace(install_root=install_root, release_id="f" * 16)
            staged = SimpleNamespace(
                root=stage,
                release_id="f" * 16,
                manifest_path=stage / "release-manifest.json",
            )
            cfg = {
                "enabled": True,
                "role": "producer-replica",
                "transcript_paths": [],
                "codex_sessions_path": "",
                "claude_project_path": "",
            }

            def move_then_fail(source, destination):
                source.rename(destination)
                raise OSError("directory durability failed")

            with (
                mock.patch.object(
                    install_beacon_sync,
                    "load_beacon_sync_config",
                    return_value=cfg,
                ),
                mock.patch(
                    "install_runtime.build_windows_sync_release_plan",
                    return_value=plan,
                ),
                mock.patch("install_runtime.stage_runtime", return_value=staged),
                mock.patch("install_runtime._validate_staged_runtime"),
                mock.patch(
                    "install_runtime._durable_replace",
                    side_effect=move_then_fail,
                ),
                mock.patch("install_runtime.verify_installed_release") as verify,
            ):
                with self.assertRaisesRegex(OSError, "durability"):
                    install_beacon_sync.prepare_windows_runtime(
                        config_path=root / "config.yaml",
                        runtime_root=runtime_root,
                        source_python=sys.executable,
                    )

            verify.assert_not_called()
            self.assertFalse(stage.exists())
            self.assertFalse(install_root.exists())

    def test_windows_task_is_current_user_utf8_ignore_new_and_has_two_triggers(self):
        xml = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id="DESKTOP\\demo",
            interval_minutes=2,
        )
        root = ET.fromstring(xml)

        self.assertEqual(
            root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=TASK_NS),
            "IgnoreNew",
        )
        self.assertEqual(
            root.findtext("t:Principals/t:Principal/t:UserId", namespaces=TASK_NS),
            "DESKTOP\\demo",
        )
        self.assertEqual(
            root.findtext("t:Principals/t:Principal/t:RunLevel", namespaces=TASK_NS),
            "LeastPrivilege",
        )
        self.assertIsNotNone(root.find("t:Triggers/t:LogonTrigger", TASK_NS))
        self.assertIsNotNone(root.find("t:Triggers/t:CalendarTrigger", TASK_NS))
        self.assertEqual(
            root.findtext(
                "t:RegistrationInfo/t:URI",
                namespaces=TASK_NS,
            ),
            install_beacon_sync.WINDOWS_TASK_OWNER_URI,
        )
        self.assertEqual(
            root.findtext(
                "t:RegistrationInfo/t:Description",
                namespaces=TASK_NS,
            ),
            install_beacon_sync.WINDOWS_TASK_OWNER_DESCRIPTION,
        )
        command = root.findtext("t:Actions/t:Exec/t:Command", namespaces=TASK_NS)
        arguments = root.findtext("t:Actions/t:Exec/t:Arguments", namespaces=TASK_NS)
        self.assertEqual(command, self.python)
        self.assertIn("-E", arguments)
        self.assertIn("-s", arguments)
        self.assertIn("-X utf8", arguments)
        self.assertIn(f'"{self.script}"', arguments)
        self.assertIn(f'"{self.config}"', arguments)
        self.assertNotIn("SYSTEM", xml.upper())
        self.assertNotIn("cmd.exe", xml.lower())

        hook_command = install_beacon_sync.build_collector_command(
            self.python,
            self.script,
            self.config,
        )
        self.assertIn("-E", hook_command)
        self.assertIn("-s", hook_command)
        self.assertIn("-X utf8", hook_command)
        self.assertIn("-B", hook_command)

    def test_windows_task_registration_uri_tracks_custom_task_name(self):
        task_name = r"Agent Memory Beacon CI 1234"
        xml = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
            task_name=task_name,
        )
        root = ET.fromstring(xml)

        self.assertEqual(
            root.findtext(
                "t:RegistrationInfo/t:URI",
                namespaces=TASK_NS,
            ),
            rf"\{task_name}",
        )
        self.assertTrue(
            install_beacon_sync._owned_windows_task_xml(
                xml,
                task_name=task_name,
            )
        )
        runner, state, calls, _payloads = self._windows_task_runner(None)

        result = install_beacon_sync.install_windows_scheduler(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
            task_name=task_name,
            command_runner=runner,
        )

        self.assertTrue(result["changed"])
        self.assertTrue(
            install_beacon_sync._owned_windows_task_xml(
                state["xml"],
                task_name=task_name,
            )
        )
        create = next(arguments for arguments in calls if "/Create" in arguments)
        self.assertEqual(create[create.index("/TN") + 1], task_name)

    def test_current_windows_user_reads_process_token_and_releases_resources(self):
        class Box:
            def __init__(self, value=0):
                self.value = value

        class TokenBuffer:
            def __init__(self, size):
                self.size = size
                self.sid = None

        class Pointer:
            def __init__(self, value):
                self.value = value

            def __getitem__(self, index):
                if index != 0:
                    raise IndexError(index)
                return self.value

        class Function:
            def __init__(self, callback):
                self.callback = callback
                self.argtypes = None
                self.restype = None
                self.calls = []

            def __call__(self, *args):
                self.calls.append(args)
                return self.callback(*args)

        def open_process_token(_process, access, token):
            self.assertEqual(access, 0x0008)
            token.value = 1234
            return True

        def get_token_information(
            _token,
            information_class,
            buffer,
            buffer_size,
            required_size,
        ):
            self.assertEqual(information_class, 1)
            if buffer is None:
                self.assertEqual(buffer_size, 0)
                required_size.value = 48
                return False
            self.assertEqual(buffer_size, 48)
            buffer.sid = 5678
            return True

        def convert(_sid, string_sid):
            self.assertEqual(_sid, 5678)
            string_sid.value = "S-1-5-21-100-200-300-1001"
            return True

        get_current_process = Function(lambda: -1)
        open_token = Function(open_process_token)
        get_token = Function(get_token_information)
        convert_function = Function(convert)
        close_handle = Function(lambda _handle: True)
        local_free = Function(lambda _pointer: 0)
        advapi32 = SimpleNamespace(
            OpenProcessToken=open_token,
            GetTokenInformation=get_token,
            ConvertSidToStringSidW=convert_function,
        )
        kernel32 = SimpleNamespace(
            GetCurrentProcess=get_current_process,
            CloseHandle=close_handle,
            LocalFree=local_free,
        )
        wintypes = SimpleNamespace(
            LPVOID=object(),
            LPWSTR=Box,
            DWORD=Box,
            BOOL=object(),
            HANDLE=Box,
            HLOCAL=object(),
        )
        ctypes_module = ModuleType("ctypes")
        ctypes_module.wintypes = wintypes
        ctypes_module.WinDLL = lambda name, **_kwargs: (
            advapi32 if name == "advapi32" else kernel32
        )
        ctypes_module.POINTER = lambda value: ("pointer", value)
        ctypes_module.byref = lambda value: value
        ctypes_module.create_string_buffer = TokenBuffer
        ctypes_module.get_last_error = lambda: 122
        ctypes_module.WinError = lambda error: OSError(error, "win32")
        ctypes_module.cast = lambda value, _kind: (
            Pointer(value.sid) if isinstance(value, TokenBuffer) else value
        )

        with (
            mock.patch.object(install_beacon_sync.os, "name", "nt"),
            mock.patch.dict(
                install_beacon_sync.os.environ,
                {
                    "LOGNAME": "wrong-logname",
                    "USER": "wrong-user",
                    "USERNAME": "wrong-username",
                    "USERDOMAIN": "WRONG-DOMAIN",
                },
                clear=True,
            ),
            mock.patch.dict(
                sys.modules,
                {"ctypes": ctypes_module, "ctypes.wintypes": wintypes},
            ),
        ):
            sid = install_beacon_sync._current_windows_user()

        self.assertEqual(sid, "S-1-5-21-100-200-300-1001")
        self.assertEqual(get_current_process.calls, [()])
        self.assertEqual(len(open_token.calls), 1)
        self.assertEqual(len(get_token.calls), 2)
        self.assertEqual(get_token.calls[0][2:4], (None, 0))
        self.assertEqual(len(close_handle.calls), 1)
        self.assertEqual(local_free.argtypes, (wintypes.HLOCAL,))
        self.assertIs(local_free.restype, wintypes.HLOCAL)
        self.assertEqual(len(local_free.calls), 1)
        self.assertEqual(close_handle.argtypes, (wintypes.HANDLE,))
        self.assertIs(close_handle.restype, wintypes.BOOL)

    def test_windows_task_uses_windows_working_directory_on_non_windows_host(self):
        script = r"C:\Program Files\Agent Memory Beacon\beacon_sync.py"

        xml = install_beacon_sync.build_windows_task_xml(
            python_path=r"C:\Python\python.exe",
            script_path=script,
            config_path=r"C:\Beacon\config.yaml",
            user_id=r"DESKTOP\demo",
        )
        root = ET.fromstring(xml)

        self.assertEqual(
            root.findtext(
                "t:Actions/t:Exec/t:WorkingDirectory",
                namespaces=TASK_NS,
            ),
            r"C:\Program Files\Agent Memory Beacon",
        )

    def test_windows_interval_and_user_are_required(self):
        with self.assertRaises(ValueError):
            install_beacon_sync.build_windows_task_xml(
                python_path=self.python,
                script_path=self.script,
                config_path=self.config,
                user_id="",
                interval_minutes=1,
            )
        with self.assertRaises(ValueError):
            install_beacon_sync.build_windows_task_xml(
                python_path=self.python,
                script_path=self.script,
                config_path=self.config,
                user_id="demo",
                interval_minutes=0,
            )

    def test_hook_merge_preserves_third_party_order_and_is_idempotent(self):
        third_party = {
            "matcher": "all",
            "hooks": [{"type": "command", "command": "node third.js", "keep": 1}],
        }
        document = {"other": {"keep": True}, "hooks": {"Stop": [third_party]}}
        command = install_beacon_sync.build_collector_command(
            self.python,
            self.script,
            self.config,
        )

        merged, changed = install_beacon_sync.merge_collector_hooks(
            document,
            command,
        )
        merged_again, changed_again = install_beacon_sync.merge_collector_hooks(
            merged,
            command,
        )

        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(merged["other"], {"keep": True})
        self.assertEqual(merged["hooks"]["Stop"][0], third_party)
        self.assertEqual(merged, merged_again)
        self.assertEqual(len(merged["hooks"]["Stop"]), 2)
        self.assertEqual(len(merged["hooks"]["SessionStart"]), 1)

    def test_hook_file_dry_run_does_not_write_and_install_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "hooks.json"
            path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "node third.js",
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                ),
                encoding="utf-8",
            )
            before = path.read_bytes()
            command = install_beacon_sync.build_collector_command(
                self.python,
                self.script,
                self.config,
            )

            dry = install_beacon_sync.install_collector_hook_file(
                path,
                command,
                dry_run=True,
            )
            first = install_beacon_sync.install_collector_hook_file(path, command)
            installed = path.read_bytes()
            second = install_beacon_sync.install_collector_hook_file(path, command)

            self.assertTrue(dry["changed"])
            self.assertEqual(path.read_bytes(), installed)
            self.assertNotEqual(before, installed)
            self.assertTrue(first["changed"])
            self.assertFalse(second["changed"])

    def test_hook_uninstall_removes_only_marker_owned_collectors(self):
        command = install_beacon_sync.build_collector_command(
            self.python,
            self.script,
            self.config,
        )
        foreign = {
            "type": "command",
            "command": (
                '"C:\\Other\\python.exe" -B '
                '"C:\\Other\\beacon_sync.py" collect'
            ),
            "timeout": 30,
        }
        document = {
            "hooks": {
                event: [
                    {"hooks": [foreign]},
                    {"hooks": [{"type": "command", "command": command, "timeout": 30}]},
                ]
                for event in install_beacon_sync.HOOK_EVENTS
            }
        }

        merged, changed = install_beacon_sync.merge_collector_hooks(
            document,
            command,
            uninstall=True,
        )

        self.assertIn(
            f"-X {install_beacon_sync.HOOK_OWNER_MARKER}",
            command,
        )
        self.assertTrue(changed)
        for event in install_beacon_sync.HOOK_EVENTS:
            self.assertEqual(merged["hooks"][event], [{"hooks": [foreign]}])

    @unittest.skipIf(os.name == "nt", "macOS scheduler test")
    def test_macos_scheduler_dry_run_and_uninstall_do_not_touch_unrelated_files(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            unrelated = home / "Library" / "LaunchAgents" / "third.party.plist"
            unrelated.parent.mkdir(parents=True)
            unrelated.write_bytes(plistlib.dumps({"Label": "third.party"}))
            calls = []

            def runner(arguments):
                calls.append(arguments)
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="not loaded",
                )

            dry = install_beacon_sync.install_macos_scheduler(
                python_path="/python",
                script_path="/beacon_sync.py",
                config_path="/config.yaml",
                log_dir="/logs",
                home=home,
                dry_run=True,
                command_runner=runner,
            )
            removed = install_beacon_sync.install_macos_scheduler(
                python_path="/python",
                script_path="/beacon_sync.py",
                config_path="/config.yaml",
                log_dir="/logs",
                home=home,
                uninstall=True,
                command_runner=runner,
            )

            self.assertTrue(dry["changed"])
            self.assertFalse(removed["changed"])
            self.assertTrue(unrelated.is_file())
            self.assertEqual(
                calls,
                [
                    [
                        "/bin/launchctl",
                        "print",
                        f"gui/{os.getuid()}/{install_beacon_sync.LAUNCHD_LABEL}",
                    ],
                    [
                        "/bin/launchctl",
                        "print",
                        f"gui/{os.getuid()}/{install_beacon_sync.LAUNCHD_LABEL}",
                    ],
                ],
            )

    @unittest.skipIf(os.name == "nt", "macOS scheduler test")
    def test_macos_scheduler_refuses_foreign_same_name_plist(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            path = (
                home
                / "Library"
                / "LaunchAgents"
                / f"{install_beacon_sync.LAUNCHD_LABEL}.plist"
            )
            path.parent.mkdir(parents=True)
            path.write_bytes(
                plistlib.dumps(
                    {
                        "Label": "com.example.foreign",
                        "ProgramArguments": ["/usr/bin/true"],
                    }
                )
            )
            before = path.read_bytes()

            for uninstall in (False, True):
                with self.subTest(uninstall=uninstall):
                    with self.assertRaisesRegex(
                        install_beacon_sync.InstallerError,
                        "not owned",
                    ):
                        install_beacon_sync.install_macos_scheduler(
                            python_path="/python",
                            script_path="/beacon_sync.py",
                            config_path="/config.yaml",
                            log_dir="/logs",
                            home=home,
                            dry_run=True,
                            uninstall=uninstall,
                            command_runner=lambda _arguments: SimpleNamespace(
                                returncode=1,
                                stdout="",
                                stderr="not loaded",
                            ),
                        )
                    self.assertEqual(path.read_bytes(), before)

    @unittest.skipIf(os.name == "nt", "macOS scheduler test")
    def test_macos_identical_plist_bootstraps_unloaded_service_and_verifies(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            path = (
                home
                / "Library"
                / "LaunchAgents"
                / f"{install_beacon_sync.LAUNCHD_LABEL}.plist"
            )
            path.parent.mkdir(parents=True)
            desired = plistlib.dumps(
                install_beacon_sync.build_launchd_plist(
                    python_path="/python",
                    script_path="/beacon_sync.py",
                    config_path="/config.yaml",
                    log_dir="/logs",
                ),
                fmt=plistlib.FMT_XML,
                sort_keys=True,
            )
            path.write_bytes(desired)
            calls = []
            print_count = 0

            def runner(arguments):
                nonlocal print_count
                calls.append(arguments)
                if arguments[1] == "print":
                    print_count += 1
                    return SimpleNamespace(
                        returncode=1 if print_count == 1 else 0,
                        stdout="",
                        stderr="not loaded",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            result = install_beacon_sync.install_macos_scheduler(
                python_path="/python",
                script_path="/beacon_sync.py",
                config_path="/config.yaml",
                log_dir="/logs",
                home=home,
                command_runner=runner,
            )

            self.assertTrue(result["changed"])
            self.assertEqual(path.read_bytes(), desired)
            self.assertEqual(
                [arguments[1] for arguments in calls],
                ["print", "bootstrap", "print"],
            )

    @unittest.skipIf(os.name == "nt", "macOS scheduler test")
    def test_macos_dry_run_reports_identical_but_unloaded_service(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            path = (
                home
                / "Library"
                / "LaunchAgents"
                / f"{install_beacon_sync.LAUNCHD_LABEL}.plist"
            )
            path.parent.mkdir(parents=True)
            path.write_bytes(
                plistlib.dumps(
                    install_beacon_sync.build_launchd_plist(
                        python_path="/python",
                        script_path="/beacon_sync.py",
                        config_path="/config.yaml",
                        log_dir="/logs",
                    ),
                    fmt=plistlib.FMT_XML,
                    sort_keys=True,
                )
            )
            calls = []

            def runner(arguments):
                calls.append(arguments)
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="not loaded",
                )

            result = install_beacon_sync.install_macos_scheduler(
                python_path="/python",
                script_path="/beacon_sync.py",
                config_path="/config.yaml",
                log_dir="/logs",
                home=home,
                dry_run=True,
                command_runner=runner,
            )

            self.assertTrue(result["changed"])
            self.assertEqual([arguments[1] for arguments in calls], ["print"])

    @unittest.skipIf(os.name == "nt", "macOS scheduler test")
    def test_macos_identical_plist_verification_failure_restores_unloaded_state(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            path = (
                home
                / "Library"
                / "LaunchAgents"
                / f"{install_beacon_sync.LAUNCHD_LABEL}.plist"
            )
            path.parent.mkdir(parents=True)
            desired = plistlib.dumps(
                install_beacon_sync.build_launchd_plist(
                    python_path="/python",
                    script_path="/beacon_sync.py",
                    config_path="/config.yaml",
                    log_dir="/logs",
                ),
                fmt=plistlib.FMT_XML,
                sort_keys=True,
            )
            path.write_bytes(desired)
            calls = []

            def runner(arguments):
                calls.append(arguments)
                if arguments[1] == "print":
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="not loaded",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with self.assertRaisesRegex(
                install_beacon_sync.InstallerError,
                "verification",
            ):
                install_beacon_sync.install_macos_scheduler(
                    python_path="/python",
                    script_path="/beacon_sync.py",
                    config_path="/config.yaml",
                    log_dir="/logs",
                    home=home,
                    command_runner=runner,
                )

            self.assertEqual(path.read_bytes(), desired)
            self.assertEqual(
                sum(arguments[1] == "bootstrap" for arguments in calls),
                1,
            )
            self.assertEqual(
                sum(arguments[1] == "bootout" for arguments in calls),
                1,
            )

    @unittest.skipIf(os.name == "nt", "macOS scheduler test")
    def test_macos_uninstall_boots_out_orphan_service_by_label(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            calls = []
            print_count = 0

            def runner(arguments):
                nonlocal print_count
                calls.append(arguments)
                if arguments[1] == "print":
                    print_count += 1
                    return SimpleNamespace(
                        returncode=0 if print_count == 1 else 1,
                        stdout="",
                        stderr="not loaded",
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            result = install_beacon_sync.install_macos_scheduler(
                python_path="/python",
                script_path="/beacon_sync.py",
                config_path="/config.yaml",
                log_dir="/logs",
                home=home,
                uninstall=True,
                command_runner=runner,
            )

            domain = f"gui/{os.getuid()}"
            self.assertTrue(result["changed"])
            self.assertIn(
                [
                    "/bin/launchctl",
                    "bootout",
                    f"{domain}/{install_beacon_sync.LAUNCHD_LABEL}",
                ],
                calls,
            )
            self.assertEqual(
                [arguments[1] for arguments in calls],
                ["print", "bootout", "print"],
            )

    def test_windows_task_file_uses_utf16_bom_and_matching_declaration(self):
        xml = install_beacon_sync.build_windows_task_xml(
            python_path=r"C:\记忆信标\python.exe",
            script_path=r"C:\记忆信标\beacon_sync.py",
            config_path=r"C:\用户\配置.yaml",
            user_id=r"DESKTOP\demo",
        )
        payloads = []

        def runner(arguments):
            xml_path = Path(arguments[arguments.index("/XML") + 1])
            payloads.append(xml_path.read_bytes())
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        install_beacon_sync._create_windows_task(
            runner,
            "Agent Memory Beacon Encoding Test",
            xml,
            replace=False,
        )

        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertTrue(
            payload.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE))
        )
        decoded = payload.decode("utf-16")
        self.assertIn("encoding='utf-16'", decoded)
        self.assertIn("记忆信标", decoded)
        self.assertTrue(install_beacon_sync._same_windows_task_xml(payload, xml))
        self.assertTrue(install_beacon_sync._same_windows_task_xml(decoded, xml))

    def test_windows_scheduler_skips_identical_task(self):
        desired = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
        )
        calls = []
        created_payloads = []

        def runner(arguments):
            calls.append(arguments)
            if "/Query" in arguments:
                return SimpleNamespace(
                    returncode=0,
                    stdout=desired,
                    stderr="",
                )
            xml_path = Path(arguments[arguments.index("/XML") + 1])
            created_payloads.append(xml_path.read_bytes())
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        unchanged = install_beacon_sync.install_windows_scheduler(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
            command_runner=runner,
        )

        self.assertFalse(unchanged["changed"])
        self.assertFalse(unchanged["dry_run"])
        self.assertEqual(len(calls), 1)
        self.assertIn("/Query", calls[0])
        self.assertEqual(created_payloads, [])

        installed_xml = None

        def missing_runner(arguments):
            nonlocal installed_xml
            if "/Query" in arguments:
                if installed_xml is not None:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=installed_xml,
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="ERROR: The system cannot find the file specified.",
                )
            xml_path = Path(arguments[arguments.index("/XML") + 1])
            created_payloads.append(xml_path.read_bytes())
            installed_xml = _decode_windows_task_payload(created_payloads[-1])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        installed = install_beacon_sync.install_windows_scheduler(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
            command_runner=missing_runner,
        )

        self.assertTrue(installed["changed"])
        self.assertEqual(len(created_payloads), 1)
        self.assertTrue(
            created_payloads[0].startswith(
                (codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)
            )
        )
        _decode_windows_task_payload(created_payloads[0])

    def test_windows_scheduler_missing_task_create_never_forces_overwrite(self):
        calls = []
        installed_xml = None

        def runner(arguments):
            nonlocal installed_xml
            calls.append(list(arguments))
            if "/Query" in arguments:
                if installed_xml is not None:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=installed_xml,
                        stderr="",
                    )
                return SimpleNamespace(
                    returncode=1,
                    stdout="",
                    stderr="错误: 系统找不到指定的文件。",
                )
            xml_path = Path(arguments[arguments.index("/XML") + 1])
            installed_xml = _decode_windows_task_payload(xml_path.read_bytes())
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        result = install_beacon_sync.install_windows_scheduler(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
            command_runner=runner,
        )

        self.assertTrue(result["changed"])
        create = next(arguments for arguments in calls if "/Create" in arguments)
        self.assertNotIn("/F", create)

    def test_windows_scheduler_requeries_and_rejects_mismatched_created_task(self):
        desired = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id="S-1-5-21-100-200-300-1001",
        )
        mismatched = desired.replace("PT1M", "PT9M", 1)
        query_count = 0
        calls = []

        def runner(arguments):
            nonlocal query_count
            calls.append(list(arguments))
            if "/Query" in arguments:
                query_count += 1
                if query_count <= 2:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="ERROR: The system cannot find the file specified.",
                    )
                return SimpleNamespace(
                    returncode=0,
                    stdout=mismatched,
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with self.assertRaisesRegex(
            install_beacon_sync.InstallerError,
            r"verification failed.*Interval",
        ):
            install_beacon_sync.install_windows_scheduler(
                python_path=self.python,
                script_path=self.script,
                config_path=self.config,
                user_id="S-1-5-21-100-200-300-1001",
                command_runner=runner,
            )

        self.assertGreaterEqual(query_count, 4)
        self.assertTrue(any("/Create" in arguments for arguments in calls))

    def test_windows_scheduler_creation_verification_failure_restores_original(self):
        previous = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id="S-1-5-21-100-200-300-1001",
            interval_minutes=9,
        )
        state = {"xml": previous}
        calls = []
        create_count = 0

        def runner(arguments):
            nonlocal create_count
            calls.append(list(arguments))
            if "/Query" in arguments:
                if state["xml"] is None:
                    return SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr="ERROR: The system cannot find the file specified.",
                    )
                return SimpleNamespace(returncode=0, stdout=state["xml"], stderr="")
            if "/Create" in arguments:
                create_count += 1
                if create_count == 1:
                    state["xml"] = None
                else:
                    xml_path = Path(arguments[arguments.index("/XML") + 1])
                    state["xml"] = _decode_windows_task_payload(
                        xml_path.read_bytes()
                    )
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(arguments)

        with self.assertRaisesRegex(
            install_beacon_sync.InstallerError,
            "creation verification failed",
        ):
            install_beacon_sync.install_windows_scheduler(
                python_path=self.python,
                script_path=self.script,
                config_path=self.config,
                user_id="S-1-5-21-100-200-300-1001",
                command_runner=runner,
            )

        self.assertTrue(install_beacon_sync._same_windows_task_xml(state["xml"], previous))
        self.assertEqual(create_count, 2)
        self.assertGreaterEqual(
            sum("/Query" in arguments for arguments in calls),
            5,
        )

    def test_windows_task_restore_rejects_fake_success(self):
        task_name = "Agent Memory Beacon Restore Test"
        previous = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id="S-1-5-21-100-200-300-1001",
            interval_minutes=9,
            task_name=task_name,
        )
        current = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id="S-1-5-21-100-200-300-1001",
            interval_minutes=1,
            task_name=task_name,
        )
        calls = []

        def runner(arguments):
            calls.append(list(arguments))
            if "/Query" in arguments:
                return SimpleNamespace(returncode=0, stdout=current, stderr="")
            if "/Create" in arguments:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(arguments)

        with self.assertRaisesRegex(
            install_beacon_sync.InstallerError,
            "rollback verification failed",
        ):
            install_beacon_sync._restore_windows_task(
                runner,
                task_name,
                previous,
                expected_current=current,
            )

        self.assertTrue(any("/Create" in arguments for arguments in calls))
        self.assertGreaterEqual(sum("/Query" in arguments for arguments in calls), 2)

    def test_windows_scheduler_query_permission_failure_aborts_without_create(self):
        calls = []

        def runner(arguments):
            calls.append(list(arguments))
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="ERROR: Access is denied.",
            )

        with self.assertRaisesRegex(
            install_beacon_sync.InstallerError,
            "query failed",
        ):
            install_beacon_sync.install_windows_scheduler(
                python_path=self.python,
                script_path=self.script,
                config_path=self.config,
                user_id=r"DESKTOP\demo",
                command_runner=runner,
            )

        self.assertEqual(len(calls), 1)
        self.assertIn("/Query", calls[0])

    def test_windows_scheduler_replaces_task_when_xml_attributes_differ(self):
        desired = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
        )
        existing = desired.replace('version="1.4"', 'version="1.3"', 1)
        calls = []

        installed_xml = existing

        def runner(arguments):
            nonlocal installed_xml
            calls.append(arguments)
            if "/Query" in arguments:
                return SimpleNamespace(
                    returncode=0,
                    stdout=installed_xml,
                    stderr="",
                )
            xml_path = Path(arguments[arguments.index("/XML") + 1])
            installed_xml = _decode_windows_task_payload(xml_path.read_bytes())
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        result = install_beacon_sync.install_windows_scheduler(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
            command_runner=runner,
        )

        self.assertTrue(result["changed"])
        self.assertTrue(any("/Create" in arguments for arguments in calls))

    def test_windows_scheduler_refuses_to_replace_foreign_same_name_task(self):
        desired = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
        )
        foreign = desired.replace(
            install_beacon_sync.WINDOWS_TASK_OWNER_DESCRIPTION,
            "Unrelated current-user task",
            1,
        )
        calls = []

        def runner(arguments):
            calls.append(arguments)
            if "/Query" in arguments:
                return SimpleNamespace(
                    returncode=0,
                    stdout=foreign,
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with self.assertRaisesRegex(
            install_beacon_sync.InstallerError,
            "not owned",
        ):
            install_beacon_sync.install_windows_scheduler(
                python_path=self.python,
                script_path=self.script,
                config_path=self.config,
                user_id=r"DESKTOP\demo",
                command_runner=runner,
            )

        self.assertEqual(len(calls), 1)
        self.assertIn("/Query", calls[0])

    def test_windows_scheduler_uninstall_refuses_foreign_same_name_task(self):
        desired = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
        )
        foreign = desired.replace(
            install_beacon_sync.WINDOWS_TASK_OWNER_DESCRIPTION,
            "Unrelated current-user task",
            1,
        )
        calls = []

        def runner(arguments):
            calls.append(arguments)
            return SimpleNamespace(
                returncode=0,
                stdout=foreign if "/Query" in arguments else "",
                stderr="",
            )

        with self.assertRaisesRegex(
            install_beacon_sync.InstallerError,
            "not owned",
        ):
            install_beacon_sync.install_windows_scheduler(
                python_path=self.python,
                script_path=self.script,
                config_path=self.config,
                user_id=r"DESKTOP\demo",
                uninstall=True,
                command_runner=runner,
            )

        self.assertEqual(len(calls), 1)
        self.assertIn("/Query", calls[0])

    def test_windows_scheduler_uninstall_deletes_only_owned_task(self):
        owned = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
        )
        runner, state, calls, _payloads = self._windows_task_runner(owned)

        result = install_beacon_sync.install_windows_scheduler(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
            uninstall=True,
            command_runner=runner,
        )

        self.assertTrue(result["changed"])
        self.assertIsNone(state["xml"])
        self.assertEqual(len(calls), 4)
        self.assertIn("/Query", calls[0])
        self.assertIn("/Query", calls[1])
        self.assertIn("/Delete", calls[2])
        self.assertIn("/Query", calls[3])

    def test_windows_scheduler_uninstall_rejects_fake_delete_success(self):
        owned = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
        )
        calls = []

        def runner(arguments):
            calls.append(list(arguments))
            if "/Query" in arguments:
                return SimpleNamespace(returncode=0, stdout=owned, stderr="")
            if "/Delete" in arguments:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            raise AssertionError(arguments)

        with self.assertRaisesRegex(
            install_beacon_sync.InstallerError,
            "deletion verification failed",
        ):
            install_beacon_sync.install_windows_scheduler(
                python_path=self.python,
                script_path=self.script,
                config_path=self.config,
                user_id=r"DESKTOP\demo",
                uninstall=True,
                command_runner=runner,
            )

        self.assertGreaterEqual(sum("/Query" in arguments for arguments in calls), 3)

    def test_windows_scheduler_uninstall_missing_task_is_noop(self):
        calls = []

        def runner(arguments):
            calls.append(arguments)
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="ERROR: The system cannot find the file specified.",
            )

        result = install_beacon_sync.install_windows_scheduler(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
            uninstall=True,
            command_runner=runner,
        )

        self.assertFalse(result["changed"])
        self.assertFalse(result["dry_run"])
        self.assertEqual(len(calls), 1)
        self.assertIn("/Query", calls[0])

    def test_windows_transaction_preflights_all_hooks_before_creating_task(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex = root / ".codex" / "hooks.json"
            claude = root / ".claude" / "settings.json"
            codex.parent.mkdir(parents=True)
            claude.parent.mkdir(parents=True)
            codex.write_text('{"hooks": {}}\n', encoding="utf-8")
            original_codex = codex.read_bytes()
            claude.write_bytes(b"{")
            runner, state, calls, _payloads = self._windows_task_runner(None)

            with self.assertRaisesRegex(
                install_beacon_sync.InstallerError,
                "invalid",
            ):
                install_beacon_sync.install_windows_components(
                    python_path=self.python,
                    script_path=self.script,
                    config_path=self.config,
                    user_id=r"DESKTOP\demo",
                    hook_paths=[codex, claude],
                    command_runner=runner,
                )

            self.assertIsNone(state["xml"])
            self.assertFalse(any("/Create" in arguments for arguments in calls))
            self.assertEqual(codex.read_bytes(), original_codex)
            self.assertEqual(claude.read_bytes(), b"{")

    def test_windows_transaction_hook_failure_restores_absent_task_and_hook_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex = root / ".codex" / "hooks.json"
            claude = root / ".claude" / "settings.json"
            codex.parent.mkdir(parents=True)
            original_codex = (
                '{"custom":"保留原始字节","hooks":{"Stop":[]}}\r\n'
            ).encode("utf-8")
            codex.write_bytes(original_codex)
            runner, state, calls, payloads = self._windows_task_runner(None)
            atomic_write = install_beacon_sync.portable_atomic_write
            failed = False

            def fail_second_hook(path, data, *, root, mode=0o600):
                nonlocal failed
                if Path(path) == claude and not failed:
                    failed = True
                    atomic_write(path, data, root=root, mode=mode)
                    raise OSError("injected hook write failure")
                return atomic_write(path, data, root=root, mode=mode)

            with mock.patch.object(
                install_beacon_sync,
                "portable_atomic_write",
                side_effect=fail_second_hook,
            ):
                with self.assertRaisesRegex(
                    install_beacon_sync.InstallerError,
                    "transaction failed",
                ):
                    install_beacon_sync.install_windows_components(
                        python_path=self.python,
                        script_path=self.script,
                        config_path=self.config,
                        user_id=r"DESKTOP\demo",
                        hook_paths=[codex, claude],
                        command_runner=runner,
                    )

            self.assertTrue(payloads)
            self.assertIsNone(state["xml"])
            self.assertTrue(any("/Delete" in arguments for arguments in calls))
            self.assertEqual(codex.read_bytes(), original_codex)
            self.assertFalse(claude.exists())

    def test_windows_hook_mode_comparison_uses_readonly_semantics(self):
        with mock.patch.object(install_beacon_sync.os, "name", "nt"):
            self.assertTrue(
                install_beacon_sync._hook_mode_matches(
                    stat.S_IFREG | 0o666,
                    0o600,
                )
            )
            self.assertTrue(
                install_beacon_sync._hook_mode_matches(
                    stat.S_IFREG | 0o444,
                    0o400,
                )
            )
            self.assertFalse(
                install_beacon_sync._hook_mode_matches(
                    stat.S_IFREG | 0o444,
                    0o600,
                )
            )

    def test_windows_transaction_preserves_original_hook_mode_on_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex = root / ".codex" / "hooks.json"
            claude = root / ".claude" / "settings.json"
            codex.parent.mkdir(parents=True)
            codex.write_text('{"hooks": {}}\n', encoding="utf-8")
            codex.chmod(0o640)
            original_mode = codex.stat().st_mode & 0o777
            runner, _state, _calls, _payloads = self._windows_task_runner(None)
            atomic_write = install_beacon_sync.portable_atomic_write

            def fail_second_hook(path, data, *, root, mode=0o600):
                if Path(path) == claude:
                    raise OSError("injected hook write failure")
                return atomic_write(path, data, root=root, mode=mode)

            with mock.patch.object(
                install_beacon_sync,
                "portable_atomic_write",
                side_effect=fail_second_hook,
            ):
                with self.assertRaises(install_beacon_sync.InstallerError):
                    install_beacon_sync.install_windows_components(
                        python_path=self.python,
                        script_path=self.script,
                        config_path=self.config,
                        user_id=r"DESKTOP\demo",
                        hook_paths=[codex, claude],
                        command_runner=runner,
                    )

            self.assertEqual(codex.stat().st_mode & 0o777, original_mode)

    def test_windows_transaction_does_not_overwrite_hook_changed_after_preflight(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            hook = root / ".codex" / "hooks.json"
            hook.parent.mkdir(parents=True)
            hook.write_text('{"hooks": {}}\n', encoding="utf-8")
            external = b'{"external":"new owner","hooks":{}}\n'
            base_runner, _state, calls, _payloads = self._windows_task_runner(None)

            def runner(arguments):
                if "/Create" in arguments:
                    result = base_runner(arguments)
                    hook.write_bytes(external)
                    return result
                return base_runner(arguments)

            with self.assertRaisesRegex(
                install_beacon_sync.InstallerError,
                "changed after preflight",
            ):
                install_beacon_sync.install_windows_components(
                    python_path=self.python,
                    script_path=self.script,
                    config_path=self.config,
                    user_id=r"DESKTOP\demo",
                    hook_paths=[hook],
                    command_runner=runner,
                )

            self.assertEqual(hook.read_bytes(), external)
            self.assertTrue(any("/Delete" in arguments for arguments in calls))

    def test_windows_transaction_aborts_if_task_changes_after_preflight(self):
        desired = install_beacon_sync.build_windows_task_xml(
            python_path=self.python,
            script_path=self.script,
            config_path=self.config,
            user_id=r"DESKTOP\demo",
            interval_minutes=9,
        )
        foreign = desired.replace(
            install_beacon_sync.WINDOWS_TASK_OWNER_DESCRIPTION,
            "Concurrent foreign task",
            1,
        )
        calls = []
        query_count = 0

        def runner(arguments):
            nonlocal query_count
            calls.append(list(arguments))
            if "/Query" in arguments:
                query_count += 1
                return SimpleNamespace(
                    returncode=0,
                    stdout=desired if query_count == 1 else foreign,
                    stderr="",
                )
            raise AssertionError(f"unexpected scheduler command: {arguments}")

        with self.assertRaisesRegex(
            install_beacon_sync.InstallerError,
            "changed after preflight",
        ):
            install_beacon_sync.install_windows_components(
                python_path=self.python,
                script_path=self.script,
                config_path=self.config,
                user_id=r"DESKTOP\demo",
                command_runner=runner,
            )

        self.assertEqual(query_count, 2)
        self.assertFalse(any("/Create" in arguments for arguments in calls))

    def test_windows_transaction_hook_failure_restores_previous_task_xml(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex = root / ".codex" / "hooks.json"
            claude = root / ".claude" / "settings.json"
            original_claude = b'{"custom":"exact","hooks":{}}\n'
            claude.parent.mkdir(parents=True)
            claude.write_bytes(original_claude)
            previous_xml = install_beacon_sync.build_windows_task_xml(
                python_path=self.python,
                script_path=self.script,
                config_path=self.config,
                user_id=r"DESKTOP\demo",
                interval_minutes=9,
            )
            runner, state, _calls, payloads = self._windows_task_runner(
                previous_xml
            )
            atomic_write = install_beacon_sync.portable_atomic_write
            failed = False

            def fail_first_hook(path, data, *, root, mode=0o600):
                nonlocal failed
                if Path(path) == codex and not failed:
                    failed = True
                    atomic_write(path, data, root=root, mode=mode)
                    raise OSError("injected hook write failure")
                return atomic_write(path, data, root=root, mode=mode)

            with mock.patch.object(
                install_beacon_sync,
                "portable_atomic_write",
                side_effect=fail_first_hook,
            ):
                with self.assertRaisesRegex(
                    install_beacon_sync.InstallerError,
                    "transaction failed",
                ):
                    install_beacon_sync.install_windows_components(
                        python_path=self.python,
                        script_path=self.script,
                        config_path=self.config,
                        user_id=r"DESKTOP\demo",
                        hook_paths=[codex, claude],
                        command_runner=runner,
                    )

            self.assertGreaterEqual(len(payloads), 2)
            self.assertTrue(
                install_beacon_sync._same_windows_task_xml(
                    payloads[-1], previous_xml
                )
            )
            self.assertTrue(
                install_beacon_sync._same_windows_task_xml(
                    state["xml"], previous_xml
                )
            )
            self.assertFalse(codex.exists())
            self.assertEqual(claude.read_bytes(), original_claude)

    def test_windows_uninstall_hook_failure_restores_task_and_all_hook_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            codex = root / ".codex" / "hooks.json"
            claude = root / ".claude" / "settings.json"
            command = install_beacon_sync.build_collector_command(
                self.python,
                self.script,
                self.config,
            )
            document = {
                "custom": "preserve",
                "hooks": {
                    event: [
                        {
                            "hooks": [
                                {"type": "command", "command": "node third.js"}
                            ]
                        },
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": command,
                                    "timeout": 30,
                                }
                            ]
                        },
                    ]
                    for event in install_beacon_sync.HOOK_EVENTS
                },
            }
            original_codex = (
                json.dumps(document, ensure_ascii=False, separators=(",", ":"))
                + "\r\n"
            ).encode("utf-8")
            original_claude = (
                json.dumps(document, ensure_ascii=False, indent=4) + "\n"
            ).encode("utf-8")
            codex.parent.mkdir(parents=True)
            claude.parent.mkdir(parents=True)
            codex.write_bytes(original_codex)
            claude.write_bytes(original_claude)
            previous_xml = install_beacon_sync.build_windows_task_xml(
                python_path=self.python,
                script_path=self.script,
                config_path=self.config,
                user_id=r"DESKTOP\demo",
            )
            runner, state, calls, payloads = self._windows_task_runner(
                previous_xml
            )
            atomic_write = install_beacon_sync.portable_atomic_write
            failed = False

            def fail_second_hook(path, data, *, root, mode=0o600):
                nonlocal failed
                if Path(path) == claude and not failed:
                    failed = True
                    atomic_write(path, data, root=root, mode=mode)
                    raise OSError("injected hook write failure")
                return atomic_write(path, data, root=root, mode=mode)

            with mock.patch.object(
                install_beacon_sync,
                "portable_atomic_write",
                side_effect=fail_second_hook,
            ):
                with self.assertRaisesRegex(
                    install_beacon_sync.InstallerError,
                    "transaction failed",
                ):
                    install_beacon_sync.install_windows_components(
                        python_path=self.python,
                        script_path=self.script,
                        config_path=self.config,
                        user_id=r"DESKTOP\demo",
                        hook_paths=[codex, claude],
                        uninstall=True,
                        command_runner=runner,
                    )

            self.assertTrue(any("/Delete" in arguments for arguments in calls))
            self.assertTrue(
                install_beacon_sync._same_windows_task_xml(
                    payloads[-1], previous_xml
                )
            )
            self.assertTrue(
                install_beacon_sync._same_windows_task_xml(
                    state["xml"], previous_xml
                )
            )
            self.assertEqual(codex.read_bytes(), original_codex)
            self.assertEqual(claude.read_bytes(), original_claude)

    @unittest.skipIf(os.name == "nt", "macOS scheduler test")
    def test_macos_bootstrap_failure_restores_previous_plist_and_service(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            path = (
                home
                / "Library"
                / "LaunchAgents"
                / f"{install_beacon_sync.LAUNCHD_LABEL}.plist"
            )
            path.parent.mkdir(parents=True)
            old_bytes = plistlib.dumps(
                {
                    "Label": install_beacon_sync.LAUNCHD_LABEL,
                    "ProgramArguments": ["/old/python", "/old/beacon_sync.py"],
                }
            )
            path.write_bytes(old_bytes)
            path.chmod(0o644)
            calls = []
            bootstrap_count = 0

            def runner(arguments):
                nonlocal bootstrap_count
                calls.append(arguments)
                if arguments[1] == "print":
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if arguments[1] == "bootstrap":
                    bootstrap_count += 1
                    if bootstrap_count == 1:
                        return SimpleNamespace(
                            returncode=1,
                            stdout="",
                            stderr="injected bootstrap failure",
                        )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with self.assertRaisesRegex(
                install_beacon_sync.InstallerError,
                "bootstrap failure",
            ):
                install_beacon_sync.install_macos_scheduler(
                    python_path="/new/python",
                    script_path="/new/beacon_sync.py",
                    config_path="/new/config.yaml",
                    log_dir="/logs",
                    home=home,
                    command_runner=runner,
                )

            self.assertEqual(path.read_bytes(), old_bytes)
            self.assertEqual(path.stat().st_mode & 0o777, 0o644)
            self.assertEqual(bootstrap_count, 2)
            self.assertGreaterEqual(
                sum(arguments[1] == "bootout" for arguments in calls),
                2,
            )


if __name__ == "__main__":
    unittest.main()
