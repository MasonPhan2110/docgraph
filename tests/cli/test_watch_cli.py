from unittest.mock import patch, MagicMock

import pytest

from docgraph.cli_watch import run_watch_command


def _resp(status_code: int, json_body: dict):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body
    r.raise_for_status = MagicMock()
    return r


def test_enable_command(capsys):
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.post.return_value = _resp(
            202, {"enabled": True, "dirs": 0, "reconcile_started": True}
        )
        rc = run_watch_command(["enable"], "http://127.0.0.1:8088")
    assert rc == 0
    out = capsys.readouterr().out
    assert "enabled" in out.lower()


def test_status_command(capsys):
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.get.return_value = _resp(200, {
            "enabled": True, "running": True, "dirs_count": 2,
            "queue_depth": 0, "queue_capacity": 500, "workers": 4,
            "last_enabled_at": None, "stats": {
                "events_received": 0, "events_debounced": 0, "events_processed": 0,
                "events_dropped_queue_full": 0, "reconcile_runs": 1, "last_reconcile_at": None,
            },
        })
        rc = run_watch_command(["status"], "http://127.0.0.1:8088")
    assert rc == 0


def test_add_command(capsys, tmp_path):
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.post.return_value = _resp(
            201, {"id": "wd_x", "path": str(tmp_path), "scheduled": True}
        )
        rc = run_watch_command(
            ["add", str(tmp_path), "--folder", "notes", "--tags", "a,b"],
            "http://127.0.0.1:8088",
        )
    assert rc == 0


def test_server_unreachable(capsys):
    import httpx
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.get.side_effect = httpx.ConnectError("refused")
        rc = run_watch_command(["status"], "http://127.0.0.1:8088")
    assert rc == 1
    err = capsys.readouterr().err
    assert "server not running" in err.lower()


def test_disable_command(capsys):
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.post.return_value = _resp(
            200, {"enabled": False, "queue_drained": 5, "queue_dropped": 0}
        )
        rc = run_watch_command(["disable"], "http://127.0.0.1:8088")
    assert rc == 0


def test_list_command(capsys, tmp_path):
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.get.return_value = _resp(200, {
            "dirs": [
                {"id": "wd_a", "path": str(tmp_path), "folder": "notes",
                 "tags": ["personal"], "ignore_globs": [],
                 "created_at": "2026-06-07T00:00:00Z", "doc_count": 3},
            ],
        })
        rc = run_watch_command(["list"], "http://127.0.0.1:8088")
    assert rc == 0
    out = capsys.readouterr().out
    assert "wd_a" in out
    assert "docs=3" in out


def test_list_command_empty(capsys):
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.get.return_value = _resp(200, {"dirs": []})
        rc = run_watch_command(["list"], "http://127.0.0.1:8088")
    assert rc == 0
    assert "no watched dirs" in capsys.readouterr().out.lower()


def test_remove_command(capsys):
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.delete.return_value = _resp(
            200, {"id": "wd_a", "deleted_docs": 0, "unwatched": True}
        )
        rc = run_watch_command(["remove", "wd_a"], "http://127.0.0.1:8088")
    assert rc == 0
    # Verify default delete_docs=false was passed.
    call = m.return_value.__enter__.return_value.delete.call_args
    assert call.kwargs["params"]["delete_docs"] == "false"


def test_remove_command_with_delete_docs(capsys):
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.delete.return_value = _resp(
            200, {"id": "wd_a", "deleted_docs": 7, "unwatched": True}
        )
        rc = run_watch_command(
            ["remove", "wd_a", "--delete-docs"], "http://127.0.0.1:8088",
        )
    assert rc == 0
    call = m.return_value.__enter__.return_value.delete.call_args
    assert call.kwargs["params"]["delete_docs"] == "true"


def test_reconcile_command(capsys):
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.post.return_value = _resp(
            200, {"reconcile_started": True, "dirs": 2}
        )
        rc = run_watch_command(["reconcile"], "http://127.0.0.1:8088")
    assert rc == 0


def test_http_error_returns_exit_code_2(capsys):
    import httpx
    err_response = MagicMock()
    err_response.status_code = 409
    err_response.text = '{"detail": "watcher transition in progress"}'
    err = httpx.HTTPStatusError("conflict", request=MagicMock(), response=err_response)
    with patch("docgraph.cli_watch.httpx.Client") as m:
        m.return_value.__enter__.return_value.post.return_value = _resp(409, {})
        m.return_value.__enter__.return_value.post.return_value.raise_for_status.side_effect = err
        rc = run_watch_command(["enable"], "http://127.0.0.1:8088")
    assert rc == 2
    stderr = capsys.readouterr().err
    assert "409" in stderr
