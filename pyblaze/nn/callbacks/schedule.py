from .base import TrainingCallback, ValueTrainingCallback

class LearningRateScheduler(TrainingCallback):
    """
    The learning rate scheduler may be used with a PyTorch learning rate scheduler. The callback is
    automatically triggered after the end of every iteration or epoch.
    """

    def __init__(self, scheduler, after_batch=False):
        """
        Initializes a new learning rate scheduler for the given PyTorch scheduler.

        Parameters
        ----------
        scheduler: torch.optim.lr_scheduler
            The PyTorch scheduler.
        after_batch: bool, default: False
            Whether to call the scheduler after every batch or after every epoch.
        """
        self.exec_after_batch = after_batch
        self.scheduler = scheduler

    def after_batch(self, metrics):
        if self.exec_after_batch:
            self.scheduler.step()

    def after_epoch(self, metrics):
        if not self.exec_after_batch:
            self.scheduler.step()


class ParameterScheduler(ValueTrainingCallback):
    """
    The parameter scheduler is able to change the value of a variable over the course of the
    training.
    """

    def __init__(self, parameter, schedule, after_batch=False):
        """
        Initalizes a new scheduler for the given parameter.

        Parameters
        ----------
        parameter: object
            The parameter which should be modified over the course of the training.
        schedule: func (object, int) -> object
            Function which should update the parameter (given as first argument) based on itself
            and the current epoch/iteration (second argument). The function must return the updated
            parameter. The scheduler function is called after every epoch or iteration, depending
            on the `after_batch` argument.
        after_batch: bool, default: False
            Whether to call the scheduler after every batch instead of after every epoch. The
            schedule function is then passed as second parameter the current iteration (number of
            all batches) instead of the epoch.
        """
        self.parameter = parameter
        self.schedule = schedule
        self.exec_after_batch = after_batch
        self.epoch = None
        self.iterations = None

    def read(self):
        return self.parameter

    def before_training(self, model, num_epochs):
        self.iterations = 0

    def before_epoch(self, current, num_iterations):
        self.epoch = current

    def after_batch(self, metrics):
        self.iterations += 1
        self._update(True)

    def after_epoch(self, metrics):
        self._update(False)

    def after_training(self):
        self.epoch = None
        self.iterations = None

    def _update(self, is_batch_update):
        if is_batch_update != self.exec_after_batch:
            return
        if self.exec_after_batch:
            update = self.schedule(self.parameter, self.iterations)
        else:
            update = self.schedule(self.parameter, self.epoch)
        self.parameter = update
