"""
firstpass.dashboard — Dashboard API routes and partner health for FirstPass EDI.

Modules:
  routes         — All /api/dashboard/* and /api/edi/* FastAPI endpoints
                   (merged from EDI-specific + general dashboard sources)
  partner_health — Trading-partner registry, health scoring, and system health

Usage in api.py:
    from firstpass.dashboard.routes import register_dashboard_routes
    register_dashboard_routes(app)
"""

from .routes import register_dashboard_routes
from .partner_health import (
    get_partner_summary,
    get_partner,
    register_partner,
    update_partner_status,
    compute_edi_health_score,
)

__all__ = [
    "register_dashboard_routes",
    "get_partner_summary",
    "get_partner",
    "register_partner",
    "update_partner_status",
    "compute_edi_health_score",
]
