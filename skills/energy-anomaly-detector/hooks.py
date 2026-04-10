# Copyright 2026 Ori Nexus Systems LTD
# SPDX-License-Identifier: Apache-2.0

"""Hooks for the bundled energy-anomaly-detector skill."""


def pre_trigger_eval(context):
    return context


def post_reasoning(result, context):
    return result
