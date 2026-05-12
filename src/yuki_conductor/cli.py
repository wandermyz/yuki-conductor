"""CLI entry point for yuki-conductor."""

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="yuki-conductor",
        description="Bridge chat-app messages (Slack, Teams CLI, web) to Claude Code CLI",
    )
    sub = parser.add_subparsers(dest="command")

    # run
    sub.add_parser("run", help="Start Slack listener (foreground)")

    # daemon
    daemon_parser = sub.add_parser("daemon", help="Manage LaunchAgent daemon")
    daemon_parser.add_argument(
        "action",
        choices=["install", "uninstall", "restart", "status", "log"],
    )

    # simulate
    sim_parser = sub.add_parser("simulate", help="Test without Slack")
    sim_sub = sim_parser.add_subparsers(dest="sim_command")

    msg_parser = sim_sub.add_parser("message", help="Send a new message")
    msg_parser.add_argument("text", help="Message text")

    reply_parser = sim_sub.add_parser("reply", help="Reply in a thread")
    reply_parser.add_argument("thread_ts", help="Thread timestamp")
    reply_parser.add_argument("text", help="Reply text")

    # web
    web_parser = sub.add_parser("web", help="Web frontend management")
    web_sub = web_parser.add_subparsers(dest="web_command")
    web_sub.add_parser(
        "rebuild",
        help="Rebuild the frontend (hot-reload without restarting the daemon)",
    )

    import importlib.metadata
    for ep in importlib.metadata.entry_points(group="yuki_conductor.cli_plugins"):
        register_fn = ep.load()
        register_fn(sub)

    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    if args.command == "run":
        from yuki_conductor.runtime import start

        start()
    elif args.command == "daemon":
        from yuki_conductor.daemon import handle_daemon

        handle_daemon(args.action)
    elif args.command == "simulate":
        if args.sim_command is None:
            sim_parser.print_help()
            sys.exit(1)
        from yuki_conductor.claude_runner import run_claude
        from yuki_conductor.store import SessionStore

        store = SessionStore()
        if args.sim_command == "message":
            result = run_claude(args.text)
            fake_ts = f"sim_{id(result)}"
            if result.session_id:
                store.set(fake_ts, result.session_id)
            print(f"thread_ts: {fake_ts}")
            print(f"session_id: {result.session_id}")
            print(f"response:\n{result.text}")
        elif args.sim_command == "reply":
            session_id = store.get(args.thread_ts)
            if session_id is None:
                print(f"No session found for thread_ts={args.thread_ts}", file=sys.stderr)
                sys.exit(1)
            result = run_claude(args.text, session_id=session_id)
            if result.session_id:
                store.set(args.thread_ts, result.session_id)
            print(f"session_id: {result.session_id}")
            print(f"response:\n{result.text}")
    elif args.command == "web":
        if args.web_command is None:
            web_parser.print_help()
            sys.exit(1)
        if args.web_command == "rebuild":
            import urllib.request

            from yuki_conductor.web_server import WEB_PORT

            url = f"http://localhost:{WEB_PORT}/api/admin/rebuild"
            try:
                req = urllib.request.Request(url, method="POST", data=b"")
                with urllib.request.urlopen(req, timeout=120) as resp:
                    if resp.status == 200:
                        print("Frontend rebuilt. Connected browsers will reload automatically.")
                    else:
                        print(f"Rebuild endpoint returned status {resp.status}", file=sys.stderr)
                        sys.exit(1)
            except urllib.error.URLError:
                # Daemon not running — fall back to local build
                print("Daemon not reachable; building locally...")
                from yuki_conductor.config import build_web_frontend

                success = build_web_frontend()
                if not success:
                    sys.exit(1)
                print("Frontend rebuilt. Restart the daemon to serve the new assets.")


if __name__ == "__main__":
    main()
