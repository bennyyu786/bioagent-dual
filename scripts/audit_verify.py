"""Verify one SQLite database's task/build audit chain without changing it."""
import argparse
from pathlib import Path
from biomni_dual import AuditLog

parser=argparse.ArgumentParser()
parser.add_argument('db',type=Path,help='SQLite task or build database')
args=parser.parse_args()
if not args.db.is_file():
    parser.error('Database does not exist')
log=AuditLog(args.db)
valid=log.verify()
print('VALID' if valid else 'INVALID',args.db)
raise SystemExit(0 if valid else 1)
