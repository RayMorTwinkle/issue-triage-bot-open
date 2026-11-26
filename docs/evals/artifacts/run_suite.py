"""Run the private unittest suite in an exported checkout on macOS, without user credentials."""
import argparse
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--without-gh-fixture", action="store_true")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="itb-suite-") as temp:
        root = Path(temp)
        bindir = root / "bin"
        bindir.mkdir()
        if not args.without_gh_fixture:
            gh = bindir / "gh"
            gh.write_text("""#!/bin/sh
if [ "$#" = 4 ] && [ "$1" = api ] && [ "$2" = repos/a/b/releases/latest ] && [ "$3" = --jq ] && [ "$4" = .tag_name ]; then
    echo v0.0.0-fixture
    exit 0
fi
echo "offline fixture: unhandled gh request" >&2
exit 2
""")
            gh.chmod(0o755)
        scratch = root / "tmp"
        scratch.mkdir()
        env = dict(PATH=str(bindir) + ":/usr/bin:/bin:/usr/sbin:/sbin",
                   HOME=temp, TMPDIR=str(scratch), LANG="en_US.UTF-8",
                   PYTHONDONTWRITEBYTECODE="1", GIT_CONFIG_NOSYSTEM="1",
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_ALLOW_PROTOCOL="file",
                   GIT_AUTHOR_NAME="Fixture", GIT_COMMITTER_NAME="Fixture",
                   GIT_AUTHOR_EMAIL="fixture@example.invalid",
                   GIT_COMMITTER_EMAIL="fixture@example.invalid")
        proc = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                              env=env, cwd=args.checkout)
        return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
