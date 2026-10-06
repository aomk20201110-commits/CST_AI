"""CST_AI plugin layer — module version table.

Kept out of the orchestrator so the orchestration logic carries no numeric
version literals of its own.
"""

from __future__ import annotations

MODULE_VERSIONS = {
    "cst_session_discovery_v19a": "1.9A.0",
    "cst_model_builder_v17": "1.7.0",
    "cst_model_executor_v17": "1.7.0",
    "cst_modelspec_v17": "1.7.0",
    "cst_solver_control_v19a": "1.9A.0",
    "cst_result_api_v19": "1.9.0",
    "v19b_real_solve": "1.9B.0",
    "v110c_analysis": "1.10C.0",
    "cst_ai_plugin": "1.15.0",
}
