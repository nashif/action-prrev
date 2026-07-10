import diffparse

SAMPLE = """diff --git a/app/auth.py b/app/auth.py
index 1111111..2222222 100644
--- a/app/auth.py
+++ b/app/auth.py
@@ -10,6 +10,9 @@ def login(request):
     user = lookup(request.form["email"])
     if not user:
         return None
+    query = "SELECT * FROM sessions WHERE token = '" + token + "'"
+    db.execute(query)
+    return user

 def logout():
     pass
diff --git a/package-lock.json b/package-lock.json
index 3333333..4444444 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,3 +1,3 @@
-  "version": "1.0.0"
+  "version": "1.0.1"
diff --git a/assets/logo.png b/assets/logo.png
new file mode 100644
index 0000000..5555555
Binary files /dev/null and b/assets/logo.png differ
"""


def test_parse_splits_every_file():
    files = diffparse.parse(SAMPLE)
    assert [f.path for f in files] == ["app/auth.py", "package-lock.json", "assets/logo.png"]


def test_added_lines_get_new_side_numbers():
    auth = diffparse.parse(SAMPLE)[0]
    # Hunk starts at new line 10; three context lines precede the additions.
    assert auth.commentable_lines == {13, 14, 15}
    assert auth.additions == 3
    assert auth.deletions == 0


def test_status_and_binary_detection():
    files = {f.path: f for f in diffparse.parse(SAMPLE)}
    assert files["assets/logo.png"].status == "added"
    assert files["assets/logo.png"].binary is True
    assert files["app/auth.py"].status == "modified"


def test_rename_uses_new_path():
    diff = "diff --git a/old/a.py b/new/a.py\nrename from old/a.py\nrename to new/a.py\n"
    (file,) = diffparse.parse(diff)
    assert file.path == "new/a.py"
    assert file.old_path == "old/a.py"
    assert file.status == "renamed"


def test_exclude_treats_leading_globstar_as_optional():
    patterns = ["**/*.lock", "**/node_modules/**", "**/package-lock.json"]
    assert diffparse.is_excluded("Cargo.lock", patterns)
    assert diffparse.is_excluded("crates/inner/Cargo.lock", patterns)
    assert diffparse.is_excluded("node_modules/left-pad/index.js", patterns)
    assert diffparse.is_excluded("web/node_modules/x/y.js", patterns)
    assert diffparse.is_excluded("package-lock.json", patterns)
    assert not diffparse.is_excluded("app/auth.py", patterns)
    assert not diffparse.is_excluded("app/locket.py", patterns)


def test_filter_drops_binary_and_excluded():
    kept, dropped = diffparse.filter_files(diffparse.parse(SAMPLE), ["**/package-lock.json"])
    assert [f.path for f in kept] == ["app/auth.py"]
    assert sorted(dropped) == ["assets/logo.png", "package-lock.json"]


def test_chunking_keeps_whole_files_together():
    files, _ = diffparse.filter_files(diffparse.parse(SAMPLE), [])
    chunks, truncated = diffparse.chunk(files, chunk_chars=10_000, max_chunks=8)
    assert len(chunks) == 1
    assert truncated is False


def test_chunking_splits_when_over_budget():
    files, _ = diffparse.filter_files(diffparse.parse(SAMPLE), [])
    chunks, truncated = diffparse.chunk(files, chunk_chars=200, max_chunks=8)
    assert len(chunks) > 1
    assert truncated is False
    # Every reviewable patch survives somewhere in the output. The binary file
    # is absent because filter_files drops it before chunking.
    joined = "".join(chunks)
    assert "app/auth.py" in joined and "package-lock.json" in joined
    assert "assets/logo.png" not in joined


def test_chunking_reports_truncation_at_the_cap():
    files, _ = diffparse.filter_files(diffparse.parse(SAMPLE), [])
    chunks, truncated = diffparse.chunk(files, chunk_chars=100, max_chunks=1)
    assert len(chunks) == 1
    assert truncated is True


def test_oversized_single_file_splits_on_hunk_boundaries():
    hunks = "".join(f"@@ -{i},1 +{i},1 @@\n-old{i}\n+new{i}\n" for i in range(1, 40))
    diff = f"diff --git a/big.py b/big.py\n--- a/big.py\n+++ b/big.py\n{hunks}"
    files = diffparse.parse(diff)
    chunks, _ = diffparse.chunk(files, chunk_chars=300, max_chunks=20)
    assert len(chunks) > 1
    # Each slice repeats the file header so the model always knows the path.
    assert all(chunk.startswith("diff --git a/big.py b/big.py") for chunk in chunks)
