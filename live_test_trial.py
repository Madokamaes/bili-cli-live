"""Run one explicitly authorized, expiring local test. No retries or parallel POSTs.

Runtime JSON is private and must not be published with the source package.
The one-second minimum is measured conservatively after each HTTP completion:
network delay can make actual start-to-start intervals longer than the target.
"""
import argparse
import asyncio
import json
import os
import time
from pathlib import Path

from bilibili_api import select_client
from bilibili_api.utils import network
from live_danmaku import ScopedTestSession


async def run(settings):
    if os.environ.get('BILI_CLI_NO_RATE_LIMIT') == '1':
        raise ValueError('Disabled local guard is forbidden for this trial')
    select_client('httpx')
    session = ScopedTestSession(**settings)
    rows = []
    try:
        for key in sorted(session.keys):
            if rows:
                await asyncio.sleep(session.interval + 0.02)
            result = await asyncio.wait_for(session.send(key), timeout=30)
            row = {'key': key, **result}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
            if result.get('status') != 'accepted' or result.get('idempotent_replay') or result.get('audit_warning'):
                break
    except Exception as exc:
        print(json.dumps({'stopped': True, 'exception_type': type(exc).__name__,
                          'retry_allowed': False, 'completed_results': len(rows)}), flush=True)
    finally:
        await network.get_client().close()
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, type=Path)
    args = parser.parse_args()
    asyncio.run(run(json.loads(args.config.read_text())))
