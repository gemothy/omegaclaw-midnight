"""Agent writes a script that creates 3 numbered dirs and runs it.

Graded 0/1/2: weak models (MiniMax) often write only the shebang to the
script via write-file and never call shell to execute it. The diagnostic
prints (wf=, sh=, script_present=, perms=) tell which stage was reached.
"""
import time

from helpers import (
    Checker, dexec, dexec_root, find_skill_calls, make_prompt,
    send_prompt, try_with_clarification,
)

TARGET_DIR = "/tmp/test_dirs"
SCRIPT_PATH = f"{TARGET_DIR}/mkdirs.sh"
EXPECTED_DIRS = ["test1", "test2", "test3"]


def test_run_create_dirs():
    with Checker("create dirs script", cleanup_dirs=[TARGET_DIR]) as c:
        print(f"\n=== create dirs script (run-id {c.run_id}) ===", flush=True)

        c.verify_clean()

        c.step("pre-create target dir 0777 (rules out perms)")
        dexec_root("mkdir", "-p", TARGET_DIR)
        dexec_root("chmod", "777", TARGET_DIR)
        c.ok("pre-create dir", TARGET_DIR)

        start_ts = int(time.time()) - 1

        c.step("send prompt via IRC")
        prompt = make_prompt(
            c.run_id,
            f"Create script {SCRIPT_PATH} that creates dirs test1, test2, "
            f"test3 inside {TARGET_DIR}/. Make it executable, then run "
            f"it. Use ONE write-file with the full body in a single quoted "
            f"string with literal \\n between lines, e.g. "
            f'(write-file {SCRIPT_PATH} "#!/bin/bash\\nmkdir -p '
            f'{TARGET_DIR}/test1 {TARGET_DIR}/test2 {TARGET_DIR}/test3"). '
            f"Then (shell chmod +x {SCRIPT_PATH}) and (shell {SCRIPT_PATH}).",
        )
        if not send_prompt(prompt):
            c.fail("irc", "could not deliver prompt within 60s")
        c.ok("irc", f"run-id={c.run_id}")

        c.step("verify all expected dirs exist (graded)")

        def all_dirs_exist():
            for name in EXPECTED_DIRS:
                if dexec("test", "-d", f"{TARGET_DIR}/{name}").returncode != 0:
                    return None
            return True

        clarification = (
            f"The dirs test1/test2/test3 still do not exist. "
            f"Run EXACTLY these three skill calls in order: "
            f'(write-file {SCRIPT_PATH} "#!/bin/bash\\nmkdir -p '
            f'{TARGET_DIR}/test1 {TARGET_DIR}/test2 {TARGET_DIR}/test3\\n") '
            f"then (shell chmod +x {SCRIPT_PATH}) "
            f"then (shell {SCRIPT_PATH}). "
            f"The body of write-file MUST be one quoted string with the "
            f"literal two characters backslash-n separating lines. "
            f"Do NOT split the body into separate (mkdir ...) calls."
        )
        grade, _ = try_with_clarification(
            c, all_dirs_exist, clarification,
            timeout_first=120, timeout_second=180,
        )
        c.set_grade(grade)

        wf = find_skill_calls(c.run_id, "write-file") or []
        sh = find_skill_calls(c.run_id, "shell") or []
        script_present = dexec("test", "-f", SCRIPT_PATH).returncode == 0
        perms = (
            dexec("stat", "-c", "%A", SCRIPT_PATH).stdout.strip()
            if script_present else "<no script>"
        )
        existing = [
            n for n in EXPECTED_DIRS
            if dexec("test", "-d", f"{TARGET_DIR}/{n}").returncode == 0
        ]
        print(
            f"       diagnostics: wf={len(wf)} sh={len(sh)} "
            f"script_present={script_present} perms={perms} dirs={existing}",
            flush=True,
        )

        if grade == Checker.GRADE_FAIL:
            missing = [n for n in EXPECTED_DIRS if n not in existing]
            c.fail(
                "dirs exist",
                f"missing after clarification: {missing}. "
                f"wf={len(wf)} sh={len(sh)} "
                f"script_present={script_present} perms={perms}",
            )
        c.ok("dirs exist", f"{EXPECTED_DIRS} (grade={grade})")

        c.step("verify (write-file ...) targeted mkdirs.sh")
        if not any(SCRIPT_PATH in a or "mkdirs.sh" in a for a in wf):
            c.fail("write-file mkdirs.sh", f"no write-file mentioned mkdirs.sh: {wf[:3]}")
        c.ok("write-file mkdirs.sh", f"{len(wf)} write-file calls")

        c.step("verify (shell ...) was invoked to run the script")
        if not any("mkdirs.sh" in a or SCRIPT_PATH in a for a in sh):
            c.fail("shell invoked", f"no shell call referencing mkdirs.sh: {sh[:3]}")
        c.ok("shell invoked", f"{len(sh)} shell calls")

        c.step("check directory mtimes are fresh")
        for name in EXPECTED_DIRS:
            res = dexec("stat", "-c", "%Y", f"{TARGET_DIR}/{name}")
            if res.returncode != 0 or int(res.stdout.strip()) < start_ts:
                c.fail("dir timestamps", f"{name} has stale or missing mtime")
        c.ok("dir timestamps", f"all >= {start_ts}")

        c.done()
