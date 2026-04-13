"""Sidecar entry point."""
import asyncio
import logging

log = logging.getLogger("sidecar")

async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log.info("sidecar starting")
    log.info("sidecar ready")
    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
