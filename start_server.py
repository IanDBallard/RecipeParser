"""
Launcher: starts uvicorn as a fully detached process and exits immediately.
Run with: python start_server.py
The server will keep running after this script exits.

The launcher runs the API in the same verifying configuration the Cayenne
client expects in production: bearer tokens are checked against Supabase and
each recipe is filed under the ``sub`` claim of the caller's own JWT.

Local work that genuinely has no Supabase session can opt out with
``--disable-auth --test-user-id <uuid>``. Both flags are required together,
the bypass binds to loopback unless you override --host, and the launcher says
loudly what it did — an unauthenticated server that silently files every
ingested recipe under one hardcoded user is exactly the failure this avoids.
"""
import argparse
import os
import subprocess
import sys
import uuid

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _uuid_arg(value: str) -> str:
    """argparse type: accept only a well-formed UUID."""
    try:
        uuid.UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a UUID. Supabase user ids are UUIDs — pass the "
            "id of a real row in auth.users, or the recipe will be written "
            "where nothing can sync it."
        ) from None
    return value


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--host",
        default=None,
        help="Bind address. Defaults to 0.0.0.0, or 127.0.0.1 with --disable-auth.",
    )
    parser.add_argument("--port", default="8000", help="Bind port (default 8000).")
    parser.add_argument(
        "--log-level", default="info", help="uvicorn log level (default info)."
    )
    parser.add_argument(
        "--disable-auth",
        action="store_true",
        help=(
            "LOCAL DEVELOPMENT ONLY: skip JWT verification and attribute every "
            "request to --test-user-id. Requires --test-user-id."
        ),
    )
    parser.add_argument(
        "--test-user-id",
        type=_uuid_arg,
        default=None,
        help="Supabase user UUID that --disable-auth attributes writes to.",
    )
    args = parser.parse_args(argv)

    if args.test_user_id and not args.disable_auth:
        parser.error("--test-user-id is meaningless without --disable-auth.")
    if args.disable_auth and not args.test_user_id:
        parser.error(
            "--disable-auth requires --test-user-id <uuid>. The bypass files "
            "every ingested recipe under that id, so it must be stated "
            "explicitly rather than defaulted to someone's test account."
        )
    if args.host is None:
        args.host = "127.0.0.1" if args.disable_auth else "0.0.0.0"
    return args


def build_env(args: argparse.Namespace) -> dict:
    """Return the child environment for uvicorn.

    Any inherited DISABLE_AUTH/TEST_USER_ID is stripped first: the launcher's
    own flags are the only thing that decides the auth mode, so a stale shell
    export or .env line cannot quietly unauthenticate the server.
    """
    env = os.environ.copy()
    env.pop("DISABLE_AUTH", None)
    env.pop("TEST_USER_ID", None)
    if args.disable_auth:
        env["DISABLE_AUTH"] = "1"
        env["TEST_USER_ID"] = args.test_user_id
    return env


def main(argv=None) -> int:
    args = parse_args(argv)
    env = build_env(args)

    log_path = os.path.join(os.path.dirname(__file__), "uvicorn.log")

    creationflags = 0
    if sys.platform == "win32":
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    with open(log_path, "w") as log_file:
        proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                "recipeparser.adapters.api:app",
                "--host", args.host,
                "--port", str(args.port),
                "--log-level", args.log_level,
            ],
            cwd=os.path.dirname(__file__),
            env=env,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
            close_fds=True,
            start_new_session=sys.platform != "win32",
        )

    if args.disable_auth:
        print(
            "*** AUTH BYPASS ENABLED — JWT verification is OFF and every "
            f"ingested recipe is filed under user {args.test_user_id}. ***\n"
            "*** Local development only. Never expose this server. ***"
        )
    else:
        print("Auth: verifying Supabase JWTs (production configuration).")
    print(f"Server started with PID {proc.pid} on {args.host}:{args.port}. Logs: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
