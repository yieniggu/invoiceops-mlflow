"""Run pinned MLflow migration callbacks without database URLs in process argv."""

import os
import sys


def main():
    # Redirect descriptors before MLflow imports (including logging/native output).
    # This short-lived process owns library engines; exit also releases resources
    # on paths where the pinned upstream callback does not dispose its engine.
    with open(os.devnull, "w") as sink:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
    try:
        if sys.argv[1:] == ["tracking"]:
            from mlflow.db import upgrade

            upgrade.callback(url=os.environ["MLFLOW_BACKEND_STORE_URI"])
        elif sys.argv[1:] == ["auth"]:
            from mlflow.server.auth.db.cli import upgrade

            upgrade.callback(url=os.environ["MLFLOW_AUTH_DATABASE_URI"], revision="head")
        else:
            return 1
    except BaseException:
        # Never render import errors, client exceptions, or SystemExit payloads.
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
