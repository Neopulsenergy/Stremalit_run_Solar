import logging
import sys

from core.pipeline import run_pipeline, load_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("SolarLayoutGenerator")


def print_capacity_summary(config: dict, result: dict) -> None:
    module_cfg = config.get("module", {})
    table_cfg = config.get("table", {})

    print("=" * 60)
    print("SOLAR PLANT DESIGN CALCULATION")
    print("=" * 60)
    print(f"Module Rating                 : {module_cfg.get('power_wp')} Wp")
    print(f"MMS Configuration             : {table_cfg.get('rows')} x {table_cfg.get('columns')}")
    print(f"Modules per MMS               : {table_cfg.get('modules_per_table')}")
    print(f"\nTotal MMS (tables placed)     : {result['table_count']}")
    print(f"Total Modules                 : {result['module_count']}")
    print(f"\nDC Capacity                   : {result['actual_dc_mwp']:.3f} MWp")
    if result.get("ac_capacity_mw") is not None:
        print(f"AC Capacity                   : {result['ac_capacity_mw']:.3f} MW")
        print(f"\nActual DC/AC Ratio            : {result['dc_ac_ratio']:.2f}")
        print(f"Actual DC Oversizing          : {result['dc_ac_oversizing_percent']:.2f}%")
    else:
        print("\n(No inverter data supplied -- AC capacity / DC:AC ratio not computed.)")

    ac_dc_plan = result.get("ac_dc_plan")
    plan = result.get("capacity_plan")
    requirement = result.get("requirement")
    grouping = result.get("grouping")

    if grouping:
        from core.calculation_engine import actual_layout_report
        print("\n============================================")
        print("THEORETICAL CALCULATION")
        print("============================================")
        for line in grouping.summary_lines():
            print(line)

        print("\n============================================")
        print("ACTUAL LAYOUT VALUES")
        print("============================================")
        for line in actual_layout_report(grouping, result["table_count"]):
            print(line)

        if grouping.warnings:
            print("\nWARNINGS")
            for warning in grouping.warnings:
                print(f"  {warning}")
    elif ac_dc_plan:
        print("\n--------------------------------------------")
        print(f"Required for AC={ac_dc_plan.target_ac_mw:.3f} MW / DC={ac_dc_plan.target_dc_mwp:.3f} MWp targets")
        print("--------------------------------------------")
        print(f"Inverters Required             : {ac_dc_plan.num_inverters_required} Nos "
              f"(@ {ac_dc_plan.inverter_rating_kw:.0f} kW each, from config)")
        print(f"Required Modules               : {ac_dc_plan.required_modules}")
        print(f"Required Tables (MMS)          : {ac_dc_plan.required_tables}")
        print(f"Required Strings               : {ac_dc_plan.required_strings if ac_dc_plan.required_strings is not None else 'N/A'}")
        print(f"Implied DC:AC Oversizing       : {ac_dc_plan.dc_ac_oversizing_percent:.2f}%")
        extra_modules = result["module_count"] - ac_dc_plan.required_modules
        print(f"\nExtra Modules in Layout        : {extra_modules}")
    elif plan:
        print("\n--------------------------------------------")
        print(f"Required for {plan.dc_oversizing_percent:.1f}% DC Oversizing")
        print("--------------------------------------------")
        print(f"Required DC Capacity          : {plan.required_dc_capacity_mw:.3f} MWp")
        print(f"Required Modules              : {plan.required_modules}")
        print(f"Required Tables               : {plan.required_tables}")
        if requirement and requirement.required_strings is not None:
            print(f"Required Strings              : {requirement.required_strings}")
        extra_modules = result["module_count"] - plan.required_modules
        print(f"\nExtra Modules in Layout       : {extra_modules}")
    elif result.get("target_dc_mwp"):
        print(f"\nExplicit DC target used       : {result['target_dc_mwp']:.3f} MWp")
        if requirement:
            print(f"Required Modules               : {requirement.required_modules}")
            print(f"Required Tables (MMS)          : {requirement.required_tables}")
            if requirement.required_strings is not None:
                print(f"Required Strings               : {requirement.required_strings}")

    print(f"\nTotal Land Area                : {result['land_area_acres']:.2f} ACRES (APPROX.)")
    print("=" * 60)


def main():
    try:
        config_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
        logger.info("Loading config: %s", config_path)
        config = load_json(config_path)

        result = run_pipeline(config)

        logger.info("DXF saved: %s", result["dxf_path"])
        if result.get("dwg_path"):
            logger.info("DWG saved: %s", result["dwg_path"])
        else:
            logger.warning(result["dwg_export_message"])
        logger.info("--------------------------------------------")
        logger.info("DXF Successfully Generated")
        logger.info("--------------------------------------------")

        print()
        print_capacity_summary(config, result)

    except Exception as ex:
        logger.exception(ex)
        sys.exit(1)


if __name__ == "__main__":
    main()
