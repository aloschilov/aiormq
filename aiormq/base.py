import abc
import asyncio
import typing
from contextlib import suppress
from functools import wraps
from typing import TypeVar, Type, Union

T = TypeVar('T')


class TaskWrapper:
    def __init__(self, task: asyncio.Task):
        self.task = task
        self.exception = asyncio.CancelledError

    def throw(self, exception):
        self.exception = exception
        self.task.cancel()

    def __await__(self, *args, **kwargs):
        try:
            return (yield from self.task.__await__(*args, **kwargs))
        except asyncio.CancelledError as e:
            raise self.exception from e

    def __getattr__(self, item):
        return getattr(self.task, item)


def create_blank_future(loop):
    future = loop.create_future()
    future.set_result(None)
    return future


class FutureStore:
    __slots__ = "futures", "loop", "parent"

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.futures = set()      # type: typing.Set[TaskWrapper]
        self.loop = loop        # type: asyncio.AbstractEventLoop
        self.parent = None      # type: FutureStore

    def add(self, task: typing.Union[asyncio.Future, TaskWrapper]):
        self.futures.add(task)
        task.add_done_callback(lambda *_: self.futures.remove(task))

        if self.parent:
            self.parent.add(task)

    def reject_all(self, exception: Exception):
        tasks = []

        while self.futures:
            future = self.futures.pop()     # type: TaskWrapper

            if future.done():
                continue

            if isinstance(future, TaskWrapper):
                future.throw(exception)
                tasks.append(future)
            elif isinstance(future, asyncio.Future):
                future.set_exception(exception)

        if not tasks:
            return create_blank_future(self.loop)

        return self.loop.create_task(asyncio.wait(tasks, loop=self.loop))

    def create_task(self, coro: T) -> T:
        task = TaskWrapper(self.loop.create_task(coro))
        self.add(task)
        return task

    def create_future(self):
        future = self.loop.create_future()
        self.add(future)
        return future

    def get_child(self) -> "FutureStore":
        store = FutureStore(self.loop)
        store.parent = self
        return store


class Base:
    __slots__ = 'loop', '__future_store', 'closing'

    def __init__(self, *, loop, parent: 'Base'=None):
        self.loop = loop

        if parent:
            self.__future_store = parent._future_store_child()
        else:
            self.__future_store = FutureStore(loop=self.loop)

        self.closing = self._create_closing_future()

    def _create_closing_future(self):
        future = self.__future_store.create_future()
        future.add_done_callback(lambda x: x.exception())
        return future

    def _cancel_tasks(self, exc: Union[Exception, Type[Exception]]=None):
        return self.__future_store.reject_all(exc)

    def _future_store_child(self):
        return self.__future_store.get_child()

    # noinspection PyShadowingNames
    def create_task(self, coro) -> asyncio.Future:
        return self.__future_store.create_task(coro)

    def create_future(self) -> asyncio.Future:
        return self.__future_store.create_future()

    async def _on_close(self, exc=None):
        pass

    async def __closer(self, exc):
        if self.is_closed:
            return

        with suppress(Exception):
            await self._on_close(exc)

        with suppress(Exception):
            await self._cancel_tasks(exc)

        if self.is_closed:
            return

        self.closing.set_exception(exc)

    def close(self, exc=asyncio.CancelledError()):
        if self.is_closed:
            return create_blank_future(self.loop)
        return self.loop.create_task(self.__closer(exc))

    def __repr__(self):
        cls_name = self.__class__.__name__
        return '<{0}: "{1}">'.format(cls_name, str(self))

    @abc.abstractmethod
    def __str__(self):
        raise NotImplementedError

    @property
    def is_closed(self):
        return self.closing.done()


def task(func: T) -> T:
    @wraps(func)
    async def wrap(self: "Base", *args, **kwargs):
        # noinspection PyCallingNonCallable
        return await self.create_task(func(self, *args, **kwargs))

    return wrap
