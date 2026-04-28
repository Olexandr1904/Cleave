"""Tests for orchestrator.gradle_remediation."""

from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.gradle_remediation import (
    clear_gradle_transforms,
    looks_like_gradle_cache_corruption,
)


class TestLooksLikeGradleCacheCorruption:
    def test_matches_daemon_startup_failed(self):
        msg = (
            "Execution failed for AarResourcesCompilerTransform: ...\n"
            "AAPT2 aapt2-8.6.1-11315950-linux Daemon #5: Daemon startup failed\n"
            "This should not happen under normal circumstances, please file an issue."
        )
        assert looks_like_gradle_cache_corruption(msg)

    def test_matches_syntax_error_unexpected(self):
        msg = (
            "AAPT2 aapt2-8.6.1-11315950-linux Daemon #2: Unexpected error output: "
            "/home/admin0/.gradle/caches/8.14.1/transforms/abc/transformed/"
            "aapt2-8.6.1-11315950-linux/aapt2: 2: Syntax error: \"(\" unexpected"
        )
        assert looks_like_gradle_cache_corruption(msg)

    def test_matches_real_world_push_failure(self):
        # Verbatim from ACME-12058 state.json — full pre-push hook output
        msg = (
            "Git command failed: git -C /data/sickle/.../source push -u origin feature/ACME-12058\n"
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
