from icloudz.sync import _should_exclude, _should_include, _parse_mtime, _fmt_bytes


class TestExclude:
    def test_exact_name(self):
        assert _should_exclude(".DS_Store", [".DS_Store"])

    def test_glob_extension(self):
        assert _should_exclude("file.tmp", ["*.tmp"])

    def test_nested_name(self):
        assert _should_exclude("subdir/.DS_Store", [".DS_Store"])

    def test_no_match(self):
        assert not _should_exclude("document.pdf", ["*.tmp", ".DS_Store"])

    def test_empty_excludes(self):
        assert not _should_exclude("anything.txt", [])


class TestSelective:
    def test_empty_selective_includes_all(self):
        assert _should_include("anything/file.txt", [])

    def test_prefix_match(self):
        assert _should_include("Documents/report.pdf", ["/Documents"])

    def test_exact_match(self):
        assert _should_include("Desktop", ["/Desktop"])

    def test_no_match(self):
        assert not _should_include("Photos/img.jpg", ["/Documents"])

    def test_multiple_prefixes(self):
        assert _should_include("Desktop/note.txt", ["/Documents", "/Desktop"])


class TestParseMtime:
    def test_iso_format(self):
        ts = _parse_mtime("2024-06-15T12:00:00+00:00")
        assert ts > 0

    def test_z_suffix(self):
        ts = _parse_mtime("2024-06-15T12:00:00Z")
        assert ts > 0

    def test_invalid(self):
        assert _parse_mtime("not-a-date") == 0.0

    def test_empty(self):
        assert _parse_mtime("") == 0.0


class TestFmtBytes:
    def test_bytes(self):
        assert _fmt_bytes(500) == "500.0 B"

    def test_kilobytes(self):
        assert _fmt_bytes(2048) == "2.0 KB"

    def test_megabytes(self):
        assert _fmt_bytes(5 * 1024 * 1024) == "5.0 MB"

    def test_gigabytes(self):
        assert _fmt_bytes(2 * 1024 ** 3) == "2.0 GB"
