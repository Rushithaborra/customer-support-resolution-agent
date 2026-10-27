"""Refund policy parameters. Part 8 links these to the policy *text* for citations,
but the numbers that decide money stay here, in code."""

from datetime import timedelta

# Must match the refund policy version currently in effect (policies/refund_policy_v2.md);
# tests check that the numbers below appear in that text.
POLICY_VERSION = "refund_policy@v2"

# A return must reach the warehouse within this many days of delivery.
RETURN_WINDOW_DAYS = 30
# Damage must be reported within this many days of delivery.
DAMAGE_REPORT_WINDOW_DAYS = 7

# How long a reviewer's approval stays valid before execution.
APPROVAL_TTL = timedelta(hours=24)
# How long a proposal may wait for a reviewer before it expires and goes to a human.
PROPOSAL_TTL = timedelta(hours=48)
