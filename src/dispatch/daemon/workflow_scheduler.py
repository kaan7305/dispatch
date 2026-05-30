"""Cron scheduler for trigger.cron nodes.

Wakes every 60s, fetches the user's workflows from the broker, and for
every workflow whose definition contains a trigger.cron node, checks
whether its cron expression should have fired since the last evaluation.
If yes, kicks off a run via POST /workflows/{id}/run — the broker then
WS-pushes the start back to the daemon exactly as if a user had clicked
"Run", so cron-driven runs are indistinguishable from manual ones in
the run-history view.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import httpx

try:
    from croniter import croniter
except ImportError:
    croniter = None  # type: ignore[assignment]

logger = logging.getLogger("dispatch.daemon.scheduler")


class CronScheduler:
    def __init__(self, engine, broker_url: str, broker_token_getter):
        self.engine = engine
        self.broker_url = broker_url.rstrip("/")
        # Callable rather than a str so token rotation post-login is
        # picked up without having to re-instantiate the scheduler.
        self.broker_token_getter = broker_token_getter
        self.last_fired: dict[tuple[UUID, str], datetime] = {}
        self.task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self.task and not self.task.done():
            return
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        # 60s precision is fine for human-scale workflows; tighter polls
        # would mean more wasted broker calls.
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("scheduler tick failed")
            await asyncio.sleep(60)

    async def _tick(self) -> None:
        if croniter is None:
            return  # silently skip if croniter not installed
        token = self.broker_token_getter()
        if not token:
            return

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{self.broker_url}/workflows",
                headers={"Authorization": f"Bearer {token}"},
            )
        if resp.status_code != 200:
            return
        wfs = resp.json().get("workflows", [])

        # Workflow summaries don't include the definition, so we have to
        # GET each one to see if it has a trigger.cron node. This is O(N)
        # broker calls per tick but N is small (a single user's workflows).
        now = datetime.now(timezone.utc)
        for wf in wfs:
            wf_id_str = wf.get("workflow_id")
            if not wf_id_str:
                continue
            try:
                wf_id = UUID(wf_id_str)
            except (ValueError, TypeError):
                continue

            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.get(
                    f"{self.broker_url}/workflows/{wf_id_str}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if r.status_code != 200:
                continue
            wf_full = r.json()
            definition = wf_full.get("definition", {}) or {}

            for node in definition.get("nodes", []) or []:
                if node.get("type") != "trigger.cron":
                    continue
                params = node.get("params") or {}
                expr = str(params.get("expression", "") or "").strip()
                if not expr or not croniter.is_valid(expr):
                    continue

                key = (wf_id, node["id"])
                # First time we see this trigger: anchor to "way in the
                # past" so croniter computes the next valid fire from a
                # cold start, then we compare against `now`.
                last = self.last_fired.get(key, now.replace(year=2000))
                try:
                    next_fire = croniter(expr, last).get_next(datetime)
                except Exception:
                    continue
                # croniter returns a naive datetime in the base's tz;
                # we passed a UTC-aware base, so we need to coerce.
                if next_fire.tzinfo is None:
                    next_fire = next_fire.replace(tzinfo=timezone.utc)

                if next_fire <= now:
                    static_input = params.get("input") or {}
                    if not isinstance(static_input, dict):
                        static_input = {}
                    # Recipients now live on the cron node — without them
                    # the broker has nobody to dispatch the workflow to.
                    recipient_ids = params.get("recipient_ids") or []
                    if not isinstance(recipient_ids, list) or not recipient_ids:
                        logger.info(
                            "skipping cron for %s: no recipient_ids on node",
                            wf_id_str,
                        )
                        self.last_fired[key] = now
                        continue
                    fired = await self._start_run_via_broker(
                        wf_id, recipient_ids, static_input, token,
                    )
                    if fired:
                        self.last_fired[key] = now
                        logger.info(
                            "cron-triggered workflow %s for %d recipient(s)",
                            wf_id_str, len(recipient_ids),
                        )

    async def _start_run_via_broker(
        self, workflow_id: UUID, recipient_ids: list, input_: dict, token: str,
    ) -> bool:
        # POST through the broker (not engine.run_for_dispatch directly)
        # so the dispatch fan-out + run-row creation happen exactly as
        # they would for a manual click — scheduler-launched runs are
        # indistinguishable in the history view.
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{self.broker_url}/workflows/{workflow_id}/run",
                    json={"recipient_ids": list(recipient_ids), "input": input_},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError:
            logger.exception("failed to POST cron-trigger run for %s", workflow_id)
            return False
        return r.status_code == 200
