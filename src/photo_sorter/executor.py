from __future__ import annotations

import shutil
from pathlib import Path

from .config import AppConfig
from .plan_writer import PlanItem
from .state_db import StateStore


def _cleanup_empty_dirs(start: Path, *, stop_at: Path) -> None:
    """Remove `start` and any now-empty ancestor directories, stopping at (and
    never removing) `stop_at`. Used after a reclassification move leaves an
    old destination folder (e.g. a superseded quarterly bucket) empty.
    """
    current = start
    while current != stop_at and stop_at in current.parents:
        try:
            next(current.iterdir())
            break  # not empty
        except StopIteration:
            pass
        except OSError:
            break
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def execute_plan(
    *,
    cfg: AppConfig,
    plan_items: list[PlanItem],
    state: StateStore,
    run_id: int,
    effective_mode: str,
    execute: bool,
    echo: callable,
) -> dict[str, int]:
    stats = {
        "applied": 0,
        "skipped": 0,
        "failed": 0,
    }

    for item in plan_items:
        action = item.action
        if action in {"copy", "move"} and action != effective_mode:
            echo(
                f"[yellow]Warning[/yellow]: plan action '{action}' differs from run mode '{effective_mode}'. "
                "Plan action is used."
            )

        if action == "skip":
            stats["skipped"] += 1
            state.record_operation(
                run_id=run_id,
                source_path=item.source,
                destination_path=item.destination,
                action=action,
                status="skipped",
                error=None,
                sha=item.sha,
            )
            continue

        if action == "review":
            stats["skipped"] += 1
            state.record_operation(
                run_id=run_id,
                source_path=item.source,
                destination_path=None,
                action=action,
                status="review",
                error=None,
                sha=item.sha,
            )
            continue

        if item.destination is None:
            stats["failed"] += 1
            state.record_operation(
                run_id=run_id,
                source_path=item.source,
                destination_path=None,
                action=action,
                status="failed",
                error="missing destination in plan",
                sha=item.sha,
            )
            continue

        src = Path(item.source)
        if action == "quarantine":
            dst = cfg.paths.quarantine_root / item.destination
        else:
            dst = cfg.paths.target_root / item.destination

        if not execute:
            echo(f"[dry-run] {src} -> {dst} ({action})")
            stats["skipped"] += 1
            state.record_operation(
                run_id=run_id,
                source_path=str(src),
                destination_path=str(dst),
                action=action,
                status="dry_run",
                error=None,
                sha=item.sha,
            )
            continue

        dst.parent.mkdir(parents=True, exist_ok=True)

        try:
            if action in {"copy", "quarantine"}:
                shutil.copy2(src, dst)
            elif action == "move":
                shutil.move(str(src), str(dst))
                if src.parent.is_relative_to(cfg.paths.target_root):
                    _cleanup_empty_dirs(src.parent, stop_at=cfg.paths.target_root)
            else:
                raise ValueError(f"Unsupported action in plan: {action}")

            if action == "quarantine":
                destination_rel = str(dst.relative_to(cfg.paths.quarantine_root))
            else:
                destination_rel = str(dst.relative_to(cfg.paths.target_root))
            source_name = Path(item.source).name
            state.upsert_placement(
                sha=item.sha,
                destination_rel=destination_rel,
                run_id=run_id,
                month_key=item.bucket_key,
                source_name=source_name,
            )

            stats["applied"] += 1
            state.record_operation(
                run_id=run_id,
                source_path=str(src),
                destination_path=str(dst),
                action=action,
                status="applied",
                error=None,
                sha=item.sha,
            )
        except Exception as e:
            stats["failed"] += 1
            state.record_operation(
                run_id=run_id,
                source_path=str(src),
                destination_path=str(dst),
                action=action,
                status="failed",
                error=str(e),
                sha=item.sha,
            )

    return stats