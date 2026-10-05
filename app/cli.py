"""Usage: python -m app.cli create-admin <username> <password> [display name]
          python -m app.cli run-jobs"""

import sys

from app.db import SessionLocal, init_engine


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    init_engine()
    if argv[0] == "create-admin" and len(argv) >= 3:
        from app.bootstrap import create_super_admin
        from app.main import run_migrations

        run_migrations()
        with SessionLocal() as db:
            u = create_super_admin(db, argv[1], argv[2], " ".join(argv[3:]) or "Admin")
            db.commit()
            print(f"created super admin {u.username} ({u.id})")
        return 0
    if argv[0] == "run-jobs":
        from app.jobs import run_jobs_once

        run_jobs_once()
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
