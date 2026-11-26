"""Show a plan, confirm, execute."""
from __future__ import annotations

from ..context import Context
from ..plan import Plan


def run_plan(ctx: Context, plan: Plan, dry_run: bool = False, yes: bool = False) -> bool:
    """Returns True if the plan was executed."""
    ctx.ui.log(plan.render())
    if dry_run or plan.empty:
        return False
    if plan.destructive and not yes and not ctx.ui.confirm("Apply these changes?"):
        ctx.ui.log("cancelled")
        return False
    if not plan.destructive and not yes and not ctx.ui.confirm("Install?"):
        ctx.ui.log("cancelled")
        return False
    journal = plan.base / ".xemfg" if plan.base else None
    plan.execute(journal)
    return True
