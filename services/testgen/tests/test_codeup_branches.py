"""CodeupClient.list_branches / list_commits 解析单测（只读 API，mock 网络层）。

仅校验「响应 → 结构化字典」的映射正确性，不发真实 HTTP 请求。
"""

from __future__ import annotations

from engine.codeup_client import CodeupClient


def _client() -> CodeupClient:
    return CodeupClient(token="pt-x", org_id="org", verify_ssl=False)


def test_list_branches_parses_commit_and_flags() -> None:
    c = _client()
    c._get = lambda path, params=None: {
        "result": [
            {
                "name": "main",
                "defaultBranch": True,
                "protected": True,
                "commit": {
                    "shortId": "abc123",
                    "title": "init",
                    "message": "initial commit",
                    "authorName": "a",
                    "authoredDate": "2024-01-01T00:00:00Z",
                    "committedDate": "2024-01-02T00:00:00Z",
                },
            },
            {
                "name": "dev",
                "commit": {
                    "shortId": "def456",
                    "title": "feat: x",
                    "authoredDate": "2024-02-01T00:00:00Z",
                },
            },
        ]
    }
    branches = c.list_branches("repo")
    assert len(branches) == 2
    b0 = branches[0]
    assert b0["name"] == "main"
    assert b0["default_branch"] is True
    assert b0["protected"] is True
    assert b0["commit"]["short_id"] == "abc123"
    assert b0["commit"]["title"] == "init"
    b1 = branches[1]
    assert b1["name"] == "dev"
    assert b1["default_branch"] is False
    assert b1["commit"]["title"] == "feat: x"


def test_list_commits_parses() -> None:
    c = _client()
    c._get = lambda path, params=None: {
        "result": [
            {
                "id": "id1",
                "shortId": "s1",
                "title": "c1",
                "authorName": "a",
                "committedDate": "2024-03-01T00:00:00Z",
            },
        ]
    }
    commits = c.list_commits("repo", ref_name="main")
    assert len(commits) == 1
    assert commits[0]["short_id"] == "s1"
    assert commits[0]["title"] == "c1"
    assert commits[0]["author_name"] == "a"
