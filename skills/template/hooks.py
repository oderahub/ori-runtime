# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""Template hooks for skill authors."""


def pre_trigger_eval(context):
    """Optional: compute derived variables before rule evaluation."""
    return context


def post_reasoning(result, context):
    """Optional: enrich ReasoningResult before dispatch."""
    return result
