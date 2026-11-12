import importlib.util
import os

import numpy as np
import pandas as pd

def load_script():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "zone.py")
    spec = importlib.util.spec_from_file_location("zone", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def create_flows():
    """
    Two basins of five municipalities each. Every basin has a center (A0, B0)
    which attracts the flows of its own municipalities. The basins are only
    linked by a small flow from A4 to B0.
    """
    flows = []

    for basin in ("A", "B"):
        center = basin + "0"
        flows.append((center, center, 500.0))

        for index in range(1, 5):
            municipality = "{}{}".format(basin, index)
            flows.append((municipality, municipality, 50.0))
            flows.append((municipality, center, 100.0))

    flows.append(("A4", "B0", 10.0))

    df_flows = pd.DataFrame(flows, columns = ["origin_id", "destination_id", "weight"])
    municipalities = sorted(set(df_flows["origin_id"]))

    return df_flows, municipalities

def test_zone_follows_flows():
    zone_script = load_script()
    df_flows, municipalities = create_flows()

    graph = zone_script.FlowGraph(df_flows, municipalities)
    target = graph.index["A0"]

    # Without budget, the zone is the basin of the target
    zone = graph.find_largest_zone(target, None, alpha = 0.1)
    assert set(graph.municipalities[zone]) == {"A0", "A1", "A2", "A3", "A4"}

    result = graph.evaluate(zone, target)
    assert result["municipalities"] == 5
    assert result["persons"] == 500.0 + 4 * 150.0 + 10.0
    assert result["target_cut_share"] == 0.0
    assert np.isclose(result["cut_share"], 10.0 / (500.0 + 4 * 150.0 + 10.0))

def test_zone_respects_budget():
    zone_script = load_script()
    df_flows, municipalities = create_flows()

    graph = zone_script.FlowGraph(df_flows, municipalities)
    target = graph.index["A0"]

    # The budget does not allow for the full basin
    zone = graph.find_largest_zone(target, 900.0, alpha = 0.1)

    assert zone[target]
    assert graph.sizes[zone].sum() <= 900.0
    assert not any(municipality.startswith("B") for municipality in graph.municipalities[zone])

    # The target alone exceeds the budget: it is returned on its own
    zone = graph.find_largest_zone(target, 100.0, alpha = 0.1)
    assert set(graph.municipalities[zone]) == {"A0"}

def test_zone_script(data_path, tmpdir):
    import yaml
    zone_script = load_script()

    config_path = str(tmpdir.join("config.yml"))
    with open(config_path, "w+") as f:
        yaml.dump(dict(config = dict(data_path = data_path)), f)

    df_codes = zone_script.load_codes(
        zone_script.Path(data_path), "codes_2024/reference_IRIS_geo2024.zip", "reference_IRIS_geo2024.xlsx")
    target = df_codes.index[0]

    output_path = str(tmpdir.join("zone.csv"))
    zone_script.main(zone_script.Path(config_path), target,
        budget = None, alpha = 0.1, minimum_department_share = 0.01,
        output_path = zone_script.Path(output_path), geometry_path = None)

    df_zone = pd.read_csv(output_path, dtype = str)
    assert target in df_zone["commune_id"].values
