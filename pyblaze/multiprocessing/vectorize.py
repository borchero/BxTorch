import os
import functools
import numpy as np
import torch.multiprocessing as mp
from pyblaze.utils.stdmp import terminate

class Vectorizer:
    """
    The Vectorizer class ought to be used in cases where a result tensor of size N is filled with
    values computed in some complex way. The computation of these N computations can then be
    parallelized over multiple processes.
    """

    # MARK: Initialization
    def __init__(self, worker_func, worker_init=None, callback_func=None, num_workers=-1, **kwargs):
        """
        Initializes a new vectorizer.

        Parameters
        ----------
        worker_func: callable
            The function which receives as input an item of the input to process and outputs a
            value which ought to be returned.
        worker_init: callable, default: None
            The function receives as input the rank of the worker (i.e. every time this function is
            called, it is called with a different integer as parameter). Its return values are
            passed as *last* parameters to `worker_func` upon every invocation.
        callback_func: callable, default: None
            A function to call after every item has been processed. Must not need to be a free
            function as it is called on the main thread.
        num_workers: int, default: -1
            The number of processes to use. If set to -1, it defaults to the number of available
            cores. If set to 0, everything is executed on the main thread.
        kwargs: keyword arguments
            Additional arguments passed to the worker initialization function.
        """
        self.num_workers = os.cpu_count() if num_workers == -1 else num_workers
        self.worker_func = worker_func
        self.worker_init = worker_init
        self.callback_func = callback_func
        self.init_kwargs = kwargs
        self._shutdown_fn = None

    # MARK: Instance Methods
    def process(self, items, *args):
        """
        Uses the vectorizer's worker function in order to process all items in parallel.

        Parameters
        ----------
        items: list of object or iterable of object
            The items to be processed by the workers. If given as iterable only (i.e. it does not
            support index access), the performance might suffer slightly due to an increased number
            of synchronizations.
        args: variadic arguments
            Additional arguments passed directly to the worker function.

        Returns
        -------
        list of object
            The output generated by the worker function for each of the input items.
        """
        if self.num_workers == 0: # execute sequentially
            result = []
            init = _init_if_needed(self.worker_init, 0, **self.init_kwargs)
            all_args = _combine_args(args, init)
            for item in items:
                result.append(self.worker_func(item, *all_args))
            return result

        process_batches = hasattr(items, '__getitem__')

        if process_batches:
            result = self._process_batches(items, *args)
        else:
            result = self._process_consumers(items, *args)

        self._shutdown_fn()
        self._shutdown_fn = None
        return result

    # MARK: Private Methods
    def _process_batches(self, items, *args):
        num_items = len(items)

        splits = np.array_split(np.arange(num_items), self.num_workers)
        splits = [0] + [a[-1] + 1 for a in splits]

        result = []

        processes = []
        queues = []
        done = mp.Event()
        if self.callback_func is not None:
            tick_queue = mp.Queue()
        else:
            tick_queue = None

        for i in range(self.num_workers):
            queue = mp.Queue()
            process = mp.Process(
                target=_batch_worker,
                args=(
                    queue, done, tick_queue, i,
                    self.worker_func, self.worker_init,
                    self.init_kwargs, items[splits[i]:splits[i+1]],
                    *args
                )
            )
            process.daemon = True
            process.start()
            processes.append(process)
            queues.append(queue)

        self._shutdown_fn = functools.partial(
            self._shutdown_batches, processes, done
        )

        if self.callback_func is not None:
            for _ in range(num_items):
                tick_queue.get()
                self.callback_func()

        for i, q in enumerate(queues):
            result.extend(q.get())
            q.close()

        return result

    def _shutdown_batches(self, processes, done):
        done.set()
        terminate(*processes)

    def _process_consumers(self, items, *args):
        result = []

        processes = []
        push_queue = mp.Queue()
        pull_queue = mp.Queue()

        for i in range(self.num_workers):
            process = mp.Process(
                target=_consumer_worker,
                args=(push_queue, pull_queue, i,
                      self.worker_func, self.worker_init, self.init_kwargs,
                      *args)
            )
            process.daemon = True
            process.start()
            processes.append(process)

        self._shutdown_fn = functools.partial(
            self._shutdown_consumers, processes, pull_queue, push_queue
        )

        iterator = iter(items)
        index = 0
        expect = 0
        try:
            for _ in range(self.num_workers):
                item = next(iterator)
                expect += 1
                push_queue.cancel_join_thread()
                push_queue.put((index, item))
                index += 1

            while True:
                result.append(pull_queue.get())
                if self.callback_func is not None:
                    self.callback_func()
                expect -= 1
                item = next(iterator)
                expect += 1
                push_queue.cancel_join_thread()
                push_queue.put((index, item))
                index += 1

        except StopIteration:
            for _ in range(expect):
                result.append(pull_queue.get())
                if self.callback_func is not None:
                    self.callback_func()

        return [r[1] for r in sorted(result, key=lambda r: r[0])]

    def _shutdown_consumers(self, processes, pull_queue, push_queue):
        pull_queue.close()

        for _ in range(len(processes)):
            push_queue.cancel_join_thread()
            push_queue.put(None)

        push_queue.close()

        terminate(*processes)

    # MARK: Special Methods
    def __del__(self):
        if self._shutdown_fn is not None:
            self._shutdown_fn()


def _batch_worker(push_queue, done, tick_queue, rank,
                  worker_func, worker_init, init_kwargs, items, *args):

    init = _init_if_needed(worker_init, rank, **init_kwargs)
    all_args = _combine_args(args, init)

    result = []

    for item in items:
        result.append(worker_func(item, *all_args))
        if tick_queue is not None:
            tick_queue.cancel_join_thread()
            tick_queue.put(None)

    push_queue.cancel_join_thread()
    push_queue.put(result)
    done.wait()


def _consumer_worker(pull_queue, push_queue, rank, worker_func, worker_init, init_kwargs, *args):

    init = _init_if_needed(worker_init, rank, **init_kwargs)
    all_args = _combine_args(args, init)

    while True:
        item = pull_queue.get()
        if item is None:
            break

        idx, item = item
        result = worker_func(item, *all_args)

        push_queue.cancel_join_thread()
        push_queue.put((idx, result))


def _init_if_needed(init, rank, **kwargs):
    if init is None:
        return None
    return init(rank, **kwargs)


def _combine_args(a, b):
    if b is None:
        return a
    if isinstance(b, tuple):
        return a + b
    return a + (b,)