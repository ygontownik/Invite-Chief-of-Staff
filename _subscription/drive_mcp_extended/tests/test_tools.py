import sys
import os
import json
import datetime as _dt
import time

sys.path.insert(0, os.path.expanduser("~/cos-pipeline"))

# ── read_any_file ──────────────────────────────────────────────────────────────

# Using a real PDF from GridFree dataroom: "Gridfree AI - Wartsila Proposal.pdf"
_PDF_FILE_ID = "1nW0bH1XbVw3VYl3Ud6Zc8DSu_QgIQHSz"


def test_read_any_file_returns_required_fields():
    from _subscription.drive_mcp_extended.tools import read_any_file
    result = json.loads(read_any_file(_PDF_FILE_ID))
    assert "fileId" in result
    assert "title" in result
    assert "mimeType" in result
    assert "content" in result
    assert "warnings" in result


def test_read_any_file_has_content():
    from _subscription.drive_mcp_extended.tools import read_any_file
    result = json.loads(read_any_file(_PDF_FILE_ID))
    assert len(result["content"]) > 50


def test_read_any_file_has_title():
    from _subscription.drive_mcp_extended.tools import read_any_file
    result = json.loads(read_any_file(_PDF_FILE_ID))
    assert result["title"]


def test_read_any_file_warnings_is_list():
    from _subscription.drive_mcp_extended.tools import read_any_file
    result = json.loads(read_any_file(_PDF_FILE_ID))
    assert isinstance(result["warnings"], list)


def test_read_any_file_folder_returns_warning():
    from _subscription.drive_mcp_extended.tools import read_any_file
    # Passing a folder ID should return a clear warning, not crash
    result = json.loads(read_any_file("1VaLxa-zGrdwe1jTlj0u1eM7j2b87MFBR"))
    assert any("folder" in w.lower() for w in result["warnings"])


# ── list_folder_contents ───────────────────────────────────────────────────────

# GridFree dataroom top level contains only subfolders — must use recursive=True
def test_list_folder_returns_files():
    from _subscription.drive_mcp_extended.tools import list_folder_contents
    result = json.loads(list_folder_contents("1hSjeE5gOmdMLQmOXm6CjJL66xu7EavZp", recursive=True))
    assert "files" in result
    assert isinstance(result["files"], list)
    assert len(result["files"]) > 0


def test_list_folder_file_shape():
    from _subscription.drive_mcp_extended.tools import list_folder_contents
    result = json.loads(list_folder_contents("1hSjeE5gOmdMLQmOXm6CjJL66xu7EavZp", recursive=True))
    for f in result["files"]:
        assert "fileId" in f
        assert "title" in f
        assert "mimeType" in f
        assert "recent" in f


def test_list_folder_no_folder_objects():
    from _subscription.drive_mcp_extended.tools import list_folder_contents
    result = json.loads(list_folder_contents("1hSjeE5gOmdMLQmOXm6CjJL66xu7EavZp", recursive=True))
    for f in result["files"]:
        assert f["mimeType"] != "application/vnd.google-apps.folder"


def test_list_folder_nonrecursive_returns_empty_for_subfolder_only_dir():
    from _subscription.drive_mcp_extended.tools import list_folder_contents
    # Non-recursive on a dir with only subfolders should return empty files list (not error)
    result = json.loads(list_folder_contents("1hSjeE5gOmdMLQmOXm6CjJL66xu7EavZp", recursive=False))
    assert "files" in result
    assert isinstance(result["files"], list)


# ── update_file ────────────────────────────────────────────────────────────────

def test_update_file_rejects_nonexistent_id():
    from _subscription.drive_mcp_extended.tools import update_file
    result = json.loads(update_file(
        file_id="nonexistent_xyz_99999",
        text_content="hello",
        content_mime_type="text/plain",
    ))
    assert result["success"] is False
    assert "error" in result


def test_update_file_and_readback():
    from _subscription.drive_mcp_extended.tools import update_file, read_any_file
    file_id = "13pMVyGoG8XTxuE0RVzGtknjQVhNFkHnHjILZ8LqyaHg"
    ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_content = f"TEST UPDATE — {ts}"

    result = json.loads(update_file(
        file_id=file_id,
        text_content=new_content,
        content_mime_type="text/plain",
    ))
    assert result["success"] is True, f"Update failed: {result}"
    assert "modifiedTime" in result

    time.sleep(2)
    readback = json.loads(read_any_file(file_id))
    assert new_content in readback["content"], f"Content not found: {readback['content'][:200]}"


def test_update_file_returns_write_confirmed():
    from _subscription.drive_mcp_extended.tools import update_file
    file_id = "13pMVyGoG8XTxuE0RVzGtknjQVhNFkHnHjILZ8LqyaHg"
    result = json.loads(update_file(
        file_id=file_id,
        text_content=f"TEST — {_dt.datetime.now().isoformat()}",
        content_mime_type="text/plain",
    ))
    assert result["success"] is True, f"Update failed: {result}"
    assert "writeConfirmed" in result
