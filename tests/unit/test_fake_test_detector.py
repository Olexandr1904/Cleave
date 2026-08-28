"""Tests for the source-reading pseudo-test detector.

10 of the 21 test files the pipeline authored assert on production source
text instead of behaviour. Such a test cannot fail — the agent that wrote the
source wrote the assertion in the same run.
"""

from pathlib import Path

from orchestrator.fake_test_detector import detect_source_reading_tests

# Shape taken from PreviewAcmeAtomMigrationGuardTest (PR #1542).
FAKE_GREP = '''
class PreviewGuardTest {
    @Test fun `annotation is used`() {
        val src = File("app/src/main/java/Button.kt").readText()
        assertTrue(src.contains("@PreviewAcmeAtom"))
    }
}
'''

# Shape taken from ThemeWindowBackgroundTest (PR #1521).
FAKE_XML = '''
class ThemeWindowBackgroundTest {
    @Test fun `every base theme sets nav bar color`() {
        val f = File("app/src/main/res/values/styles.xml")
        val doc = DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(f)
        assertNotNull(doc)
    }
}
'''

REAL = '''
class ButtonTest {
    @Test fun `disabled button ignores clicks`() {
        val button = Button(enabled = false)
        button.click()
        assertEquals(0, button.clickCount)
    }
}
'''

# A legitimate test that touches the filesystem but does not read production
# source — must NOT be flagged, or the gate blocks real work.
REAL_WITH_FIXTURE = '''
class ParserTest {
    @Test fun `parses payload`() {
        val json = File(tempDir, "payload.json").readText()
        assertEquals(3, parse(json).items.size)
    }
}
'''


def _write(tmp_path: Path, rel: str, body: str) -> str:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return rel


def test_flags_test_reading_source_as_text(tmp_path):
    rel = _write(tmp_path, "app/src/test/java/PreviewGuardTest.kt", FAKE_GREP)
    assert detect_source_reading_tests(tmp_path, [rel]) == [rel]


def test_flags_test_parsing_resource_xml(tmp_path):
    rel = _write(tmp_path, "app/src/test/java/ThemeTest.kt", FAKE_XML)
    assert detect_source_reading_tests(tmp_path, [rel]) == [rel]


def test_ignores_behavioural_test(tmp_path):
    rel = _write(tmp_path, "app/src/test/java/ButtonTest.kt", REAL)
    assert detect_source_reading_tests(tmp_path, [rel]) == []


def test_ignores_test_reading_its_own_fixture(tmp_path):
    rel = _write(tmp_path, "app/src/test/java/ParserTest.kt", REAL_WITH_FIXTURE)
    assert detect_source_reading_tests(tmp_path, [rel]) == []


def test_ignores_non_test_files(tmp_path):
    rel = _write(tmp_path, "app/src/main/java/FileReader.kt", FAKE_GREP)
    assert detect_source_reading_tests(tmp_path, [rel]) == []


def test_missing_file_is_skipped(tmp_path):
    assert detect_source_reading_tests(tmp_path, ["app/src/test/java/Gone.kt"]) == []


def test_reports_every_offender(tmp_path):
    a = _write(tmp_path, "app/src/test/java/AGuardTest.kt", FAKE_GREP)
    b = _write(tmp_path, "app/src/test/java/BGuardTest.kt", FAKE_XML)
    ok = _write(tmp_path, "app/src/test/java/CTest.kt", REAL)
    found = detect_source_reading_tests(tmp_path, [a, b, ok])
    assert sorted(found) == sorted([a, b])
