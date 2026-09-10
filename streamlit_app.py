"""
Solar Plant Layout Generator -- Streamlit front-end.

Client-facing workflow: upload the three surveyed coordinate CSVs, say how
many megawatts are required, click Generate. Everything else (module type,
table config, spacing, setbacks, service road) is pre-filled with sensible
defaults but stays adjustable under "Advanced layout settings" so the same
app can be reused for a different client/site without touching code.

Run with:  streamlit run streamlit_app.py
"""
import io
import logging
import tempfile
from pathlib import Path

import ezdxf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from ezdxf.addons.drawing import RenderContext, Frontend
from ezdxf.addons.drawing.matplotlib import MatplotlibBackend

from core.pipeline import run_pipeline
from core.dwg_export import DWGExportError

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

st.set_page_config(page_title="Solar Plant Layout Generator", page_icon="\u2600\ufe0f", layout="wide")

st.title("\u2600\ufe0f Solar Plant Layout Generator")
st.caption(
    "Upload -- or paste -- the site's surveyed coordinates, say how many megawatts are "
    "required, and generate a DXF/DWG plant layout."
)


def compute_table_height(rows, module_length_m, module_width_m, portrait):
    """Matches core.packing.TableSpec.table_height exactly."""
    return rows * (module_length_m if portrait else module_width_m)


PASTE_PLACEHOLDER = "POINT,EASTING,NORTHING\nB-01,535216.622,2267041.462\nB-02,535208.4,2267038.9\n..."


def coordinates_input(label: str, key_prefix: str, required: bool = True):
    """
    Lets the user either upload a CSV/Excel file or paste coordinate data directly
    (e.g. copied from Excel -- tab or comma separated both work).
    Returns a pandas DataFrame, or None if nothing was provided yet.
    """
    st.markdown(f"**{label}**{' *' if required else ' (optional)'}")
    method = st.radio(
        "Input method", ["Upload CSV / Excel", "Paste data"],
        horizontal=True, key=f"{key_prefix}_method", label_visibility="collapsed",
    )
    df = None
    if method == "Upload CSV / Excel":
        file = st.file_uploader("CSV or Excel file", type=["csv", "xlsx", "xls"], key=f"{key_prefix}_file", label_visibility="collapsed")
        if file:
            try:
                if file.name.lower().endswith((".xlsx", ".xls")):
                    df = pd.read_excel(file)
                else:
                    try:
                        df = pd.read_csv(file)
                    except Exception:
                        file.seek(0)
                        df = pd.read_excel(file)
            except Exception as exc:
                st.error(f"Couldn't read that file: {exc}")
    else:
        text = st.text_area(
            "Pasted data", height=140, key=f"{key_prefix}_paste",
            placeholder=PASTE_PLACEHOLDER, label_visibility="collapsed",
        )
        if text.strip():
            try:
                df = pd.read_csv(io.StringIO(text), sep=None, engine="python")
            except Exception as exc:
                st.error(f"Couldn't parse pasted data: {exc}")
    return df


def multi_coordinates_input(label: str, key_prefix: str, min_count: int = 0,
                            required_first: bool = False):
    """
    Like coordinates_input(), but supports any number of zones (e.g.
    multiple site parcels, or multiple physical control/inverter room
    buildings) via Add/Remove buttons -- the same pattern as the private
    areas section, generalized.

    min_count is the floor the Remove button won't go below (e.g. 1 for
    boundary, since at least one parcel is required; 0 for optional zones).

    Returns a list of DataFrames (may be empty).
    """
    state_key = f"num_{key_prefix}"
    if state_key not in st.session_state:
        st.session_state[state_key] = max(min_count, 1 if required_first else min_count)

    dfs = []
    for i in range(st.session_state[state_key]):
        zone_label = f"{label} {i + 1}" if st.session_state[state_key] > 1 else label
        is_required = required_first and i == 0
        dfs.append(coordinates_input(zone_label, f"{key_prefix}_{i}", required=is_required))

    bcol1, bcol2 = st.columns(2)
    with bcol1:
        if st.button(f"+ Add {label.lower()}", key=f"add_{key_prefix}"):
            st.session_state[state_key] += 1
            st.rerun()
    with bcol2:
        if st.session_state[state_key] > min_count:
            if st.button(f"− Remove last", key=f"remove_{key_prefix}"):
                st.session_state[state_key] -= 1
                st.rerun()

    return dfs


# ---------------------------------------------------------------------
# Required client inputs
# ---------------------------------------------------------------------
st.header("1. Site Coordinates")
st.caption(
    "Columns: `POINT`, `EASTING`, `NORTHING` -- upload a CSV or paste rows copied from Excel. "
    "Add more than one zone for multiple parcels, or multiple physical control/inverter room "
    "buildings (e.g. this project's own drawings report 2 separate inverter control rooms)."
)

col1, col2, col3 = st.columns(3)
with col1:
    boundary_dfs = multi_coordinates_input("Boundary parcel", "boundary", min_count=1, required_first=True)
with col2:
    control_dfs = multi_coordinates_input("Control room", "control", min_count=0)
with col3:
    inverter_dfs = multi_coordinates_input("Inverter room", "inverter", min_count=0)

st.header("2. Private / Restricted Areas (optional)")
st.caption(
    "Any zone that must be kept clear of tables AND marked as REJECTED on the drawing "
    "(bold outline + diagonal strike-out hatch + area label) -- distinct from the control/"
    "inverter rooms above, which are excluded but not specially marked."
)
if "num_private_areas" not in st.session_state:
    st.session_state.num_private_areas = 0

private_area_dfs = []
for i in range(st.session_state.num_private_areas):
    df = coordinates_input(f"Private area {i + 1}", f"private_{i}", required=False)
    private_area_dfs.append(df)

bcol1, bcol2 = st.columns(2)
with bcol1:
    if st.button("+ Add another private area"):
        st.session_state.num_private_areas += 1
        st.rerun()
with bcol2:
    if st.session_state.num_private_areas > 0:
        if st.button("− Remove last private area"):
            st.session_state.num_private_areas -= 1
            st.rerun()

st.header("3. Required Capacity")
c1, c2 = st.columns(2)
with c1:
    target_ac_mw = st.number_input(
        "Total AC capacity required (MW)",
        min_value=0.0, value=6.0, step=0.1,
        help="Total inverter output capacity. The number of inverters needed is calculated "
             "from this and the inverter rating set under Advanced settings below.",
    )
with c2:
    dc_ac_ratio = st.number_input(
        "DC/AC Ratio",
        min_value=0.1, value=1.293, step=0.01,
        help="Required DC Capacity = AC capacity x DC/AC Ratio. This ratio -- not a separately "
             "entered DC figure -- is what drives strings/tables per inverter.",
    )
implied_dc_mwp = target_ac_mw * dc_ac_ratio
st.caption(
    f"Implied DC capacity: {target_ac_mw:.2f} MW x {dc_ac_ratio:.3f} = **{implied_dc_mwp:.2f} MWp**. "
    "Inverter *rating* (kW) is set once under Advanced settings and reused; inverter *count* is "
    "calculated, not entered."
)

# ---------------------------------------------------------------------
# Advanced / per-client configuration -- spacing, module, table, etc.
# Collapsed by default; the client normally never needs to open this.
# ---------------------------------------------------------------------
with st.expander("\u2699\ufe0f Advanced layout settings (module, table, spacing, roads)"):

    st.subheader("Module")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        power_wp = st.number_input("Module power (Wp)", min_value=1.0, value=532.0, step=1.0)
    with c2:
        module_length_mm = st.number_input("Module length (mm)", min_value=1.0, value=2300.0, step=1.0)
    with c3:
        module_width_mm = st.number_input("Module width (mm)", min_value=1.0, value=1233.33, step=1.0)
    with c4:
        technology = st.text_input("Technology", value="THINFILM")

    st.subheader("Table (Mounting Structure)")
    c1, c2, c3 = st.columns(3)
    with c1:
        table_rows = st.number_input("Rows per table", min_value=1, value=2, step=1)
    with c2:
        table_columns = st.number_input("Columns per table", min_value=1, value=24, step=1)
    with c3:
        portrait = st.selectbox("Orientation", ["Portrait", "Landscape"], index=0) == "Portrait"
    st.caption(f"= {int(table_rows) * int(table_columns)} modules per table")

    st.subheader("Spacing")
    c1, c2, c3 = st.columns(3)
    with c1:
        table_gap_m = st.number_input(
            "Gap between tables in a row (m)", min_value=0.0, value=0.5, step=0.1,
            help="Horizontal gap, table to table, within the same row.",
        )
    with c2:
        row_pitch_m = st.number_input(
            "Row pitch, front-to-front (m)", min_value=0.1, value=6.0, step=0.1,
            help="Vertical spacing measured from the front edge of one table row to the front edge of the next.",
        )
    with c3:
        tilt_angle_deg = st.number_input("Tilt angle (deg)", min_value=0.0, value=0.0, step=0.5)

    st.subheader("Boundary Setback & Lightning Protection")
    c1, c2 = st.columns(2)
    with c1:
        boundary_setback_m = st.number_input(
            "Boundary setback (m)", min_value=0.0, value=8.0, step=0.5,
            help="Minimum clearance kept between the property boundary and any panel. Fully enforced but not drawn as a separate line.",
        )
    with c2:
        arrester_radius_m = st.number_input(
            "Lightning Arrester Radius (m)", min_value=1.0, value=107.0, step=1.0,
            help="Protection radius for ESE lightning arresters placed over the PV array.",
        )

    st.subheader("Internal Service Road (hugs the panel array)")
    c1, c2, c3 = st.columns(3)
    with c1:
        service_offset_m = st.number_input(
            "Offset from panels (m)", min_value=0.0, value=2.0, step=0.5,
            help="Clear gap between the panel array and the start of the road corridor.",
        )
    with c2:
        service_road_width_m = st.number_input("Road width (m)", min_value=0.0, value=3.0, step=0.5)
    with c3:
        service_shoulder_m = st.number_input("Shoulder width, each side (m)", min_value=0.0, value=0.5, step=0.1)
    st.caption(
        f"Total corridor reserved around the array: {service_offset_m:.1f} + {service_shoulder_m:.1f} + "
        f"{service_road_width_m:.1f} + {service_shoulder_m:.1f} = "
        f"{service_offset_m + 2*service_shoulder_m + service_road_width_m:.1f} m. "
        "Set road width to 0 to omit the service road entirely."
    )

    st.subheader("DWG Export")
    oda_path = st.text_input(
        "ODA File Converter path (leave blank if using the default install location)",
        value="",
        help=r'Only needed if installed somewhere other than the default, e.g. '
             r'"C:\Program Files\ODA\ODAFileConverter 27.1.0\ODAFileConverter.exe" '
             r'-- recent installers add the version number to the folder name.',
    )
    require_dwg = st.checkbox(
        "Require DWG output (stop with an error if it can't be produced)",
        value=True,
        help="DWG is a closed, proprietary format -- producing a real one needs the free "
             "ODA File Converter installed on this machine. With this checked, the run "
             "fails clearly if that conversion doesn't succeed, instead of silently "
             "falling back to DXF-only.",
    )

    st.subheader("Electrical (inverter rating drives the derived inverter count above)")
    c1, c2, c3 = st.columns(3)
    with c1:
        inverter_kw_25c = st.number_input("Inverter rating @25\u00b0C (kW)", min_value=0.0, value=1000.0, key="elec_kw25")
    with c2:
        inverter_kw_50c = st.number_input("Inverter rating @50\u00b0C (kW)", min_value=0.0, value=910.0)
    with c3:
        num_icr = st.number_input("No. of ICR", min_value=0, value=1, step=1)
    modules_per_string = st.number_input(
        "Modules per string (approved string size)", min_value=1, value=6, step=1
    )

    st.subheader("Table grouping (how tables are assigned to inverters / SCBs)")
    inverter_architecture = st.radio(
        "Inverter architecture",
        ["String Inverter", "Centralized Inverter"],
        horizontal=True,
        help="String Inverter: tables group directly under inverters. "
             "Centralized Inverter: tables group under SCBs (string combiner boxes), "
             "which then group under the central inverter.",
    )
    strings_per_scb = None
    if inverter_architecture == "Centralized Inverter":
        strings_per_scb = st.number_input(
            "Strings per SCB", min_value=1, value=96, step=1,
            help="How many strings one string combiner box accepts (default 96 for 6×2 MMS tables with 8 strings/table).",
        )
    st.caption(
        "Table count is no longer a simple module-count target -- it's derived bottom-up from "
        "how many strings each inverter"
        + (" (via 6×2 SCBs)" if inverter_architecture == "Centralized Inverter" else "")
        + " actually needs, so the built plant reflects whole, physically buildable equipment counts."
    )

st.divider()
generate = st.button("\U0001f680 Generate Layout", type="primary", use_container_width=True)

if generate:
    if not any(df is not None for df in boundary_dfs):
        st.error("Boundary coordinates are required (upload a CSV or paste the data).")
        st.stop()

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)

        boundary_paths = []
        for i, df in enumerate(boundary_dfs):
            if df is not None:
                p = tmp / f"boundary_{i + 1}.csv"
                df.to_csv(p, index=False)
                boundary_paths.append(str(p))

        control_paths = []
        for i, df in enumerate(control_dfs):
            if df is not None:
                p = tmp / f"control_room_{i + 1}.csv"
                df.to_csv(p, index=False)
                control_paths.append(str(p))

        inverter_paths = []
        for i, df in enumerate(inverter_dfs):
            if df is not None:
                p = tmp / f"inverter_room_{i + 1}.csv"
                df.to_csv(p, index=False)
                inverter_paths.append(str(p))

        private_area_paths = []
        for i, df in enumerate(private_area_dfs):
            if df is not None:
                p = tmp / f"private_area_{i + 1}.csv"
                df.to_csv(p, index=False)
                private_area_paths.append(str(p))

        output_dxf = tmp / "solar_layout.dxf"

        module_length_m = module_length_mm / 1000.0
        module_width_m = module_width_mm / 1000.0
        table_height_m = compute_table_height(int(table_rows), module_length_m, module_width_m, portrait)
        row_gap_m = max(0.0, row_pitch_m - table_height_m)

        config = {
            "input": {
                "boundary_csvs": boundary_paths,
                "control_room_csvs": control_paths,
                "inverter_room_csvs": inverter_paths,
                "private_area_csvs": private_area_paths,
            },
            "module": {
                "manufacturer": "Client",
                "model": f"{power_wp:.0f}Wp {technology}",
                "power_wp": power_wp,
                "length_mm": module_length_mm,
                "width_mm": module_width_mm,
                "technology": technology,
            },
            "table": {
                "type": f"MMS-{int(table_rows)}x{int(table_columns)}",
                "rows": int(table_rows),
                "columns": int(table_columns),
                "modules_per_table": int(table_rows) * int(table_columns),
                "portrait": portrait,
                "table_gap_m": table_gap_m,
            },
            "layout": {
                "boundary_setback_m": boundary_setback_m,
                "row_angle": 0.0,
                "row_gap_m": row_gap_m,
                "minimum_row_length": 5.0,
                "minimum_gap_m": 0.0,
                "target_ac_mw": target_ac_mw if target_ac_mw else None,
                "target_dc_capacity_mwp": None,
                "tilt_angle_deg": tilt_angle_deg,
                "arrester_radius_m": arrester_radius_m,
            },
            "electrical": {
                "inverter_rating_kw_25c": float(inverter_kw_25c),
                "inverter_rating_kw_50c": float(inverter_kw_50c),
                "num_inverters": None,  # derived from target_ac_mw / inverter_rating_kw_25c
                "num_icr": int(num_icr),
                "modules_per_string": int(modules_per_string),
                "dc_ac_ratio": dc_ac_ratio,
                "inverter_architecture": (
                    "centralized_inverter" if inverter_architecture == "Centralized Inverter"
                    else "string_inverter"
                ),
                "strings_per_scb": int(strings_per_scb) if strings_per_scb else 96,
                "scb_block_rows": 6,
                "scb_block_columns": 2,
            },
            "service_road": {
                "offset_from_panels_m": service_offset_m,
                "road_width_m": service_road_width_m,
                "shoulder_m": service_shoulder_m,
            },
            "oda_file_converter_path": oda_path.strip() or None,
            "dxf": {
                "output_file": str(output_dxf),
                "require_dwg": require_dwg,
                "layers": {
                    "boundary": "BOUNDARY", "setback": "SETBACK", "road": "ROADS",
                    "service_road": "SERVICE_ROAD",
                    "exclusion": "EXCLUSIONS", "pv_table": "PV_TABLES", "module": "MODULES",
                    "string": "STRINGS", "combiner": "COMBINERS", "inverter": "INVERTERS",
                    "transformer": "TRANSFORMERS", "cable": "CABLES", "text": "TEXT",
                },
            },
        }

        try:
            with st.spinner("Generating layout..."):
                result = run_pipeline(config)
        except DWGExportError as exc:
            st.error(f"DWG output was required but could not be produced.\n\n{exc}")
            st.info("Uncheck 'Require DWG output' under Advanced settings to proceed with DXF only instead.")
            st.stop()
        except Exception as exc:
            st.error(f"Layout generation failed: {exc}")
            st.stop()

        st.success("Layout generated.")

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Tables placed", result["table_count"])
        m2.metric("Modules", result["module_count"])
        m3.metric("Actual DC Capacity", f"{result['actual_dc_mwp']:.2f} MWp")
        m4.metric("Land Area", f"{result['land_area_acres']:.2f} acres")

        if result.get("ac_capacity_mw") is not None:
            m5, m6 = st.columns(2)
            m5.metric("AC Capacity", f"{result['ac_capacity_mw']:.2f} MW")
            m6.metric("Actual DC:AC Oversizing", f"{result['dc_ac_oversizing_percent']:.1f}%")

        grouping = result.get("grouping")
        if grouping:
            st.subheader(f"Plant Calculation — {grouping.inverter_type.replace('_', ' ').title()}")
            r1, r2, r3, r4 = st.columns(4)
            r1.metric("Number of inverters", f"{grouping.number_of_inverters} Nos")
            r2.metric("Strings in a table", grouping.strings_per_table)
            r3.metric("Strings per inverter", grouping.strings_per_inverter)
            r4.metric("Tables per inverter", grouping.tables_per_inverter)

            if grouping.inverter_type == "centralized_inverter":
                s1, s2 = st.columns(2)
                s1.metric("SCBs per inverter", grouping.scbs_per_inverter)
                s2.metric("Tables per SCB", grouping.tables_per_scb)

            t1, t2, t3 = st.columns(3)
            t1.metric("Total tables (required)", grouping.total_tables,
                      delta=result["table_count"] - grouping.total_tables)
            t2.metric("Total strings", f"{grouping.total_strings:,}")
            t3.metric("Total modules", f"{grouping.total_modules:,}")

            if grouping.warnings:
                with st.expander(f"⚠️ {len(grouping.warnings)} calculation warning(s) — grouping may need adjustment"):
                    for warning in grouping.warnings:
                        st.warning(warning)
        else:
            ac_dc_plan = result.get("ac_dc_plan")
            if ac_dc_plan:
                st.subheader("Required vs. Actual")
                r1, r2, r3, r4 = st.columns(4)
                r1.metric("Inverters Required", f"{ac_dc_plan.num_inverters_required} Nos")
                r2.metric("Tables (MMS) Required", ac_dc_plan.required_tables, delta=result["table_count"] - ac_dc_plan.required_tables)
                r3.metric("Modules Required", ac_dc_plan.required_modules, delta=result["module_count"] - ac_dc_plan.required_modules)
                if ac_dc_plan.required_strings is not None:
                    r4.metric("Strings Required", ac_dc_plan.required_strings)

        if result.get("capacity_note"):
            st.info(result["capacity_note"])

        doc = ezdxf.readfile(str(output_dxf))
        msp = doc.modelspace()
        fig = plt.figure(figsize=(8, 12))
        ax = fig.add_axes([0, 0, 1, 1])
        Frontend(RenderContext(doc), MatplotlibBackend(ax)).draw_layout(msp, finalize=True)

        col_preview, col_tables = st.columns([2, 1])
        with col_preview:
            st.pyplot(fig, use_container_width=True)
        with col_tables:
            for i, df in enumerate(boundary_dfs):
                if df is not None:
                    label = f"Boundary parcel {i + 1}" if len(boundary_dfs) > 1 else "Boundary coordinates"
                    st.markdown(f"**{label}**")
                    st.dataframe(df, height=200, use_container_width=True)
            any_control = any(df is not None for df in control_dfs)
            if any_control:
                for i, df in enumerate(control_dfs):
                    if df is not None:
                        label = f"Control room {i + 1}" if len(control_dfs) > 1 else "Control room coordinates"
                        st.markdown(f"**{label}**")
                        st.dataframe(df, height=160, use_container_width=True)
            else:
                st.markdown("**Control room coordinates**")
                st.caption("None provided")
            any_inverter = any(df is not None for df in inverter_dfs)
            if any_inverter:
                for i, df in enumerate(inverter_dfs):
                    if df is not None:
                        label = f"Inverter room {i + 1}" if len(inverter_dfs) > 1 else "Inverter room coordinates"
                        st.markdown(f"**{label}**")
                        st.dataframe(df, height=160, use_container_width=True)
            else:
                st.markdown("**Inverter room coordinates**")
                st.caption("None provided")
            if private_area_dfs:
                for i, df in enumerate(private_area_dfs):
                    if df is not None:
                        st.markdown(f"**Private area {i + 1} (REJECTED)**")
                        st.dataframe(df, height=140, use_container_width=True)

        st.download_button(
            "\u2b07\ufe0f Download DXF",
            data=output_dxf.read_bytes(),
            file_name="solar_layout.dxf",
            mime="image/vnd.dxf",
            use_container_width=True,
        )

        if result.get("dwg_path"):
            with open(result["dwg_path"], "rb") as f:
                st.download_button(
                    "\u2b07\ufe0f Download DWG",
                    data=f.read(),
                    file_name="solar_layout.dwg",
                    mime="application/acad",
                    use_container_width=True,
                )
        else:
            st.info(
                "DWG not available on this server (the free ODA File Converter isn't installed "
                "here). The DXF above opens and edits identically to a DWG in AutoCAD -- use "
                "File > Save As > DWG there for a one-click local conversion. To enable automatic "
                "DWG export, install the ODA File Converter on the machine running this app: "
                "https://www.opendesign.com/guestfiles/oda_file_converter"
            )
