import abc
import dataclasses
import logging
from concurrent.futures import Future, ProcessPoolExecutor, ThreadPoolExecutor
from typing import Any, Callable, Dict, List

from hamilton.execution.graph_functions import execute_subdag
from hamilton.execution.grouping import NodeGroupPurpose, TaskImplementation
from hamilton.execution.state import ExecutionState, GraphState, TaskState

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class TaskFuture:
    get_state: Callable[[], TaskState]
    get_result: Callable[[], Any]


class TaskExecutor(abc.ABC):
    """Abstract class for a task executor. All this does is submit a task and return a future.
    It also tells us if it can do that"""

    @abc.abstractmethod
    def init(self):
        pass

    @abc.abstractmethod
    def finalize(self):
        pass

    @abc.abstractmethod
    def submit_task(self, task: TaskImplementation) -> TaskFuture:
        """Submits a task to the executor. Returns a task ID that can be used to query the status.
        Effectively a future.

        :param task:
        :return:
        """
        pass

    @abc.abstractmethod
    def can_submit_task(self) -> bool:
        """Returns whether or not we can submit a task to the executor.
        For instance, if the maximum parallelism is reached, we may not be able to submit a task.

        TODO -- consider if this should be a "parallelism" value instead of a boolean, forcing
        the ExecutionState to store the state prior to executing a task.

        :return: whether or not we can submit a task.
        """
        pass


def base_execute_task(task: TaskImplementation) -> Dict[str, Any]:
    """This is a utility function to execute a base task. In an ideal world this would be recursive,
    but for now we just call out to good old DFS.

    We should probably have a simple way of doing this for single-node tasks, as they're
    going to be common.

    :param task:
    :return:
    """
    return execute_subdag(
        nodes=task.nodes,
        inputs=task.dynamic_inputs,
        adapter=task.adapters[0],  # TODO -- wire through multiple graph adapters
        overrides={**task.dynamic_inputs, **task.overrides},
    )


class SynchronousLocalTaskExecutor(TaskExecutor):
    """Basic synchronous/local task executor that runs tasks
    in the same process, at submit time."""

    def submit_task(self, task: TaskImplementation) -> TaskFuture:
        """Submitting a task is literally just running it.

        :param task:
        :return:
        """
        # No error management for now
        result = base_execute_task(task)
        return TaskFuture(get_state=lambda: TaskState.SUCCESSFUL, get_result=lambda: result)

    def can_submit_task(self) -> bool:
        """We can always submit a task as the task submission is blocking!
        Your fault if you call us too much.

        :return: True
        """
        return True

    def init(self):
        pass

    def finalize(self):
        pass


class TaskFutureWrappingPythonFuture(TaskFuture):
    """Wraps a python future in a TaskFuture"""

    def __init__(self, future: Future):
        self.future = future

    def get_state(self):
        if self.future.done():
            try:
                self.future.result()
            except Exception:
                logger.exception("Task failed")
                return TaskState.FAILED
            return TaskState.SUCCESSFUL
        else:
            return TaskState.RUNNING

    def get_result(self):
        if not self.future.done():
            return None
        out = self.future.result()
        return out


class PoolExecutor(TaskExecutor, abc.ABC):
    def __init__(self, max_tasks: int):
        self.active_futures = []
        self.initialized = False
        self.pool = None
        self.max_tasks = max_tasks

    def _prune_active_futures(self):
        self.active_futures = [f for f in self.active_futures if not f.done()]

    @abc.abstractmethod
    def create_pool(self) -> Any:
        """Creates a pool to submit tasks to.

        :return:
        """
        pass

    def init(self):
        if not self.initialized:
            self.pool = self.create_pool()
            self.initialized = True
        else:
            raise RuntimeError("Cannot initialize an already initialized executor")

    def finalize(self):
        if self.initialized:
            self.pool.shutdown()
            self.initialized = False
        else:
            raise RuntimeError("Cannot finalize an uninitialized executor")

    def submit_task(self, task: TaskImplementation) -> TaskFuture:
        """Submitting a task is literally just running it.

        :param task:
        :return:
        """
        # First submit it
        # Then we need to wrap it in a future
        future = self.pool.submit(base_execute_task, task)
        self.active_futures.append(future)
        return TaskFutureWrappingPythonFuture(future)

    def can_submit_task(self) -> bool:
        """We can always submit a task as the task submission is blocking!
        Your fault if you call us too much.

        :return: True
        """
        self._prune_active_futures()
        return len(self.active_futures) < self.max_tasks


class MultiThreadingExecutor(PoolExecutor):
    """Basic synchronous/local task executor that runs tasks
    in the same process, at submit time."""

    def create_pool(self) -> Any:
        return ThreadPoolExecutor(max_workers=self.max_tasks)


class MultiProcessingExecutor(PoolExecutor):
    """Basic synchronous/local task executor that runs tasks
    in the same process, at submit time."""

    def create_pool(self) -> Any:
        return ProcessPoolExecutor(max_workers=self.max_tasks)


class ExecutionManager(abc.ABC):
    """Manages execution per task. This enables you to have different executors for different
    tasks/task types. Note that, currently, it just uses the task information, but we could
    theoretically add metadata in a task as well.
    """

    def __init__(self, executors: List[TaskExecutor]):
        """Initializes the execution manager. Note this does not start it up/claim resources --
        you need to call init() to do that.

        :param executors:
        """
        self.executors = executors

    def init(self):
        """Initializes each of the executors."""
        for executor in self.executors:
            executor.init()

    def finalize(self):
        """Finalizes each of the executors."""
        for executor in self.executors:
            executor.finalize()

    @abc.abstractmethod
    def get_executor_for_task(self, task: TaskImplementation) -> TaskExecutor:
        """Selects the executor for the task. This enables us to set the appropriate executor
        for specific tasks (so that we can run some locally, some remotely, etc...).

        Note that this is the power-user case -- in all likelihood, people will use the default
        ExecutionManager.

        :param task:  Task to choose execution manager for
        :return: The executor to use for this task
        """
        pass


class DefaultExecutionManager(ExecutionManager):
    def __init__(self, local_executor=None, remote_executor=None):
        """Instantiates a BasicExecutionManager with a local/remote executor.
        These enable us to run certain tasks locally (simple transformations, generating sets of files),
        and certain tasks remotely (processing files in large datasets, etc...)

        :param local_executor: Executor to use for running tasks locally
        :param remote_executor:  Executor to use for running tasks remotely
        """
        if local_executor is None:
            local_executor = SynchronousLocalTaskExecutor()
        if remote_executor is None:
            remote_executor = MultiProcessingExecutor(max_tasks=5)
        super().__init__([local_executor, remote_executor])
        self.local_executor = local_executor
        self.remote_executor = remote_executor

    def get_executor_for_task(self, task: TaskImplementation) -> TaskExecutor:
        """Simple implementation that returns the local executor for single task executions,

        :param task: Task to get executor for
        :return: A local task if this is a "single-node" task, a remote task otherwise
        """

        if task.purpose == NodeGroupPurpose.EXECUTE_SINGLE:
            return self.local_executor
        return self.remote_executor


class GraphRunner:
    """Live component that runs the graph until completion.
    Its job is continually pushing through and updating the ExecutionState.
    It is separate from the ExecutionState as it carries no state itself, just queries/updates.

    It might be that we want to push the DAG Walker/decision about where to go in here as well,
    but we'll decide that later.

    """

    def __init__(self, execution_state: ExecutionState, execution_manager: ExecutionManager):
        """Initializes a graph runner with the execution state and the result cache

        :param state: Execution state -- this stores the state. We push updates to it and get the
        next step.
        :param task_executor: Task executor -- this is what we use to submit tasks.
        In fact, we may actually consider having tasks update their own state...
        """
        self.execution_state = execution_state
        self.execution_manager = execution_manager
        # self.result_cache = result_cache
        self.task_futures = {}

    def run_until_complete(self):
        # TODO -- get this to return a task result
        """Blocking call to run through until completion"""
        # Until the graph is done
        self.execution_manager.init()
        try:
            while not GraphState.is_terminal(self.execution_state.get_graph_state()):
                # get the next task from the queue
                next_task = self.execution_state.release_next_task()
                if next_task is not None:
                    task_executor = self.execution_manager.get_executor_for_task(next_task)
                    if task_executor.can_submit_task():
                        submitted = task_executor.submit_task(next_task)
                        self.task_futures[next_task.task_id] = submitted
                    else:
                        # Whoops, back on the queue
                        # We should probably wait a bit here, but for now we're going to keep
                        # burning through
                        self.execution_state.reject_task(task_to_reject=next_task)
                # update all the tasks in flight
                # copy so we can modify
                for task_name, task_future in self.task_futures.copy().items():
                    state = task_future.get_state()
                    result = task_future.get_result()
                    self.execution_state.update_task_state(task_name, state, result)
                    if TaskState.is_terminal(state):
                        del self.task_futures[task_name]
            logger.info(f"Graph is done, graph state is {self.execution_state.get_graph_state()}")
        finally:
            self.execution_manager.finalize()