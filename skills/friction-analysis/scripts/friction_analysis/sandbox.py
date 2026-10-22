"""Run the CLI under test in a throwaway sandbox.

The sandbox is the only way this skill executes anything. It owns HOME, every XDG
directory, TMPDIR and the working directory (a fresh, empty, non-git directory),
starts from an empty environment instead of inheriting the caller's, routes HTTP
through a loopback discard port, closes stdin, and kills the whole process group
on timeout. The CLI-specific switches that disable telemetry, daemons and network
calls come from the user (``--env`` / ``--env-file``): this module cannot know them.

This redirects where a well-behaved CLI reads and writes. It is not a filesystem
boundary: the CLI runs with the caller's permissions and can open absolute paths.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# port 9 is the discard service; nothing listens there on a normal machine, so
# any HTTP call the CLI makes fails fast instead of reaching a real endpoint
DISCARD_URL = 'http://127.0.0.1:9'
SAFE_PATH = os.pathsep.join(['/usr/bin', '/bin', '/usr/sbin', '/sbin'])

# keys the sandbox owns; a user env that tries to set them is rejected, so a replay
# cannot be pointed at the real HOME or routed around the loopback discard proxy
PROXY_KEYS = frozenset(
    name
    for base in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY', 'FTP_PROXY')
    for name in (base, base.lower())
)
RESERVED_KEYS = (
    frozenset(
        {
            'HOME',
            'USERPROFILE',
            'XDG_CONFIG_HOME',
            'XDG_CACHE_HOME',
            'XDG_DATA_HOME',
            'XDG_STATE_HOME',
            'XDG_RUNTIME_DIR',
            'TMPDIR',
            'TMP',
            'TEMP',
            'PATH',
        }
    )
    | PROXY_KEYS
)
# after a timeout kill, how long to keep draining pipes a detached descendant may hold open
CLEANUP_SECONDS = 2.0
# help text is small; anything past this is a runaway writer, not signal
MAX_OUTPUT_BYTES = 4 * 1024 * 1024
ENV_KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


@dataclass
class RunResult:
    exit_code: int | None  # None means the call timed out
    stdout: str
    stderr: str


@dataclass
class Sandbox:
    binary: list[str]
    user_env: dict[str, str] = field(default_factory=dict)
    timeout: float = 10.0
    root: Path | None = None

    def __enter__(self) -> Sandbox:
        self.root = Path(tempfile.mkdtemp(prefix='friction-sandbox-')).resolve()
        for name in (
            'home',
            'home/.config',
            'home/.cache',
            'home/.local/share',
            'home/.local/state',
            'home/run',
            'tmp',
            'cwd',
        ):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        os.chmod(self.root / 'home' / 'run', 0o700)
        return self

    def __exit__(self, *exc: object) -> None:
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)

    @property
    def cwd(self) -> Path:
        assert self.root is not None
        return self.root / 'cwd'

    def env(self) -> dict[str, str]:
        assert self.root is not None
        home = self.root / 'home'
        false_bin = shutil.which('false') or '/usr/bin/false'
        env = {
            'PATH': SAFE_PATH,
            'HOME': str(home),
            'USERPROFILE': str(home),
            'XDG_CONFIG_HOME': str(home / '.config'),
            'XDG_CACHE_HOME': str(home / '.cache'),
            'XDG_DATA_HOME': str(home / '.local/share'),
            'XDG_STATE_HOME': str(home / '.local/state'),
            'XDG_RUNTIME_DIR': str(home / 'run'),
            'TMPDIR': str(self.root / 'tmp'),
            'TMP': str(self.root / 'tmp'),
            'TEMP': str(self.root / 'tmp'),
            'LANG': 'en_US.UTF-8',
            'TERM': 'dumb',
            'NO_COLOR': '1',
            'CLICOLOR': '0',
            'DO_NOT_TRACK': '1',
            'HTTP_PROXY': DISCARD_URL,
            'HTTPS_PROXY': DISCARD_URL,
            'ALL_PROXY': DISCARD_URL,
            'http_proxy': DISCARD_URL,
            'https_proxy': DISCARD_URL,
            'all_proxy': DISCARD_URL,
            'NO_PROXY': '',
            'no_proxy': '',
            'BROWSER': false_bin,
            'EDITOR': false_bin,
            'VISUAL': false_bin,
            'PAGER': 'cat',
            'GIT_PAGER': 'cat',
            'GIT_TERMINAL_PROMPT': '0',
            'GIT_CONFIG_NOSYSTEM': '1',
            'GIT_CEILING_DIRECTORIES': str(self.root),
        }
        # windows needs these to start any process at all
        for key in ('SYSTEMROOT', 'COMSPEC', 'WINDIR'):
            if key in os.environ:
                env[key] = os.environ[key]
        env.update(self.user_env)
        return env

    def scrub(self, text: str) -> str:
        """Replace sandbox paths in captured output so reports never carry machine paths."""
        if self.root is None:
            return text
        return text.replace(str(self.root), '<sandbox>')

    def run(self, args: list[str]) -> RunResult:
        assert self.root is not None
        popen_kwargs: dict[str, object] = {}
        if os.name == 'posix':
            popen_kwargs['start_new_session'] = True
        # output goes to temporary files, not pipes: a detached descendant that
        # keeps a pipe open can block a pipe reader (on Windows even the cleanup),
        # but it cannot stop us from reading a file once the direct child is gone
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(
                self.binary + args,
                cwd=self.cwd,
                env=self.env(),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                **popen_kwargs,  # type: ignore[arg-type]
            )
            code: int | None
            try:
                code = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                kill_tree(process)
                try:
                    process.wait(timeout=CLEANUP_SECONDS)
                except subprocess.TimeoutExpired:
                    pass  # the direct child survived a kill; do not block the analysis on it
                code = None
            stdout, stderr = read_capped(out), read_capped(err)
        return RunResult(
            code, self.scrub(stdout.decode('utf-8', 'replace')), self.scrub(stderr.decode('utf-8', 'replace'))
        )


def read_capped(fh) -> bytes:
    """Read what the command wrote, bounded so a runaway writer cannot exhaust memory."""
    fh.seek(0)
    return fh.read(MAX_OUTPUT_BYTES)


def kill_tree(process: subprocess.Popen) -> None:
    if os.name == 'posix':
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    process.kill()


def resolve_binary(command: str) -> list[str]:
    """Split the --binary string and pin argv[0] to an absolute path before PATH is narrowed."""
    parts = shlex.split(command)
    if not parts:
        raise ValueError('empty --binary')
    resolved = shutil.which(parts[0])
    if resolved is None and not Path(parts[0]).exists():
        raise ValueError(f'binary not found: {parts[0]}')
    parts[0] = str(Path(resolved or parts[0]).resolve())
    return parts


def load_env(pairs: list[str], files: list[Path]) -> dict[str, str]:
    """Parse KEY=VALUE pairs from env files and flags; reject keys the sandbox owns."""
    env: dict[str, str] = {}
    lines: list[tuple[str, str]] = []
    for path in files:
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            stripped = line.strip()
            if stripped and not stripped.startswith('#'):
                lines.append((f'{path.name}:{number}', stripped.removeprefix('export ').strip()))
    lines.extend(('--env', pair) for pair in pairs)
    for origin, line in lines:
        key, sep, value = line.partition('=')
        key = key.strip()
        if not sep or not ENV_KEY.match(key):
            raise ValueError(f'{origin}: expected KEY=VALUE')
        if key in RESERVED_KEYS or key.startswith('XDG_'):
            raise ValueError(f'{origin}: {key} is owned by the sandbox and cannot be overridden')
        env[key] = value.strip().strip('"').strip("'")
    return env


def warn(message: str) -> None:
    print(f'friction-analysis: {message}', file=sys.stderr)
