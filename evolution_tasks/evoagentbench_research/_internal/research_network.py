"""Fixed public endpoints through Unix sockets into a networkless container.

No shell, destination selection protocol, host network or extra mounts. The
candidate can use public MCP, scoped Qwen and public session Redis only.
"""

import asyncio
import os
import sys
import threading
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager, suppress
from functools import partial
from pathlib import Path

PORTS = {"mcp": 19116, "model": 19118, "redis": 49154}


async def _relay(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    socket_path: Path | None = None,
    port: int | None = None,
) -> None:
    peer = None
    tasks: list[asyncio.Task[None]] = []
    try:
        if socket_path is not None:
            incoming, peer = await asyncio.open_unix_connection(str(socket_path))
        else:
            incoming, peer = await asyncio.open_connection("127.0.0.1", port)

        async def pump(
            source: asyncio.StreamReader, destination: asyncio.StreamWriter
        ) -> None:
            while data := await source.read(65536):
                destination.write(data)
                await destination.drain()

        tasks = [
            asyncio.create_task(pump(reader, peer)),
            asyncio.create_task(pump(incoming, writer)),
        ]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except (ConnectionError, OSError):
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for stream in (peer, writer):
            if stream is not None:
                stream.close()
                with suppress(ConnectionError, OSError):
                    await stream.wait_closed()


@contextmanager
def _servers(  # noqa: C901 - one owner binds and closes the finite endpoint set
    directory: Path, endpoints: Mapping[str, int], *, host: bool
) -> Iterator[None]:
    if not endpoints or not set(endpoints) <= PORTS.keys():
        raise ValueError("Only fixed public endpoint names are allowed")
    directory.mkdir(parents=True, exist_ok=True)
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    errors: list[BaseException] = []

    async def serve() -> None:
        servers: list[asyncio.AbstractServer] = []
        try:
            for name, port in endpoints.items():
                path = directory / (name + ".sock")
                if host:
                    bind_path = (
                        Path(f"/proc/self/fd/{directory_fd}") / (name + ".sock")
                        if sys.platform == "linux"
                        else path
                    )
                    servers.append(
                        await asyncio.start_unix_server(
                            partial(_relay, port=port), path=str(bind_path)
                        )
                    )
                    path.chmod(0o600)
                else:
                    servers.append(
                        await asyncio.start_server(
                            partial(_relay, socket_path=path), "127.0.0.1", port
                        )
                    )
            ready.set()
            await asyncio.Future()
        finally:
            for server in servers:
                server.close()
                await server.wait_closed()

    def owner() -> None:
        asyncio.set_event_loop(loop)
        task = loop.create_task(serve())
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
        except BaseException as error:
            errors.append(error)
            ready.set()
        finally:
            pending = asyncio.all_tasks(loop)
            for item in pending:
                item.cancel()
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.close()

    thread = threading.Thread(target=owner, daemon=True, name="research-public-relay")
    thread.start()
    if not ready.wait(10):
        raise TimeoutError("Public relay startup")
    try:
        if errors:
            raise errors[0]
        yield
    finally:
        if not loop.is_closed():
            loop.call_soon_threadsafe(
                lambda: [task.cancel() for task in asyncio.all_tasks(loop)]
            )
        thread.join(10)
        if thread.is_alive():
            raise TimeoutError("Public relay cleanup")
        if host:
            for name in endpoints:
                with suppress(FileNotFoundError):
                    os.unlink(name + ".sock", dir_fd=directory_fd)
        os.close(directory_fd)


def host_relays(
    directory: Path, endpoints: Mapping[str, int] = PORTS
) -> AbstractContextManager[None]:
    """Private host worker owns socket to fixed loopback endpoints."""
    if endpoints is PORTS:
        endpoints = {
            name: int(os.environ.get("EAB_PUBLIC_" + name.upper() + "_PORT", str(port)))
            for name, port in endpoints.items()
        }
    return _servers(directory, endpoints, host=True)


def worker_relays(
    directory: Path, endpoints: Mapping[str, int] = PORTS
) -> AbstractContextManager[None]:
    """Container loopback reaches only its public socket directory."""
    return _servers(directory, endpoints, host=False)


if __name__ == "__main__":
    from benchmarks.evoagentbench.runners.runtime_worker import main

    with worker_relays(Path("/workspace/research-sockets")):
        main()
