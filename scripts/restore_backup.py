"""Offline maintenance CLI. The Telegram worker must be stopped."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

def main():
    parser=argparse.ArgumentParser(description='Restore a verified database backup while the bot is stopped')
    parser.add_argument('backup',type=Path)
    parser.add_argument('--confirm-restore',action='store_true',help='Confirm replacement of the database configured by DATABASE_URL')
    args=parser.parse_args()
    if not args.confirm_restore:parser.error('Pass --confirm-restore after checking DATABASE_URL and stopping the service')
    from database import db
    try:
        # Keep a recoverable copy of the destination before replacing it.
        saved=db.create_backup()
        print('Pre-restore backup:',saved)
        db.restore_postgres_backup(str(args.backup.resolve()))
        print('Restore completed. Restart the service to open fresh connections.')
    finally:db.close()

if __name__=='__main__':main()
