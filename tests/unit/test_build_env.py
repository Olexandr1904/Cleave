from __future__ import annotations

from orchestrator.build_env import apply_build_memory_limits


def _read(p):
    return p.read_text(encoding="utf-8")


class TestApplyBuildMemoryLimits:
    def test_creates_file_with_capped_heaps(self, tmp_path):
        guh = tmp_path / "gradle_home"
        props = apply_build_memory_limits(guh, gradle_heap_mb=2048, kotlin_heap_mb=1536)
        assert props == guh / "gradle.properties"
        text = _read(props)
        assert "org.gradle.jvmargs=-Xmx2048m" in text
        assert "kotlin.daemon.jvmargs=-Xmx1536m" in text

    def test_overrides_existing_heap_keys_preserves_others(self, tmp_path):
        guh = tmp_path / "gradle_home"
        guh.mkdir()
        (guh / "gradle.properties").write_text(
            "org.gradle.caching=true\n"
            "org.gradle.jvmargs=-Xmx5g -XX:MaxMetaspaceSize=2g\n"
            "kotlin.daemon.jvmargs=-Xmx4g\n"
            "org.gradle.parallel=false\n",
            encoding="utf-8",
        )
        apply_build_memory_limits(guh, gradle_heap_mb=2048, kotlin_heap_mb=1536)
        text = _read(guh / "gradle.properties")
        # Oversized heaps replaced
        assert "-Xmx5g" not in text
        assert "-Xmx4g" not in text
        assert "org.gradle.jvmargs=-Xmx2048m" in text
        assert "kotlin.daemon.jvmargs=-Xmx1536m" in text
        # Unrelated keys preserved
        assert "org.gradle.caching=true" in text
        assert "org.gradle.parallel=false" in text

    def test_idempotent(self, tmp_path):
        guh = tmp_path / "gradle_home"
        apply_build_memory_limits(guh, gradle_heap_mb=2048, kotlin_heap_mb=1536)
        first = _read(guh / "gradle.properties")
        apply_build_memory_limits(guh, gradle_heap_mb=2048, kotlin_heap_mb=1536)
        assert _read(guh / "gradle.properties") == first
