from __future__ import annotations

from pathlib import Path
from typing import List

import streamlit as st

from amazon_scraper.feature_flags import (
    run_scoped_schema_enabled as read_run_scoped_schema_enabled,
    run_scoped_text_merge_mode as read_run_scoped_text_merge_mode,
)
from amazon_scraper.input_normalization import (
    build_jobs_from_table_rows,
    collect_requested_field_columns,
    get_supported_field_options,
    normalize_requested_field_input,
    parse_asin_input,
)
from amazon_scraper.product_type_schemas import load_product_type_field_defaults
from amazon_scraper.run_field_contracts import (
    MERGE_SIMILAR_VALUES_HELP,
    OUTPUT_TYPE_HELP,
    OUTPUT_TYPE_LABELS,
    UNSELECTED_OUTPUT_TYPE_LABEL,
    attach_run_field_contracts_to_jobs,
    build_generated_contract_editor_rows,
    deserialize_run_field_contracts,
    parse_run_field_contract_rows,
    serialize_run_field_contracts,
)
from ui_run_manager import TERMINAL_RUN_STATES, get_run_handle, remove_run_handle, request_stop, start_batch_run


def main() -> None:
    st.set_page_config(page_title="Amazon PDP Batch Extractor", layout="wide")
    _initialize_session_state()
    _sync_active_run_snapshot()

    product_type_defaults = load_product_type_field_defaults()
    supported_fields = get_supported_field_options()
    display_name_by_field = {
        option["canonical_name"]: option["display_name"]
        for option in supported_fields
    }

    st.title("Amazon PDP Batch Extractor")
    st.caption(
        "Build a mixed-product batch. Each row can use its own product type and required fields."
    )

    _render_sidebar()

    st.subheader("Bulk Row Builder")
    batch_product_type = st.selectbox(
        "Default product type for new rows",
        options=[""] + sorted(product_type_defaults.keys()),
        format_func=lambda value: "None" if not value else value.replace("_", " ").title(),
        help="Used only when creating new draft rows. You can change product type per row later.",
    )
    st.session_state["batch_product_type"] = batch_product_type

    left_column, right_column = st.columns([1.2, 1.0], gap="large")

    with left_column:
        raw_asins = st.text_area(
            "Paste ASINs",
            value=st.session_state["raw_asins"],
            height=220,
            placeholder="B0DJWV4BQ1\nB0FSGHP1FC\nB0FRVXJTW2",
            help="Paste ASINs to create draft rows. You can mix product types later in the table below.",
        )
        st.session_state["raw_asins"] = raw_asins

    with right_column:
        st.subheader("Default Required Fields For New Rows")
        default_fields_input = st.text_area(
            "Required fields",
            value=st.session_state["default_fields_input"],
            height=180,
            placeholder="Serving size, ingredients, packaging type",
            help="Type field names separated by commas. These are only used to seed new rows. The app will normalize them internally before scraping.",
        )
        st.session_state["default_fields_input"] = default_fields_input

        default_column, clear_column = st.columns(2)
        with default_column:
            if st.button("Load Defaults", use_container_width=True, disabled=not batch_product_type):
                _handle_load_defaults(batch_product_type, product_type_defaults, display_name_by_field)
        with clear_column:
            if st.button("Clear Fields", use_container_width=True):
                st.session_state["default_fields_input"] = ""
                st.session_state["field_feedback"] = []

        _render_field_feedback()
        _render_default_field_preview(default_fields_input, display_name_by_field)

    seed_column, clear_rows_column = st.columns(2)
    with seed_column:
        if st.button("Create Draft Rows From ASINs", use_container_width=True):
            _handle_create_rows_from_asins(batch_product_type)
    with clear_rows_column:
        if st.button("Clear Job Rows", use_container_width=True):
            st.session_state["job_rows"] = []
            st.session_state["run_field_contracts"] = {}
            st.session_state["run_field_contract_editor_rows"] = []
            st.session_state["run_field_contract_missing_fields"] = []
            st.session_state["field_feedback"] = [("success", "Cleared all draft job rows.")]

    st.subheader("Job Rows")
    st.caption(
        "Each row is independent. Set product type and required fields per ASIN if your batch contains different products."
    )
    edited_rows = st.data_editor(
        st.session_state["job_rows"],
        use_container_width=True,
        num_rows="dynamic",
        hide_index=True,
        column_config={
            "asin": st.column_config.TextColumn("ASIN", required=True),
            "product_type": st.column_config.TextColumn(
                "Product Type",
                help="Optional. Known schema types can drive defaults, but you can type any product label here.",
            ),
            "required_fields": st.column_config.TextColumn(
                "Required Fields",
                help="Comma, pipe, newline, or semicolon separated. Human-friendly names are okay.",
                width="large",
            ),
        },
    )
    st.session_state["job_rows"] = edited_rows
    if product_type_defaults:
        st.caption(
            "Known default-enabled product types: "
            + ", ".join(sorted(product_type_defaults.keys()))
        )

    build_result = build_jobs_from_table_rows(
        st.session_state["job_rows"],
        product_type_defaults=product_type_defaults,
    )

    st.subheader("Batch Preview")
    if st.session_state["extract_supplement_facts_details"]:
        st.info(
            "Run option enabled: `extract_supplement_facts_details=true`. "
            "This run will flatten Supplement Facts metadata columns and build run-scoped ingredient columns."
        )
    _render_build_issues(build_result)
    _render_run_field_contract_editor(build_result)
    _render_preview_table(build_result, display_name_by_field)
    _render_run_controls(build_result)
    _render_run_results()


def _initialize_session_state() -> None:
    st.session_state.setdefault("raw_asins", "")
    st.session_state.setdefault("default_fields_input", "")
    st.session_state.setdefault("field_feedback", [])
    st.session_state.setdefault("batch_product_type", "")
    st.session_state.setdefault("run_output_path", "")
    st.session_state.setdefault("run_download_name", "")
    st.session_state.setdefault("run_download_bytes", b"")
    st.session_state.setdefault("run_rows", [])
    st.session_state.setdefault("run_status", "idle")
    st.session_state.setdefault("run_message", "")
    st.session_state.setdefault("run_error", "")
    st.session_state.setdefault("run_started_at", "")
    st.session_state.setdefault("run_finished_at", "")
    st.session_state.setdefault("active_run_id", "")
    st.session_state.setdefault("headless", False)
    st.session_state.setdefault("extract_supplement_facts_details", False)
    st.session_state.setdefault("delay_seconds", 1.5)
    st.session_state.setdefault("output_filename", "streamlit_batch_results.csv")
    st.session_state.setdefault("log_level", "INFO")
    st.session_state.setdefault("debug_html_dir", "logs/html_snapshots")
    st.session_state.setdefault("job_rows", [])
    st.session_state.setdefault("run_field_contracts", {})
    st.session_state.setdefault("run_field_contract_editor_rows", [])
    st.session_state.setdefault("run_field_contract_missing_fields", [])


def _render_sidebar() -> None:
    st.sidebar.header("Run Options")
    st.session_state["headless"] = st.sidebar.checkbox(
        "Headless browser",
        value=st.session_state["headless"],
        help="Turn this off for testing if you want to watch the browser and manually handle Amazon prompts.",
    )
    st.session_state["extract_supplement_facts_details"] = st.sidebar.checkbox(
        "Extract Supplement Facts Details",
        value=st.session_state["extract_supplement_facts_details"],
        help=(
            "When enabled, the run parses Supplement Facts panels into flattened metadata columns "
            "plus run-scoped ingredient columns. When disabled, the run keeps the existing "
            "requested-field-only behavior."
        ),
    )
    st.session_state["delay_seconds"] = st.sidebar.number_input(
        "Delay between ASINs (seconds)",
        min_value=0.0,
        value=float(st.session_state["delay_seconds"]),
        step=0.5,
    )
    st.session_state["log_level"] = st.sidebar.selectbox(
        "Log level",
        options=["DEBUG", "INFO", "WARNING", "ERROR"],
        index=["DEBUG", "INFO", "WARNING", "ERROR"].index(st.session_state["log_level"]),
    )
    st.session_state["debug_html_dir"] = st.sidebar.text_input(
        "Debug HTML directory",
        value=st.session_state["debug_html_dir"],
        help="Leave blank to disable per-ASIN HTML snapshot export.",
    )
    st.session_state["output_filename"] = st.sidebar.text_input(
        "Output CSV filename",
        value=st.session_state["output_filename"],
    )

def _handle_load_defaults(
    product_type: str,
    product_type_defaults: dict[str, List[str]],
    display_name_by_field: dict[str, str],
) -> None:
    default_fields = list(dict.fromkeys(product_type_defaults.get(product_type, [])))
    if not default_fields:
        st.session_state["field_feedback"] = [("error", "No default fields exist for the selected product type.")]
        return
    st.session_state["default_fields_input"] = ", ".join(
        display_name_by_field.get(field_name, field_name.replace("_", " ").title())
        for field_name in default_fields
    )
    st.session_state["field_feedback"] = [
        ("success", f"Loaded {len(default_fields)} default field(s) for {product_type.replace('_', ' ')}.")
    ]


def _handle_create_rows_from_asins(default_product_type: str) -> None:
    asins, asin_issues = parse_asin_input(st.session_state["raw_asins"])
    feedback: list[tuple[str, str]] = []
    for issue in asin_issues:
        feedback.append(("warning" if issue.level == "warning" else "error", issue.message))
    if not asins:
        st.session_state["field_feedback"] = feedback or [("error", "Paste at least one valid ASIN first.")]
        return

    seeded_rows = list(st.session_state["job_rows"])
    existing_asins = {str(row.get("asin", "") or "").strip().upper() for row in seeded_rows}
    default_fields = st.session_state["default_fields_input"].strip()
    added_count = 0
    for asin in asins:
        if asin in existing_asins:
            continue
        seeded_rows.append(
            {
                "asin": asin,
                "product_type": default_product_type,
                "required_fields": default_fields,
            }
        )
        added_count += 1

    st.session_state["job_rows"] = seeded_rows
    feedback.insert(
        0,
        (
            "success",
            f"Added {added_count} draft row(s). You can now edit product type and required fields per ASIN.",
        ),
    )
    st.session_state["field_feedback"] = feedback


def _render_field_feedback() -> None:
    for level, message in st.session_state["field_feedback"]:
        if level == "success":
            st.success(message)
        elif level == "warning":
            st.warning(message)
        else:
            st.error(message)


def _render_default_field_preview(raw_field_input: str, display_name_by_field: dict[str, str]) -> None:
    if not raw_field_input.strip():
        st.info("No explicit fields added yet. If you leave this empty, batch product-type defaults will be used.")
        return

    normalized_fields, selections, issues = normalize_requested_field_input(raw_field_input)
    for issue in issues:
        if issue.level == "warning":
            st.warning(issue.message)
        else:
            st.error(issue.message)

    if not normalized_fields:
        return

    with st.expander("Show internal field mapping", expanded=False):
        rows = [
            {
                "Order": index,
                "Requested Label": selection.raw_input,
                "Display Name": display_name_by_field.get(
                    selection.canonical_name,
                    selection.canonical_name.replace("_", " ").title(),
                ),
                "Internal Key": selection.canonical_name,
                "Matched By": selection.matched_by,
            }
            for index, selection in enumerate(selections, start=1)
        ]
        st.dataframe(rows, use_container_width=True, hide_index=True)


def _render_build_issues(build_result) -> None:
    for issue in build_result.issues:
        if issue.level == "warning":
            st.warning(issue.message)
        else:
            st.error(issue.message)


def _render_run_field_contract_editor(build_result) -> None:
    current_contracts = deserialize_run_field_contracts(st.session_state["run_field_contracts"])
    editor_rows = build_generated_contract_editor_rows(
        build_result.field_selections,
        current_contracts,
        st.session_state["run_field_contract_editor_rows"],
    )
    if not editor_rows:
        st.session_state["run_field_contracts"] = {}
        st.session_state["run_field_contract_editor_rows"] = []
        st.session_state["run_field_contract_missing_fields"] = []
        return

    st.subheader("Current Run Field Contract")
    st.caption(
        "Generated fields need a type for this run. These definitions apply only to the current run and are not saved automatically."
    )
    if not read_run_scoped_schema_enabled(default=False):
        st.warning(
            "Run-scoped schema runtime is currently disabled by `RUN_SCOPED_SCHEMA_ENABLED=false`. "
            "The contract is still captured in the UI, but the legacy runtime path remains active until the flag is enabled."
        )
    merge_mode = read_run_scoped_text_merge_mode(default="off")
    if merge_mode == "off":
        st.info(
            "Run-scoped text merge is currently disabled by `RUN_SCOPED_TEXT_MERGE_MODE=off`. "
            "`Merge Similar Values` selections are captured, but text values are not evaluated or rewritten yet."
        )
    elif merge_mode == "shadow":
        st.info(
            "Run-scoped text merge is in shadow mode via `RUN_SCOPED_TEXT_MERGE_MODE=shadow`. "
            "Merge decisions are evaluated and logged, but exported values are not rewritten."
        )
    with st.expander("Data type help", expanded=False):
        st.markdown(
            "`Number`: one numeric value only, like `30` or `500`.\n\n"
            "`Short list`: multiple items kept in one cell and joined with ` | `.\n\n"
            "`Text`: one cleaned phrase or value, like `vanilla` or `unflavored`."
        )

    edited_rows = st.data_editor(
        editor_rows,
        key="run-field-contract-editor",
        use_container_width=True,
        hide_index=True,
        disabled=["Requested Label", "Internal Key", "Matched By"],
        column_config={
            "Requested Label": st.column_config.TextColumn(
                "Requested Label",
                help="The exact field label typed in the UI or CSV.",
            ),
            "Internal Key": st.column_config.TextColumn(
                "Internal Key",
                help="Stable internal field key used in output columns.",
            ),
            "Matched By": st.column_config.TextColumn(
                "Matched By",
                help="How the resolver classified this field before typing.",
            ),
            "Data Type": st.column_config.SelectboxColumn(
                "Data Type",
                options=[UNSELECTED_OUTPUT_TYPE_LABEL, "Number", "Short list", "Text"],
                help=(
                    f"Number: {OUTPUT_TYPE_HELP['number']} "
                    f"Short list: {OUTPUT_TYPE_HELP['short_list']} "
                    f"Text: {OUTPUT_TYPE_HELP['text']}"
                ),
            ),
            "Merge Similar Values": st.column_config.CheckboxColumn(
                "Merge Similar Values",
                help=MERGE_SIMILAR_VALUES_HELP,
            ),
        },
    )
    editor_needs_refresh = edited_rows != editor_rows
    st.session_state["run_field_contract_editor_rows"] = edited_rows
    contracts, missing_fields = parse_run_field_contract_rows(edited_rows)
    st.session_state["run_field_contracts"] = serialize_run_field_contracts(contracts)
    st.session_state["run_field_contract_missing_fields"] = missing_fields

    if editor_needs_refresh:
        st.rerun()

    if missing_fields:
        st.warning("Assign a data type before running for: " + ", ".join(missing_fields))

    if contracts:
        preview_rows = [
            {
                "Requested Label": contract.requested_label,
                "Internal Key": contract.internal_key,
                "Data Type": OUTPUT_TYPE_LABELS[contract.output_type],
                "Merge Similar Values": contract.merge_similar_values,
            }
            for contract in contracts.values()
        ]
        st.dataframe(preview_rows, use_container_width=True, hide_index=True)


def _render_preview_table(build_result, display_name_by_field: dict[str, str]) -> None:
    if not build_result.jobs:
        st.info("Create at least one valid job row to preview the batch.")
        return

    preview_rows = []
    for job in build_result.jobs:
        preview_rows.append(
            {
                "ASIN": job.asin,
                "Product Type": job.product_type or "ad_hoc",
                "Required Fields": ", ".join(
                    display_name_by_field.get(field_name, field_name.replace("_", " ").title())
                    for field_name in job.requested_fields
                ),
                "Canonical Fields": ", ".join(job.requested_fields),
            }
        )
    st.dataframe(preview_rows, use_container_width=True, hide_index=True)


def _render_run_controls(build_result) -> None:
    missing_contracts = st.session_state["run_field_contract_missing_fields"]
    _render_run_controls_fragment(
        build_result,
        not build_result.jobs or bool(build_result.errors) or bool(missing_contracts),
    )


@st.fragment(run_every=2)
def _render_run_controls_fragment(build_result, run_disabled: bool) -> None:
    active_handle = _get_active_run_handle()
    st.subheader("Batch Run")
    if active_handle is None:
        if st.button("Run Batch", type="primary", disabled=run_disabled, key="run-batch-button"):
            _start_batch_run(build_result)
            st.rerun()
        _render_last_run_summary()
        return

    snapshot = active_handle.snapshot()
    state = str(snapshot["state"])
    if state in TERMINAL_RUN_STATES:
        _sync_active_run_snapshot()
        st.rerun()
        return

    info_column, action_column = st.columns([3.0, 1.0], gap="large")
    with info_column:
        st.info(
            f"Status: {state.title()} | Progress: {snapshot['completed_jobs']}/{snapshot['total_jobs']} job(s)"
        )
        if snapshot["message"]:
            st.caption(str(snapshot["message"]))
        if snapshot["current_asin"]:
            st.caption(f"Current ASIN: {snapshot['current_asin']}")
    with action_column:
        if st.button("Stop Batch", type="secondary", use_container_width=True, key="stop-batch-button"):
            request_stop(active_handle.run_id)
            st.rerun()

    if snapshot["stop_requested"]:
        st.warning("Stop requested. The app is closing the active browser session and waiting for the current network step to exit.")


def _start_batch_run(build_result) -> None:
    output_path = Path("outputs") / st.session_state["output_filename"]
    debug_html_dir = (st.session_state["debug_html_dir"] or "").strip() or None
    run_field_contracts = deserialize_run_field_contracts(st.session_state["run_field_contracts"])
    jobs = attach_run_field_contracts_to_jobs(build_result.jobs, run_field_contracts)
    export_columns = collect_requested_field_columns(jobs)
    handle = start_batch_run(
        jobs=jobs,
        output_path=output_path,
        export_columns=export_columns,
        headless=st.session_state["headless"],
        extract_supplement_facts_details=bool(st.session_state["extract_supplement_facts_details"]),
        debug_html_dir=debug_html_dir,
        delay_seconds=float(st.session_state["delay_seconds"]),
        log_level=st.session_state["log_level"],
    )
    st.session_state["active_run_id"] = handle.run_id
    st.session_state["run_status"] = "queued"
    st.session_state["run_message"] = handle.message
    st.session_state["run_error"] = ""
    st.session_state["run_started_at"] = ""
    st.session_state["run_finished_at"] = ""


def _render_run_results() -> None:
    if st.session_state["run_output_path"]:
        st.caption(f"Latest output: {st.session_state['run_output_path']}")

    if not st.session_state["run_rows"]:
        return

    st.subheader("Latest Results")
    st.dataframe(st.session_state["run_rows"], use_container_width=True, hide_index=True)
    if st.session_state["run_download_bytes"]:
        st.download_button(
            "Download CSV",
            data=st.session_state["run_download_bytes"],
            file_name=st.session_state["run_download_name"] or "batch_results.csv",
            mime="text/csv",
        )


def _render_last_run_summary() -> None:
    status = st.session_state["run_status"]
    message = st.session_state["run_message"]
    error = st.session_state["run_error"]

    if status == "completed" and message:
        st.success(message)
    elif status == "stopped" and message:
        st.warning(message)
    elif status == "error":
        st.error(error or message or "The last batch failed.")


def _sync_active_run_snapshot() -> None:
    active_run_id = st.session_state["active_run_id"]
    if not active_run_id:
        return

    handle = get_run_handle(active_run_id)
    if handle is None:
        st.session_state["active_run_id"] = ""
        return

    snapshot = handle.snapshot()
    st.session_state["run_status"] = str(snapshot["state"])
    st.session_state["run_message"] = str(snapshot["message"])
    st.session_state["run_error"] = str(snapshot["error"])
    st.session_state["run_started_at"] = str(snapshot["started_at"])
    st.session_state["run_finished_at"] = str(snapshot["finished_at"])

    if snapshot["state"] not in TERMINAL_RUN_STATES:
        return

    if snapshot["rows"]:
        st.session_state["run_rows"] = list(snapshot["rows"])
        st.session_state["run_output_path"] = str(snapshot["output_path_text"])
        st.session_state["run_download_name"] = str(snapshot["download_name"])
        st.session_state["run_download_bytes"] = snapshot["download_bytes"]
    else:
        # A terminal run that produced no rows (e.g. a config error before
        # scraping, or a stop/budget event before the first job finished) must
        # not leave a previous run's results and download on screen as if they
        # were the current result.
        st.session_state["run_rows"] = []
        st.session_state["run_output_path"] = ""
        st.session_state["run_download_name"] = ""
        st.session_state["run_download_bytes"] = b""

    st.session_state["active_run_id"] = ""
    remove_run_handle(active_run_id)


def _get_active_run_handle():
    active_run_id = st.session_state["active_run_id"]
    if not active_run_id:
        return None
    handle = get_run_handle(active_run_id)
    if handle is None:
        st.session_state["active_run_id"] = ""
        return None
    if str(handle.snapshot()["state"]) in TERMINAL_RUN_STATES:
        return None
    return handle
if __name__ == "__main__":
    main()
