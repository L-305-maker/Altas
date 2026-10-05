class DuplicateStepError(Exception):
    pass


class StepNotFoundError(Exception):
    pass


class SelfDependencyError(Exception):
    pass


class CycleDetectedError(Exception):
    pass
