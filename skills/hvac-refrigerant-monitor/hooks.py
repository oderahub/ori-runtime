# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""Hooks for hvac-refrigerant-monitor skill."""


def post_reasoning(result, ctx):
    """Prepend joint label to reasoning text when available."""
    joint_label = None

    readings = getattr(ctx, "readings", {})
    if isinstance(readings, dict):
        maybe_label = readings.get("joint_label")
        if isinstance(maybe_label, str) and maybe_label.strip():
            joint_label = maybe_label.strip()

    if joint_label is None:
        reading_obj = getattr(ctx, "reading", None)
        metadata = getattr(reading_obj, "metadata", None)
        if isinstance(metadata, dict):
            maybe_label = metadata.get("joint_label")
            if isinstance(maybe_label, str) and maybe_label.strip():
                joint_label = maybe_label.strip()

    if joint_label:
        result.text = f"[Joint: {joint_label}] {result.text}"
    return result
