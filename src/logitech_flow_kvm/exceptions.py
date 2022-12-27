class LogitechFlowKvmError(Exception):
    pass


class UserError(LogitechFlowKvmError):
    pass


class NoCertificateAvailable(LogitechFlowKvmError):
    pass


class ServerNotAvailable(UserError):
    pass


class PairingFailed(UserError):
    pass


class ServerNotPaired(UserError):
    pass


class DeviceNotFound(UserError):
    pass


class CannotChangeHost(UserError):
    pass


class ChangeHostFailed(LogitechFlowKvmError):
    pass
