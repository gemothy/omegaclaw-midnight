"""Agent clones a public git repository over anonymous HTTPS."""
from helpers import (
    Checker,
    dexec,
    dexec_root,
    find_skill_calls,
    get_git_remote,
    make_prompt,
    send_prompt,
    try_with_clarification,
)

TARGET_DIR = "/tmp/git_pull"


def _normalize_git_url(url: str) -> str:
    """
    Normalize git URL for comparison.

    Examples:
    https://github.com/org/repo.git -> https://github.com/org/repo
    https://github.com/org/repo/    -> https://github.com/org/repo
    """
    url = (url or "").strip()

    if url.endswith("/"):
        url = url.rstrip("/")

    if url.endswith(".git"):
        url = url[:-4]

    return url


def test_git_pull_public():
    remote = get_git_remote()

    with Checker("git pull from public repo", cleanup_dirs=[TARGET_DIR]) as c:
        print(f"\n=== git pull public (run-id {c.run_id}) ===", flush=True)

        c.verify_clean()

        c.step("pre-create parent dir")
        dexec_root("mkdir", "-p", TARGET_DIR)
        dexec_root("chmod", "777", TARGET_DIR)
        dexec_root("git", "config", "--global", "--add", "safe.directory", TARGET_DIR)
        c.ok("pre-create dir", TARGET_DIR)

        c.step("send prompt via IRC")
        prompt = make_prompt(
            c.run_id,
            f"Clone the public git repository {remote} into {TARGET_DIR}/. "
            "Use anonymous HTTPS, no credentials are needed. "
            "Do not initialize an empty git repository. "
            "Use git clone. "
            "After cloning, list the files in the repository root.",
        )

        if not send_prompt(prompt):
            c.fail("irc", "could not deliver prompt within 60s")

        c.ok("irc", f"run-id={c.run_id}")

        c.step("wait for valid cloned repository (graded)")

        def has_clone():
            # 1. .git must exist
            git_dir = dexec("test", "-d", f"{TARGET_DIR}/.git")
            if git_dir.returncode != 0:
                return None

            # 2. HEAD must point to a real commit
            head = dexec_root(
                "git",
                "-C",
                TARGET_DIR,
                "rev-parse",
                "--verify",
                "HEAD",
            )
            if head.returncode != 0 or not head.stdout.strip():
                return None

            # 3. HEAD must contain at least one tracked file
            files = dexec_root(
                "git",
                "-C",
                TARGET_DIR,
                "ls-tree",
                "-r",
                "--name-only",
                "HEAD",
            )
            if files.returncode != 0 or not files.stdout.strip():
                return None

            # 4. origin must exist
            origin = dexec_root(
                "git",
                "-C",
                TARGET_DIR,
                "remote",
                "get-url",
                "origin",
            )
            if origin.returncode != 0 or not origin.stdout.strip():
                return None

            expected_remote = _normalize_git_url(remote)
            actual_remote = _normalize_git_url(origin.stdout.strip())

            if expected_remote != actual_remote:
                print(
                    f"       [WARN] origin mismatch: expected={expected_remote!r}, actual={actual_remote!r}",
                    flush=True,
                )
                return None

            return True

        clarification = (
            f"Remove any incomplete directory first, then run: "
            f"`rm -rf {TARGET_DIR} && git clone {remote} {TARGET_DIR}`. "
            f"Do not use `git init`. "
            f"After cloning, run `git -C {TARGET_DIR} ls-tree -r --name-only HEAD` "
            f"and list the files in the repository root."
        )

        grade, _ = try_with_clarification(
            c,
            has_clone,
            clarification,
            timeout_first=120,
            timeout_second=180,
        )

        c.set_grade(grade)

        if grade == Checker.GRADE_FAIL:
            c.fail(
                "clone",
                f"valid cloned repository not present at {TARGET_DIR} after clarification",
            )

        c.ok("clone", f"valid cloned repository present (grade={grade})")

        c.step("verify clone has at least one tracked file")
        ls = dexec_root(
            "git",
            "-C",
            TARGET_DIR,
            "ls-tree",
            "-r",
            "--name-only",
            "HEAD",
        )
        files = [f for f in ls.stdout.split() if f]

        if not files:
            c.fail("tracked files", f"no files in HEAD: {ls.stdout!r}")

        c.ok("tracked files", f"{len(files)} files, e.g. {files[:3]}")

        c.step("verify HEAD has a commit")
        log = dexec_root(
            "git",
            "-C",
            TARGET_DIR,
            "log",
            "--format=%H %s",
            "-1",
        )
        head = log.stdout.strip()

        if not head:
            c.fail("HEAD", f"no commits visible: {log.stderr!r}")

        c.ok("HEAD", head[:80])

        c.step("verify origin remote")
        origin = dexec_root(
            "git",
            "-C",
            TARGET_DIR,
            "remote",
            "get-url",
            "origin",
        )
        actual_remote = _normalize_git_url(origin.stdout.strip())
        expected_remote = _normalize_git_url(remote)

        if origin.returncode != 0 or actual_remote != expected_remote:
            c.fail(
                "origin",
                f"origin mismatch: expected={expected_remote!r}, actual={actual_remote!r}",
            )

        c.ok("origin", actual_remote)

        c.step("verify agent invoked shell with git clone")
        sh_calls = find_skill_calls(c.run_id, "shell") or []

        if not any("git" in a and "clone" in a for a in sh_calls):
            print(
                "       [WARN] no shell call combined 'git' and 'clone'",
                flush=True,
            )

        c.ok("shell clone", f"{len(sh_calls)} shell calls")

        c.done()