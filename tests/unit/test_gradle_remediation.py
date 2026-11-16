"""Tests for orchestrator.gradle_remediation."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator.gradle_remediation import (
    ARCH_MISMATCH_HELP,
    _GRADLE_DAEMON_MAIN_CLASS,
    clear_gradle_transforms,
    looks_like_aapt2_arch_mismatch,
    looks_like_detekt_hook_cleanup_failure,
    looks_like_gradle_cache_corruption,
    looks_like_pre_push_hook_environmental_failure,
)


class TestLooksLikeDetektHookCleanupFailure:
    """The detekt pre-push hook's /tmp/detekt-<epoch> temp file collides when
    two tickets push in the same second; the loser's `rm` fails and trips the
    hook even though detekt passed. Reproduces ACME-14806 / ACME-11252."""

    _RACE = (
        "Git command failed: git -C /ws/source push -u origin feature/ACME-14806\n"
        "rm: /tmp/detekt-1780305799: No such file or directory\n"
        "error: failed to push some refs to 'https://github.com/acme-org/x.git'"
    )

    def test_matches_concurrent_cleanup_race(self):
        assert looks_like_detekt_hook_cleanup_failure(self._RACE)

    def test_classified_environmental_for_no_verify_retry(self):
        assert looks_like_pre_push_hook_environmental_failure(self._RACE)

    def test_does_not_bypass_real_detekt_findings(self):
        # detekt actually failed AND the cleanup rm also raced — the real
        # finding markers must keep this BLOCKING (no --no-verify bypass).
        msg = (
            "Running detekt check...\n"
            "StartViewModel.kt:42:1: MagicNumber: ... (1 issues found)\n"
            "rm: /tmp/detekt-1780305799: No such file or directory\n"
            "detekt failed\n"
            "Please fix the above issues before pushing\n"
            "error: failed to push some refs"
        )
        assert not looks_like_detekt_hook_cleanup_failure(msg)
        assert not looks_like_pre_push_hook_environmental_failure(msg)

    def test_does_not_match_unrelated_rm_error(self):
        assert not looks_like_detekt_hook_cleanup_failure(
            "rm: /tmp/build-cache: No such file or directory"
        )

    def test_handles_none_and_empty(self):
        assert not looks_like_detekt_hook_cleanup_failure(None)
        assert not looks_like_detekt_hook_cleanup_failure("")


class TestLooksLikeGradleCacheCorruption:
    def test_matches_daemon_startup_failed(self):
        msg = (
            "Execution failed for AarResourcesCompilerTransform: ...\n"
            "AAPT2 aapt2-8.6.1-11315950-linux Daemon #5: Daemon startup failed\n"
            "This should not happen under normal circumstances, please file an issue."
        )
        assert looks_like_gradle_cache_corruption(msg)

    def test_matches_syntax_error_unexpected_without_arch_path(self):
        # AAPT2 + Syntax error without the `aapt2-<ver>-linux/aapt2:` path —
        # could happen on x86_64 hosts where the cache content is genuinely
        # corrupt but the architecture is right. The same signature with the
        # canonical arch-mismatch path is handled by looks_like_aapt2_arch_mismatch
        # (see tests below); cache-corruption defers to it for that case.
        msg = (
            "AAPT2 Daemon #2: Unexpected error output: Syntax error: \"(\" unexpected"
        )
        assert looks_like_gradle_cache_corruption(msg)

    def test_matches_real_world_push_failure(self):
        # Verbatim from ACME-12058 state.json — full pre-push hook output
        msg = (
            "Git command failed: git -C /data/cleave/.../source push -u origin feature/ACME-12058\n"
            "AAPT2 aapt2-8.6.1-11315950-linux Daemon #1: Unexpected error output: "
            "/home/admin0/.gradle/caches/8.14.1/transforms/.../aapt2: 2: Syntax error: \"(\" unexpected\n"
            "FAILURE: Build failed with an exception."
        )
        assert looks_like_gradle_cache_corruption(msg)

    def test_does_not_match_unrelated_build_error(self):
        msg = "FAILURE: Build failed with an exception. Compilation error in MainActivity.kt"
        assert not looks_like_gradle_cache_corruption(msg)

    def test_does_not_match_generic_aapt2_text(self):
        # The word aapt2 alone, without the corruption signatures, must not match
        msg = "Configured AAPT2 version: 8.6.1"
        assert not looks_like_gradle_cache_corruption(msg)

    def test_handles_none_and_empty(self):
        assert not looks_like_gradle_cache_corruption(None)
        assert not looks_like_gradle_cache_corruption("")

    def test_matches_aar_resources_compiler_transform_failure(self):
        # Real second-stage corruption signature observed on ACME-12058: after
        # AAPT2 daemon starts working again, Gradle finds .aar transforms with
        # missing AndroidManifest.xml inside. Same root cause (transforms tree
        # in an inconsistent state), same fix.
        msg = (
            "FAILURE: Build failed with an exception.\n"
            "* What went wrong:\n"
            "Execution failed for task ':app:processDebugResources'.\n"
            "> Could not resolve all files for configuration ':app:debugRuntimeClasspath'.\n"
            "   > Failed to transform library-4.2.0.aar (com.github.chuckerteam.chucker:library:4.2.0) ...\n"
            "      > Execution failed for AarResourcesCompilerTransform: "
            "/home/admin0/.gradle/caches/8.14.1/transforms/5e7fcbe4e62ca53cf7a71fe52b9c2f26/transformed/jetified-library-4.2.0.\n"
            "         > /home/admin0/.gradle/caches/8.14.1/transforms/5e7fcbe4e62ca53cf7a71fe52b9c2f26/transformed/jetified-library-4.2.0/AndroidManifest.xml"
        )
        assert looks_like_gradle_cache_corruption(msg)

    def test_matches_failed_to_transform_aar_with_transforms_path_nearby(self):
        msg = (
            "Failed to transform foo-1.0.aar (com.example:foo:1.0) to match attributes ...\n"
            "  > Inner cause referencing /home/user/.gradle/caches/8.14.1/transforms/abc/transformed"
        )
        assert looks_like_gradle_cache_corruption(msg)

    def test_does_not_match_failed_to_transform_aar_unrelated_to_cache(self):
        # `.aar` failure that doesn't point at the transforms cache must not match.
        msg = (
            "Failed to transform some.aar — incompatible architecture\n"
            "Library only supports x86_64 but build target is armv8"
        )
        assert not looks_like_gradle_cache_corruption(msg)

    def test_arch_mismatch_does_not_classify_as_cache_corruption(self):
        # The arch-mismatch error textually contains both AAPT2 + Syntax error
        # patterns, but wiping the cache won't help — the next download will
        # still produce an x86-64 binary on a non-x86 host. cache-corruption
        # must defer to the more specific arch-mismatch detector.
        msg = (
            "AAPT2 aapt2-8.6.1-11315950-linux Daemon #0: Unexpected error output: "
            "/home/admin0/.gradle/caches/8.14.1/transforms/abc/transformed/"
            "aapt2-8.6.1-11315950-linux/aapt2: 2: Syntax error: \"(\" unexpected"
        )
        assert looks_like_aapt2_arch_mismatch(msg)
        # Cache-corruption returns False so the 🧹 button doesn't fire on this
        assert not looks_like_gradle_cache_corruption(msg)


class TestLooksLikeAapt2ArchMismatch:
    def test_matches_real_world_error(self):
        msg = (
            "/home/admin0/.gradle/caches/8.14.1/transforms/7abf4a41bf9deaace477b90ab915558d/"
            "transformed/aapt2-8.6.1-11315950-linux/aapt2: 2: Syntax error: \"(\" unexpected"
        )
        assert looks_like_aapt2_arch_mismatch(msg)

    def test_does_not_match_aarch64_path(self):
        # When AGP picks the aarch64 build, the path has -linux_aarch64. That
        # is the working configuration, not a mismatch.
        msg = (
            "AAPT2 aapt2-8.6.1-11315950-linux_aarch64 Daemon #0: "
            "/home/admin0/.gradle/caches/8.14.1/transforms/abc/transformed/"
            "aapt2-8.6.1-11315950-linux_aarch64/aapt2: 2: Syntax error: \"(\" unexpected"
        )
        assert not looks_like_aapt2_arch_mismatch(msg)

    def test_does_not_match_daemon_startup_failed_alone(self):
        # "Daemon startup failed" without the syntax-error path is the older
        # cache-corruption symptom on x86_64 hosts. Should not be classified
        # as arch mismatch.
        msg = "AAPT2 aapt2-8.6.1-linux Daemon #0: Daemon startup failed"
        assert not looks_like_aapt2_arch_mismatch(msg)

    def test_handles_none_and_empty(self):
        assert not looks_like_aapt2_arch_mismatch(None)
        assert not looks_like_aapt2_arch_mismatch("")

    def test_help_text_is_actionable(self):
        # Sanity check that the operator-facing hint contains the two main
        # remediation options. If we tweak the wording, this still asserts
        # the operator can actually unblock themselves.
        assert "qemu-user-static" in ARCH_MISMATCH_HELP
        assert "aarch64" in ARCH_MISMATCH_HELP
        assert "Retry" in ARCH_MISMATCH_HELP


def _log(home: Path, pid: int) -> None:
    """The log a Gradle daemon started from `home` writes there."""
    d = home / "daemon" / "8.14.1"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"daemon-{pid}.out.log").write_text("log")


@pytest.fixture
def fake_daemon():
    """Start a sleeping process whose command line is a Gradle daemon run from `home`'s wrapper distribution."""
    procs: list[subprocess.Popen] = []

    def start(home: Path) -> int:
        jar = f"{home}/wrapper/dists/gradle-8.14.1-bin/x/gradle-8.14.1/lib/gradle-daemon-main-8.14.1.jar"
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "-cp", jar,
                                 _GRADLE_DAEMON_MAIN_CLASS, "8.14.1"])
        procs.append(proc)
        return proc.pid

    yield start
    for proc in procs:
        proc.kill()
        proc.wait()


class TestClearGradleTransforms:
    def test_removes_transforms_under_each_version(self, tmp_path):
        gradle_home = tmp_path / ".gradle"
        # Two version dirs each with a transforms subdir + some payload
        for ver in ("8.14.1", "8.10"):
            t = gradle_home / "caches" / ver / "transforms"
            t.mkdir(parents=True)
            (t / "abc.txt").write_text("x" * 1000)
        # An unrelated file under caches that must NOT be touched
        unrelated = gradle_home / "caches" / "modules-2" / "files-2.1"
        unrelated.mkdir(parents=True)
        (unrelated / "keep.txt").write_text("preserve")

        freed = clear_gradle_transforms(gradle_home)

        assert freed >= 2000  # two 1000-byte files
        assert not (gradle_home / "caches" / "8.14.1" / "transforms").exists()
        assert not (gradle_home / "caches" / "8.10" / "transforms").exists()
        # Sibling preserved
        assert (unrelated / "keep.txt").read_text() == "preserve"

    def test_returns_zero_when_no_caches_dir(self, tmp_path):
        gradle_home = tmp_path / "empty"
        gradle_home.mkdir()
        assert clear_gradle_transforms(gradle_home) == 0

    def test_returns_zero_when_no_transforms_present(self, tmp_path):
        gradle_home = tmp_path / ".gradle"
        (gradle_home / "caches" / "8.14.1").mkdir(parents=True)
        (gradle_home / "caches" / "8.14.1" / "modules").mkdir()
        assert clear_gradle_transforms(gradle_home) == 0

    def test_respects_gradle_user_home_env(self, tmp_path, monkeypatch):
        env_home = tmp_path / "env-gradle"
        (env_home / "caches" / "8.14.1" / "transforms" / "x").mkdir(parents=True)
        (env_home / "caches" / "8.14.1" / "transforms" / "x" / "f.txt").write_text("y" * 500)
        monkeypatch.setenv("GRADLE_USER_HOME", str(env_home))

        freed = clear_gradle_transforms()  # no explicit home, picks up env

        assert freed >= 500
        assert not (env_home / "caches" / "8.14.1" / "transforms").exists()

    def test_explicit_home_overrides_env(self, tmp_path, monkeypatch):
        # Env home has no transforms; explicit home does. Explicit must win.
        env_home = tmp_path / "env-gradle"
        env_home.mkdir()
        explicit_home = tmp_path / "explicit-gradle"
        (explicit_home / "caches" / "8.14.1" / "transforms" / "f").mkdir(parents=True)
        (explicit_home / "caches" / "8.14.1" / "transforms" / "f" / "x").write_text("z" * 100)
        monkeypatch.setenv("GRADLE_USER_HOME", str(env_home))

        freed = clear_gradle_transforms(explicit_home)

        assert freed >= 100
        assert not (explicit_home / "caches" / "8.14.1" / "transforms").exists()

    def test_stops_the_daemons_of_this_home(self, tmp_path, fake_daemon, gradle_signals):
        # A daemon left running keeps references to the deleted transforms and reproduces the corruption.
        home = tmp_path / "cleave-gradle"
        pid = fake_daemon(home)
        _log(home, pid)

        clear_gradle_transforms(home)

        assert gradle_signals == [pid]

    def test_leaves_a_daemon_of_another_home_alone(self, tmp_path, fake_daemon, gradle_signals):
        # The pid in an old log of this home now belongs to another home's daemon (a CI runner's, say).
        home, other = tmp_path / "cleave-gradle", tmp_path / "shared-gradle"
        pid = fake_daemon(other)
        _log(home, pid)

        clear_gradle_transforms(home)

        assert gradle_signals == []

    def test_no_explicit_home_leaves_the_shared_home_alone(self, tmp_path, monkeypatch, gradle_signals):
        # Without GRADLE_USER_HOME gradle uses ~/.gradle, which other builds on the host share.
        monkeypatch.delenv("GRADLE_USER_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        transforms = tmp_path / ".gradle" / "caches" / "8.14.1" / "transforms"
        transforms.mkdir(parents=True)
        (transforms / "f").write_text("x")

        with pytest.raises(Exception, match="GRADLE_USER_HOME"):
            clear_gradle_transforms()

        assert (transforms / "f").exists()
        assert gradle_signals == []

    def test_process_listing_failure_does_not_stop_the_wipe(self, tmp_path, gradle_signals):
        """Stopping daemons is best-effort; the wipe is the must-have."""
        gradle_home = tmp_path / ".gradle"
        transforms = gradle_home / "caches" / "8.14.1" / "transforms"
        transforms.mkdir(parents=True)
        (transforms / "f").write_text("a" * 200)
        _log(gradle_home, 4242)

        with patch(
            "orchestrator.gradle_remediation.subprocess.run",
            side_effect=FileNotFoundError("no ps"),
        ):
            freed = clear_gradle_transforms(gradle_home)

        assert freed >= 200
        assert not transforms.exists()
        assert gradle_signals == []

    def test_wipes_daemon_registry(self, tmp_path):
        """Stale registry entries cause Gradle to wait on dead daemons.
        clear_gradle_transforms must remove registry.bin (and its lock) under
        every version subdir so the next invocation spawns a fresh daemon
        cleanly."""
        gradle_home = tmp_path / ".gradle"
        for ver in ("8.14.1", "8.10"):
            d = gradle_home / "daemon" / ver
            d.mkdir(parents=True)
            (d / "registry.bin").write_bytes(b"\x01" * 50)
            (d / "registry.bin.lock").write_bytes(b"\x02")
            (d / "daemon-1234.out.log").write_text("keep me")  # logs preserved
        # No transforms to wipe — we're focused on the registry path here
        with patch("orchestrator.gradle_remediation.subprocess.run") as m:
            m.return_value = subprocess.CompletedProcess([], 1)
            clear_gradle_transforms(gradle_home)

        for ver in ("8.14.1", "8.10"):
            d = gradle_home / "daemon" / ver
            assert not (d / "registry.bin").exists()
            assert not (d / "registry.bin.lock").exists()
            # Logs left intact — they have post-mortem value
            assert (d / "daemon-1234.out.log").read_text() == "keep me"

    def test_handles_missing_daemon_dir_gracefully(self, tmp_path):
        """If the daemon dir doesn't exist (fresh Gradle install) the registry
        wipe is a no-op and the cache wipe still runs."""
        gradle_home = tmp_path / ".gradle"
        transforms = gradle_home / "caches" / "8.14.1" / "transforms"
        transforms.mkdir(parents=True)
        (transforms / "f").write_text("x" * 100)
        # No daemon/ subtree

        with patch("orchestrator.gradle_remediation.subprocess.run") as m:
            m.return_value = subprocess.CompletedProcess([], 1)
            freed = clear_gradle_transforms(gradle_home)

        assert freed >= 100
        assert not transforms.exists()
