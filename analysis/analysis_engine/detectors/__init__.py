# Copied from: analysis phase cidecode hackathon/analysis phase cidecode hackathon/analysis_engine/detectors/__init__.py
"""Detector registry. Each detector is a self-contained module with one public function."""

from .accumulation import detect_accumulation_accounts
from .circular import detect_circular_flows
from .credit_to_cash import detect_credit_to_cash_chains
from .cross_statement import detect_cross_statement_flows
from .duplicates import detect_duplicate_cross_check
from .dormant_reactivation import detect_dormant_reactivation
from .first_contact import detect_first_contact_large_transfers
from .high_throughput import detect_high_throughput_pass_through
from .holding_accounts import detect_holding_accounts
from .hub_ranking import detect_high_risk_hub_ranking
from .internal_flow_hub import detect_matched_internal_flow_hub
from .low_value_testing import detect_low_value_testing
from .money_trail import detect_auto_money_trails, detect_requested_money_trails, trace_credit
from .ml_ensemble import detect_ml_ensemble_anomaly_leads
from .reversal_clusters import detect_reversal_clusters
from .reversals import detect_reversals
from .round_trip import detect_round_trips
from .round_value_debits import detect_round_value_debits
from .shared_upi import detect_shared_upi_identifiers
from .structuring import detect_structuring
from .suspicious_ranking import detect_top_suspicious_ranking
from .transit import detect_transit_accounts

__all__ = [
    "detect_accumulation_accounts",
    "detect_circular_flows",
    "detect_credit_to_cash_chains",
    "detect_cross_statement_flows",
    "detect_duplicate_cross_check",
    "detect_dormant_reactivation",
    "detect_first_contact_large_transfers",
    "detect_high_risk_hub_ranking",
    "detect_high_throughput_pass_through",
    "detect_holding_accounts",
    "detect_low_value_testing",
    "detect_matched_internal_flow_hub",
    "detect_ml_ensemble_anomaly_leads",
    "detect_auto_money_trails",
    "detect_requested_money_trails",
    "detect_reversal_clusters",
    "detect_reversals",
    "detect_round_trips",
    "detect_round_value_debits",
    "detect_shared_upi_identifiers",
    "detect_structuring",
    "detect_top_suspicious_ranking",
    "detect_transit_accounts",
    "trace_credit",
]
