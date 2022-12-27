class LogitechFlowKvmError(Exception):
    pass


class UserError(LogitechFlowKvmError):
    pass


class DeviceNotFound(UserError):
    pass


class CannotChangeHost(UserError):
    pass


class ChangeHostFailed(LogitechFlowKvmError):
    pass
