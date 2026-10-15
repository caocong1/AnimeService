"""Read the current personal workflow without importing workers or writing DB."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from anime.db import Store
from anime.viewing_workflow import queue_snapshot


class ReadStore(Store):
    def __init__(self,con):self.con=con
    def rows(self,sql,args=()):return [dict(row) for row in self.con.execute(sql,args)]
    def execute(self,*args,**kwargs):raise RuntimeError('只读队列禁止写入')


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser()
    parser.add_argument('--lane',choices=('wish','watching'))
    args=parser.parse_args()
    con=sqlite3.connect((ROOT/'data/anime.db').resolve().as_uri()+'?mode=ro',uri=True)
    con.row_factory=sqlite3.Row
    try:
        con.execute('PRAGMA query_only=ON');con.execute('BEGIN')
        result=queue_snapshot(ReadStore(con))
        if args.lane:result['queues']={args.lane:result['queues'][args.lane]}
        print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:con.rollback();con.close()


if __name__=='__main__':main()
