"""Manual check: clone a repo with the bot's GitLab PAT and NO SSH key.

Simulates the cluster (token-only, no SSH agent) on your laptop:

  * builds a repo config with local_path=None  -> forces a fresh clone into a
    throwaway workspaces dir (bypasses your local checkout);
  * hard-disables SSH for this process (GIT_SSH_COMMAND=/bin/false) -> if the
    clone still succeeds it PROVES auth went over HTTPS+PAT, not your key;
  * asserts the clone exists, `origin` carries NO token, and a file reads back.

Run (reads GITLAB_URL / GITLAB_TOKEN from your .env via Settings):

    uv run python scripts/verify_token_clone.py bellingshausen

Nothing is deleted; your ~/.ssh is untouched.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from virtual_dev.adapters.vcs import GitIdentity, GitLabVcs
from virtual_dev.infrastructure.config.loader import load_config
from virtual_dev.infrastructure.config.schema import (
    AgentsCfg,
    AppConfig,
    MappingsCfg,
    RepositoryCfg,
)
from virtual_dev.infrastructure.config.settings import Settings


async def main(repo_key: str) -> int:
    settings = Settings()
    if not settings.gitlab_url or not settings.gitlab_token:
        print("!! GITLAB_URL / GITLAB_TOKEN not set (check your .env)")
        return 2

    # Pull the repo's real SSH url from config/repositories.yaml, but drop any
    # local_path so ensure_clone takes the fresh-clone path instead of reusing
    # your existing checkout.
    full = load_config("config")
    src = full.get_repository(repo_key)
    if src is None:
        print(f"!! repo {repo_key!r} not found in config/repositories.yaml")
        return 2
    repo = RepositoryCfg(key=src.key, url=src.url, default_branch=src.default_branch)
    cfg = AppConfig(repositories=[repo], agents=AgentsCfg(), mappings=MappingsCfg())

    workspaces = Path(tempfile.mkdtemp(prefix="verify-clone-"))
    vcs = GitLabVcs(
        config=cfg,
        gitlab_url=settings.gitlab_url,
        gitlab_token=settings.gitlab_token,
        workspaces_dir=workspaces,
        identity=GitIdentity(name="verify", email="verify@example"),
    )

    # The smoking gun: make ANY ssh attempt fail. Clone must not fall back to it.
    os.environ["GIT_SSH_COMMAND"] = "/bin/false"

    print(f">> repo url in config : {src.url}")
    print(f">> cloning into       : {workspaces / repo_key}")
    print(">> SSH is disabled for this process (GIT_SSH_COMMAND=/bin/false)")
    try:
        dest = Path(await vcs.ensure_clone(repo_key))
    except Exception as exc:  # this is a diagnostic script
        print(f"\nFAILED to clone: {exc}")
        return 1

    ok = (dest / ".git").is_dir()
    origin = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=dest, capture_output=True, text=True,
    ).stdout.strip()
    token_leaked = settings.gitlab_token in origin

    print(f"\n.git present          : {ok}")
    print(f"origin url            : {origin}")
    print(f"token NOT in origin   : {not token_leaked}")
    some = sorted(p.name for p in dest.iterdir() if p.name != ".git")[:8]
    print(f"top-level entries     : {some}")

    if ok and not token_leaked:
        print("\nOK — cloned over HTTPS with the PAT, no SSH key, no token in .git/config.")
        return 0
    print("\nSomething is off — see above.")
    return 1


if __name__ == "__main__":
    key = sys.argv[1] if len(sys.argv) > 1 else "bellingshausen"
    raise SystemExit(asyncio.run(main(key)))
