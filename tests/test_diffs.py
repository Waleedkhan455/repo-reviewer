import subprocess

import pytest

from src.diffs import DiffError, changed_lines, format_ranges, parse_unified_diff

DIFF = """diff --git a/app.py b/app.py
index 1..2 100644
--- a/app.py
+++ b/app.py
@@ -3,0 +4,2 @@ def f():
+    x = 1
+    y = 2
@@ -10 +12 @@ def g():
-    old
+    new
@@ -20,3 +23,0 @@ def h():
-    gone
diff --git a/old.py b/old.py
deleted file mode 100644
--- a/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-a
-b
diff --git a/a.js b/web/b.js
similarity index 90%
rename from a.js
rename to web/b.js
--- a/a.js
+++ b/web/b.js
@@ -1 +1 @@
-let a = 1
+let a = 2
"""


def test_parse_unified_diff():
    assert parse_unified_diff(DIFF) == {"app.py": {4, 5, 12}, "web/b.js": {1}}


def test_format_ranges():
    assert format_ranges({3, 4, 5, 9, 11, 12}) == "3-5,9,11-12"
    assert format_ranges(set()) == ""


def _git(root, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=root, check=True,
                   capture_output=True)


def test_changed_lines_against_merge_base(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("a = 1\nb = 2\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "app.py").write_text("a = 1\nb = 3\nc = 4\n")
    (root / "new.py").write_text("print('hi')\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "change")
    base = subprocess.run(["git", "rev-parse", "main"], cwd=root, capture_output=True, text=True).stdout.strip()
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    assert changed_lines(root, base, head) == {"app.py": {2, 3}, "new.py": {1}}


@pytest.mark.parametrize("sha", ["main; rm -rf /", "--output=x", "HEAD"])
def test_rejects_non_sha_refs(tmp_path, sha):
    with pytest.raises(DiffError):
        changed_lines(tmp_path, sha, "a" * 40)
