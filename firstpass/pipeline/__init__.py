"""
firstpass.pipeline — End-to-end EDI order pipeline for FirstPass EDI.

Modules:
  order_pipeline    — Universal order lifecycle monitor (850→SO→shipped→856→997)
  reconciliation    — Cross-system order reconciliation (ERP + ShipStation + Orderful)

Typical usage:
    from firstpass.pipeline.order_pipeline import OrderPipeline
    from firstpass.pipeline.reconciliation import Reconciler

    # Register FastAPI routes
    OrderPipeline.register_routes(app)
    Reconciler.register_routes(app)

    # Run a pipeline sync (manual trigger or background loop)
    summary = OrderPipeline.sync_and_summarize()

    # Run a full reconcile
    snapshot = Reconciler.run(lookback_days=30)
"""

from .order_pipeline import OrderPipeline
from .reconciliation import Reconciler

__all__ = ["OrderPipeline", "Reconciler"]
