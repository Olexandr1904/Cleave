"""Manual smoke test against a real Trello board.

Usage:
  export TRELLO_KEY=...
  export TRELLO_TOKEN=...
  export TRELLO_BOARD_ID=...
  python scripts/smoke_trello.py
"""

from __future__ import annotations

import asyncio
import os

from integrations.trello.trello_adapter import TrelloAdapter


async def main() -> None:
    a = TrelloAdapter(
        api_key=os.environ["TRELLO_KEY"],
        token=os.environ["TRELLO_TOKEN"],
        board_id=os.environ["TRELLO_BOARD_ID"],
        trigger_labels=["ai-pipeline"],
        list_mapping={},
    )
    try:
        await a._ensure_board_lists()
        print("Lists on board:")
        for lid, name in a._list_id_to_name.items():
            print(f"  {lid}: {name}")
        tickets = await a.poll_tickets()
        print(f"\n{len(tickets)} tickets with trigger label 'ai-pipeline':")
        for t in tickets:
            print(f"  {t.id}: {t.summary} (labels={t.labels})")
    finally:
        await a.close()


if __name__ == "__main__":
    asyncio.run(main())
