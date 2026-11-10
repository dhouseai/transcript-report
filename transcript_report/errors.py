"""The one exception type raised for problems the user can fix."""


class TranscriptReportError(Exception):
    """A problem to show the user as a one-line message, without a traceback.

    Library code raises this instead of calling sys.exit(), so cleanup in `finally` blocks
    still runs and the functions can be tested. main() turns it into sys.exit(message).
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
