"""Operator commands, run in the support container:

  docker compose exec support python -m app.cli agents list
  docker compose exec support python -m app.cli agents add --name "Priya Sharma" --phone "+91 98765 43210"
  docker compose exec support python -m app.cli agents update <phone-or-id> [--name ...] [--phone ...]
  docker compose exec support python -m app.cli agents deactivate <phone-or-id>
  docker compose exec support python -m app.cli agents reactivate <phone-or-id>
  docker compose exec support python -m app.cli tickets list [--status PENDING]
  docker compose exec support python -m app.cli tickets reassign RCK-XXXXXXXX --to <phone-or-id>
  docker compose exec support python -m app.cli tickets close RCK-XXXXXXXX [--reason "spam"]
  docker compose exec support python -m app.cli notifications retry RCK-XXXXXXXX
  docker compose run --rm migrate                  (python -m app.cli migrate, as the owner)

Changes made here reach open chat sockets within a sweep (30 s): the running service re-checks them.
The same agent management is on /admin/support in the web admin panel.
"""
from __future__ import annotations

import argparse
import sys
import uuid

from .config import NAME_MAX, Settings
from .db import migrate, open_pool
from .store import AgentExists, Store
from .tokens import normalize_reference
from .validation import InvalidInput, clean_name, normalize_phone


def _store(s: Settings) -> Store:
    if not s.database_url:
        sys.exit("SUPPORT_DATABASE_URL is not set")
    return Store(open_pool(s.database_url, max_size=2))


def _agent(st: Store, key: str, country: str | None) -> dict:
    try:
        row = st.agent(uuid.UUID(key))
    except ValueError:
        try:
            row = st.agent_by_phone(normalize_phone(key, country))
        except InvalidInput as e:
            sys.exit(f"{key}: {e}")
    if not row:
        sys.exit(f"no agent {key}")
    return row


def _print_agents(rows: list[dict]) -> None:
    for a in rows:
        state = "active  " if a["is_active"] else "inactive"
        print(f"{a['id']}  {state}  {a['normalized_phone_number']:<16}  {a['agent_name']}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply migrations as the owner (SUPPORT_MIGRATE_DATABASE_URL)")

    ag = sub.add_parser("agents").add_subparsers(dest="action", required=True)
    ag.add_parser("list")
    a_add = ag.add_parser("add")
    a_add.add_argument("--name", required=True)
    a_add.add_argument("--phone", required=True, help="with +country code, or use --country")
    a_add.add_argument("--country", help="ISO country for a national-format number, e.g. IN")
    a_up = ag.add_parser("update")
    a_up.add_argument("agent", help="phone number or id")
    a_up.add_argument("--name")
    a_up.add_argument("--phone")
    a_up.add_argument("--country")
    for name in ("deactivate", "reactivate"):
        x = ag.add_parser(name)
        x.add_argument("agent", help="phone number or id")
        x.add_argument("--country")

    tk = sub.add_parser("tickets").add_subparsers(dest="action", required=True)
    t_list = tk.add_parser("list")
    t_list.add_argument("--status", choices=["PENDING", "CLAIMED", "RESOLVED", "CLOSED", "EXPIRED"])
    t_list.add_argument("--limit", type=int, default=30)
    t_re = tk.add_parser("reassign")
    t_re.add_argument("reference")
    t_re.add_argument("--to", required=True, help="the new agent's phone number or id")
    t_re.add_argument("--country")
    t_cl = tk.add_parser("close")
    t_cl.add_argument("reference")
    t_cl.add_argument("--reason", default="")

    nt = sub.add_parser("notifications").add_subparsers(dest="action", required=True)
    n_re = nt.add_parser("retry")
    n_re.add_argument("reference")

    args = p.parse_args(argv)
    s = Settings()

    if args.cmd == "migrate":
        if not s.migrate_database_url:
            sys.exit("SUPPORT_MIGRATE_DATABASE_URL is not set")
        done = migrate(s.migrate_database_url, s.app_db_password)
        print(f"migrations applied: {', '.join(done) if done else 'none pending'}")
        print("support_app password set" if s.app_db_password else "SUPPORT_DB_APP_PASSWORD empty: support_app password left as it was")
        return 0

    st = _store(s)
    try:
        if args.cmd == "agents":
            if args.action == "list":
                _print_agents(st.list_agents())
            elif args.action == "add":
                try:
                    name = clean_name(args.name, max_len=NAME_MAX)
                    e164 = normalize_phone(args.phone, args.country)
                    _print_agents([st.create_agent(name, e164)])
                except InvalidInput as e:
                    sys.exit(str(e))
                except AgentExists:
                    sys.exit("an agent with this phone number already exists")
            elif args.action == "update":
                a = _agent(st, args.agent, args.country)
                try:
                    name = clean_name(args.name, max_len=NAME_MAX) if args.name else None
                    e164 = normalize_phone(args.phone, args.country) if args.phone else None
                    _print_agents([st.update_agent(a["id"], name=name, e164=e164)])
                except InvalidInput as e:
                    sys.exit(str(e))
                except AgentExists:
                    sys.exit("an agent with this phone number already exists")
            else:
                a = _agent(st, args.agent, args.country)
                _print_agents([st.update_agent(a["id"], active=args.action == "reactivate")])
        elif args.cmd == "tickets":
            if args.action == "list":
                for t in st.list_tickets(limit=args.limit, status=args.status):
                    print(f"{t['reference']}  {t['effective_status']:<8}  {t['created_at']:%Y-%m-%d %H:%M}Z  {t['agent_name'] or '-':<20}  discord {t['discord_status'] or '-'}")
            else:
                ref = normalize_reference(args.reference) or sys.exit("not a ticket reference (RCK-XXXXXXXX)")
                if args.action == "reassign":
                    print(st.admin_reassign(ref, _agent(st, args.to, args.country)["id"]))
                else:
                    print(st.admin_close(ref, args.reason))
        elif args.cmd == "notifications":
            ref = normalize_reference(args.reference) or sys.exit("not a ticket reference (RCK-XXXXXXXX)")
            print(f"{st.retry_failed_notifications(ref)} notification(s) queued again; the service sends them within 10 seconds")
    finally:
        st.pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
