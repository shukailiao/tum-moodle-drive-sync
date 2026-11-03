class SyncError(Exception):
    """A user-actionable failure with a safe, credential-free message."""


class AuthenticationExpired(SyncError):
    def __init__(self):
        super().__init__("Moodle session missing or expired. Run `tum-moodle-sync login` again.")
