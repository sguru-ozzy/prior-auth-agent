"""Test worker: persist a pending intent, then hold its native case lock."""

import argparse
import time
from pathlib import Path

from prior_auth_agent.storage.journal import JournalStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("case_key")
    parser.add_argument("record_digest")
    arguments = parser.parse_args()
    with JournalStore(arguments.directory, arguments.case_key, arguments.record_digest) as store:
        record = store.load()
        store.save(record.model_copy(update={"phase": "SEND_PENDING", "request_id": "REQ-CRASH"}))
        print("locked", flush=True)
        time.sleep(30)


if __name__ == "__main__":
    main()
