# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""
Hooks for pc-system-health skill.
Computes derived metrics that cannot be expressed in YAML conditions alone.
"""


def pre_trigger_eval(context):
    """
    Called before trigger condition evaluation.
    Computes write_rate_mb_per_min from cumulative disk_write_mb delta.
    Injects derived values into context so YAML conditions can use them.
    """
    current_write_mb = context.readings.get("disk_write_mb", 0)
    last_write_mb = context.history.last_value("disk_write_mb")
    last_timestamp = context.history.last_timestamp("disk_write_mb")

    if last_write_mb is not None and last_timestamp is not None:
        elapsed_min = (context.timestamp - last_timestamp) / 60_000
        if elapsed_min > 0:
            delta = current_write_mb - last_write_mb
            context.derived["write_rate_mb_per_min"] = delta / elapsed_min
            baseline = context.history.avg_hours("write_rate_mb_per_min", 1) or 1.0
            context.derived["write_rate_ratio"] = (
                context.derived["write_rate_mb_per_min"] / baseline
            )

    return context


def post_reasoning(result, ctx):
    """
    Enriches sleep_blocked reasoning with specific process names
    and actionable termination suggestion.
    """
    if ctx.trigger_name == "sleep_blocked":
        processes = ctx.readings.get("sleep_blocking_processes_metadata", {})
        process_list = processes.get("processes", [])

        if process_list:
            names = [p["name"] for p in process_list]
            result.metadata["blocking_process_names"] = ", ".join(names)
            result.metadata["blocking_pids"] = [p["pid"] for p in process_list]

            # Note for future Tier C action:
            # These PIDs can be terminated with psutil.Process(pid).terminate()
            # without root privileges, as they are user-owned processes.
            # Phase 2: implement process_manager action with approval workflow.

    return result
