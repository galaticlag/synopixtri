from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer
from rich import print

from .config import ensure_dirs, load_config, validate_paths
from .executor import execute_plan
from .exif import ExifToolError, exiftool_version
from .exif import extract_metadata_json
from .hashing import file_hash
from .logical_units import MediaRecord
from .plan_writer import PlanItem, read_plan_jsonl, write_plan_jsonl
from .planner import (
    BuildPlanOptions,
    build_plan_items_from_records,
    build_reclassification_items,
    classify_or_defer,
    month_key_for_datetime,
)
from .scan import scan_media_files
from .state_db import StateStore

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _effective_mode(mode: str | None, default_mode: str) -> str:
    effective = (mode or default_mode).strip().lower()
    if effective not in {"copy", "move"}:
        raise typer.BadParameter("--mode must be copy or move")
    return effective


def _build_plan_options(cfg, effective_mode: str) -> BuildPlanOptions:
    return BuildPlanOptions(
        mode=effective_mode,
        duplicate_policy=cfg.duplicate_policy,
        orphan_live_policy=cfg.orphan_live_policy,
        cluster_time_window_minutes=cfg.cluster_time_window_minutes,
        event_min_items=cfg.event_min_items,
        month_min_items=cfg.month_min_items,
        quarter_fallback_enabled=cfg.quarter_fallback_enabled,
        live_subfolder_suffix=cfg.live_subfolder_suffix,
        same_place_radius_m=cfg.same_place_radius_m,
        place_change_radius_m=cfg.place_change_radius_m,
        stay_radius_km=cfg.stay_radius_km,
        max_gap_days_in_stay=cfg.max_gap_days_in_stay,
        usual_place_min_distinct_days=cfg.usual_place_min_distinct_days,
        confidence_review_enabled=cfg.confidence_review_enabled,
        geocode_cache_path=cfg.geocode_cache,
        home_gps=(cfg.home_lat, cfg.home_lon) if cfg.home_lat is not None and cfg.home_lon is not None else None,
        home_radius_km=cfg.home_radius_km,
        away_event_min_items=cfg.away_event_min_items,
    )


def _common_options():
    return {
        "env_file": typer.Option(
            ".env",
            "--env-file",
            help="Path to .env file (paths + MOVE_MODE).",
            show_default=True,
        ),
        "config": typer.Option(
            "config.yaml",
            "--config",
            help="Path to config.yaml (business rules).",
            show_default=True,
        ),
    }


@app.command()
def doctor(
    env_file: str = _common_options()["env_file"],
    config: str = _common_options()["config"],
):
    """Validate configuration and show resolved paths."""

    cfg = load_config(config_path=Path(config), env_path=Path(env_file))
    validate_paths(cfg)
    ensure_dirs(cfg)

    print("[bold green]OK[/bold green] Configuration loaded")
    print(f"SOURCE_ROOT: {cfg.paths.source_root}")
    print(f"TARGET_ROOT: {cfg.paths.target_root}")
    print(f"OUTPUT_ROOT: {cfg.paths.output_root}")
    print(f"QUARANTINE_ROOT: {cfg.paths.quarantine_root}")
    print(f"REVIEW_ROOT: {cfg.paths.review_root}")
    print(f"MOVE_MODE (default): {cfg.move_mode}")
    print(f"DRY_RUN_DEFAULT: {cfg.dry_run_default}")


@app.command()
def plan(
    env_file: str = _common_options()["env_file"],
    config: str = _common_options()["config"],
    batch_size: int = typer.Option(200, help="Number of files per ExifTool batch"),
    skip_exiftool: bool = typer.Option(False, help="Generate a minimal plan without ExifTool metadata"),
    mode: str | None = typer.Option(
        None,
        "--mode",
        help="Plan execution intent (copy|move). Defaults to MOVE_MODE from .env.",
    ),
    reclassify_scope: str = typer.Option(
        "auto",
        "--reclassify-scope",
        help="Incremental reclassification scope: auto|off.",
    ),
):
    """Scan SOURCE_ROOT and write a plan file (plan.jsonl) — no files are touched.

    Always safe to run. Review the plan before calling 'run --execute'."""

    cfg = load_config(config_path=Path(config), env_path=Path(env_file))
    validate_paths(cfg)
    ensure_dirs(cfg)

    ver: str | None = None
    if not skip_exiftool:
        try:
            ver = exiftool_version(cfg.exiftool_path)
        except ExifToolError as e:
            print(f"[bold red]ExifTool error[/bold red]: {e}")
            print("Tip: re-run with --skip-exiftool for a minimal plan.")
            raise typer.Exit(code=2)

    effective_mode = _effective_mode(mode, cfg.move_mode)
    reclassify_scope = reclassify_scope.strip().lower()
    if reclassify_scope not in {"auto", "off"}:
        raise typer.BadParameter("--reclassify-scope must be auto or off")

    files = scan_media_files(cfg.paths.source_root)
    if ver is not None:
        print(f"ExifTool: {ver}")
    print(f"Found {len(files)} media files under SOURCE_ROOT (recursive)")
    print(f"Reports will be written under: {cfg.paths.output_root}")

    if not files:
        raise typer.Exit(code=0)

    plan_items: list[PlanItem] = []
    deferred_records: list[MediaRecord] = []
    state = StateStore(cfg.paths.output_root / "state.sqlite")
    plan_run_id = state.start_run(mode=effective_mode, execute=False)
    impacted_months: set[str] = set()
    seen_in_run: set[str] = set()
    try:
        for i in range(0, len(files), batch_size):
            batch = files[i : i + batch_size]

            by_source: dict[str, dict] = {}
            if not skip_exiftool:
                meta = extract_metadata_json(batch, exiftool_path=cfg.exiftool_path)
                # ExifTool normalizes SourceFile separators (e.g. always "/" on
                # Windows even when invoked with "\" paths), so re-parse through
                # Path() on both sides to match regardless of the OS separator.
                by_source = {
                    str(Path(str(m["SourceFile"]))): m
                    for m in meta
                    if isinstance(m, dict) and m.get("SourceFile")
                }

            for p in batch:
                exif = by_source.get(str(p), {})
                sha = file_hash(p, algorithm=cfg.hash_algorithm)
                stat = p.stat()
                size = stat.st_size

                result = classify_or_defer(
                    source=p,
                    sha=sha,
                    size=size,
                    exif=exif,
                    options=_build_plan_options(cfg, effective_mode),
                    state=state,
                    seen_in_run=seen_in_run,
                )

                if isinstance(result, MediaRecord):
                    deferred_records.append(result)
                    month_key = month_key_for_datetime(result.reference_datetime)
                else:
                    plan_items.append(result)
                    month_key = result.bucket_key or month_key_for_datetime(datetime.fromtimestamp(stat.st_mtime))

                changed = state.record_file_snapshot(
                    source_path=str(p),
                    sha=sha,
                    size=size,
                    mtime_ns=stat.st_mtime_ns,
                    month_key=month_key,
                    run_id=plan_run_id,
                )
                if changed:
                    impacted_months.add(month_key)

        clustered_items = build_plan_items_from_records(
            deferred_records,
            options=_build_plan_options(cfg, effective_mode),
        )
        plan_items.extend(clustered_items)

        if reclassify_scope == "auto":
            reclass_items = build_reclassification_items(
                state=state,
                target_root=cfg.paths.target_root,
                impacted_month_keys=impacted_months,
                new_items=plan_items,
                live_subfolder_suffix=cfg.live_subfolder_suffix,
            )
            plan_items.extend(reclass_items)

        state.finish_run(
            plan_run_id,
            status="ok",
            summary={
                "planned": len(plan_items),
                "impacted_months": len(impacted_months),
            },
        )
    except Exception:
        state.finish_run(plan_run_id, status="failed", summary={"planned": len(plan_items)})
        raise
    finally:
        state.close()

    out_path = cfg.paths.output_root / "plan.jsonl"
    write_plan_jsonl(out_path, plan_items)
    print(f"Wrote plan: {out_path}")
    print(f"Impacted months in this scan: {len(impacted_months)}")

    actionable = [p for p in plan_items if p.action in {"copy", "move", "quarantine"}]
    print(f"Planned actionable items: {len(actionable)}")
    for item in actionable[:20]:
        print(f"- {item.source} -> {item.destination} [{item.action}] ({item.reason})")
    if len(actionable) > 20:
        print(f"... and {len(actionable) - 20} more")


@app.command()
def run(
    env_file: str = _common_options()["env_file"],
    config: str = _common_options()["config"],
    mode: str | None = typer.Option(
        None,
        "--mode",
        help="Override MOVE_MODE from .env (copy|move).",
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Actually perform file operations. Without this flag everything is a dry-run (nothing is copied/moved).",
    ),
    plan_file: str = typer.Option(
        "plan.jsonl",
        "--plan-file",
        help="Plan filename under OUTPUT_ROOT.",
    ),
):
    """Execute a previously generated plan (plan.jsonl).

    By default this is a DRY-RUN — pass --execute to actually copy/move files.

    Typical workflow::

        ./run.sh plan          # scan + write plan.jsonl, nothing touched
        ./run.sh run           # dry-run: shows what would happen
        ./run.sh run --execute # for real: copies/moves files
    """

    cfg = load_config(config_path=Path(config), env_path=Path(env_file))
    validate_paths(cfg)
    ensure_dirs(cfg)

    effective_mode = _effective_mode(mode, cfg.move_mode)

    effective_execute = execute
    if not effective_execute:
        print("[bold]Dry-run[/bold]: no files will be modified")
    print(f"Mode: {effective_mode}")

    plan_path = cfg.paths.output_root / plan_file
    if not plan_path.exists():
        raise FileNotFoundError(f"Plan file not found: {plan_path}")
    plan_items = read_plan_jsonl(plan_path)

    state = StateStore(cfg.paths.output_root / "state.sqlite")
    run_id = state.start_run(mode=effective_mode, execute=effective_execute)

    try:
        stats = execute_plan(
            cfg=cfg,
            plan_items=plan_items,
            state=state,
            run_id=run_id,
            effective_mode=effective_mode,
            execute=effective_execute,
            echo=print,
        )

        final_status = "failed" if stats["failed"] else "ok"
        state.finish_run(run_id, status=final_status, summary=stats)
    finally:
        state.close()

    print(f"Run #{run_id} summary: applied={stats['applied']} skipped={stats['skipped']} failed={stats['failed']}")
    if stats["failed"]:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
