"""Shared machinery for engines that drive an official CLI (claude, codex, agy) as a headless child process.

Each call runs the vendor's own binary on the user's cached login, so usage counts against that subscription.
We never read or reuse their credentials. The child gets an empty working directory, a scrubbed environment
(our Jev key never reaches it), a deadline, and is killed with its whole process group if it overruns or the run
is cancelled.

Most of a CLI call's latency is the binary starting up (about 8s of agy's 12s, under 1s for claude), not the model.
Engines that accept a prompt as an NDJSON line on stdin (`persistent`) are therefore started ahead of time: after each
call a replacement with the same flags is spawned and left waiting, so the next call with those flags only pays for
the model. Every warm process still answers exactly one prompt and exits, so no conversation state carries over.
"""
import asyncio
import json
import os
import shutil
import signal
import tempfile
import time

from .base import Engine, EngineError, Reply

TIMEOUT = float(os.environ.get('TG_ENGINE_TIMEOUT', 180))
CONCURRENCY = int(os.environ.get('TG_ENGINE_CONCURRENCY', 2))  # plans have rate limits; don't fan out 10 CLIs at once
SECRET_ENV = ('TYPESAFE_API_KEY',)
LINE_LIMIT = 16 * 1024 * 1024  # one NDJSON event can carry a whole long answer
WARM = int(os.environ.get('TG_WARM_POOL', 3))  # idle pre-started processes kept per engine; 0 turns warm starts off
WARM_TTL = float(os.environ.get('TG_WARM_TTL', 600))  # seconds an idle process may wait before it is discarded


class Parser:
    """Turns one engine's NDJSON events into text deltas and a final Reply."""

    def feed(self, event: dict) -> str | None:
        """Returns a text delta to show, if this event carries one."""
        raise NotImplementedError

    def finish(self, returncode: int, stderr: str) -> Reply:
        """Builds the Reply after the process exits, or raises EngineError."""
        raise NotImplementedError


class CliEngine(Engine):
    billing = 'subscription'
    binary = ''
    bin_env = ''  # env var that overrides the binary path, e.g. TG_CLAUDE_BIN
    drop_env: tuple[str, ...] = ()  # extra variables to hide from this CLI
    login_hint = ''

    def __init__(self, path: str | None = None, timeout: float = TIMEOUT, concurrency: int = CONCURRENCY):
        self.path = path or os.environ.get(self.bin_env) or shutil.which(self.binary)
        self.timeout = timeout
        self.sem = asyncio.Semaphore(concurrency)
        self.workdir = None
        self.warm = WARM
        self.idle: list[tuple[tuple, float, asyncio.subprocess.Process]] = []  # (flags, started at, process), oldest first
        self.refills: set[asyncio.Task] = set()

    def available(self):
        if not self.path:
            return False, f'`{self.binary}` is not installed or not on PATH'
        return True, ''

    def env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV and k not in self.drop_env}
        env.update(NO_COLOR='1', TERM='dumb')
        return env

    def cwd(self) -> str:
        # An empty scratch directory: no project files, no repo instructions for the agent to pick up.
        if self.workdir is None:
            self.workdir = tempfile.mkdtemp(prefix=f'tracegraph-{self.name}-')
            self.prepare(self.workdir)
        return self.workdir

    def prepare(self, workdir: str):
        pass

    def command(self, *, system, prompt, effort, web, schema) -> tuple[list[str], str]:
        """Returns (argv after the binary, stdin text)."""
        raise NotImplementedError

    def persistent(self, *, system, prompt, effort, web, schema) -> tuple[list[str], str] | None:
        """(argv after the binary, one stdin line) for a process that can start before the prompt is known, or None
        when this engine has no stream-json input. The argv must not depend on the prompt: it is the warm-pool key."""
        return None

    def parser(self) -> Parser:
        raise NotImplementedError

    # ---------- warm pool ----------

    def take(self, key: tuple):
        """A live idle process started with these flags, if one is waiting. Expired or dead ones are discarded."""
        now, found, keep = time.monotonic(), None, []
        for k, born, proc in self.idle:
            if proc.returncode is not None or now - born > WARM_TTL:
                kill_group(proc)
            elif found is None and k == key:
                found = proc
            else:
                keep.append((k, born, proc))
        self.idle = keep
        return found

    async def refill(self, key: tuple):
        """Starts a replacement for the process a call just used, evicting the oldest idle one past the cap."""
        try:
            proc = await start(self.path, list(key), env=self.env(), cwd=self.cwd())
        except EngineError:
            return
        self.idle.append((key, time.monotonic(), proc))
        while len(self.idle) > self.warm:
            kill_group(self.idle.pop(0)[2])

    async def prewarm(self, *, system='', effort='medium', web=False, schema=None):
        """Starts a process for calls with these flags before the first one arrives (used when an engine is selected)."""
        spec = self.persistent(system=system, prompt='', effort=effort, web=web, schema=schema) if self.warm else None
        if spec and self.available()[0] and not any(k == tuple(spec[0]) for k, _, _ in self.idle):
            await self.refill(tuple(spec[0]))

    async def stream(self, *, system, prompt, effort='medium', emit_delta=None, max_tokens=2048, web=False, schema=None,
                     exec=False):
        ok, why = self.available()
        if not ok:
            raise EngineError(why)
        # Code execution gets a fresh scratch dir per call, so one run's files never leak into another's.
        scratch = tempfile.mkdtemp(prefix=f'tracegraph-{self.name}-run-') if exec and self.supports_exec else None
        extra = {'exec': True, 'workdir': scratch} if scratch else {}
        spec = None if scratch or not self.warm else self.persistent(system=system, prompt=prompt, effort=effort, web=web,
                                                                      schema=schema)
        try:
            if spec:
                key, line = tuple(spec[0]), spec[1]
                async with self.sem:
                    proc = self.take(key)
                    task = asyncio.create_task(self.refill(key))  # warms up while this call runs
                    self.refills.add(task)
                    task.add_done_callback(self.refills.discard)
                    if proc is None:
                        proc = await start(self.path, list(key), env=self.env(), cwd=self.cwd())
                    return await drive(proc, line, self.parser(), emit_delta, timeout=self.timeout,
                                       login_hint=self.login_hint)
            args, stdin = self.command(system=system, prompt=prompt, effort=effort, web=web, schema=schema, **extra)
            async with self.sem:
                return await run(self.path, args, stdin, self.parser(), emit_delta, env=self.env(), cwd=scratch or self.cwd(),
                                 timeout=self.timeout, login_hint=self.login_hint)
        finally:
            if scratch:
                shutil.rmtree(scratch, ignore_errors=True)

    async def aclose(self):
        for task in list(self.refills):
            task.cancel()
        for _, _, proc in self.idle:
            kill_group(proc)
        self.idle = []
        if self.workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)
            self.workdir = None


def kill_group(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def start(path, args, *, env, cwd):
    try:
        return await asyncio.create_subprocess_exec(
            path, *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env, cwd=cwd, start_new_session=True, limit=LINE_LIMIT)
    except OSError as e:
        raise EngineError(f'could not start {os.path.basename(path)}: {e.strerror or e}')


async def run(path, args, stdin, parser: Parser, emit_delta, *, env, cwd, timeout, login_hint='') -> Reply:
    proc = await start(path, args, env=env, cwd=cwd)
    return await drive(proc, stdin, parser, emit_delta, timeout=timeout, login_hint=login_hint)


async def drive(proc, stdin, parser: Parser, emit_delta, *, timeout, login_hint='') -> Reply:
    """Sends stdin to an already started CLI, then streams its events until it exits."""
    shown: list[str] = []
    stderr_task = asyncio.create_task(proc.stderr.read())

    async def pump():
        try:
            if stdin:
                proc.stdin.write(stdin.encode())
                await proc.stdin.drain()
            proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError):
            pass  # it already exited (a warm process whose login expired); its output and exit code say why
        async for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue  # banners and warnings some CLIs print on stdout
            delta = parser.feed(event) if isinstance(event, dict) else None
            if delta:
                shown.append(delta)
                if emit_delta:
                    emit_delta(delta)
        await proc.wait()

    try:
        async with asyncio.timeout(timeout):
            await pump()
    except TimeoutError:
        kill_group(proc)
        raise EngineError(f'timed out after {timeout:.0f}s', ''.join(shown))
    except BaseException:  # cancelled run, or a parse bug: never leave the CLI running behind us
        kill_group(proc)
        raise
    finally:
        try:
            err = (await asyncio.wait_for(stderr_task, 2)).decode(errors='replace')
        except (TimeoutError, asyncio.CancelledError):
            err = ''
    reply = parser.finish(proc.returncode, err)
    if not reply.text:
        raise EngineError(no_answer_reason(proc.returncode, err, login_hint), ''.join(shown))
    return reply


LOGIN_WORDS = ('not logged in', 'login', 'log in', 'unauthorized', 'authenticate', 'credentials')


def no_answer_reason(code, stderr: str, login_hint: str) -> str:
    tail = ' '.join(stderr.strip().split())[-200:]
    if any(w in tail.lower() for w in LOGIN_WORDS):
        return f'not logged in ({login_hint})' if login_hint else 'not logged in'
    return f'exited with code {code}' + (f': {tail}' if tail else '') if code else 'returned no answer'
