"""
Minimal GitHub Contents API client for reading and writing JSON files
in the user's own repo. Used to persist the HR tracker across Streamlit
Cloud redeploys (which wipe the ephemeral file system).

Auth: GitHub Personal Access Token (fine-grained, Contents R/W on the
target repo only).

Endpoints used:
  GET  /repos/{owner}/{repo}/contents/{path}   — fetch file (returns base64 + sha)
  PUT  /repos/{owner}/{repo}/contents/{path}   — create/update file
"""
import base64
import json
import os
import ssl as _ssl_compat
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

_UNVERIFIED_SSL = _ssl_compat._create_unverified_context()
API = "https://api.github.com"


class GitHubStorageError(Exception):
    pass


def _headers(token: str) -> dict:
    return {
        "Authorization":        f"Bearer {token}",
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent":           "mlb-dashboard/1.0",
    }


def _request(method: str, url: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=_headers(token))
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20, context=_UNVERIFIED_SSL) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read().decode())
        except Exception:
            err_body = {"message": "unknown error"}
        return e.code, err_body
    except urllib.error.URLError as e:
        raise GitHubStorageError(f"Network error: {e.reason}")


def get_file(token: str, owner: str, repo: str, path: str) -> dict | None:
    """
    Fetch a file's contents and SHA. Returns dict with keys:
      {sha, content (decoded str), parsed (json-decoded if applicable)}
    Returns None if file doesn't exist (404).
    Raises GitHubStorageError on other failures.
    """
    url = f"{API}/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}"
    status, body = _request("GET", url, token)
    if status == 404:
        return None
    if status != 200:
        raise GitHubStorageError(f"GitHub GET {path} failed [{status}]: {body.get('message')}")
    raw = base64.b64decode(body.get("content", "")).decode("utf-8")
    out = {"sha": body.get("sha"), "content": raw, "parsed": None}
    try:
        out["parsed"] = json.loads(raw)
    except json.JSONDecodeError:
        pass
    return out


def put_file(token: str, owner: str, repo: str, path: str,
             content: str, message: str, branch: str = "main") -> dict:
    """
    Create or update a file at `path` with the given UTF-8 string content.
    Auto-handles sha lookup for updates.

    Returns the GitHub API response dict.
    """
    existing = get_file(token, owner, repo, path)
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch":  branch,
    }
    if existing and existing.get("sha"):
        payload["sha"] = existing["sha"]

    url = f"{API}/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}"
    status, body = _request("PUT", url, token, payload)
    if status not in (200, 201):
        raise GitHubStorageError(
            f"GitHub PUT {path} failed [{status}]: {body.get('message')}")
    return body


def list_dir(token: str, owner: str, repo: str, path: str) -> list:
    """
    List files in a directory. Returns list of {name, path, sha, size, type}.
    Empty list if dir doesn't exist.
    """
    url = f"{API}/repos/{owner}/{repo}/contents/{urllib.parse.quote(path)}"
    status, body = _request("GET", url, token)
    if status == 404:
        return []
    if status != 200:
        return []
    if not isinstance(body, list):
        return []
    return [{
        "name":  item.get("name"),
        "path":  item.get("path"),
        "sha":   item.get("sha"),
        "size":  item.get("size"),
        "type":  item.get("type"),
    } for item in body]


def save_json(token: str, owner: str, repo: str, path: str, data: dict,
              commit_msg: str | None = None, branch: str = "main") -> dict:
    """Convenience wrapper: serialize dict → write as JSON file."""
    content = json.dumps(data, indent=2, default=str)
    msg = commit_msg or f"Auto-save {path} @ {datetime.utcnow().isoformat()}Z"
    return put_file(token, owner, repo, path, content, msg, branch)


def load_json(token: str, owner: str, repo: str, path: str) -> dict | None:
    """Convenience wrapper: read JSON file and return parsed dict (or None)."""
    res = get_file(token, owner, repo, path)
    if res is None:
        return None
    return res.get("parsed")


if __name__ == "__main__":
    # Sanity test
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not tok:
        print("Set GITHUB_TOKEN env var to test.")
        raise SystemExit(0)
    OWNER, REPO = "Theo1984-ai", "MLB-DASHBOARD-"
    # Test write
    test_payload = {"test": True, "at": datetime.utcnow().isoformat()}
    r = save_json(tok, OWNER, REPO, "hr_tracker/_test.json", test_payload,
                  commit_msg="github_storage.py self-test")
    print(f"Wrote: {r.get('content', {}).get('html_url')}")
    # Test read back
    loaded = load_json(tok, OWNER, REPO, "hr_tracker/_test.json")
    print(f"Read back: {loaded}")
