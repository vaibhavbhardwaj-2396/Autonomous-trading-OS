"""
Send a Telegram message. A dedicated, permission-allowed entry point so the agent always
has a working way to reach Vaibhav — including, critically, when something has gone wrong.

The first live run couldn't send its state-mismatch alert because every python invocation
it tried was denied by the permission layer. An alerting path that fails exactly when you
need it is worse than none, because you believe you're covered.

    python scripts/notify.py "message text"
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from telegram_notify import send_message  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/notify.py \"message\"", file=sys.stderr)
        return 2
    text = " ".join(sys.argv[1:])
    try:
        send_message(text)
    except Exception as e:
        print(f"FAILED to send: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    print("sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
