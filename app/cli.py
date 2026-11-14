"""Safe operator provisioning. Password is prompted; never passed via a CLI flag."""
import argparse
import getpass
import re
import sys

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.database import SessionLocal
from app.models import Tenant, User
from app.security import create_api_key, password_hash


def main():
    parser = argparse.ArgumentParser(description='RAG Platform operator CLI (run migrations first)')
    sub = parser.add_subparsers(dest='command', required=True)
    bootstrap = sub.add_parser('bootstrap', help='Create workspace and owner without public registration')
    bootstrap.add_argument('--tenant', required=True)
    bootstrap.add_argument('--name', required=True)
    bootstrap.add_argument('--email', required=True)
    key = sub.add_parser('issue-key', help='Issue API key for an existing workspace')
    key.add_argument('--tenant', required=True)
    key.add_argument('--email', required=True)
    key.add_argument('--name', required=True)
    key.add_argument('--role', default='member', choices=['viewer', 'member', 'admin'])
    args = parser.parse_args()
    with SessionLocal() as db:
        if args.command == 'bootstrap':
            if not re.fullmatch(r'[a-z][a-z0-9-]{2,59}', args.tenant):
                parser.error('Workspace slug: lowercase letters, digits, dashes, 3-60 chars')
            email = args.email.strip().lower()
            password = getpass.getpass('New owner password (12+ characters): ')
            confirm = getpass.getpass('Confirm password: ')
            if password != confirm:
                parser.error('Passwords did not match')
            tenant = Tenant(name=args.name.strip(), slug=args.tenant)
            db.add(tenant)
            try:
                db.flush()
                owner = User(tenant_id=tenant.id, email=email, role='owner',
                             password_hash=password_hash(password))
                db.add(owner)
                db.commit()
            except IntegrityError:
                db.rollback()
                parser.error('Workspace slug already exists')
            print(f'Created workspace {tenant.slug} ({tenant.id}) and owner {email}')
        else:
            tenant = db.scalar(select(Tenant).where(Tenant.slug == args.tenant))
            user = (db.scalar(select(User).where(User.tenant_id == tenant.id,
                                                 User.email == args.email.lower())) if tenant else None)
            if not user or user.role != 'owner':
                parser.error('Existing workspace owner required')
            secret, _ = create_api_key(db, tenant.id, user.id, args.name, args.role)
            db.commit()
            print('API key (shown once; save securely):', secret)


if __name__ == '__main__':
    main()
